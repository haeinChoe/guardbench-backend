"""Local mock E2E harness connecting the real Runner and k6 against a local HTTP test double."""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from . import cli
from .failure import FAILURE_FILENAME
from .results import RESULT_FILENAMES

DEFAULT_SCENARIO = Path(__file__).resolve().parents[1] / "fixtures/profiles/smoke.yaml"
DEFAULT_DATASET = Path(__file__).resolve().parents[1] / "fixtures/datasets/two-cases.yaml"
MOCK_TARGET_URL = "http://127.0.0.1:18080/v1/chat/completions"
MOCK_TARGET_MODEL = "fixture-model"
MOCK_TARGET_REVISION = "fixture-v1"
_RUN_DETAIL_PATH = re.compile(r"^/api/v1/test-runs/([0-9]+)$")
_RUN_RESULTS_PATH = re.compile(r"^/api/v1/test-runs/([0-9]+)/results$")
_PRESERVED_HOST_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "K6_BIN")
# Must match IDLE_SLEEP_SECONDS in performance/k6/test-run.js.
K6_IDLE_SLEEP_SECONDS = 1


def resolve_k6_binary() -> str | None:
    """Return the configured k6 executable path, or None if unavailable."""
    return shutil.which(os.environ.get("K6_BIN", "k6"))


def list_child_k6_pids() -> list[int]:
    """Return PIDs of direct child k6 processes belonging to this process."""
    parent_pid = os.getpid()
    pids: list[int] = []
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                stat_text = (entry / "stat").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rparen = stat_text.rfind(")")
            lparen = stat_text.find("(")
            if lparen < 0 or rparen <= lparen:
                continue
            comm = stat_text[lparen + 1:rparen]
            rest = stat_text[rparen + 2:].split()
            if len(rest) < 2:
                continue
            try:
                ppid = int(rest[1])
            except ValueError:
                continue
            if ppid == parent_pid and comm == "k6":
                pids.append(int(entry.name))
        return sorted(pids)

    completed = subprocess.run(
        ["ps", "-o", "pid=,ppid=,comm=", "-e"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        if ppid == parent_pid and parts[2] == "k6":
            pids.append(pid)
    return sorted(pids)


def _ensure_no_child_k6_processes() -> list[int]:
    """Terminate any lingering child k6 processes and return the lingering PIDs found."""
    lingering = list_child_k6_pids()
    if not lingering:
        return []
    for pid in lingering:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and list_child_k6_pids():
        time.sleep(0.05)
    for pid in list_child_k6_pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    return lingering


class _ThreadingApiServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class LocalMockApiServer:
    """In-process mock HTTP API mimicking GuardBench Runner and k6 endpoints."""

    def __init__(self, *, run_outcome: str = "COMPLETED", fail_on_diagnostics: bool = False) -> None:
        self.run_outcome = run_outcome
        self.fail_on_diagnostics = fail_on_diagnostics
        self._lock = threading.Lock()
        self.health_checks = 0
        self.suites_created: list[dict[str, Any]] = []
        self.runs_created: list[dict[str, Any]] = []
        self.run_polls = 0
        self._next_suite_id = 1
        self._next_run_id = 100
        self._server: _ThreadingApiServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise RuntimeError("LocalMockApiServer가 시작되지 않았습니다.")
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def __enter__(self) -> LocalMockApiServer:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return None

            def _read_json_body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length > 0 else b"{}"
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
                return parsed if isinstance(parsed, dict) else {}

            def _respond(self, status_code: int, payload: dict[str, Any]) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)

                if path == "/api/v1/test-suites":
                    with owner._lock:
                        owner.health_checks += 1
                    self._respond(200, {"data": {"items": [], "page": {"hasNext": False}}})
                    return

                if path == "/api/v1/test-runs":
                    statuses = set(query.get("status", []))
                    has_created_from = bool(query.get("createdFrom"))
                    if owner.fail_on_diagnostics and has_created_from:
                        self._respond(500, {"error": "simulated run query failure"})
                        return
                    with owner._lock:
                        items = [
                            {
                                "id": run["id"],
                                "testSuiteId": run["testSuiteId"],
                                "status": run["status"],
                                "executionOutcome": run["executionOutcome"],
                            }
                            for run in owner.runs_created
                            if not statuses or run["status"] in statuses
                        ]
                    self._respond(200, {"data": {"items": items, "page": {"hasNext": False}}})
                    return

                results_match = _RUN_RESULTS_PATH.fullmatch(path)
                if results_match:
                    run_id = int(results_match.group(1))
                    with owner._lock:
                        target_run = next((r for r in owner.runs_created if r["id"] == run_id), None)
                        items = (
                            [{"error": {"stage": "EVALUATOR", "code": "MOCK_EXECUTION_ERROR"}}]
                            if target_run is not None and target_run["executionOutcome"] not in (None, "COMPLETED")
                            else []
                        )
                    self._respond(200, {"data": {"items": items, "page": {"hasNext": False}}})
                    return

                detail_match = _RUN_DETAIL_PATH.fullmatch(path)
                if detail_match:
                    run_id = int(detail_match.group(1))
                    with owner._lock:
                        target_run = next((r for r in owner.runs_created if r["id"] == run_id), None)
                        if target_run is None:
                            self._respond(404, {"error": f"unknown test run {run_id}"})
                            return
                        owner.run_polls += 1
                        target_run["polls"] += 1
                        if target_run["polls"] >= 2:
                            target_run["status"] = "FINISHED"
                            target_run["executionOutcome"] = owner.run_outcome
                        response_data: dict[str, Any] = {
                            "id": target_run["id"],
                            "testSuiteId": target_run["testSuiteId"],
                            "status": target_run["status"],
                        }
                        if target_run["executionOutcome"] is not None:
                            response_data["executionOutcome"] = target_run["executionOutcome"]
                    self._respond(200, {"data": response_data})
                    return

                self._respond(404, {"error": f"not found: {path}"})

            def do_POST(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                if path == "/api/v1/test-suites":
                    body = self._read_json_body()
                    test_cases = body.get("testCases", [])
                    with owner._lock:
                        suite_id = owner._next_suite_id
                        owner._next_suite_id += 1
                        owner.suites_created.append({
                            "id": suite_id,
                            "name": body.get("name"),
                            "testCaseCount": len(test_cases),
                        })
                    self._respond(201, {"data": {"id": suite_id, "testCaseCount": len(test_cases)}})
                    return

                if path == "/api/v1/test-runs":
                    body = self._read_json_body()
                    idempotency_key = self.headers.get("Idempotency-Key")
                    with owner._lock:
                        run_id = owner._next_run_id
                        owner._next_run_id += 1
                        owner.runs_created.append({
                            "id": run_id,
                            "testSuiteId": body.get("testSuiteId"),
                            "target": body.get("target"),
                            "idempotencyKey": idempotency_key,
                            "polls": 0,
                            "status": "RUNNING",
                            "executionOutcome": None,
                        })
                    self._respond(202, {"data": {"id": run_id}})
                    return

                self._respond(404, {"error": f"not found: {path}"})

        self._server = _ThreadingApiServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            if self._thread.is_alive():
                raise RuntimeError("LocalMockApiServer 스레드가 정상 종료되지 않았습니다.")


@contextmanager
def _isolated_harness_environment():
    env: dict[str, str] = {}
    for key in _PRESERVED_HOST_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update({
        "K6_NO_USAGE_REPORT": "true",
        "K6_WEB_DASHBOARD": "false",
    })
    with patch.dict(os.environ, env, clear=True):
        yield


def _cli_argv(
    api_url: str,
    scenario_path: Path,
    dataset_path: Path,
    result_dir: Path,
) -> list[str]:
    return [
        "--api-url",
        api_url,
        "--target-url",
        MOCK_TARGET_URL,
        "--target-model",
        MOCK_TARGET_MODEL,
        "--target-revision",
        MOCK_TARGET_REVISION,
        "--scenario",
        str(scenario_path),
        "--dataset",
        str(dataset_path),
        "--result-dir",
        str(result_dir),
    ]


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str
    work_dir: Path


def max_expected_k6_iterations(workload: dict[str, Any], started: int) -> float:
    """Upper bound of the default k6 `iterations` metric for a capped-iteration workload.

    Every started iteration is a real one. After reaching max_iterations_per_vu, a VU runs
    one empty iteration per K6_IDLE_SLEEP_SECONDS for the rest of the workload. Each VU gets
    at most duration / sleep of them, +1 for the iteration straddling the end (it finishes
    within graceful stop) and +1 slack for scheduling jitter. A busy loop exceeds this by
    orders of magnitude (hundreds of thousands vs. single digits).
    """
    stages = workload.get("stages")
    if stages:
        duration = sum(stage["duration_seconds"] for stage in stages)
    else:
        duration = workload.get("ramp_up_seconds", 0) + workload["duration_seconds"]
    return started + workload["concurrent_test_runs"] * (duration / K6_IDLE_SLEEP_SECONDS + 2)


def _run_normal_completion(scenario_path: Path, dataset_path: Path, result_dir: Path) -> str:
    with LocalMockApiServer() as server, _isolated_harness_environment():
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = cli.main(_cli_argv(server.base_url, scenario_path, dataset_path, result_dir))
        assert code == 0, f"expected exit code 0, got {code}: {stderr_buf.getvalue()}"

        for filename in RESULT_FILENAMES:
            artifact = result_dir / filename
            assert artifact.is_file() and artifact.stat().st_size > 0, f"missing or empty {filename}"
        assert not (result_dir / FAILURE_FILENAME).exists(), "unexpected failure.json on normal completion"

        result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
        plan = json.loads((result_dir / "execution-plan.json").read_text(encoding="utf-8"))
        report = (result_dir / "report.md").read_text(encoding="utf-8")

        assert result["schema_version"] == 2
        assert result["acceptance"]["status"] == "PASS"
        assert result["k6"]["exit_code"] == 0
        assert result["execution_plan"] == plan
        assert result["scenario"] == {"id": "FIXTURE-SMOKE", "type": "SMOKE"}
        assert result["tuple"] == {"concurrent_test_runs": 2, "test_case_count": 2}
        assert result["connection"]["target"] == {
            "type": "HTTP_ENDPOINT",
            "url": MOCK_TARGET_URL,
            "model": MOCK_TARGET_MODEL,
            "revision": MOCK_TARGET_REVISION,
        }
        summary_metrics = json.loads((result_dir / "k6-summary.json").read_text(encoding="utf-8"))["metrics"]
        started = int(summary_metrics["test_run_iterations_started"]["count"])
        k6_iterations = summary_metrics["iterations"]["count"]
        iteration_limit = max_expected_k6_iterations(result["execution_plan"]["scenario"]["workload"], started)
        assert k6_iterations <= iteration_limit, (
            f"idle VUs appear to busy-loop: k6 iterations={k6_iterations} > limit {iteration_limit}"
        )
        assert result["test_run_diagnostics"]["test_run_count"] >= 1
        assert result["test_run_diagnostics"]["failed_test_run_ids"] == []
        for marker in (
            "FIXTURE-SMOKE",
            "SMOKE",
            "fixture-two-cases-v1",
            MOCK_TARGET_MODEL,
            MOCK_TARGET_REVISION,
            "(2, 2)",
            "**PASS**",
        ):
            assert marker in report, f"missing {marker} in report.md"

        assert len(server.suites_created) == 1
        assert len(server.runs_created) >= 1
        suite_id = result["dataset"]["suite_id"]
        run_id = result["run_id"]
        for created_run in server.runs_created:
            assert created_run["testSuiteId"] == suite_id
            assert str(created_run["idempotencyKey"]).startswith(f"perf-{run_id}-")
            assert created_run["target"]["identifier"] == MOCK_TARGET_URL
            assert created_run["target"]["model"] == MOCK_TARGET_MODEL
            assert created_run["target"]["revision"] == MOCK_TARGET_REVISION
            assert created_run["status"] == "FINISHED"
        assert server.run_polls >= len(server.runs_created)

        return (
            f"6개 결과 artifact 생성, real k6 요청 {len(server.runs_created)}건 완료, "
            f"scenario={result['scenario']['id']}, exit=0"
        )


def _run_measurement_fail(scenario_path: Path, dataset_path: Path, result_dir: Path) -> str:
    with LocalMockApiServer(run_outcome="ERROR") as server, _isolated_harness_environment():
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = cli.main(_cli_argv(server.base_url, scenario_path, dataset_path, result_dir))
        assert code == 1, f"expected exit code 1, got {code}: {stderr_buf.getvalue()}"

        for filename in RESULT_FILENAMES:
            artifact = result_dir / filename
            assert artifact.is_file() and artifact.stat().st_size > 0, f"missing or empty {filename}"
        assert not (result_dir / FAILURE_FILENAME).exists(), "unexpected failure.json on measurement FAIL"

        result = json.loads((result_dir / "result.json").read_text(encoding="utf-8"))
        report = (result_dir / "report.md").read_text(encoding="utf-8")

        assert result["schema_version"] == 2
        assert result["acceptance"]["status"] == "FAIL"
        assert result["k6"]["exit_code"] == cli.K6_THRESHOLD_FAILURE_EXIT_CODE
        assert result["failure_categories"]["thresholds"] == "FAIL"
        assert result["failure_categories"]["execution_failures"] >= 1
        assert result["test_run_diagnostics"]["error_summary"] == {
            "EVALUATOR:MOCK_EXECUTION_ERROR": result["failure_categories"]["execution_failures"]
        }
        checks = {item["name"]: item for item in result["acceptance"]["checks"]}
        assert checks["completion.failure_rate"]["passed"] is False
        assert checks["k6.thresholds"]["passed"] is False
        assert "**FAIL**" in report

        return (
            f"acceptance 위반 시 exit=1, execution_failures={result['failure_categories']['execution_failures']}, "
            "result.json(FAIL)·report.md 생성 및 failure.json 미생성 확인"
        )


def _run_execution_error(scenario_path: Path, dataset_path: Path, result_dir: Path) -> str:
    with LocalMockApiServer(fail_on_diagnostics=True) as server, _isolated_harness_environment():
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = cli.main(_cli_argv(server.base_url, scenario_path, dataset_path, result_dir))
        assert code == 2, f"expected exit code 2, got {code}"
        assert "ERROR:" in stderr_buf.getvalue()

        assert len(server.suites_created) == 1
        assert len(server.runs_created) >= 1
        failure_path = result_dir / FAILURE_FILENAME
        assert failure_path.is_file(), "expected failure.json on execution error"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["schema_version"] == 1
        assert failure["status"] == "ERROR"
        assert failure["phase"] == "report"
        assert failure["error"]["type"] == "ApiError"
        assert failure["inputs"]["scenario_id"] == "FIXTURE-SMOKE"
        assert failure["inputs"]["suite_id"] >= 1
        for expected_artifact in (
            "scenario.yaml",
            "dataset.yaml",
            "execution-plan.json",
            "k6-summary.json",
            "failure.json",
        ):
            assert expected_artifact in failure["available_artifacts"], f"missing {expected_artifact}"
        summary = json.loads((result_dir / "k6-summary.json").read_text(encoding="utf-8"))
        assert isinstance(summary, dict) and "metrics" in summary
        for absent_file in ("result.json", "report.md"):
            assert not (result_dir / absent_file).exists(), f"unexpected {absent_file} after execution error"

        return f"seed·real k6({len(server.runs_created)}건) 후 오류 시 exit=2, failure.json(phase=report) 및 k6-summary.json 보존"


def _execute_scenario(name: str, runner_fn: Any, scenario_path: Path, dataset_path: Path) -> ScenarioResult:
    work_dir_holder: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix=f"guardbench-local-e2e-{name}-") as tmp_dir:
            work_dir = Path(tmp_dir)
            work_dir_holder.append(work_dir)
            detail = runner_fn(scenario_path, dataset_path, work_dir / "result")
        lingering = _ensure_no_child_k6_processes()
        if lingering:
            return ScenarioResult(name, False, f"잔여 k6 자식 프로세스가 발견되었습니다: {lingering}", work_dir_holder[0])
        if work_dir_holder[0].exists():
            return ScenarioResult(name, False, f"임시 디렉터리가 정리되지 않았습니다: {work_dir_holder[0]}", work_dir_holder[0])
        return ScenarioResult(name, True, detail, work_dir_holder[0])
    except Exception as exc:
        lingering = _ensure_no_child_k6_processes()
        suffix = f" (잔여 k6 PID: {lingering})" if lingering else ""
        work_dir = work_dir_holder[0] if work_dir_holder else Path("")
        return ScenarioResult(name, False, f"{type(exc).__name__}: {exc}{suffix}", work_dir)


def run_all(
    scenario_path: Path = DEFAULT_SCENARIO,
    dataset_path: Path = DEFAULT_DATASET,
) -> list[ScenarioResult]:
    """Run the three local E2E scenarios sequentially using real Runner and k6."""
    scenarios = (
        ("normal_completion", _run_normal_completion),
        ("measurement_fail", _run_measurement_fail),
        ("execution_error", _run_execution_error),
    )
    return [_execute_scenario(name, fn, scenario_path, dataset_path) for name, fn in scenarios]


def format_scope_banner(k6_path: str, scenario_path: Path, dataset_path: Path) -> str:
    return "\n".join([
        "=== GuardBench Local Mock E2E Harness ===",
        f"Scenario: {scenario_path}",
        f"Dataset: {dataset_path}",
        f"Target mock URL: {MOCK_TARGET_URL} (model={MOCK_TARGET_MODEL}, revision={MOCK_TARGET_REVISION})",
        f"k6 binary: {k6_path}",
        "실제 실행 구성 요소: Runner CLI 입력 해석(load_execution_plan), cli.main/execute orchestration, "
        "실제 k6 프로세스(performance/k6/test-run.js), 앱 수준 acceptance 판정, 결과·실패 진단 보존",
        "테스트 대역 범위: local_e2e.py 내부 로컬 ThreadingHTTPServer 모의 GuardBench API 및 고정 모의 Target URL",
        "주의: 본 실행은 로컬 기능 검증이며 실제 성능 Baseline 검증이 아닙니다.",
    ])


def run_harness(
    scenario_path: Path = DEFAULT_SCENARIO,
    dataset_path: Path = DEFAULT_DATASET,
) -> tuple[int, list[ScenarioResult]]:
    """Validate prerequisites, run all scenarios, and return (exit_code, results)."""
    k6_path = resolve_k6_binary()
    if not k6_path:
        print(
            "ERROR: k6 실행 파일을 찾을 수 없습니다. PATH 또는 K6_BIN을 설정한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
        return 2, []

    print(format_scope_banner(k6_path, scenario_path, dataset_path))
    results = run_all(scenario_path, dataset_path)
    for outcome in results:
        status = "PASS" if outcome.passed else "FAIL"
        print(f"[{status}] {outcome.name}: {outcome.detail}")
    all_passed = all(outcome.passed for outcome in results)
    passed_count = sum(1 for outcome in results if outcome.passed)
    print(f"Summary: {passed_count}/{len(results)} scenarios passed")
    return (0 if all_passed else 1), results


def main(argv: list[str] | None = None) -> int:
    _ = argv
    code, _ = run_harness()
    return code


if __name__ == "__main__":
    raise SystemExit(main())
