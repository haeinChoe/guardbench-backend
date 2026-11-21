"""Experiment input resolution, snapshot provenance and no-call CLI boundaries."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import yaml

from performance.runner import cli
from performance.runner.config import ConfigurationError
from performance.runner.plan import load_execution_plan


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class ExecutionPlanTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        shutil.copytree(FIXTURES, self.directory / "fixtures")
        self.manifest_path = self.directory / "fixtures/experiments/smoke.yaml"
        self.profile_path = self.directory / "fixtures/profiles/smoke.yaml"
        self.dataset_path = self.directory / "fixtures/datasets/two-cases.yaml"
        self.manifest = yaml.safe_load(self.manifest_path.read_text())
        self.profile = yaml.safe_load(self.profile_path.read_text())

    def write_manifest(self):
        self.manifest_path.write_text(yaml.safe_dump(self.manifest))

    def test_source_relative_resolution_original_hashes_and_full_plan(self):
        with patch.dict(os.environ, {}, clear=True):
            resolved = load_execution_plan(self.manifest_path)
        plan = resolved.to_dict()
        self.assertEqual({"schema_version", "experiment", "infrastructure", "profile", "dataset", "sources"}, set(plan))
        self.assertEqual({"id": "fixture-smoke"}, plan["experiment"])
        self.assertEqual("fixture-v1", plan["infrastructure"]["revision"])
        self.assertEqual(self.profile, plan["profile"])
        self.assertEqual({"id": "fixture-two-cases-v1", "test_case_count": 2}, plan["dataset"])
        self.assertEqual(2, len(resolved.seed_payload["testCases"]))
        for name, path in (("experiment", self.manifest_path), ("profile", self.profile_path), ("dataset", self.dataset_path)):
            self.assertEqual(str(path.resolve()), plan["sources"][name]["path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), plan["sources"][name]["sha256"])
        self.assertNotIn("infrastructure", plan["profile"])

    def test_snapshots_survive_file_environment_and_returned_object_mutation(self):
        self.profile["target"]["model"] = "${FIXTURE_MODEL}"
        self.profile_path.write_text(yaml.safe_dump(self.profile))
        with patch.dict(os.environ, {"FIXTURE_MODEL": "resolved-model"}):
            resolved = load_execution_plan(self.manifest_path)
        original = resolved.to_dict()
        resolved.profile["target"]["model"] = "mutation"
        resolved.dataset["id"] = "mutation"
        resolved.seed_payload["testCases"].clear()
        resolved.to_dict()["infrastructure"].clear()
        resolved.source_bytes.clear()
        for path in (self.manifest_path, self.profile_path, self.dataset_path):
            path.unlink()
        self.assertEqual(original, resolved.to_dict())
        self.assertEqual("resolved-model", resolved.profile["target"]["model"])
        self.assertEqual(2, len(resolved.seed_payload["testCases"]))

    def test_cpu_memory_normalization_optional_api_and_aws_mode(self):
        expected = self.manifest["infrastructure"]["expected"]
        worker = expected["ecs"]["worker"]
        worker.update(task_cpu=256, task_memory="00512")
        expected["ecs"]["api"] = {key: value for key, value in worker.items() if key != "work_items_concurrency"}
        self.write_manifest()
        plan = load_execution_plan(self.manifest_path).to_dict()
        for role in ("api", "worker"):
            self.assertEqual("256", plan["infrastructure"]["expected"]["ecs"][role]["task_cpu"])
            self.assertEqual("512", plan["infrastructure"]["expected"]["ecs"][role]["task_memory"])

    def test_missing_unknown_and_wrong_nested_manifest_values_are_rejected(self):
        mutations = [
            (("schema_version",), None), (("schema_version",), True), (("schema_version",), 2),
            (("id",), ""), (("id",), []), (("mode",), "synthetic"), (("mode",), "aws"),
            (("profile",), ""), (("infrastructure",), []),
            (("infrastructure", "revision"), ""),
            (("infrastructure", "expected", "ecs"), []),
            (("infrastructure", "expected", "ecs", "worker"), []),
            (("infrastructure", "expected", "ecs", "api"), {}),
            (("infrastructure", "expected", "rds"), None),
            (("infrastructure", "expected", "sagemaker"), None),
        ]
        for keys, value in mutations:
            with self.subTest(keys=keys, value=value):
                self.manifest = yaml.safe_load((FIXTURES / "experiments/smoke.yaml").read_text())
                target = self.manifest
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.write_manifest()
                with self.assertRaises(ConfigurationError):
                    load_execution_plan(self.manifest_path)
        for keys in ((), ("infrastructure",), ("infrastructure", "expected"),
                     ("infrastructure", "expected", "ecs"), ("infrastructure", "expected", "ecs", "worker"),
                     ("infrastructure", "expected", "rds"), ("infrastructure", "expected", "sagemaker")):
            with self.subTest(unknown_at=keys):
                self.manifest = yaml.safe_load((FIXTURES / "experiments/smoke.yaml").read_text())
                target = self.manifest
                for key in keys:
                    target = target[key]
                target["unknown"] = "unsupported"
                self.write_manifest()
                with self.assertRaisesRegex(ConfigurationError, "unknown"):
                    load_execution_plan(self.manifest_path)

    def test_every_required_manifest_field_is_required(self):
        original = yaml.safe_load(self.manifest_path.read_text())
        required_paths = []

        def collect(value, keys=()):
            for key, child in value.items():
                required_paths.append(keys + (key,))
                if isinstance(child, dict):
                    collect(child, keys + (key,))

        collect(original)
        for keys in required_paths:
            with self.subTest(missing=keys):
                self.manifest = json.loads(json.dumps(original))
                target = self.manifest
                for key in keys[:-1]:
                    target = target[key]
                del target[keys[-1]]
                self.write_manifest()
                with self.assertRaises(ConfigurationError):
                    load_execution_plan(self.manifest_path)

    def test_each_yaml_source_is_read_once(self):
        read_bytes = Path.read_bytes
        reads = []

        def capture(path):
            reads.append(path)
            return read_bytes(path)

        with patch.object(Path, "read_bytes", capture):
            load_execution_plan(self.manifest_path)
        self.assertCountEqual([self.manifest_path, self.profile_path, self.dataset_path], reads)

    def test_invalid_yaml_encoding_and_top_level_type_are_configuration_errors(self):
        for source in (b"\xff", b"[invalid", b"[one, two]", b"null"):
            with self.subTest(source=source):
                self.manifest_path.write_bytes(source)
                with self.assertRaises(ConfigurationError):
                    load_execution_plan(self.manifest_path)

    def test_capacity_numbers_are_strict_positive_and_finite(self):
        worker = self.manifest["infrastructure"]["expected"]["ecs"]["worker"]
        for field in ("desired_count", "work_items_concurrency", "task_cpu", "task_memory"):
            original = worker[field]
            for value in (True, 0, -1, 1.5, float("nan"), float("inf"), float("-inf"), [], {}):
                with self.subTest(field=field, value=value):
                    worker[field] = value
                    self.write_manifest()
                    with self.assertRaises(ConfigurationError):
                        load_execution_plan(self.manifest_path)
            worker[field] = original

    def test_missing_files_and_unresolved_references_are_configuration_errors(self):
        for field in ("profile", "dataset"):
            original = self.manifest[field]
            for value in ("missing.yaml", "${MISSING_FIXTURE_PATH}"):
                with self.subTest(field=field, value=value), patch.dict(os.environ, {}, clear=True):
                    self.manifest[field] = value
                    self.write_manifest()
                    with self.assertRaises(ConfigurationError):
                        load_execution_plan(self.manifest_path)
            self.manifest[field] = original
        with self.assertRaises(ConfigurationError):
            load_execution_plan(self.directory / "missing.yaml")

    def test_profile_accepts_only_adr_0016_test_types(self):
        for test_type in ("SMOKE", "STRESS", "SOAK"):
            with self.subTest(test_type=test_type), patch.dict(os.environ, {}, clear=True):
                profile = json.loads(json.dumps(self.profile))
                profile["test"]["type"] = test_type
                self.profile_path.write_text(yaml.safe_dump(profile))
                self.assertEqual(test_type, load_execution_plan(self.manifest_path).profile["test"]["type"])
        for test_type in ("LOAD", "PEAK", "smoke"):
            with self.subTest(test_type=test_type), patch.dict(os.environ, {}, clear=True):
                profile = json.loads(json.dumps(self.profile))
                profile["test"]["type"] = test_type
                self.profile_path.write_text(yaml.safe_dump(profile))
                with self.assertRaises(ConfigurationError):
                    load_execution_plan(self.manifest_path)

    def test_profile_rejects_nonfinite_wrong_nested_types_unknown_and_unresolved_values(self):
        mutations = [
            (("test", "id"), []), (("test", "type"), []),
            (("target", "identifier"), []), (("target", "model"), "${MISSING_FIXTURE_MODEL}"),
            (("target", "identifier"), "https://user:secret@example.test/path"),
            (("target", "identifier"), "http://[invalid"),
            (("workload", "duration_seconds"), float("nan")),
            (("workload", "polling_interval_seconds"), float("inf")),
            (("workload", "concurrent_test_runs"), True),
            (("acceptance", "api"), []), (("acceptance", "api", "create_latency_ms"), []),
            (("acceptance", "completion"), []),
            (("acceptance", "completion", "max_seconds"), float("inf")),
            (("acceptance", "completion", "failure_rate"), float("nan")),
            (("acceptance", "api", "error_rate"), float("nan")),
            (("acceptance", "api", "create_latency_ms", "p95"), float("inf")),
            (("infrastructure",), {}),
        ]
        for keys, value in mutations:
            with self.subTest(keys=keys), patch.dict(os.environ, {}, clear=True):
                profile = json.loads(json.dumps(self.profile))
                target = profile
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.profile_path.write_text(yaml.safe_dump(profile))
                with self.assertRaises(ConfigurationError):
                    load_execution_plan(self.manifest_path)

    def test_http_dataset_source_is_verified_and_snapshot_is_retained(self):
        source = self.directory / "fixtures/datasets/source.http"
        payload = {"name": "fixture", "testCases": [{"name": "one"}]}
        source.write_text("POST /seed\n\n" + json.dumps(payload))
        dataset = {"id": "fixture-http", "immutable": True, "source": {
            "type": "http-testset", "path": "source.http", "expected_test_case_count": 1,
            "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}}
        self.dataset_path.write_text(yaml.safe_dump(dataset))
        resolved = load_execution_plan(self.manifest_path)
        source.write_text("changed")
        self.assertEqual(payload, resolved.seed_payload)
        with self.assertRaisesRegex(ConfigurationError, "checksum"):
            load_execution_plan(self.manifest_path)

    def test_manifest_dry_run_creates_plan_and_never_calls_external_interfaces(self):
        args = cli.parser().parse_args(["--experiment", str(self.manifest_path), "--dry-run",
                                       "--result-dir", str(self.directory / "result")])
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            for name in ("ApiClient", "QueueInspector", "CloudWatchMetricCollector", "InfrastructureCapacityCollector",
                         "run_k6", "queue_urls_from_environment", "upload_result_directory"):
                stack.enter_context(patch.object(cli, name, side_effect=AssertionError(f"unexpected {name}")))
            stack.enter_context(patch.object(cli.subprocess, "run", side_effect=AssertionError("unexpected process")))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(0, cli.execute(args))
        result = self.directory / "result"
        self.assertEqual(json.loads(output.getvalue()), json.loads((result / "execution-plan.json").read_text()))
        self.assertFalse((result / "result.json").exists())
        for name, source in (("experiment", self.manifest_path), ("profile", self.profile_path), ("dataset", self.dataset_path)):
            self.assertEqual(source.read_bytes(), (result / f"{name}.yaml").read_bytes())

    def test_explicit_legacy_inputs_cannot_be_combined_with_experiment(self):
        for option, path in (("--profile", self.profile_path), ("--dataset", self.dataset_path)):
            args = cli.parser().parse_args(["--experiment", str(self.manifest_path), option, str(path), "--dry-run"])
            with self.assertRaisesRegex(ConfigurationError, "함께"):
                cli.execute(args)

    def test_experiment_actual_run_is_staged_until_capacity_preflight_integration(self):
        args = cli.parser().parse_args(["--experiment", str(self.manifest_path)])
        with patch.object(cli, "ApiClient", side_effect=AssertionError("unexpected API")):
            with self.assertRaisesRegex(ConfigurationError, "capacity preflight"):
                cli.execute(args)

    def test_legacy_explicit_and_default_dry_run_consumers_remain_supported(self):
        for explicit in (False, True):
            result = self.directory / f"legacy-{explicit}"
            options = ["--dry-run", "--result-dir", str(result)]
            if explicit:
                options += ["--profile", str(self.profile_path), "--dataset", str(self.dataset_path)]
            with patch.dict(os.environ, {"PERF_TARGET_URL": "https://example.test", "PERF_TARGET_MODEL": "fixture"}, clear=True), redirect_stdout(io.StringIO()):
                self.assertEqual(0, cli.execute(cli.parser().parse_args(options)))
            plan = json.loads((result / "dry-run.json").read_text())
            self.assertEqual(2 if explicit else 78, plan["test_case_count"])


if __name__ == "__main__":
    unittest.main()
