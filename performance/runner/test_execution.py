"""Manifest execution without DB lifecycle inputs, preserving preflight and capacity boundaries."""
import copy
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import yaml

from performance.runner import cli
from performance.runner.aws import InfrastructureCapacityCollector
from performance.runner.config import ConfigurationError
from performance.runner.plan import load_execution_plan
from performance.runner.test_capacity import MANIFEST, clients
from performance.runner.test_runner import passing_summary


class PerformanceExecutionTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.result_dir = directory / "result"
        manifest = yaml.safe_load(MANIFEST.read_text())
        self.profile_path = directory / "profile.yaml"
        self.dataset_path = directory / "dataset.yaml"
        shutil.copyfile(MANIFEST.parent / manifest["profile"], self.profile_path)
        shutil.copyfile(MANIFEST.parent / manifest["dataset"], self.dataset_path)
        manifest["profile"] = str(self.profile_path)
        manifest["dataset"] = str(self.dataset_path)
        ecs = manifest["infrastructure"]["expected"]["ecs"]
        ecs["api"] = {**ecs["worker"], "service_identifier": "fixture-api"}
        del ecs["api"]["work_items_concurrency"]
        self.manifest_path = directory / "experiment.yaml"
        self.manifest_path.write_text(yaml.safe_dump(manifest))
        self.args = cli.parser().parse_args(
            ["--experiment", str(self.manifest_path), "--result-dir", str(self.result_dir)])
        self.expected = load_execution_plan(self.manifest_path).to_dict()["infrastructure"]["expected"]

        self.stack.enter_context(patch.dict(os.environ, {
            "APP_REVISION": "app-sha", "INFRA_REVISION": "infra-sha",
            "PERF_BASE_URL": "https://api.example.test",
        }, clear=True))
        self.api = self.stack.enter_context(patch.object(cli, "ApiClient")).return_value
        self.api.list_runs.return_value = []
        self.api.create_suite.return_value = 7
        self.stack.enter_context(patch.object(cli, "queue_urls_from_environment", return_value=(["source"], ["dlq"])))
        self.inspector = self.stack.enter_context(patch.object(cli, "QueueInspector")).return_value
        self.inspector.snapshot.return_value = []
        self.inspector.is_empty.return_value = True
        self.metric_collector = self.stack.enter_context(patch.object(cli, "CloudWatchMetricCollector"))
        self.metrics = self.metric_collector.return_value
        self.metrics.collect.return_value = {"status": "COLLECTED", "metrics": [
            {"id": name, "values": [1]} for name in
            ("ecs_cpu", "sqs_visible", "rds_cpu", "sagemaker_invocations")
        ]}
        ecs_client, rds_client, sm_client, _, _ = clients(self.expected)
        observed = InfrastructureCapacityCollector(
            ecs_client=ecs_client, rds_client=rds_client, sagemaker_client=sm_client).collect(expected=self.expected)
        self.capacity = self.stack.enter_context(patch.object(cli, "InfrastructureCapacityCollector")).return_value
        self.capacity.collect.return_value = observed

        def run_k6(*args):
            args[4].write_text(json.dumps(passing_summary()))
            return 0
        self.k6 = self.stack.enter_context(patch.object(cli, "run_k6", side_effect=run_k6))
        self.subprocess = self.stack.enter_context(
            patch.object(cli.subprocess, "run", side_effect=AssertionError("unexpected process")))

    def assert_seed_and_workload_blocked(self):
        self.api.create_suite.assert_not_called()
        self.k6.assert_not_called()

    def test_execution_without_database_inputs_preserves_results_and_capacity(self):
        self.assertEqual(0, cli.execute(self.args))

        result = json.loads((self.result_dir / "result.json").read_text())
        plan = json.loads((self.result_dir / "execution-plan.json").read_text())
        self.assertEqual(2, result["schema_version"])
        self.assertEqual(plan, result["execution_plan"])
        self.assertEqual({"id": "FIXTURE-SMOKE", "type": "SMOKE"}, result["scenario"])
        self.assertEqual(7, result["dataset"]["suite_id"])
        self.assertEqual("fixture-db", result["infrastructure_capacity"]["rds"]["db_instance_identifier"])
        self.assertEqual(self.capacity.collect.return_value["ecs"], result["infrastructure_capacity"]["ecs"])
        self.assertEqual({"work_items_concurrency": 2, "concurrent_test_runs": 2, "ecs_task_count": 1},
                         result["experiment"])
        self.assertNotIn("completion.iterations", [check["name"] for check in result["acceptance"]["checks"]])
        self.capacity.collect.assert_called_once_with(expected=self.expected)
        self.assertEqual(self.expected, self.metric_collector.call_args.kwargs["resources"])
        self.metrics.validate_configuration.assert_called_once()
        self.api.create_suite.assert_called_once()
        self.k6.assert_called_once()
        self.subprocess.assert_not_called()
        for name in ("report.md", "execution-plan.json", "experiment.yaml", "profile.yaml", "dataset.yaml"):
            self.assertTrue((self.result_dir / name).is_file(), name)
        report = (self.result_dir / "report.md").read_text()
        self.assertNotIn("Iteration validity", report)
        for expected in (
            "fixture-smoke",
            "fixture-infra",
            "fixture-v1",
            "FIXTURE-SMOKE",
            "SMOKE",
            "fixture-two-cases-v1",
            "2 TestCases",
            "(2, 2, 1)",
            "최대 configured VU target; VU 하나가 TestRun 하나 완료를 기다림",
            "Selected Infrastructure Capacity (Manifest Expected)",
            "Observed Infrastructure Capacity Snapshot (Pre-Run)",
            "Input Sources",
            plan["sources"]["experiment"]["path"],
            plan["sources"]["experiment"]["sha256"],
            plan["sources"]["profile"]["sha256"],
            plan["sources"]["dataset"]["sha256"],
        ):
            self.assertIn(expected, report)

    def test_result_uses_resolved_snapshots_when_files_change_during_workload(self):
        expected_plan = load_execution_plan(self.manifest_path).to_dict()

        def mutate_sources(*args):
            for path in (self.manifest_path, self.profile_path, self.dataset_path):
                path.write_text("invalid after workload started")
            args[4].write_text(json.dumps(passing_summary()))
            return 0

        self.k6.side_effect = mutate_sources
        self.assertEqual(0, cli.execute(self.args))
        result = json.loads((self.result_dir / "result.json").read_text())
        self.assertEqual("fixture-two-cases-v1", result["dataset"]["id"])
        self.assertEqual(2, result["dataset"]["test_case_count"])
        self.assertEqual("FIXTURE-SMOKE", result["profile"]["test"]["id"])
        self.assertEqual({"id": "FIXTURE-SMOKE", "type": "SMOKE"}, result["scenario"])
        self.assertEqual(expected_plan["sources"], result["execution_plan"]["sources"])
        self.assertEqual(2, len(self.api.create_suite.call_args.args[0]["testCases"]))

    def test_staged_workload_preserves_curve_acceptance_and_iteration_validity_in_report(self):
        def run_staged_k6(*args):
            summary = passing_summary()
            summary["metrics"]["test_run_iterations_started"] = {"values": {"count": 1}}
            summary["metrics"]["test_run_iterations_completed"] = {"values": {"count": 1}}
            args[4].write_text(json.dumps(summary))
            return 0

        shutil.copyfile(MANIFEST.parent.parent / "profiles/staged-stress.yaml", self.profile_path)
        self.k6.side_effect = run_staged_k6

        self.assertEqual(0, cli.execute(self.args))

        result = json.loads((self.result_dir / "result.json").read_text())
        self.assertEqual(3, result["experiment"]["concurrent_test_runs"])
        self.assertEqual(5, len(result["profile"]["workload"]["stages"]))
        self.assertEqual(5, len(result["execution_plan"]["profile"]["workload"]["stages"]))
        self.assertEqual({"id": "FIXTURE-STAGED-STRESS", "type": "STRESS"}, result["scenario"])
        self.assertEqual(0, result["profile"]["acceptance"]["completion"]["failure_rate"])
        report = (self.result_dir / "report.md").read_text()
        self.assertIn("max configured VU target 3", report)
        self.assertIn("1s → 0 VUs", report)
        self.assertIn("'interrupted': 0", report)
        self.assertIn("**PASS**", report)

    def test_same_infrastructure_distinguishes_scenarios_and_definition_revisions(self):
        work_dir = self.manifest_path.parent
        stress_profile = work_dir / "staged-stress.yaml"
        shutil.copyfile(MANIFEST.parent.parent / "profiles/staged-stress.yaml", stress_profile)

        base_manifest = yaml.safe_load(self.manifest_path.read_text())
        stress_manifest = copy.deepcopy(base_manifest)
        stress_manifest["id"] = "fixture-stress"
        stress_manifest["profile"] = str(stress_profile)
        stress_manifest_path = work_dir / "stress-experiment.yaml"
        stress_manifest_path.write_text(yaml.safe_dump(stress_manifest))

        v2_manifest = copy.deepcopy(base_manifest)
        v2_manifest["id"] = "fixture-smoke-v2"
        v2_manifest["infrastructure"]["revision"] = "fixture-v2"
        v2_manifest_path = work_dir / "v2-experiment.yaml"
        v2_manifest_path.write_text(yaml.safe_dump(v2_manifest))

        def run_for(manifest_path: Path, suffix: str, staged: bool = False):
            if staged:
                def run_staged(*args):
                    summary = passing_summary()
                    summary["metrics"]["test_run_iterations_started"] = {"values": {"count": 1}}
                    summary["metrics"]["test_run_iterations_completed"] = {"values": {"count": 1}}
                    args[4].write_text(json.dumps(summary))
                    return 0
                self.k6.side_effect = run_staged
            else:
                def run_normal(*args):
                    args[4].write_text(json.dumps(passing_summary()))
                    return 0
                self.k6.side_effect = run_normal

            result_dir = self.result_dir.parent / suffix
            args = cli.parser().parse_args(["--experiment", str(manifest_path), "--result-dir", str(result_dir)])
            self.assertEqual(0, cli.execute(args))
            return json.loads((result_dir / "result.json").read_text()), (result_dir / "report.md").read_text()

        smoke_res, smoke_rep = run_for(self.manifest_path, "run-smoke-v1")
        stress_res, stress_rep = run_for(stress_manifest_path, "run-stress-v1", staged=True)
        v2_res, v2_rep = run_for(v2_manifest_path, "run-smoke-v2")

        self.assertEqual("fixture-infra", smoke_res["execution_plan"]["infrastructure"]["id"])
        self.assertEqual("fixture-infra", stress_res["execution_plan"]["infrastructure"]["id"])
        self.assertEqual("fixture-infra", v2_res["execution_plan"]["infrastructure"]["id"])

        self.assertEqual("fixture-v1", smoke_res["execution_plan"]["infrastructure"]["revision"])
        self.assertEqual("fixture-v1", stress_res["execution_plan"]["infrastructure"]["revision"])
        self.assertNotEqual(smoke_res["scenario"], stress_res["scenario"])
        self.assertEqual({"id": "FIXTURE-STAGED-STRESS", "type": "STRESS"}, stress_res["scenario"])
        self.assertNotEqual(
            smoke_res["execution_plan"]["sources"]["profile"]["sha256"],
            stress_res["execution_plan"]["sources"]["profile"]["sha256"],
        )
        self.assertEqual(2, smoke_res["experiment"]["concurrent_test_runs"])
        self.assertEqual(3, stress_res["experiment"]["concurrent_test_runs"])
        self.assertIn("FIXTURE-STAGED-STRESS", stress_rep)
        self.assertIn("max configured VU target 3", stress_rep)

        self.assertEqual(smoke_res["scenario"], v2_res["scenario"])
        self.assertEqual("fixture-v2", v2_res["execution_plan"]["infrastructure"]["revision"])
        self.assertNotEqual(
            smoke_res["execution_plan"]["sources"]["experiment"]["sha256"],
            v2_res["execution_plan"]["sources"]["experiment"]["sha256"],
        )
        self.assertIn("fixture-v2", v2_rep)
        self.assertNotIn("fixture-v2", smoke_rep)

    def test_active_run_blocks_seed_and_workload(self):
        self.api.list_runs.return_value = [{"id": 9}]
        with self.assertRaisesRegex(cli.RunnerError, "처리 중"):
            cli.execute(self.args)
        self.assert_seed_and_workload_blocked()

    def test_source_or_dlq_backlog_blocks_seed_and_workload(self):
        for states in ([False], [True, False]):
            with self.subTest(states=states):
                self.args.result_dir = str(self.result_dir) + str(len(states))
                self.inspector.is_empty.side_effect = states
                with self.assertRaisesRegex(cli.RunnerError, "비어 있지"):
                    cli.execute(self.args)
                self.assert_seed_and_workload_blocked()

    def test_missing_execution_identifiers_block_workload(self):
        for key in ("APP_REVISION", "INFRA_REVISION", "PERF_BASE_URL"):
            with self.subTest(key=key), patch.dict(os.environ, {key: ""}):
                self.args.result_dir = str(self.result_dir) + key
                with self.assertRaises(ConfigurationError):
                    cli.execute(self.args)
                self.k6.assert_not_called()

    def test_metric_configuration_failure_blocks_seed(self):
        self.metrics.validate_configuration.side_effect = ConfigurationError("QueueName")
        with self.assertRaises(ConfigurationError):
            cli.execute(self.args)
        self.assert_seed_and_workload_blocked()

    def test_capacity_lookup_failure_blocks_seed_and_workload(self):
        self.capacity.collect.side_effect = ConfigurationError("expected observable / actual lookup failed")
        with self.assertRaises(ConfigurationError):
            cli.execute(self.args)
        self.assert_seed_and_workload_blocked()

    def test_every_capacity_mismatch_and_missing_field_blocks_seed_and_workload(self):
        original = copy.deepcopy(self.capacity.collect.return_value)
        paths = []
        for role, config in self.expected["ecs"].items():
            paths += [("ecs", role, key) for key in config]
            paths += [("ecs", role, key) for key in
                      ("running_task_count", "pending_task_count", "status", "task_definition_arn", "deployments")]
            paths += [("ecs", role, "deployments", 0, key) for key in original["ecs"][role]["deployments"][0]]
        for resource in ("rds", "sagemaker"):
            paths += [(resource, key) for key in original[resource]]
        for index, path in enumerate(paths):
            for missing in (False, True):
                with self.subTest(path=path, missing=missing):
                    actual = copy.deepcopy(original)
                    parent = actual
                    for key in path[:-1]:
                        parent = parent[key]
                    if missing:
                        del parent[path[-1]]
                    else:
                        parent[path[-1]] = "WRONG"
                    self.capacity.collect.return_value = actual
                    self.args.result_dir = str(self.result_dir) + f"-{index}-{missing}"
                    with self.assertRaisesRegex(ConfigurationError, "expected .* / actual"):
                        cli.execute(self.args)
                    self.assert_seed_and_workload_blocked()


if __name__ == "__main__":
    unittest.main()
