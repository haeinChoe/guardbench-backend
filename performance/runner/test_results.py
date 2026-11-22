"""Scenario identity, provenance, and report rendering tests."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

from performance.runner.plan import load_execution_plan
from performance.runner.results import CONCURRENCY_SEMANTICS, render_report, result_identity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def completed_result(
    *,
    scenario_name: str = "smoke.yaml",
    target_revision: str | None = "target-rev-1",
) -> tuple[dict, dict]:
    plan = load_execution_plan(
        api_url="https://api.example.test",
        target_url="https://target.example.test/v1/chat/completions",
        target_model="fixture-model",
        target_revision=target_revision,
        scenario_path=FIXTURES / "profiles" / scenario_name,
        dataset_path=FIXTURES / "datasets/two-cases.yaml",
    ).to_dict()
    workload = copy.deepcopy(plan["scenario"]["workload"])
    result = {
        "schema_version": 2,
        "run_id": "fixture-run",
        "started_at": "2026-11-22T00:00:00Z",
        "finished_at": "2026-11-22T00:00:04Z",
        **result_identity(plan),
        "connection": copy.deepcopy(plan["connection"]),
        "dataset": {**plan["dataset"], "suite_id": 7},
        "workload": workload,
        "tuple": {
            "concurrent_test_runs": workload["concurrent_test_runs"],
            "test_case_count": plan["dataset"]["test_case_count"],
        },
        "k6": {"exit_code": 0, "thresholds": "PASS"},
        "failure_categories": {"thresholds": "PASS", "execution_failures": 0},
        "test_run_diagnostics": {"failed_test_run_ids": [], "error_summary": {}},
        "preflight": {"activeRuns": 0},
        "acceptance": {
            "status": "FAIL",
            "checks": [{"name": "api.create_latency.p(95)", "actual": None, "passed": False}],
        },
    }
    return result, plan


class ResultTraceabilityTest(unittest.TestCase):
    def test_result_identity_returns_detached_plan_and_scenario_and_fails_closed(self):
        _, plan = completed_result()
        identity = result_identity(plan)
        self.assertEqual({"execution_plan", "scenario"}, set(identity))
        self.assertEqual(plan, identity["execution_plan"])
        self.assertEqual({"id": "FIXTURE-SMOKE", "type": "SMOKE"}, identity["scenario"])

        identity["execution_plan"]["connection"]["target"]["model"] = "mutated"
        identity["execution_plan"]["scenario"]["test"]["id"] = "mutated"
        self.assertEqual("fixture-model", plan["connection"]["target"]["model"])
        self.assertEqual("FIXTURE-SMOKE", plan["scenario"]["test"]["id"])

        for bad_plan in (
            None,
            {},
            {**copy.deepcopy(plan), "connection": {"api_url": " ", "target": {}}},
            {**copy.deepcopy(plan), "scenario": {"test": {"id": "FIXTURE-SMOKE"}}},
            {**copy.deepcopy(plan), "dataset": {"id": "d", "test_case_count": 0}},
            {**copy.deepcopy(plan), "sources": {"scenario": {"path": "/a", "sha256": "b"}}},
        ):
            with self.subTest(bad_plan=bad_plan), self.assertRaises(ValueError):
                result_identity(bad_plan)

    def test_report_includes_identity_workload_tuple_and_preserves_null_metrics(self):
        result, plan = completed_result()
        before = copy.deepcopy(result)

        report = render_report(result)
        self.assertEqual(before, result)
        for expected in (
            "Scenario: `FIXTURE-SMOKE` (SMOKE)",
            "Dataset: `fixture-two-cases-v1` (2 TestCases, suite `7`)",
            "GuardBench API: `https://api.example.test`",
            "Target: `https://target.example.test/v1/chat/completions` (HTTP_ENDPOINT, model=`fixture-model`, revision=`target-rev-1`)",
            "Workload tuple (C, T): (2, 2)",
            f"C 의미: {CONCURRENCY_SEMANTICS}",
            "## Resolved workload",
            "## Acceptance Criteria",
            "## Input Sources",
            plan["sources"]["scenario"]["path"],
            plan["sources"]["scenario"]["sha256"],
            plan["sources"]["dataset"]["path"],
            plan["sources"]["dataset"]["sha256"],
            '"actual": null',
        ):
            self.assertIn(expected, report)
        self.assertNotIn("Iteration validity", report)
        self.assertNotIn("Infrastructure", report)

    def test_staged_report_preserves_full_curve_and_optional_revision(self):
        stress_result, _ = completed_result(
            scenario_name="staged-stress.yaml",
            target_revision=None,
        )
        stress_result["acceptance"]["status"] = "PASS"
        stress_result["acceptance"]["checks"] = [
            {
                "name": "completion.iterations",
                "actual": {"started": 2, "completed": 2, "interrupted": 0},
                "expected": "started = completed > 0; no interrupted workload iterations",
                "passed": True,
            }
        ]

        stress_report = render_report(stress_result)
        for expected in (
            "Scenario: `FIXTURE-STAGED-STRESS` (STRESS)",
            "revision=`-`",
            "Workload tuple (C, T): (3, 2)",
            '"stages"',
            '"concurrent_test_runs": 0',
            "max configured VU target 3",
            "1s → 0 VUs",
            "하강 단계와 종료의 grace",
            "Iteration validity:",
            "'interrupted': 0",
        ):
            self.assertIn(expected, stress_report)


if __name__ == "__main__":
    unittest.main()
