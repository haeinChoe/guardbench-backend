"""Input resolution, URL/scenario validation, provenance, and dry-run CLI tests."""

import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
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
        self.scenario_path = self.directory / "fixtures/profiles/smoke.yaml"
        self.dataset_path = self.directory / "fixtures/datasets/two-cases.yaml"
        self.scenario = yaml.safe_load(self.scenario_path.read_text(encoding="utf-8"))
        self.base_connection = {
            "api_url": "https://api.example.test",
            "target_url": "https://target.example.test/v1/chat/completions",
            "target_model": "fixture-model",
            "target_revision": "fixture-v1",
        }

    def _load(self, **overrides):
        params = {
            **self.base_connection,
            "scenario_path": self.scenario_path,
            "dataset_path": self.dataset_path,
        }
        params.update(overrides)
        return load_execution_plan(**params)

    def test_resolution_raw_hashes_and_full_plan_shape(self):
        with patch.dict(os.environ, {}, clear=True):
            resolved = self._load(
                api_url="https://api.example.test/",
                target_revision="",
            )
        plan = resolved.to_dict()
        self.assertEqual({"schema_version", "connection", "scenario", "dataset", "sources"}, set(plan))
        self.assertEqual(1, plan["schema_version"])
        self.assertEqual(
            {
                "api_url": "https://api.example.test",
                "target": {
                    "type": "HTTP_ENDPOINT",
                    "url": "https://target.example.test/v1/chat/completions",
                    "model": "fixture-model",
                    "revision": None,
                },
            },
            plan["connection"],
        )
        self.assertEqual(self.scenario, plan["scenario"])
        self.assertEqual({"id": "fixture-two-cases-v1", "test_case_count": 2}, plan["dataset"])
        self.assertEqual(2, len(resolved.seed_payload["testCases"]))
        for name, path in (("scenario", self.scenario_path), ("dataset", self.dataset_path)):
            self.assertEqual(str(path.resolve()), plan["sources"][name]["path"])
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), plan["sources"][name]["sha256"])

    def test_snapshots_survive_file_environment_and_returned_object_mutation(self):
        self.scenario["test"]["name"] = "${FIXTURE_TEST_NAME}"
        self.scenario_path.write_text(yaml.safe_dump(self.scenario), encoding="utf-8")
        with patch.dict(os.environ, {"FIXTURE_TEST_NAME": "resolved-name"}, clear=True):
            resolved = self._load()
        original = resolved.to_dict()
        resolved.scenario["test"]["name"] = "mutation"
        resolved.connection["target"]["model"] = "mutation"
        resolved.dataset["id"] = "mutation"
        resolved.seed_payload["testCases"].clear()
        resolved.source_bytes.clear()
        for path in (self.scenario_path, self.dataset_path):
            path.unlink()
        self.assertEqual(original, resolved.to_dict())
        self.assertEqual("resolved-name", resolved.scenario["test"]["name"])
        self.assertEqual("fixture-model", resolved.connection["target"]["model"])
        self.assertEqual(2, len(resolved.seed_payload["testCases"]))

    def test_each_yaml_source_is_read_once(self):
        read_bytes = Path.read_bytes
        reads = []

        def capture(path):
            reads.append(path)
            return read_bytes(path)

        with patch.object(Path, "read_bytes", capture):
            self._load()
        self.assertCountEqual([self.scenario_path, self.dataset_path], reads)

    def test_invalid_yaml_encoding_and_top_level_type_are_configuration_errors(self):
        for path in (self.scenario_path, self.dataset_path):
            for source in (b"\xff", b"[invalid", b"[one, two]", b"null"):
                with self.subTest(path=path.name, source=source):
                    original = path.read_bytes()
                    try:
                        path.write_bytes(source)
                        with self.assertRaises(ConfigurationError):
                            self._load()
                    finally:
                        path.write_bytes(original)

    def test_connection_inputs_are_strictly_validated(self):
        invalid_overrides = [
            {"api_url": ""},
            {"api_url": "ftp://api.example.test"},
            {"api_url": "https://user:secret@api.example.test"},
            {"api_url": "https://api.example.test#frag"},
            {"api_url": "https://api.example.test?query=1"},
            {"api_url": "http://[invalid"},
            {"api_url": "https://api.example.test:badport"},
            {"target_url": ""},
            {"target_url": "ftp://target.example.test/v1"},
            {"target_url": "https://alice:pw@target.example.test/v1"},
            {"target_url": "https://target.example.test/v1#frag"},
            {"target_url": "http://[invalid"},
            {"target_model": ""},
            {"target_model": "${UNRESOLVED_MODEL}"},
            {"target_revision": "   "},
            {"target_revision": "${UNRESOLVED_REV}"},
        ]
        for override in invalid_overrides:
            with self.subTest(override=override), self.assertRaises(ConfigurationError):
                self._load(**override)

        resolved = self._load(target_url="https://target.example.test/v1/chat/completions?version=1")
        self.assertEqual(
            "https://target.example.test/v1/chat/completions?version=1",
            resolved.connection["target"]["url"],
        )

    def test_scenario_accepts_only_smoke_stress_soak_test_types(self):
        for test_type in ("SMOKE", "STRESS", "SOAK"):
            with self.subTest(test_type=test_type), patch.dict(os.environ, {}, clear=True):
                scenario = json.loads(json.dumps(self.scenario))
                scenario["test"]["type"] = test_type
                self.scenario_path.write_text(yaml.safe_dump(scenario), encoding="utf-8")
                self.assertEqual(test_type, self._load().scenario["test"]["type"])
        for test_type in ("SPIKE", "PEAK", "smoke"):
            with self.subTest(test_type=test_type), patch.dict(os.environ, {}, clear=True):
                scenario = json.loads(json.dumps(self.scenario))
                scenario["test"]["type"] = test_type
                self.scenario_path.write_text(yaml.safe_dump(scenario), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    self._load()

    def test_scenario_rejects_env_target_fields_infra_acceptance_and_bad_types(self):
        mutations = [
            (("test", "id"), []),
            (("test", "type"), []),
            (("target", "type"), "GRPC"),
            (("target", "identifier"), "https://target.example.test"),
            (("target", "url"), "https://target.example.test"),
            (("target", "model"), "fixture-model"),
            (("target", "revision"), "fixture-v1"),
            (("acceptance", "completion", "queue_drain_seconds"), 10),
            (("acceptance", "completion", "dlq_messages"), 0),
            (("workload", "duration_seconds"), float("nan")),
            (("workload", "polling_interval_seconds"), float("inf")),
            (("workload", "concurrent_test_runs"), True),
            (("acceptance", "api"), []),
            (("acceptance", "api", "create_latency_ms"), []),
            (("acceptance", "completion"), []),
            (("acceptance", "completion", "max_seconds"), float("inf")),
            (("acceptance", "completion", "failure_rate"), float("nan")),
            (("acceptance", "api", "error_rate"), float("nan")),
            (("acceptance", "api", "create_latency_ms", "p95"), float("inf")),
            (("infrastructure",), {}),
        ]
        for keys, value in mutations:
            with self.subTest(keys=keys), patch.dict(os.environ, {}, clear=True):
                scenario = json.loads(json.dumps(self.scenario))
                target = scenario
                for key in keys[:-1]:
                    target = target[key]
                target[keys[-1]] = value
                self.scenario_path.write_text(yaml.safe_dump(scenario), encoding="utf-8")
                with self.assertRaises(ConfigurationError):
                    self._load()

    def test_http_dataset_source_is_verified_and_snapshot_is_retained(self):
        source = self.directory / "fixtures/datasets/source.http"
        payload = {"name": "fixture", "testCases": [{"name": "one"}]}
        source.write_text("POST /seed\n\n" + json.dumps(payload), encoding="utf-8")
        dataset = {
            "id": "fixture-http",
            "immutable": True,
            "source": {
                "type": "http-testset",
                "path": "source.http",
                "expected_test_case_count": 1,
                "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            },
        }
        self.dataset_path.write_text(yaml.safe_dump(dataset), encoding="utf-8")
        resolved = self._load()
        source.write_text("changed", encoding="utf-8")
        self.assertEqual(payload, resolved.seed_payload)
        with self.assertRaisesRegex(ConfigurationError, "checksum"):
            self._load()

    def test_dry_run_creates_plan_and_never_calls_external_interfaces(self):
        result = self.directory / "result"
        args = cli.parser().parse_args([
            "--api-url", "https://api.example.test",
            "--target-url", "https://target.example.test/v1/chat/completions",
            "--target-model", "fixture-model",
            "--scenario", str(self.scenario_path),
            "--dataset", str(self.dataset_path),
            "--dry-run",
            "--result-dir", str(result),
        ])
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=True))
            for name in ("ApiClient", "run_k6"):
                stack.enter_context(patch.object(cli, name, side_effect=AssertionError(f"unexpected {name}")))
            stack.enter_context(patch.object(cli.subprocess, "run", side_effect=AssertionError("unexpected process")))
            output = stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(0, cli.execute(args))
        self.assertEqual(json.loads(output.getvalue()), json.loads((result / "execution-plan.json").read_text()))
        self.assertEqual(
            {"scenario.yaml", "dataset.yaml", "execution-plan.json"},
            {path.name for path in result.iterdir()},
        )
        for name, source in (("scenario", self.scenario_path), ("dataset", self.dataset_path)):
            self.assertEqual(source.read_bytes(), (result / f"{name}.yaml").read_bytes())

    def test_required_cli_options_are_enforced(self):
        full = [
            "--api-url", "https://api.example.test",
            "--target-url", "https://target.example.test/v1",
            "--target-model", "m",
            "--scenario", str(self.scenario_path),
            "--dataset", str(self.dataset_path),
        ]
        for omit in ("--api-url", "--target-url", "--target-model", "--scenario", "--dataset"):
            idx = full.index(omit)
            partial = full[:idx] + full[idx + 2:]
            with self.subTest(missing=omit), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(partial)


if __name__ == "__main__":
    unittest.main()
