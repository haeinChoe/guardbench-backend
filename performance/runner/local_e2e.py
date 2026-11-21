"""Local E2E harness connecting the real Runner and k6 against local test doubles."""

from __future__ import annotations

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
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch

from . import cli
from .api import ApiError
from .config import ConfigurationError
from .plan import load_execution_plan
from .storage import FAILURE_FILENAME, RESULT_FILENAMES

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "fixtures/experiments/smoke.yaml"
_RUN_DETAIL_PATH = re.compile(r"^/api/v1/test-runs/([0-9]+)$")
_RUN_RESULTS_PATH = re.compile(r"^/api/v1/test-runs/([0-9]+)/results$")
_PRESERVED_HOST_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "K6_BIN")
_QUEUE_ENV = {
    "PERF_SOURCE_QUEUE_URLS": (
        "https://local.invalid/source-resolve,"
        "https://local.invalid/source-work-items,"
        "https://local.invalid/source-finalize"
    ),
    "PERF_DLQ_URLS": (
        "https://local.invalid/dlq-resolve,"
        "https://local.invalid/dlq-work-items,"
        "https://local.invalid/dlq-finalize"
    ),
    "PERF_SOURCE_QUEUE_RESOLVE_NAME": "local-source-resolve",
    "PERF_SOURCE_QUEUE_WORK_ITEMS_NAME": "local-source-work-items",
    "PERF_SOURCE_QUEUE_FINALIZE_NAME": "local-source-finalize",
    "PERF_DLQ_RESOLVE_NAME": "local-dlq-resolve",
    "PERF_DLQ_WORK_ITEMS_NAME": "local-dlq-work-items",
    "PERF_DLQ_FINALIZE_NAME": "local-dlq-finalize",
}


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

    def __init__(self, *, fail_on_drain: bool = False) -> None:
        self.fail_on_drain = fail_on_drain
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
                    if owner.fail_on_drain and statuses and has_created_from:
                        self._respond(500, {"error": "simulated drain query failure"})
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
                    self._respond(200, {"data": {"items": [], "page": {"hasNext": False}}})
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
                            target_run["executionOutcome"] = "COMPLETED"
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


class _FakeEcsClient:
    def __init__(self, expected_ecs: dict[str, Any], *, capacity_mismatch: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._services: dict[str, dict[str, Any]] = {}
        self._definitions: dict[str, dict[str, Any]] = {}
        for role, config in expected_ecs.items():
            arn = f"arn:aws:ecs:local:000000000000:task-definition/{role}:1"
            desired = config["desired_count"] + (1 if capacity_mismatch and role == "worker" else 0)
            self._services[config["service_identifier"]] = {
                "clusterArn": f"arn:aws:ecs:local:000000000000:cluster/{config['cluster_identifier']}",
                "serviceName": config["service_identifier"],
                "taskDefinition": arn,
                "status": "ACTIVE",
                "desiredCount": desired,
                "runningCount": desired,
                "pendingCount": 0,
                "deployments": [{
                    "status": "PRIMARY",
                    "rolloutState": "COMPLETED",
                    "taskDefinition": arn,
                    "desiredCount": desired,
                    "runningCount": desired,
                    "pendingCount": 0,
                }],
            }
            env = (
                [{
                    "name": "GUARDBENCH_WORKER_WORK_ITEMS_CONCURRENCY",
                    "value": str(config["work_items_concurrency"]),
                }]
                if role == "worker"
                else []
            )
            self._definitions[arn] = {
                "taskDefinitionArn": arn,
                "cpu": config["task_cpu"],
                "memory": config["task_memory"],
                "containerDefinitions": [{"environment": env}] if env else [],
            }

    def describe_services(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_services", kwargs))
        service_id = kwargs["services"][0]
        return {"services": [self._services[service_id]]}

    def describe_task_definition(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_task_definition", kwargs))
        return {"taskDefinition": self._definitions[kwargs["taskDefinition"]]}


class _FakeRdsClient:
    def __init__(self, expected_rds: dict[str, Any]) -> None:
        self.calls: list[dict[str, Any]] = []
        self._instance = {
            "DBInstanceIdentifier": expected_rds["db_instance_identifier"],
            "DBInstanceClass": expected_rds["db_instance_class"],
            "DBInstanceStatus": "available",
        }

    def describe_db_instances(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"DBInstances": [self._instance]}


class _FakeSageMakerClient:
    def __init__(self, expected_sm: dict[str, Any]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._expected = expected_sm

    def describe_endpoint(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_endpoint", kwargs))
        return {
            "EndpointName": self._expected["endpoint_name"],
            "EndpointStatus": "InService",
            "EndpointConfigName": "local-endpoint-config",
            "ProductionVariants": [{
                "VariantName": self._expected["production_variant_name"],
                "DesiredInstanceCount": self._expected["desired_instance_count"],
                "CurrentInstanceCount": self._expected["desired_instance_count"],
            }],
        }

    def describe_endpoint_config(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("describe_endpoint_config", kwargs))
        return {
            "ProductionVariants": [{
                "VariantName": self._expected["production_variant_name"],
                "InstanceType": self._expected["instance_type"],
            }],
        }


class _FakeSqsClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_queue_attributes(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {
            "Attributes": {
                "ApproximateNumberOfMessages": "0",
                "ApproximateNumberOfMessagesNotVisible": "0",
                "ApproximateNumberOfMessagesDelayed": "0",
            }
        }


class _FakeCloudWatchClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def get_metric_data(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        end_time: datetime = kwargs["EndTime"]
        return {
            "MetricDataResults": [
                {
                    "Id": query["Id"],
                    "Label": query["Id"],
                    "Timestamps": [end_time],
                    "Values": [1.0],
                    "StatusCode": "Complete",
                }
                for query in kwargs["MetricDataQueries"]
            ]
        }


class _FakeBoto3:
    """In-memory boto3 client factory blocking any unmocked AWS service calls."""

    def __init__(self, expected: dict[str, Any], *, capacity_mismatch: bool = False) -> None:
        self.ecs = _FakeEcsClient(expected["ecs"], capacity_mismatch=capacity_mismatch)
        self.rds = _FakeRdsClient(expected["rds"])
        self.sagemaker = _FakeSageMakerClient(expected["sagemaker"])
        self.sqs = _FakeSqsClient()
        self.cloudwatch = _FakeCloudWatchClient()
        self.requested_services: list[str] = []

    def client(self, service_name: str, **kwargs: Any) -> Any:
        self.requested_services.append(service_name)
        mapping = {
            "ecs": self.ecs,
            "rds": self.rds,
            "sagemaker": self.sagemaker,
            "sqs": self.sqs,
            "cloudwatch": self.cloudwatch,
        }
        if service_name not in mapping:
            raise AssertionError(f"local_e2e에서 허용되지 않은 AWS service 호출입니다: {service_name}")
        return mapping[service_name]


@contextmanager
def _isolated_harness_environment(base_url: str, fake_boto3: _FakeBoto3):
    env: dict[str, str] = {}
    for key in _PRESERVED_HOST_ENV_KEYS:
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update({
        "APP_REVISION": "local-e2e-app",
        "INFRA_REVISION": "local-e2e-infra",
        "PERF_BASE_URL": base_url,
        "K6_NO_USAGE_REPORT": "true",
        "K6_WEB_DASHBOARD": "false",
        **_QUEUE_ENV,
    })
    with ExitStack() as stack:
        stack.enter_context(patch.dict(os.environ, env, clear=True))
        stack.enter_context(patch("performance.runner.aws._boto3", return_value=fake_boto3))
        stack.enter_context(
            patch("boto3.client", side_effect=AssertionError("real boto3.client is forbidden in local_e2e"))
        )
        yield


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str
    work_dir: Path


def _run_normal_completion(manifest_path: Path, expected: dict[str, Any], result_dir: Path) -> str:
    fake_boto3 = _FakeBoto3(expected)
    with LocalMockApiServer() as server, _isolated_harness_environment(server.base_url, fake_boto3):
        args = cli.parser().parse_args(["--experiment", str(manifest_path), "--result-dir", str(result_dir)])
        code = cli.execute(args)
        assert code == 0, f"expected exit code 0, got {code}"

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
        assert result["execution_plan"]["experiment"]["id"] == "fixture-smoke"
        assert result["scenario"] == {"id": "FIXTURE-SMOKE", "type": "SMOKE"}
        assert result["drain"]["passed"] is True
        assert result["aws_metrics"]["status"] == "COLLECTED"
        assert result["test_run_diagnostics"]["test_run_count"] >= 1
        assert result["test_run_diagnostics"]["failed_test_run_ids"] == []
        for marker in ("fixture-smoke", "fixture-infra", "fixture-v1", "FIXTURE-SMOKE", "SMOKE", "**PASS**"):
            assert marker in report, f"missing {marker} in report.md"

        assert len(server.suites_created) == 1
        assert len(server.runs_created) >= 1
        suite_id = result["dataset"]["suite_id"]
        run_id = result["run_id"]
        for created_run in server.runs_created:
            assert created_run["testSuiteId"] == suite_id
            assert str(created_run["idempotencyKey"]).startswith(f"perf-{run_id}-")
            assert created_run["status"] == "FINISHED"
        assert server.run_polls >= len(server.runs_created)
        assert {"ecs", "rds", "sagemaker", "sqs", "cloudwatch"}.issubset(set(fake_boto3.requested_services))

        return (
            f"8개 결과 artifact 생성, real k6 요청 {len(server.runs_created)}건 완료, "
            f"experiment_id={result['execution_plan']['experiment']['id']}, scenario={result['scenario']['id']}"
        )


def _run_capacity_mismatch(manifest_path: Path, expected: dict[str, Any], result_dir: Path) -> str:
    fake_boto3 = _FakeBoto3(expected, capacity_mismatch=True)
    with LocalMockApiServer() as server, _isolated_harness_environment(server.base_url, fake_boto3):
        args = cli.parser().parse_args(["--experiment", str(manifest_path), "--result-dir", str(result_dir)])
        raised: ConfigurationError | None = None
        try:
            cli.execute(args)
        except ConfigurationError as exc:
            raised = exc
        assert raised is not None, "expected ConfigurationError on capacity mismatch"

        assert server.suites_created == [], "seed must not run on capacity mismatch"
        assert server.runs_created == [], "k6 must not run on capacity mismatch"
        failure_path = result_dir / FAILURE_FILENAME
        assert failure_path.is_file(), "expected failure.json on capacity mismatch"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["schema_version"] == 1
        assert failure["status"] == "ERROR"
        assert failure["phase"] == "preflight"
        assert failure["error"]["type"] == "ConfigurationError"
        assert "expected" in failure["error"]["message"] and "actual" in failure["error"]["message"]
        assert failure["inputs"]["experiment_id"] == "fixture-smoke"
        assert "suite_id" not in failure["inputs"]
        for absent_file in ("k6-summary.json", "aws-metrics.json", "result.json", "report.md"):
            assert not (result_dir / absent_file).exists(), f"unexpected {absent_file} after preflight failure"

        return "preflight capacity 불일치 시 seed/k6 미실행 및 failure.json(phase=preflight) 보존"


def _run_mid_run_failure(manifest_path: Path, expected: dict[str, Any], result_dir: Path) -> str:
    fake_boto3 = _FakeBoto3(expected)
    with LocalMockApiServer(fail_on_drain=True) as server, _isolated_harness_environment(server.base_url, fake_boto3):
        args = cli.parser().parse_args(["--experiment", str(manifest_path), "--result-dir", str(result_dir)])
        raised: ApiError | None = None
        try:
            cli.execute(args)
        except ApiError as exc:
            raised = exc
        assert raised is not None, "expected ApiError on simulated drain failure"

        assert len(server.suites_created) == 1
        assert len(server.runs_created) >= 1
        failure_path = result_dir / FAILURE_FILENAME
        assert failure_path.is_file(), "expected failure.json on mid-run drain failure"
        failure = json.loads(failure_path.read_text(encoding="utf-8"))
        assert failure["schema_version"] == 1
        assert failure["status"] == "ERROR"
        assert failure["phase"] == "drain"
        assert failure["error"]["type"] == "ApiError"
        assert failure["inputs"]["experiment_id"] == "fixture-smoke"
        assert failure["inputs"]["suite_id"] >= 1
        for expected_artifact in (
            "experiment.yaml", "profile.yaml", "dataset.yaml",
            "execution-plan.json", "k6-summary.json", "failure.json",
        ):
            assert expected_artifact in failure["available_artifacts"], f"missing {expected_artifact}"
        summary = json.loads((result_dir / "k6-summary.json").read_text(encoding="utf-8"))
        assert isinstance(summary, dict) and "metrics" in summary
        for absent_file in ("aws-metrics.json", "result.json", "report.md"):
            assert not (result_dir / absent_file).exists(), f"unexpected {absent_file} after drain failure"

        return f"seed·real k6({len(server.runs_created)}건) 후 drain 오류 시 failure.json(phase=drain) 및 k6-summary.json 보존"


def _execute_scenario(name: str, runner_fn: Any, manifest_path: Path, expected: dict[str, Any]) -> ScenarioResult:
    work_dir_holder: list[Path] = []
    try:
        with tempfile.TemporaryDirectory(prefix=f"guardbench-local-e2e-{name}-") as tmp_dir:
            work_dir = Path(tmp_dir)
            work_dir_holder.append(work_dir)
            detail = runner_fn(manifest_path, expected, work_dir / "result")
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


def run_all(manifest_path: Path = DEFAULT_MANIFEST) -> list[ScenarioResult]:
    """Run the three local E2E scenarios sequentially using real Runner and k6."""
    expected = load_execution_plan(manifest_path).to_dict()["infrastructure"]["expected"]
    scenarios = (
        ("normal_completion", _run_normal_completion),
        ("capacity_mismatch", _run_capacity_mismatch),
        ("mid_run_failure", _run_mid_run_failure),
    )
    return [_execute_scenario(name, fn, manifest_path, expected) for name, fn in scenarios]


def format_scope_banner(k6_path: str, manifest_path: Path) -> str:
    return "\n".join([
        "=== GuardBench Local Mock E2E Harness ===",
        f"Manifest: {manifest_path}",
        f"k6 binary: {k6_path}",
        "실제 실행 구성 요소: Runner 입력 해석(load_execution_plan), cli.execute orchestration, "
        "preflight/capacity 대조, 실제 k6 프로세스(performance/k6/test-run.js), 결과·실패 진단 보존",
        "테스트 대역 범위: 로컬 ThreadingHTTPServer 모의 HTTP API, fake boto3 AWS client "
        "(ecs/rds/sagemaker/sqs/cloudwatch), AWS credential/endpoint 미사용, S3 업로드 생략",
        "주의: 본 실행은 로컬 기능 검증이며 실제 AWS 인프라 또는 성능 Baseline 검증이 아닙니다.",
    ])


def run_harness(manifest_path: Path = DEFAULT_MANIFEST) -> tuple[int, list[ScenarioResult]]:
    """Validate prerequisites, run all scenarios, and return (exit_code, results)."""
    k6_path = resolve_k6_binary()
    if not k6_path:
        print(
            "ERROR: k6 실행 파일을 찾을 수 없습니다. PATH 또는 K6_BIN을 설정한 뒤 다시 실행하세요.",
            file=sys.stderr,
        )
        return 2, []

    print(format_scope_banner(k6_path, manifest_path))
    results = run_all(manifest_path)
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
