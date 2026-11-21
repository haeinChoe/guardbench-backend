"""Experiment identity preservation and report rendering without fabricated metrics."""

from __future__ import annotations

import copy
import json
from typing import Any

CONCURRENCY_SEMANTICS = "최대 configured VU target; VU 하나가 TestRun 하나 완료를 기다림"


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
    experiment = _require_mapping(root.get("experiment"), "execution_plan.experiment")
    _require_non_empty_str(experiment, "id", "execution_plan.experiment")

    infrastructure = _require_mapping(root.get("infrastructure"), "execution_plan.infrastructure")
    _require_non_empty_str(infrastructure, "id", "execution_plan.infrastructure")
    _require_non_empty_str(infrastructure, "revision", "execution_plan.infrastructure")
    _require_mapping(infrastructure.get("expected"), "execution_plan.infrastructure.expected")

    profile = _require_mapping(root.get("profile"), "execution_plan.profile")
    test = _require_mapping(profile.get("test"), "execution_plan.profile.test")
    scenario_id = _require_non_empty_str(test, "id", "execution_plan.profile.test")
    scenario_type = _require_non_empty_str(test, "type", "execution_plan.profile.test")

    _require_mapping(root.get("sources"), "execution_plan.sources")

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
    """Render performance report distinguishing manifest expected values from pre-run snapshots."""
    plan = result["execution_plan"]
    infrastructure = plan["infrastructure"]
    scenario = result["scenario"]
    profile = result["profile"]
    workload = profile["workload"]
    acceptance = profile["acceptance"]
    capacity = result["infrastructure_capacity"]
    diagnostics = result.get("test_run_diagnostics", {})
    checks = result["acceptance"].get("checks", [])
    decision = result["acceptance"].get("status")
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

    aws_metrics = result.get("aws_metrics", {})
    aws_status = aws_metrics.get("status")
    empty_metric_ids = [
        metric["id"]
        for metric in (aws_metrics.get("metrics") or [])
        if isinstance(metric, dict) and metric.get("id") and not metric.get("values")
    ]

    lines = [
        f"# {scenario['id']} performance report",
        "",
        f"- Run ID: `{result['run_id']}`",
        f"- Experiment ID: `{plan['experiment']['id']}`",
        f"- 실행 시각: {result['started_at']} ~ {result['finished_at']}",
        f"- Application revision: {result['revisions']['application']}",
        f"- Infrastructure deployment revision: {result['revisions']['infrastructure']}",
        f"- Infrastructure configuration: `{infrastructure['id']}` (definition revision: `{infrastructure['revision']}`)",
        f"- Scenario: `{scenario['id']}` ({scenario['type']})",
        f"- Dataset: `{result['dataset']['id']}` ({result['dataset']['test_case_count']} TestCases)",
        f"- Workload: {workload_summary}",
        (
            f"- Experiment tuple (W,C,T): ({result['experiment']['work_items_concurrency']}, "
            f"{result['experiment']['concurrent_test_runs']}, {result['experiment']['ecs_task_count']})"
        ),
        f"- C 의미: {CONCURRENCY_SEMANTICS}",
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
        (
            f"- Queue drain: <= {acceptance['completion']['queue_drain_seconds']}s, "
            f"DLQ messages <= {acceptance['completion']['dlq_messages']}"
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
        f"- Queue drain: {result['drain']}",
        f"- AWS metrics: `aws-metrics.json` ({aws_status})",
    ]
    if empty_metric_ids:
        lines.append(
            f"- AWS metrics without datapoints (미수집, 값 미대체): {', '.join(empty_metric_ids)}"
        )
    if aws_status != "COLLECTED":
        lines.append(
            f"- AWS metrics 미수집 상태({aws_status}): 수집되지 않은 값은 정상 수치로 채우지 않습니다."
        )
    if iteration_check is not None:
        lines.append(f"- Iteration validity: {iteration_check}")
    lines += [
        "",
        "```json",
        _json(checks),
        "```",
        "",
        "## Selected Infrastructure Capacity (Manifest Expected)",
        "",
        "실행 manifest에 선언한 기대 capacity이며 실제 관측값이 아닙니다.",
        "",
        "```json",
        _json(infrastructure["expected"]),
        "```",
        "",
        "## Observed Infrastructure Capacity Snapshot (Pre-Run)",
        "",
        f"- Captured at: {capacity.get('captured_at')}",
        "",
        "```json",
        _json(capacity),
        "```",
        "",
        (
            "위 값은 실행 전 AWS API에서 조회한 configured capacity snapshot이며, "
            "실행 중 관측 시계열은 `aws-metrics.json`에 별도로 저장합니다. 미수집 값은 정상 수치로 채우지 않습니다."
        ),
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
        "## Bottleneck observations",
        "",
        "- 자동 병목 판정은 하지 않는다. k6 summary와 AWS metric 시계열을 함께 확인해 관찰을 기록한다.",
        "",
        "## Next experiment",
        "",
        "- 동일 Dataset과 Infrastructure Capacity에서 Workload만 변경해 포화 구간을 찾는다.",
        "- 인프라 효과를 볼 때는 동일 Dataset/Workload를 유지하고 Infrastructure Capacity만 변경한다.",
        "- 목표값을 바꾸어 결과를 맞추지 말고 실행 전에 Profile을 확정한다.",
    ]
    return "\n".join(lines) + "\n"
