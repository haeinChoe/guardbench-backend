"""Evaluate k6 and application-level acceptance criteria without mutating scenario input."""

from __future__ import annotations

import math
from typing import Any


def _metric(summary: dict[str, Any], name: str) -> dict[str, Any]:
    """Return metric values from both k6 summary-export shapes.

    ``k6 run --summary-export`` writes metric aggregates directly below
    ``metrics[name]`` while some fixtures use a nested ``values`` object.
    Accept both without masking missing metrics.
    """
    metric = summary.get("metrics", {}).get(name, {})
    if not isinstance(metric, dict):
        return {}
    values = metric.get("values")
    return values if isinstance(values, dict) else metric


def _value(values: dict[str, Any], key: str) -> float | None:
    value = values.get(key)
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _rate_value(metric: dict[str, Any]) -> float | None:
    """Read Rate metrics from k6 summary-export shapes."""
    value = _value(metric, "rate")
    return value if value is not None else _value(metric, "value")


def evaluate(
    scenario: dict[str, Any],
    summary: dict[str, Any],
    k6_threshold_failed: bool = False,
) -> dict[str, Any]:
    """Evaluate application-level latency, error, completion, and threshold checks."""
    api = scenario["acceptance"]["api"]
    completion = scenario["acceptance"]["completion"]
    checks: list[dict[str, Any]] = []

    workload = scenario.get("workload")
    if isinstance(workload, dict) and "stages" in workload:
        started = _value(_metric(summary, "test_run_iterations_started"), "count")
        completed = _value(_metric(summary, "test_run_iterations_completed"), "count")
        valid_counts = all(
            value is not None and math.isfinite(value) and value >= 0 and value.is_integer()
            for value in (started, completed)
        )
        if valid_counts:
            started = int(started)
            completed = int(completed)
        interrupted = max(0, started - completed) if valid_counts else None
        checks.append({
            "name": "completion.iterations",
            "actual": {"started": started, "completed": completed, "interrupted": interrupted},
            "expected": "started = completed > 0; no interrupted workload iterations",
            "passed": valid_counts and started == completed and started > 0,
        })

    latency = _metric(summary, "test_run_create_latency")
    for percentile in ("p(50)", "p(95)", "p(99)"):
        actual = _value(latency, percentile)
        expected = float(api["create_latency_ms"]["p" + percentile[2:-1]])
        checks.append({
            "name": f"api.create_latency.{percentile}",
            "actual": actual,
            "expected": f"< {expected} ms",
            "passed": actual is not None and actual < expected,
        })

    create_errors = _rate_value(_metric(summary, "test_run_create_errors"))
    checks.append({
        "name": "api.create_error_rate",
        "actual": create_errors,
        "expected": f"<= {api['error_rate']}",
        "passed": create_errors is not None and create_errors <= api["error_rate"],
    })

    completion_failures = _rate_value(_metric(summary, "test_run_completion_failures"))
    checks.append({
        "name": "completion.failure_rate",
        "actual": completion_failures,
        "expected": f"<= {completion['failure_rate']}",
        "passed": completion_failures is not None and completion_failures <= completion["failure_rate"],
    })

    completion_duration = _value(_metric(summary, "test_run_completion_duration"), "p(95)")
    checks.append({
        "name": "completion.duration.p95",
        "actual": completion_duration,
        "expected": f"<= {completion['max_seconds']} s",
        "passed": completion_duration is not None and completion_duration <= completion["max_seconds"],
    })

    checks.append({
        "name": "k6.thresholds",
        "actual": "FAIL" if k6_threshold_failed else "PASS",
        "expected": "PASS",
        "passed": not k6_threshold_failed,
    })
    return {
        "status": "PASS" if all(check["passed"] for check in checks) else "FAIL",
        "checks": checks,
    }
