"""Execution orchestration tests without infrastructure dependencies."""

import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from performance.runner import cli
from performance.runner.config import ConfigurationError
from performance.runner.failure import FAILURE_FILENAME
from performance.runner.plan import load_execution_plan
from performance.runner.results import RESULT_FILENAMES
from performance.runner.test_runner import passing_summary


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class PerformanceExecutionTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.result_dir = self.directory / "result"
        self.scenario_path = self.directory / "smoke.yaml"
        self.dataset_path = self.directory / "dataset.yaml"
        shutil.copyfile(FIXTURES / "profiles/smoke.yaml", self.scenario_path)
        shutil.copyfile(FIXTURES / "datasets/two-cases.yaml", self.dataset_path)

        self.args = self._parse_args(self.result_dir)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))

        self.api = self.stack.enter_context(patch.object(cli, "ApiClient")).return_value
        self.api.list_runs.return_value = []
        self.api.create_suite.return_value = 7

        def run_k6(*args):
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return 0

        self.k6 = self.stack.enter_context(patch.object(cli, "run_k6", side_effect=run_k6))
        self.subprocess = self.stack.enter_context(
            patch.object(cli.subprocess, "run", side_effect=AssertionError("unexpected process"))
        )

    def _parse_args(self, result_dir: Path, *, scenario_path: Path | None = None, revision: str = "rev-1"):
        argv = [
            "--api-url", "https://api.example.test",
            "--target-url", "https://target.example.test/v1/chat/completions",
            "--target-model", "fixture-model",
            "--target-revision", revision,
            "--scenario", str(scenario_path or self.scenario_path),
            "--dataset", str(self.dataset_path),
            "--result-dir", str(result_dir),
        ]
        return cli.parser().parse_args(argv)

    def assert_seed_and_workload_blocked(self):
        self.api.create_suite.assert_not_called()
        self.k6.assert_not_called()

    def test_execution_writes_app_level_result_and_report_without_infra(self):
        self.assertEqual(0, cli.execute(self.args))

        for name in RESULT_FILENAMES:
            self.assertTrue((self.result_dir / name).is_file(), name)
        self.assertFalse((self.result_dir / FAILURE_FILENAME).exists())

        result = json.loads((self.result_dir / "result.json").read_text(encoding="utf-8"))
        plan = json.loads((self.result_dir / "execution-plan.json").read_text(encoding="utf-8"))
        self.assertEqual(2, result["schema_version"])
        self.assertEqual(plan, result["execution_plan"])
        self.assertEqual({"id": "FIXTURE-SMOKE", "type": "SMOKE"}, result["scenario"])
        self.assertEqual(
            {
                "api_url": "https://api.example.test",
                "target": {
                    "type": "HTTP_ENDPOINT",
                    "url": "https://target.example.test/v1/chat/completions",
                    "model": "fixture-model",
                    "revision": "rev-1",
                },
            },
            result["connection"],
        )
        self.assertEqual({"id": "fixture-two-cases-v1", "test_case_count": 2, "suite_id": 7}, result["dataset"])
        self.assertEqual({"concurrent_test_runs": 2, "test_case_count": 2}, result["tuple"])
        self.assertNotIn("infrastructure_capacity", result)
        self.assertEqual(
            [
                "completion.failure_rate" if False else "api.create_latency.p(50)",
                "api.create_latency.p(95)",
                "api.create_latency.p(99)",
                "api.create_error_rate",
                "completion.failure_rate",
                "completion.duration.p95",
                "k6.thresholds",
            ],
            [check["name"] for check in result["acceptance"]["checks"]],
        )
        self.api.create_suite.assert_called_once()
        self.k6.assert_called_once()
        self.subprocess.assert_not_called()

        report = (self.result_dir / "report.md").read_text(encoding="utf-8")
        self.assertNotIn("Iteration validity", report)
        for expected in (
            "FIXTURE-SMOKE",
            "SMOKE",
            "fixture-two-cases-v1",
            "2 TestCases",
            "Workload tuple (C, T): (2, 2)",
            "최대 configured VU target; VU 하나가 TestRun 하나 완료를 기다림",
            "## Input Sources",
            plan["sources"]["scenario"]["path"],
            plan["sources"]["scenario"]["sha256"],
            plan["sources"]["dataset"]["sha256"],
        ):
            self.assertIn(expected, report)

    def test_result_uses_resolved_snapshots_when_files_change_during_workload(self):
        expected_plan = load_execution_plan(
            api_url="https://api.example.test",
            target_url="https://target.example.test/v1/chat/completions",
            target_model="fixture-model",
            target_revision="rev-1",
            scenario_path=self.scenario_path,
            dataset_path=self.dataset_path,
        ).to_dict()

        def mutate_sources(*args):
            for path in (self.scenario_path, self.dataset_path):
                path.write_text("invalid after workload started", encoding="utf-8")
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return 0

        self.k6.side_effect = mutate_sources
        self.assertEqual(0, cli.execute(self.args))
        result = json.loads((self.result_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual("fixture-two-cases-v1", result["dataset"]["id"])
        self.assertEqual(2, result["dataset"]["test_case_count"])
        self.assertEqual({"id": "FIXTURE-SMOKE", "type": "SMOKE"}, result["scenario"])
        self.assertEqual(expected_plan["sources"], result["execution_plan"]["sources"])
        self.assertEqual(2, len(self.api.create_suite.call_args.args[0]["testCases"]))

    def test_staged_workload_preserves_curve_and_iteration_validity(self):
        def run_staged_k6(*args):
            summary = passing_summary()
            summary["metrics"]["test_run_iterations_started"] = {"values": {"count": 1}}
            summary["metrics"]["test_run_iterations_completed"] = {"values": {"count": 1}}
            args[4].write_text(json.dumps(summary), encoding="utf-8")
            return 0

        shutil.copyfile(FIXTURES / "profiles/staged-stress.yaml", self.scenario_path)
        self.k6.side_effect = run_staged_k6

        self.assertEqual(0, cli.execute(self.args))

        result = json.loads((self.result_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual({"concurrent_test_runs": 3, "test_case_count": 2}, result["tuple"])
        self.assertEqual(5, len(result["workload"]["stages"]))
        self.assertEqual({"id": "FIXTURE-STAGED-STRESS", "type": "STRESS"}, result["scenario"])
        report = (self.result_dir / "report.md").read_text(encoding="utf-8")
        self.assertIn("max configured VU target 3", report)
        self.assertIn("1s → 0 VUs", report)
        self.assertIn("'interrupted': 0", report)
        self.assertIn("**PASS**", report)

    def test_k6_threshold_failure_exits_1_and_writes_fail_result_without_failure_json(self):
        def run_threshold_fail(*args):
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return cli.K6_THRESHOLD_FAILURE_EXIT_CODE

        self.k6.side_effect = run_threshold_fail
        self.assertEqual(1, cli.execute(self.args))

        result = json.loads((self.result_dir / "result.json").read_text(encoding="utf-8"))
        self.assertEqual("FAIL", result["acceptance"]["status"])
        self.assertEqual("FAIL", result["k6"]["thresholds"])
        self.assertEqual(cli.K6_THRESHOLD_FAILURE_EXIT_CODE, result["k6"]["exit_code"])
        self.assertTrue((self.result_dir / "report.md").is_file())
        self.assertFalse((self.result_dir / FAILURE_FILENAME).exists())

    def test_active_run_blocks_seed_and_workload(self):
        self.api.list_runs.return_value = [{"id": 9}]
        with self.assertRaisesRegex(cli.RunnerError, "처리 중"):
            cli.execute(self.args)
        self.assert_seed_and_workload_blocked()

    def test_invalid_connection_inputs_block_api_seed_and_workload(self):
        bad_args = cli.parser().parse_args([
            "--api-url", "https://user:secret@api.example.test",
            "--target-url", "https://target.example.test/v1/chat/completions",
            "--target-model", "fixture-model",
            "--scenario", str(self.scenario_path),
            "--dataset", str(self.dataset_path),
            "--result-dir", str(self.result_dir),
        ])
        with self.assertRaises(ConfigurationError):
            cli.execute(bad_args)
        self.api.health_check.assert_not_called()
        self.assert_seed_and_workload_blocked()


if __name__ == "__main__":
    unittest.main()
