"""Experiment identity, provenance and report traceability tests."""

from __future__ import annotations

import copy
import unittest
from pathlib import Path

import yaml

from performance.runner.plan import load_execution_plan
from performance.runner.results import CONCURRENCY_SEMANTICS, render_report, result_identity

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def completed_result(
    *,
    profile_path: Path | None = None,
    infrastructure_revision: str | None = None,
) -> tuple[dict, dict]:
    plan = load_execution_plan(FIXTURES / "experiments/smoke.yaml").to_dict()
    if profile_path is not None:
        profile = yaml.safe_load(profile_path.read_text(encoding="utf-8"))
        plan["profile"] = profile
    if infrastructure_revision is not None:
        plan["infrastructure"]["revision"] = infrastructure_revision
    result = {
        "schema_version": 2,
        "run_id": "fixture-run",
        "started_at": "2026-11-20T00:00:00Z",
        "finished_at": "2026-11-20T00:00:04Z",
        "revisions": {"application": "app-sha", "infrastructure": "deployment-sha"},
        "infrastructure_capacity": {
            "captured_at": "2026-11-20T00:00:00Z",
            "ecs": {
                "worker": {
                    "cluster_identifier": "fixture-cluster",
                    "service_identifier": "fixture-worker",
                    "task_cpu": "256",
                    "task_memory": "512",
                    "desired_count": 1,
                    "running_task_count": 1,
                    "pending_task_count": 0,
                    "work_items_concurrency": 2,
                }
            },
            "rds": {"db_instance_identifier": "fixture-db", "db_instance_class": None},
            "sagemaker": {
                "endpoint_name": "fixture-classifier",
                "production_variant_name": "AllTraffic",
                "instance_type": "fixture-instance-type",
                "desired_instance_count": 1,
                "current_instance_count": 1,
            },
        },
        "experiment": {
            "work_items_concurrency": 2,
            "concurrent_test_runs": plan["profile"]["workload"]["concurrent_test_runs"],
            "ecs_task_count": 1,
        },
        "dataset": {**plan["dataset"], "suite_id": 7},
        "profile": copy.deepcopy(plan["profile"]),
        "k6": {"exit_code": 0, "thresholds": "PASS"},
        "failure_categories": {"thresholds": "PASS", "execution_failures": 0},
        "test_run_diagnostics": {"failed_test_run_ids": [], "error_summary": {}},
        "preflight": {"capacity": {"passed": True}},
        "drain": {"passed": True, "duration_seconds": 0.1},
        "final_dlq": [],
        "aws_metrics": {
            "status": "COLLECTED",
            "metrics": [
                {"id": "ecs_cpu_utilization", "values": [12.5]},
                {"id": "sagemaker_invocations", "values": []},
            ],
        },
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

        identity["execution_plan"]["experiment"]["id"] = "mutated"
        identity["execution_plan"]["profile"]["test"]["id"] = "mutated"
        self.assertEqual("fixture-smoke", plan["experiment"]["id"])
        self.assertEqual("FIXTURE-SMOKE", plan["profile"]["test"]["id"])

        for bad_plan in (
            None,
            {},
            {**copy.deepcopy(plan), "experiment": {}},
            {**copy.deepcopy(plan), "infrastructure": {"id": "fixture-infra", "revision": " ", "expected": {}}},
            {**copy.deepcopy(plan), "profile": {"test": {"id": "FIXTURE-SMOKE"}}},
            {**copy.deepcopy(plan), "sources": None},
        ):
            with self.subTest(bad_plan=bad_plan), self.assertRaises(ValueError):
                result_identity(bad_plan)

    def test_report_separates_expected_observed_sources_and_uncollected_metrics(self):
        result, plan = completed_result()
        result.update(result_identity(plan))
        before = copy.deepcopy(result)

        report = render_report(result)
        self.assertEqual(before, result)
        for expected in (
            "Experiment ID: `fixture-smoke`",
            "Infrastructure deployment revision: deployment-sha",
            "Infrastructure configuration: `fixture-infra` (definition revision: `fixture-v1`)",
            "Scenario: `FIXTURE-SMOKE` (SMOKE)",
            "Dataset: `fixture-two-cases-v1` (2 TestCases)",
            "Experiment tuple (W,C,T): (2, 2, 1)",
            f"C 의미: {CONCURRENCY_SEMANTICS}",
            "## Selected Infrastructure Capacity (Manifest Expected)",
            "## Observed Infrastructure Capacity Snapshot (Pre-Run)",
            "## Input Sources",
            plan["sources"]["experiment"]["path"],
            plan["sources"]["experiment"]["sha256"],
            plan["sources"]["profile"]["sha256"],
            plan["sources"]["dataset"]["sha256"],
            '"actual": null',
            '"db_instance_class": null',
            "sagemaker_invocations",
        ):
            self.assertIn(expected, report)
        self.assertNotIn("Iteration validity", report)

        result_not_configured = copy.deepcopy(result)
        result_not_configured["aws_metrics"] = {"status": "NOT_CONFIGURED", "metrics": []}
        self.assertIn("AWS metrics 미수집 상태(NOT_CONFIGURED)", render_report(result_not_configured))

    def test_staged_report_preserves_full_curve_and_distinguishes_revision_and_scenario(self):
        smoke_result, smoke_plan = completed_result()
        smoke_result.update(result_identity(smoke_plan))

        stress_result, stress_plan = completed_result(profile_path=FIXTURES / "profiles/staged-stress.yaml")
        stress_result["acceptance"]["checks"].append({
            "name": "completion.iterations",
            "actual": {"started": 2, "completed": 2, "interrupted": 0},
            "expected": "started = completed > 0; no interrupted workload iterations",
            "passed": True,
        })
        stress_result.update(result_identity(stress_plan))

        v2_result, v2_plan = completed_result(infrastructure_revision="fixture-v2")
        v2_result.update(result_identity(v2_plan))

        self.assertEqual(
            smoke_result["execution_plan"]["infrastructure"]["id"],
            stress_result["execution_plan"]["infrastructure"]["id"],
        )
        self.assertNotEqual(smoke_result["scenario"], stress_result["scenario"])
        self.assertEqual({"id": "FIXTURE-STAGED-STRESS", "type": "STRESS"}, stress_result["scenario"])
        self.assertEqual(
            smoke_result["execution_plan"]["infrastructure"]["id"],
            v2_result["execution_plan"]["infrastructure"]["id"],
        )
        self.assertNotEqual(
            smoke_result["execution_plan"]["infrastructure"]["revision"],
            v2_result["execution_plan"]["infrastructure"]["revision"],
        )

        stress_report = render_report(stress_result)
        for expected in (
            '"stages"',
            '"concurrent_test_runs": 0',
            "max configured VU target 3",
            "1s → 0 VUs",
            "하강 단계와 종료의 grace",
            "Iteration validity:",
            "'interrupted': 0",
        ):
            self.assertIn(expected, stress_report)
        self.assertIn("definition revision: `fixture-v2`", render_report(v2_result))


if __name__ == "__main__":
    unittest.main()
