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
        self.assertEqual(7, result["dataset"]["suite_id"])
        self.assertEqual("fixture-db", result["infrastructure_capacity"]["rds"]["db_instance_identifier"])
        self.assertEqual(self.capacity.collect.return_value["ecs"], result["infrastructure_capacity"]["ecs"])
        self.assertEqual({"work_items_concurrency": 2, "concurrent_test_runs": 2, "ecs_task_count": 1},
                         result["experiment"])
        self.capacity.collect.assert_called_once_with(expected=self.expected)
        self.assertEqual(self.expected, self.metric_collector.call_args.kwargs["resources"])
        self.metrics.validate_configuration.assert_called_once()
        self.api.create_suite.assert_called_once()
        self.k6.assert_called_once()
        self.subprocess.assert_not_called()
        for name in ("report.md", "execution-plan.json", "experiment.yaml", "profile.yaml", "dataset.yaml"):
            self.assertTrue((self.result_dir / name).is_file(), name)

    def test_result_uses_resolved_snapshots_when_files_change_during_workload(self):
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
        self.assertEqual(2, len(self.api.create_suite.call_args.args[0]["testCases"]))

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
