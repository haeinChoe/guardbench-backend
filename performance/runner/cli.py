"""CLI entrypoint for GuardBench performance runner."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .acceptance import evaluate
from .api import ApiClient, ApiError
from .config import ConfigurationError
from .failure import RunContext, preserve_failure, sanitize_message
from .plan import load_execution_plan
from .results import render_report, result_identity


class RunnerError(RuntimeError):
    pass


K6_THRESHOLD_FAILURE_EXIT_CODE = 99
RESULT_SCHEMA_VERSION = 2


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _assert_preflight(api: ApiClient) -> dict[str, Any]:
    api.health_check()
    active = api.list_runs(statuses=["QUEUED", "PREPARING", "RUNNING"])
    if active:
        raise RunnerError(f"이전 TestRun workload가 처리 중입니다: {len(active)}건")
    return {"activeRuns": 0}


def _k6_environment(
    scenario: dict[str, Any],
    connection: dict[str, Any],
    run_id: str,
    suite_id: int,
) -> dict[str, str]:
    workload = scenario["workload"]
    target = connection["target"]
    api = scenario["acceptance"]["api"]
    completion = scenario["acceptance"]["completion"]
    environment = {
        "PERF_BASE_URL": connection["api_url"],
        "PERF_SUITE_ID": str(suite_id),
        "PERF_TARGET_URL": target["url"],
        "PERF_TARGET_MODEL": target["model"],
        "PERF_TARGET_REVISION": target.get("revision") or "",
        "PERF_RUN_ID": run_id,
        "PERF_CONCURRENT_TEST_RUNS": str(workload["concurrent_test_runs"]),
        "PERF_MAX_ITERATIONS_PER_VU": str(workload.get("max_iterations_per_vu", 0)),
        "PERF_COMPLETION_TIMEOUT_SECONDS": str(workload["completion_timeout_seconds"]),
        "PERF_POLLING_INTERVAL_SECONDS": str(workload["polling_interval_seconds"]),
        "PERF_API_P50_MS": str(api["create_latency_ms"]["p50"]),
        "PERF_API_P95_MS": str(api["create_latency_ms"]["p95"]),
        "PERF_API_P99_MS": str(api["create_latency_ms"]["p99"]),
        "PERF_API_ERROR_RATE": str(api["error_rate"]),
        "PERF_COMPLETION_MAX_SECONDS": str(completion["max_seconds"]),
        "PERF_COMPLETION_FAILURE_RATE": str(completion["failure_rate"]),
    }
    if "stages" in workload:
        environment["PERF_STAGES_JSON"] = json.dumps(workload["stages"], allow_nan=False)
    else:
        environment["PERF_RAMP_UP_SECONDS"] = str(workload["ramp_up_seconds"])
        environment["PERF_DURATION_SECONDS"] = str(workload["duration_seconds"])
    return environment


def _threshold_inputs(
    scenario: dict[str, Any],
    connection: dict[str, Any],
    run_id: str,
    suite_id: int,
) -> dict[str, str]:
    environment = _k6_environment(scenario, connection, run_id, suite_id)
    names = (
        "PERF_API_P50_MS",
        "PERF_API_P95_MS",
        "PERF_API_P99_MS",
        "PERF_API_ERROR_RATE",
        "PERF_COMPLETION_MAX_SECONDS",
        "PERF_COMPLETION_FAILURE_RATE",
    )
    return {name: environment[name] for name in names}


def run_k6(
    scenario: dict[str, Any],
    connection: dict[str, Any],
    run_id: str,
    suite_id: int,
    summary_path: Path,
    repo_root: Path,
) -> int:
    environment = os.environ.copy()
    for name in ("PERF_STAGES_JSON", "PERF_RAMP_UP_SECONDS", "PERF_DURATION_SECONDS"):
        environment.pop(name, None)
    environment.update(_k6_environment(scenario, connection, run_id, suite_id))
    try:
        result = subprocess.run(
            [
                os.environ.get("K6_BIN", "k6"),
                "run",
                "--summary-export",
                str(summary_path),
                str(repo_root / "performance/k6/test-run.js"),
            ],
            cwd=repo_root,
            env=environment,
            check=False,
        )
    except OSError as exc:
        raise RunnerError("k6 workload 실행에 실패했습니다.") from exc
    if result.returncode not in {0, K6_THRESHOLD_FAILURE_EXIT_CODE}:
        raise RunnerError(f"k6 script 또는 실행 환경 오류로 종료되었습니다 (exit {result.returncode}).")
    if not summary_path.is_file():
        raise RunnerError("k6가 summary export를 만들지 않았습니다.")
    return result.returncode


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunnerError(f"JSON 결과를 읽을 수 없습니다: {path}") from exc
    if not isinstance(value, dict):
        raise RunnerError(f"JSON 결과 최상위는 object여야 합니다: {path}")
    return value


def _collect_run_diagnostics(
    api: ApiClient,
    suite_id: int,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, Any]:
    runs = [
        run
        for run in api.list_runs(created_from=started_at, created_to=finished_at)
        if run.get("testSuiteId") == suite_id
    ]
    failed_runs: list[dict[str, Any]] = []
    error_counts: Counter[str] = Counter()
    for run in runs:
        run_id = run.get("id")
        if not isinstance(run_id, int):
            continue
        detail = api.get_run(run_id)
        outcome = detail.get("executionOutcome")
        if outcome == "COMPLETED":
            continue
        errors: list[dict[str, Any]] = []
        for item in api.list_run_results(run_id):
            error = item.get("error")
            if not isinstance(error, dict):
                continue
            stage = error.get("stage")
            code = error.get("code")
            if stage or code:
                error_counts[f"{stage or 'UNKNOWN'}:{code or 'UNKNOWN'}"] += 1
            errors.append({"stage": stage, "code": code})
        failed_runs.append({
            "id": run_id,
            "status": detail.get("status"),
            "executionOutcome": outcome,
            "errors": errors,
        })
    return {
        "test_run_count": len(runs),
        "failed_test_run_ids": [item["id"] for item in failed_runs],
        "failed_runs": failed_runs,
        "error_summary": dict(sorted(error_counts.items())),
    }


def execute(args: argparse.Namespace) -> int:
    repo_root = _repo_root()
    run_started_at = _now()
    fallback_run_id = f"preflight-error-{run_started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    context = RunContext(run_id=fallback_run_id, started_at=run_started_at, dry_run=bool(args.dry_run))

    try:
        context.set_phase("preflight")
        context.record_cli_inputs(
            api_url=args.api_url,
            target_url=args.target_url,
            target_model=args.target_model,
            target_revision=args.target_revision,
            scenario_path=args.scenario,
            dataset_path=args.dataset,
        )
        context.result_dir = (
            Path(args.result_dir).resolve()
            if args.result_dir
            else repo_root / "performance/results" / context.run_id
        )

        execution_plan = load_execution_plan(
            api_url=args.api_url,
            target_url=args.target_url,
            target_model=args.target_model,
            target_revision=args.target_revision or None,
            scenario_path=Path(args.scenario),
            dataset_path=Path(args.dataset),
        )
        plan = execution_plan.to_dict()
        context.record_execution_plan(plan)

        scenario = execution_plan.scenario
        connection = execution_plan.connection
        dataset = execution_plan.dataset
        payload = execution_plan.seed_payload
        test_case_count = execution_plan.test_case_count

        run_id = f"{scenario['test']['id']}-{run_started_at.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
        context.run_id = run_id
        if not args.result_dir:
            context.result_dir = repo_root / "performance/results" / run_id

        result_dir = context.result_dir
        result_dir.mkdir(parents=True, exist_ok=False)
        context.result_dir_owned = True
        for name, source in execution_plan.source_bytes.items():
            (result_dir / f"{name}.yaml").write_bytes(source)
        (result_dir / "execution-plan.json").write_text(
            json.dumps(plan, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )

        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0

        api = ApiClient(connection["api_url"])
        preflight = _assert_preflight(api)

        context.set_phase("seed")
        api.health_check()
        suite_id = api.create_suite(payload)
        if suite_id <= 0:
            raise RunnerError("seed 결과의 TestSuite id가 양수가 아닙니다.")
        context.record_input("suite_id", suite_id)

        context.set_phase("k6")
        started_at = _now()
        summary_path = result_dir / "k6-summary.json"
        threshold_inputs = _threshold_inputs(scenario, connection, run_id, suite_id)
        k6_exit_code = run_k6(scenario, connection, run_id, suite_id, summary_path, repo_root)
        summary = _read_json(summary_path)
        finished_at = _now()

        context.set_phase("report")
        workload = scenario["workload"]
        threshold_failed = k6_exit_code == K6_THRESHOLD_FAILURE_EXIT_CODE
        acceptance = evaluate(scenario, summary, k6_threshold_failed=threshold_failed)
        diagnostics = _collect_run_diagnostics(api, suite_id, started_at, finished_at)
        identity = result_identity(plan)
        result = {
            "schema_version": RESULT_SCHEMA_VERSION,
            "run_id": run_id,
            "started_at": _iso(started_at),
            "finished_at": _iso(finished_at),
            **identity,
            "connection": connection,
            "dataset": {
                "id": dataset["id"],
                "test_case_count": test_case_count,
                "suite_id": suite_id,
            },
            "workload": workload,
            "tuple": {
                "concurrent_test_runs": workload["concurrent_test_runs"],
                "test_case_count": test_case_count,
            },
            "k6": {
                "exit_code": k6_exit_code,
                "thresholds": "FAIL" if threshold_failed else "PASS",
                "threshold_inputs": threshold_inputs,
            },
            "failure_categories": {
                "thresholds": "FAIL" if threshold_failed else "PASS",
                "execution_failures": len(diagnostics["failed_test_run_ids"]),
            },
            "test_run_diagnostics": diagnostics,
            "preflight": preflight,
            "acceptance": acceptance,
        }
        report_text = render_report(result)
        (result_dir / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n", encoding="utf-8")
        (result_dir / "report.md").write_text(report_text, encoding="utf-8")

        print(f"{acceptance['status']}: {result_dir}")
        return 0 if acceptance["status"] == "PASS" else 1
    except Exception as exc:
        if not context.dry_run:
            preserve_failure(context, exc)
        raise


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="Run a GuardBench performance scenario.")
    command.add_argument("--api-url", required=True, help="GuardBench API base URL.")
    command.add_argument("--target-url", required=True, help="Application Target HTTP endpoint URL.")
    command.add_argument("--target-model", required=True, help="Application Target model identifier.")
    command.add_argument("--target-revision", default="", help="Optional Application Target revision.")
    command.add_argument("--scenario", required=True, help="Scenario YAML file path.")
    command.add_argument("--dataset", required=True, help="Dataset YAML file path.")
    command.add_argument("--result-dir", help="Local directory to write artifacts into.")
    command.add_argument("--dry-run", action="store_true", help="Validate inputs and print the execution plan.")
    return command


def main(argv: list[str] | None = None) -> int:
    try:
        return execute(parser().parse_args(argv))
    except (ConfigurationError, RunnerError, ApiError) as exc:
        print(f"ERROR: {sanitize_message(str(exc))}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {sanitize_message(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
