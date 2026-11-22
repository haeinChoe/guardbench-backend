"""Scenario identity preservation and Markdown report rendering."""

from __future__ import annotations

import copy
import json
from typing import Any

CONCURRENCY_SEMANTICS = "최대 configured VU target; VU 하나가 TestRun 하나 완료를 기다림"
RESULT_FILENAMES = (
    "scenario.yaml",
    "dataset.yaml",
    "execution-plan.json",
    "k6-summary.json",
    "result.json",
    "report.md",
)


def _require_mapping(container: Any, path: str) -> dict[str, Any]:
    if not isinstance(container, dict):
        raise ValueError(f"{path}는 object여야 합니다.")
    return container


def _require_non_empty_str(container: dict[str, Any], key: str, path: str) -> str:
    value = container.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{key}는 비어 있지 않은 문자열이어야 합니다.")
    return value


def result_identity(plan: dict[str, Any]) -> dict[str, Any]:
    """Return detached execution_plan and scenario identity from a resolved plan."""
    root = _require_mapping(plan, "execution_plan")

    connection = _require_mapping(root.get("connection"), "execution_plan.connection")
    _require_non_empty_str(connection, "api_url", "execution_plan.connection")
    target = _require_mapping(connection.get("target"), "execution_plan.connection.target")
    _require_non_empty_str(target, "type", "execution_plan.connection.target")
    _require_non_empty_str(target, "url", "execution_plan.connection.target")
    _require_non_empty_str(target, "model", "execution_plan.connection.target")

    scenario = _require_mapping(root.get("scenario"), "execution_plan.scenario")
    test = _require_mapping(scenario.get("test"), "execution_plan.scenario.test")
    scenario_id = _require_non_empty_str(test, "id", "execution_plan.scenario.test")
    scenario_type = _require_non_empty_str(test, "type", "execution_plan.scenario.test")

    dataset = _require_mapping(root.get("dataset"), "execution_plan.dataset")
    _require_non_empty_str(dataset, "id", "execution_plan.dataset")
    test_case_count = dataset.get("test_case_count")
    if isinstance(test_case_count, bool) or not isinstance(test_case_count, int) or test_case_count < 1:
        raise ValueError("execution_plan.dataset.test_case_count는 1 이상인 정수여야 합니다.")

    sources = _require_mapping(root.get("sources"), "execution_plan.sources")
    for source_name in ("scenario", "dataset"):
        entry = _require_mapping(sources.get(source_name), f"execution_plan.sources.{source_name}")
        _require_non_empty_str(entry, "path", f"execution_plan.sources.{source_name}")
        _require_non_empty_str(entry, "sha256", f"execution_plan.sources.{source_name}")

    plan_copy = copy.deepcopy(root)
    return {
        "execution_plan": plan_copy,
        "scenario": {
            "id": scenario_id,
            "type": scenario_type,
        },
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str, allow_nan=False)


def render_report(result: dict[str, Any]) -> str:
    """Render application-level performance report without fabricating missing metrics."""
    plan = result["execution_plan"]
    connection = result["connection"]
    target = connection["target"]
    scenario = result["scenario"]
    workload = result["workload"]
    acceptance = plan["scenario"]["acceptance"]
    diagnostics = result.get("test_run_diagnostics", {})
    checks = result["acceptance"].get("checks", [])
    decision = result["acceptance"].get("status")
    tuple_info = result["tuple"]
    iteration_check = next(
        (check for check in checks if check.get("name") == "completion.iterations"),
        None,
    )

    if "stages" in workload:
        curve = ", ".join(
            f"{stage['duration_seconds']}s → {stage['concurrent_test_runs']} VUs"
            for stage in workload["stages"]
        )
        workload_summary = f"max configured VU target {workload['concurrent_test_runs']}; stages: {curve}"
    else:
        workload_summary = (
            f"{workload['concurrent_test_runs']} concurrent TestRuns, "
            f"ramp-up {workload['ramp_up_seconds']}s, duration {workload['duration_seconds']}s"
        )

    lines = [
        f"# {scenario['id']} performance report",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- 실행 시각: {result['started_at']} ~ {result['finished_at']}",
        f"- Scenario: `{scenario['id']}` ({scenario['type']})",
        f"- Dataset: `{result['dataset']['id']}` ({result['dataset']['test_case_count']} TestCases, suite `{result['dataset']['suite_id']}`)",
        f"- GuardBench API: `{connection['api_url']}`",
        (
            f"- Target: `{target['url']}` ({target['type']}, model=`{target['model']}`, "
            f"revision=`{target.get('revision') or '-'}`)"
        ),
        f"- Workload: {workload_summary}",
        f"- Workload tuple (C, T): ({tuple_info['concurrent_test_runs']}, {tuple_info['test_case_count']})",
        f"- C 의미: {CONCURRENCY_SEMANTICS}",
        "- T 의미: Dataset TestCase 수",
        "",
        "## Resolved workload",
        "",
        "```json",
        _json(workload),
        "```",
        "",
        "하강 단계와 종료의 grace로 실제 종료 시각은 선언한 부하 구간보다 늦을 수 있습니다.",
        "",
        "## Acceptance Criteria",
        "",
        (
            f"- API latency: p50 < {acceptance['api']['create_latency_ms']['p50']}ms, "
            f"p95 < {acceptance['api']['create_latency_ms']['p95']}ms, "
            f"p99 < {acceptance['api']['create_latency_ms']['p99']}ms"
        ),
        f"- API error rate: <= {acceptance['api']['error_rate']}",
        (
            f"- TestRun completion: <= {acceptance['completion']['max_seconds']}s, "
            f"failure rate <= {acceptance['completion']['failure_rate']}"
        ),
        "",
        "## Results",
        "",
        f"- Decision: **{decision}**",
        "- k6 summary: `k6-summary.json`",
        f"- Performance thresholds: {result['failure_categories']['thresholds']} (k6 exit {result['k6']['exit_code']})",
        f"- Business execution failures: {result['failure_categories']['execution_failures']} TestRun(s)",
        f"- Failed TestRun IDs: {diagnostics.get('failed_test_run_ids', [])}",
        f"- Execution error summary: {diagnostics.get('error_summary', {})}",
    ]
    if iteration_check is not None:
        lines.append(f"- Iteration validity: {iteration_check}")
    lines += [
        "",
        "수집되지 않은 metric은 임의 수치로 채우지 않고 `null`로 남깁니다.",
        "",
        "```json",
        _json(checks),
        "```",
        "",
        "## Input Sources",
        "",
        "실행 전에 검증·해석한 입력 출처이며 해시는 환경변수 치환 전 원본 파일 byte의 SHA-256입니다.",
        "",
    ]
    for name, source in plan["sources"].items():
        lines.append(f"- `{name}`: `{source['path']}` (sha256: `{source['sha256']}`)")
    lines += [
        "",
        "## Observations",
        "",
        "- 자동 병목 판정은 하지 않는다. 부하 결과는 `k6-summary.json`과 `result.json`을 함께 확인한다.",
        "- 목표값을 바꾸어 결과를 맞추지 말고 실행 전에 시나리오 기준을 확정한다.",
    ]
    return "\n".join(lines) + "\n"
