"""Regression tests for Issue #25 failure diagnostics, partial artifacts, and completion markers."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml
from botocore.exceptions import ClientError

from performance.runner import cli
from performance.runner.aws import InfrastructureCapacityCollector
from performance.runner.config import ConfigurationError
from performance.runner.failure import RunContext, preserve_failure, sanitize_message
from performance.runner.plan import load_execution_plan
from performance.runner.storage import (
    FAILURE_FILENAME,
    RESULT_FILENAMES,
    UPLOAD_COMPLETE_FILENAME,
    ResultUploadError,
    upload_failure_directory,
    upload_result_directory,
)
from performance.runner.test_capacity import MANIFEST, clients
from performance.runner.test_runner import passing_summary


REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeS3Client:
    """In-memory S3 mock exposing only PutObject-equivalent upload_file."""

    def __init__(self, fail_on_filename: str | None = None, fail_all: bool = False) -> None:
        self.fail_on_filename = fail_on_filename
        self.fail_all = fail_all
        self.calls: list[tuple[str, str, str]] = []
        self.objects: dict[str, bytes] = {}

    def upload_file(self, filename: str, bucket: str, key: str) -> None:
        name = Path(filename).name
        if self.fail_all or (self.fail_on_filename is not None and name == self.fail_on_filename):
            raise ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "mocked s3 failure"}},
                "PutObject",
            )
        self.calls.append((name, bucket, key))
        self.objects[key] = Path(filename).read_bytes()


class FailureArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp_dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.result_dir = self.temp_dir / "run-result"
        self.base_env = {
            "APP_REVISION": "app-sha",
            "INFRA_REVISION": "infra-sha",
            "PERF_BASE_URL": "https://api.example.test",
            "PERFORMANCE_RESULTS_BUCKET": "perf-results-bucket",
        }
        manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
        self.profile_path = self.temp_dir / "profile.yaml"
        self.dataset_path = self.temp_dir / "dataset.yaml"
        shutil.copyfile(MANIFEST.parent / manifest["profile"], self.profile_path)
        shutil.copyfile(MANIFEST.parent / manifest["dataset"], self.dataset_path)
        manifest["profile"] = str(self.profile_path)
        manifest["dataset"] = str(self.dataset_path)
        self.manifest_path = self.temp_dir / "experiment.yaml"
        self.manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
        expected = load_execution_plan(self.manifest_path).to_dict()["infrastructure"]["expected"]
        ecs_client, rds_client, sm_client, _, _ = clients(expected)
        self.observed_capacity = InfrastructureCapacityCollector(
            ecs_client=ecs_client, rds_client=rds_client, sagemaker_client=sm_client).collect(expected=expected)

    def _argv(self, result_dir: Path, *extra: str) -> list[str]:
        return ["--experiment", str(self.manifest_path), "--result-dir", str(result_dir), *extra]

    def _args(self, result_dir: Path, *extra: str):
        return cli.parser().parse_args(self._argv(result_dir, *extra))

    def _observed_capacity(self) -> dict:
        return copy.deepcopy(self.observed_capacity)

    def _enter_runner_mocks(self, *, s3_client: FakeS3Client | None = None, env_overrides: dict[str, str] | None = None):
        env = dict(self.base_env)
        if env_overrides:
            for key, value in env_overrides.items():
                if value == "":
                    env.pop(key, None)
                else:
                    env[key] = value
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))

        fake_s3 = s3_client or FakeS3Client()
        self.stack.enter_context(patch("performance.runner.storage.boto3.client", return_value=fake_s3))

        api = self.stack.enter_context(patch.object(cli, "ApiClient")).return_value
        api.list_runs.return_value = []
        api.create_suite.return_value = 7
        api.get_run.return_value = {"id": 1, "status": "FINISHED", "executionOutcome": "COMPLETED"}

        self.stack.enter_context(patch.object(cli, "queue_urls_from_environment", return_value=(["source"], ["dlq"])))
        inspector = self.stack.enter_context(patch.object(cli, "QueueInspector")).return_value
        inspector.snapshot.return_value = [
            {"queueUrl": "dlq", "visible": 0, "notVisible": 0, "delayed": 0, "oldestAgeSeconds": None}
        ]
        inspector.is_empty.return_value = True

        metrics = self.stack.enter_context(patch.object(cli, "CloudWatchMetricCollector")).return_value
        metrics.collect.return_value = {
            "status": "COLLECTED",
            "metrics": [
                {"id": name, "values": [1]}
                for name in ("ecs_cpu", "sqs_visible", "rds_cpu", "sagemaker_invocations")
            ],
        }

        capacity = self.stack.enter_context(patch.object(cli, "InfrastructureCapacityCollector")).return_value
        capacity.collect.return_value = self._observed_capacity()

        def default_k6(*args):
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return 0

        k6 = self.stack.enter_context(patch.object(cli, "run_k6", side_effect=default_k6))
        self.stack.enter_context(patch.object(cli.subprocess, "run", side_effect=AssertionError("unexpected process")))
        return fake_s3, api, inspector, metrics, capacity, k6

    def _read_failure(self, result_dir: Path | None = None) -> dict:
        target = result_dir or self.result_dir
        return json.loads((target / FAILURE_FILENAME).read_text(encoding="utf-8"))

    def test_early_missing_experiment_writes_failure_json_and_uploads_to_failure_prefix(self):
        fake_s3 = FakeS3Client()
        self.stack.enter_context(patch("performance.runner.storage.boto3.client", return_value=fake_s3))
        self.stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "APP_REVISION": "app-sha",
                    "INFRA_REVISION": "unknown",
                    "PERFORMANCE_RESULTS_BUCKET": "perf-results-bucket",
                },
                clear=True,
            )
        )
        missing_manifest = self.temp_dir / "does-not-exist.yaml"
        args = cli.parser().parse_args(["--experiment", str(missing_manifest), "--result-dir", str(self.result_dir)])

        with self.assertRaises(ConfigurationError):
            cli.execute(args)

        failure = self._read_failure()
        self.assertEqual(1, failure["schema_version"])
        self.assertEqual("ERROR", failure["status"])
        self.assertEqual("preflight", failure["phase"])
        self.assertTrue(failure["run_id"].startswith("preflight-error-"))
        self.assertEqual("ConfigurationError", failure["error"]["type"])
        self.assertEqual({"application": "app-sha"}, failure["revisions"])
        self.assertEqual(str(missing_manifest.resolve()), failure["inputs"]["experiment_path"])
        self.assertEqual(["failure.json"], failure["available_artifacts"])
        self.assertEqual([], failure["preservation_errors"])
        for missing_name in RESULT_FILENAMES:
            self.assertFalse((self.result_dir / missing_name).exists())
        self.assertEqual({f"performance/failures/{failure['run_id']}/failure.json"}, set(fake_s3.objects))

    def test_early_invalid_plan_inputs_preserve_experiment_and_failure_json(self):
        manifest = yaml.safe_load(self.manifest_path.read_text(encoding="utf-8"))
        malformed_manifest = self.temp_dir / "malformed.yaml"
        malformed_manifest.write_text("schema_version: [\n", encoding="utf-8")
        env_profile = yaml.safe_load(self.profile_path.read_text(encoding="utf-8"))
        env_profile["target"]["identifier"] = "${PERF_TARGET_URL}"
        env_profile_path = self.temp_dir / "env-profile.yaml"
        env_profile_path.write_text(yaml.safe_dump(env_profile), encoding="utf-8")

        def manifest_with(**changes) -> Path:
            path = self.temp_dir / f"manifest-{len(changes)}-{'-'.join(changes)}.yaml"
            path.write_text(yaml.safe_dump({**manifest, **changes}), encoding="utf-8")
            return path

        cases = [
            ("malformed_yaml", malformed_manifest),
            ("missing_target_env", manifest_with(profile=str(env_profile_path))),
            ("missing_dataset", manifest_with(dataset=str(self.temp_dir / "missing-dataset.yaml"))),
        ]
        for label, manifest_path in cases:
            with self.subTest(label=label):
                fake_s3 = FakeS3Client()
                sub_dir = self.temp_dir / f"early-{label}"
                with (
                    patch("performance.runner.storage.boto3.client", return_value=fake_s3),
                    patch.dict(os.environ, {"APP_REVISION": "app-sha",
                                            "PERFORMANCE_RESULTS_BUCKET": "perf-results-bucket"}, clear=True),
                ):
                    args = cli.parser().parse_args(["--experiment", str(manifest_path), "--result-dir", str(sub_dir)])
                    with self.assertRaises(ConfigurationError):
                        cli.execute(args)

                failure = self._read_failure(sub_dir)
                self.assertEqual("preflight", failure["phase"])
                self.assertTrue(failure["run_id"].startswith("preflight-error-"))
                self.assertEqual(["experiment.yaml", "failure.json"], failure["available_artifacts"])
                self.assertEqual(manifest_path.read_bytes(), (sub_dir / "experiment.yaml").read_bytes())
                self.assertIn(f"performance/failures/{failure['run_id']}/experiment.yaml", fake_s3.objects)
                self.assertFalse((sub_dir / "result.json").exists())

    def test_existing_result_dir_is_never_overwritten_and_prints_both_errors_to_stderr(self):
        existing_early = self.temp_dir / "existing-early"
        existing_early.mkdir()
        sentinel_early = existing_early / "sentinel.txt"
        sentinel_early.write_text("do-not-overwrite", encoding="utf-8")

        missing_manifest = self.temp_dir / "missing-experiment.yaml"
        stderr_early = io.StringIO()
        with (
            patch.dict(os.environ, {"APP_REVISION": "app-sha"}, clear=True),
            redirect_stderr(stderr_early),
        ):
            code = cli.main(["--experiment", str(missing_manifest), "--result-dir", str(existing_early)])

        self.assertEqual(2, code)
        self.assertEqual("do-not-overwrite", sentinel_early.read_text(encoding="utf-8"))
        self.assertFalse((existing_early / FAILURE_FILENAME).exists())
        err_text = stderr_early.getvalue()
        self.assertIn("ERROR (original, phase=preflight", err_text)
        self.assertIn("ConfigurationError", err_text)
        self.assertIn("WARNING (preservation failed): create_result_dir(FileExistsError", err_text)
        self.assertIn("ERROR:", err_text)

        existing_post = self.temp_dir / "existing-post"
        existing_post.mkdir()
        sentinel_post = existing_post / "sentinel.txt"
        sentinel_post.write_text("keep-me", encoding="utf-8")

        stderr_post = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            redirect_stderr(stderr_post),
        ):
            code_post = cli.main(self._argv(existing_post))

        self.assertEqual(2, code_post)
        self.assertEqual("keep-me", sentinel_post.read_text(encoding="utf-8"))
        self.assertFalse((existing_post / FAILURE_FILENAME).exists())
        post_err = stderr_post.getvalue()
        self.assertIn("ERROR (original, phase=preflight", post_err)
        self.assertIn("create_result_dir(FileExistsError", post_err)

    def test_preflight_failures_preserve_failure_json_and_revisions(self):
        scenarios = [
            ("missing_infra_revision", {"INFRA_REVISION": ""}, ConfigurationError, None),
            ("active_run", None, cli.RunnerError, lambda api, ins, met, cap: setattr(api.list_runs, "return_value", [{"id": 1}])),
            ("queue_backlog", None, cli.RunnerError, lambda api, ins, met, cap: setattr(ins.is_empty, "return_value", False)),
            (
                "metric_config",
                None,
                ConfigurationError,
                lambda api, ins, met, cap: setattr(met.validate_configuration, "side_effect", ConfigurationError("dim")),
            ),
            (
                "capacity_collect",
                None,
                ConfigurationError,
                lambda api, ins, met, cap: setattr(cap.collect, "side_effect", ConfigurationError("cap")),
            ),
            (
                "capacity_mismatch",
                None,
                ConfigurationError,
                lambda api, ins, met, cap: cap.collect.return_value["ecs"]["worker"].update(desired_count=3),
            ),
        ]
        for label, env_override, exc_type, mutator in scenarios:
            with self.subTest(label=label), ExitStack() as sub_stack:
                sub_dir = self.temp_dir / f"preflight-{label}"
                env = dict(self.base_env)
                if env_override:
                    for key, value in env_override.items():
                        if value == "":
                            env.pop(key, None)
                        else:
                            env[key] = value
                sub_stack.enter_context(patch.dict(os.environ, env, clear=True))
                fake_s3 = FakeS3Client()
                sub_stack.enter_context(patch("performance.runner.storage.boto3.client", return_value=fake_s3))
                api = sub_stack.enter_context(patch.object(cli, "ApiClient")).return_value
                api.list_runs.return_value = []
                sub_stack.enter_context(patch.object(cli, "queue_urls_from_environment", return_value=(["source"], ["dlq"])))
                ins = sub_stack.enter_context(patch.object(cli, "QueueInspector")).return_value
                ins.snapshot.return_value = []
                ins.is_empty.return_value = True
                met = sub_stack.enter_context(patch.object(cli, "CloudWatchMetricCollector")).return_value
                cap = sub_stack.enter_context(patch.object(cli, "InfrastructureCapacityCollector")).return_value
                cap.collect.return_value = self._observed_capacity()
                if mutator:
                    mutator(api, ins, met, cap)

                args = self._args(sub_dir)
                with self.assertRaises(exc_type):
                    cli.execute(args)

                failure = self._read_failure(sub_dir)
                self.assertEqual("preflight", failure["phase"])
                self.assertEqual(exc_type.__name__, failure["error"]["type"])
                self.assertEqual("app-sha", failure["revisions"]["application"])
                if label != "missing_infra_revision":
                    self.assertEqual("infra-sha", failure["revisions"]["infrastructure"])
                else:
                    self.assertNotIn("infrastructure", failure["revisions"])
                self.assertNotIn("k6-summary.json", failure["available_artifacts"])
                self.assertFalse((sub_dir / "result.json").exists())

    def test_seed_failure_preserves_inputs_without_suite_id(self):
        for label, suite_side_effect, suite_return, exc_type in (
            ("api_error", cli.ApiError("seed HTTP 500"), None, cli.ApiError),
            ("non_positive_id", None, 0, cli.RunnerError),
        ):
            with self.subTest(label=label), ExitStack() as sub_stack:
                sub_dir = self.temp_dir / f"seed-{label}"
                sub_stack.enter_context(patch.dict(os.environ, self.base_env, clear=True))
                fake_s3 = FakeS3Client()
                sub_stack.enter_context(patch("performance.runner.storage.boto3.client", return_value=fake_s3))
                api = sub_stack.enter_context(patch.object(cli, "ApiClient")).return_value
                api.list_runs.return_value = []
                if suite_side_effect is not None:
                    api.create_suite.side_effect = suite_side_effect
                else:
                    api.create_suite.return_value = suite_return
                sub_stack.enter_context(patch.object(cli, "queue_urls_from_environment", return_value=(["source"], ["dlq"])))
                ins = sub_stack.enter_context(patch.object(cli, "QueueInspector")).return_value
                ins.is_empty.return_value = True
                sub_stack.enter_context(patch.object(cli, "CloudWatchMetricCollector"))
                cap = sub_stack.enter_context(patch.object(cli, "InfrastructureCapacityCollector")).return_value
                cap.collect.return_value = self._observed_capacity()
                k6 = sub_stack.enter_context(patch.object(cli, "run_k6"))

                args = self._args(sub_dir)
                with self.assertRaises(exc_type):
                    cli.execute(args)

                k6.assert_not_called()
                failure = self._read_failure(sub_dir)
                self.assertEqual("seed", failure["phase"])
                self.assertEqual("FIXTURE-SMOKE", failure["inputs"]["profile_id"])
                self.assertEqual("SMOKE", failure["inputs"]["profile_type"])
                self.assertEqual("fixture-two-cases-v1", failure["inputs"]["dataset_id"])
                self.assertEqual(2, failure["inputs"]["test_case_count"])
                self.assertEqual("fixture-smoke", failure["inputs"]["experiment_id"])
                self.assertEqual("fixture-infra", failure["inputs"]["infrastructure_id"])
                self.assertEqual("fixture-v1", failure["inputs"]["infrastructure_revision"])
                self.assertEqual(str(self.manifest_path.resolve()), failure["inputs"]["experiment_path"])
                self.assertEqual(str(self.profile_path.resolve()), failure["inputs"]["profile_path"])
                for name in ("experiment.yaml", "profile.yaml", "dataset.yaml", "execution-plan.json"):
                    self.assertIn(name, failure["available_artifacts"])
                self.assertNotIn("suite_id", failure["inputs"])
                self.assertIn(f"performance/failures/{failure['run_id']}/failure.json", fake_s3.objects)

    def test_k6_failures_preserve_partial_summary_if_present_and_do_not_fabricate(self):
        # Case 1: exit 1 without summary
        fake_s3_a, _, _, _, _, k6_a = self._enter_runner_mocks()
        dir_a = self.temp_dir / "k6-no-summary"
        k6_a.side_effect = cli.RunnerError("k6 script 또는 실행 환경 오류로 종료되었습니다 (exit 1).")
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(dir_a))
        failure_a = self._read_failure(dir_a)
        self.assertEqual("k6", failure_a["phase"])
        self.assertEqual(7, failure_a["inputs"]["suite_id"])
        self.assertNotIn("k6-summary.json", failure_a["available_artifacts"])
        self.assertFalse((dir_a / "k6-summary.json").exists())

        # Case 2: exit 1 after partial summary written
        dir_b = self.temp_dir / "k6-partial-summary"
        def partial_k6(*args):
            args[4].write_text('{"partial": true}', encoding="utf-8")
            raise cli.RunnerError("k6 script 또는 실행 환경 오류로 종료되었습니다 (exit 1).")
        k6_a.side_effect = partial_k6
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(dir_b))
        failure_b = self._read_failure(dir_b)
        self.assertEqual("k6", failure_b["phase"])
        self.assertIn("k6-summary.json", failure_b["available_artifacts"])
        self.assertIn(f"performance/failures/{failure_b['run_id']}/k6-summary.json", fake_s3_a.objects)

        # Case 3: exit 0 with corrupt JSON summary
        dir_c = self.temp_dir / "k6-corrupt-summary"
        def corrupt_k6(*args):
            args[4].write_text("{corrupt", encoding="utf-8")
            return 0
        k6_a.side_effect = corrupt_k6
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(dir_c))
        failure_c = self._read_failure(dir_c)
        self.assertEqual("k6", failure_c["phase"])
        self.assertIn("k6-summary.json", failure_c["available_artifacts"])
        self.assertEqual("{corrupt", (dir_c / "k6-summary.json").read_text(encoding="utf-8"))
        self.assertFalse((dir_c / "aws-metrics.json").exists())
        self.assertFalse((dir_c / "result.json").exists())

    def test_drain_metrics_and_report_failures_preserve_prior_artifacts_only(self):
        fake_s3, api, _, metrics, _, _ = self._enter_runner_mocks()

        # Drain error (after initial preflight call succeeds)
        api.list_runs.side_effect = [[], cli.ApiError("drain query failed")]
        dir_drain = self.temp_dir / "drain-fail"
        with self.assertRaises(cli.ApiError):
            cli.execute(self._args(dir_drain))
        failure_drain = self._read_failure(dir_drain)
        self.assertEqual("drain", failure_drain["phase"])
        self.assertIn("k6-summary.json", failure_drain["available_artifacts"])
        self.assertFalse((dir_drain / "aws-metrics.json").exists())
        self.assertFalse((dir_drain / "result.json").exists())

        # Metrics error
        api.list_runs.side_effect = None
        api.list_runs.return_value = []
        metrics.collect.side_effect = ConfigurationError("cloudwatch unavailable")
        dir_metrics = self.temp_dir / "metrics-fail"
        with self.assertRaises(ConfigurationError):
            cli.execute(self._args(dir_metrics))
        failure_metrics = self._read_failure(dir_metrics)
        self.assertEqual("metrics", failure_metrics["phase"])
        self.assertIn("k6-summary.json", failure_metrics["available_artifacts"])
        self.assertFalse((dir_metrics / "aws-metrics.json").exists())

        # Report diagnostics error
        metrics.collect.side_effect = None
        api.list_runs.side_effect = [[], [], cli.ApiError("diagnostics failed")]
        dir_report_api = self.temp_dir / "report-api-fail"
        with self.assertRaises(cli.ApiError):
            cli.execute(self._args(dir_report_api))
        failure_report_api = self._read_failure(dir_report_api)
        self.assertEqual("report", failure_report_api["phase"])
        self.assertIn("k6-summary.json", failure_report_api["available_artifacts"])
        self.assertIn("aws-metrics.json", failure_report_api["available_artifacts"])
        self.assertFalse((dir_report_api / "result.json").exists())
        self.assertFalse((dir_report_api / "report.md").exists())

        # Report formatting error (neither result.json nor report.md left behind)
        api.list_runs.side_effect = None
        api.list_runs.return_value = []
        dir_report_fmt = self.temp_dir / "report-fmt-fail"
        with patch.object(cli, "_report", side_effect=RuntimeError("template boom")):
            with self.assertRaises(RuntimeError):
                cli.execute(self._args(dir_report_fmt))
        failure_report_fmt = self._read_failure(dir_report_fmt)
        self.assertEqual("report", failure_report_fmt["phase"])
        self.assertFalse((dir_report_fmt / "result.json").exists())
        self.assertFalse((dir_report_fmt / "report.md").exists())
        self.assertIn(f"performance/failures/{failure_report_fmt['run_id']}/aws-metrics.json", fake_s3.objects)

    def test_pass_and_threshold_fail_publish_schema_v2_and_complete_marker_last(self):
        for label, k6_exit, expected_cli_code, expected_status in (
            ("pass", 0, 0, "PASS"),
            ("threshold_fail", cli.K6_THRESHOLD_FAILURE_EXIT_CODE, 1, "FAIL"),
        ):
            with self.subTest(label=label), ExitStack() as sub_stack:
                fake_s3 = FakeS3Client()
                sub_dir = self.temp_dir / f"complete-{label}"
                sub_stack.enter_context(patch.dict(os.environ, self.base_env, clear=True))
                sub_stack.enter_context(patch("performance.runner.storage.boto3.client", return_value=fake_s3))
                api = sub_stack.enter_context(patch.object(cli, "ApiClient")).return_value
                api.list_runs.return_value = []
                api.create_suite.return_value = 7
                sub_stack.enter_context(patch.object(cli, "queue_urls_from_environment", return_value=(["source"], ["dlq"])))
                ins = sub_stack.enter_context(patch.object(cli, "QueueInspector")).return_value
                ins.snapshot.return_value = []
                ins.is_empty.return_value = True
                met = sub_stack.enter_context(patch.object(cli, "CloudWatchMetricCollector")).return_value
                met.collect.return_value = {
                    "status": "COLLECTED",
                    "metrics": [
                        {"id": name, "values": [1]}
                        for name in ("ecs_cpu", "sqs_visible", "rds_cpu", "sagemaker_invocations")
                    ],
                }
                cap = sub_stack.enter_context(patch.object(cli, "InfrastructureCapacityCollector")).return_value
                cap.collect.return_value = self._observed_capacity()

                def write_summary(*args, exit_code=k6_exit):
                    args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
                    return exit_code

                sub_stack.enter_context(patch.object(cli, "run_k6", side_effect=write_summary))
                stdout_buf = io.StringIO()
                with redirect_stdout(stdout_buf):
                    code = cli.main(self._argv(sub_dir))

                self.assertEqual(expected_cli_code, code)
                self.assertFalse((sub_dir / FAILURE_FILENAME).exists())
                self.assertFalse((sub_dir / UPLOAD_COMPLETE_FILENAME).exists())

                result = json.loads((sub_dir / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(2, result["schema_version"])
                self.assertEqual(expected_status, result["acceptance"]["status"])
                self.assertEqual(k6_exit, result["k6"]["exit_code"])
                for key in (
                    "run_id", "started_at", "finished_at", "revisions", "infrastructure_capacity",
                    "experiment", "dataset", "profile", "k6", "failure_categories",
                    "test_run_diagnostics", "preflight", "drain", "final_dlq", "aws_metrics", "acceptance",
                ):
                    self.assertIn(key, result)

                run_id = result["run_id"]
                self.assertEqual(
                    [*RESULT_FILENAMES, UPLOAD_COMPLETE_FILENAME],
                    [name for name, _, _ in fake_s3.calls],
                )
                marker_key = f"performance/results/{run_id}/{UPLOAD_COMPLETE_FILENAME}"
                marker = json.loads(fake_s3.objects[marker_key].decode("utf-8"))
                self.assertEqual(1, marker["schema_version"])
                self.assertEqual("COMPLETE", marker["status"])
                self.assertEqual(run_id, marker["run_id"])
                self.assertEqual(2, marker["result_schema_version"])
                self.assertEqual(list(RESULT_FILENAMES), marker["files"])
                self.assertFalse(any(k.startswith("performance/failures/") for k in fake_s3.objects))

    def test_post_measurement_upload_failure_preserves_local_pass_and_fail_without_marker(self):
        for label, k6_exit, expected_status, fail_file in (
            ("pass_fail_on_report", 0, "PASS", "report.md"),
            ("threshold_fail_on_marker", cli.K6_THRESHOLD_FAILURE_EXIT_CODE, "FAIL", UPLOAD_COMPLETE_FILENAME),
        ):
            with self.subTest(label=label), ExitStack() as sub_stack:
                fake_s3 = FakeS3Client(fail_on_filename=fail_file)
                sub_dir = self.temp_dir / f"upload-fail-{label}"
                sub_stack.enter_context(patch.dict(os.environ, self.base_env, clear=True))
                sub_stack.enter_context(patch("performance.runner.storage.boto3.client", return_value=fake_s3))
                api = sub_stack.enter_context(patch.object(cli, "ApiClient")).return_value
                api.list_runs.return_value = []
                api.create_suite.return_value = 7
                sub_stack.enter_context(patch.object(cli, "queue_urls_from_environment", return_value=(["source"], ["dlq"])))
                ins = sub_stack.enter_context(patch.object(cli, "QueueInspector")).return_value
                ins.snapshot.return_value = []
                ins.is_empty.return_value = True
                met = sub_stack.enter_context(patch.object(cli, "CloudWatchMetricCollector")).return_value
                met.collect.return_value = {
                    "status": "COLLECTED",
                    "metrics": [
                        {"id": name, "values": [1]}
                        for name in ("ecs_cpu", "sqs_visible", "rds_cpu", "sagemaker_invocations")
                    ],
                }
                cap = sub_stack.enter_context(patch.object(cli, "InfrastructureCapacityCollector")).return_value
                cap.collect.return_value = self._observed_capacity()

                def write_summary(*args, exit_code=k6_exit):
                    args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
                    return exit_code

                sub_stack.enter_context(patch.object(cli, "run_k6", side_effect=write_summary))
                stderr_buf = io.StringIO()
                with redirect_stderr(stderr_buf):
                    code = cli.main(self._argv(sub_dir))

                self.assertEqual(2, code)
                local_result = json.loads((sub_dir / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(expected_status, local_result["acceptance"]["status"])
                self.assertEqual(2, local_result["schema_version"])
                self.assertTrue((sub_dir / "report.md").is_file())

                failure = self._read_failure(sub_dir)
                self.assertEqual("upload", failure["phase"])
                self.assertEqual("RunnerError", failure["error"]["type"])
                self.assertEqual("ResultUploadError", failure["error"]["cause"]["type"])
                self.assertFalse(any(name == UPLOAD_COMPLETE_FILENAME for name, _, _ in fake_s3.calls))
                self.assertFalse(any(key.startswith("performance/failures/") for key in fake_s3.objects))

    def test_compound_preservation_failures_preserve_original_cause(self):
        # Case 1: k6 fails AND S3 failure upload fails
        fake_s3 = FakeS3Client(fail_all=True)
        _, _, _, _, _, k6 = self._enter_runner_mocks(s3_client=fake_s3)
        k6.side_effect = cli.RunnerError("k6 original cause failure")
        dir_s3_fail = self.temp_dir / "compound-s3-fail"
        stderr_s3 = io.StringIO()
        with redirect_stderr(stderr_s3):
            with self.assertRaisesRegex(cli.RunnerError, "k6 original cause failure"):
                cli.execute(self._args(dir_s3_fail))

        failure = self._read_failure(dir_s3_fail)
        self.assertEqual("k6", failure["phase"])
        self.assertEqual("RunnerError", failure["error"]["type"])
        self.assertEqual(1, len(failure["preservation_errors"]))
        self.assertEqual("upload_failure_artifacts", failure["preservation_errors"][0]["step"])
        self.assertEqual("ResultUploadError", failure["preservation_errors"][0]["type"])
        self.assertIn("k6 original cause failure", stderr_s3.getvalue())
        self.assertIn("upload_failure_artifacts(ResultUploadError", stderr_s3.getvalue())

        # Case 2: k6 fails AND local failure.json write fails with OSError
        dir_write_fail = self.temp_dir / "compound-write-fail"
        stderr_write = io.StringIO()
        original_write_text = Path.write_text

        def fail_failure_write(path_self: Path, data: str, *args, **kwargs):
            if path_self.name == FAILURE_FILENAME:
                raise OSError("disk full")
            return original_write_text(path_self, data, *args, **kwargs)

        with (
            patch.object(Path, "write_text", autospec=True, side_effect=fail_failure_write),
            redirect_stderr(stderr_write),
        ):
            with self.assertRaisesRegex(cli.RunnerError, "k6 original cause failure"):
                cli.execute(self._args(dir_write_fail))

        self.assertIn("k6 original cause failure", stderr_write.getvalue())
        self.assertIn("write_failure_artifact(OSError: disk full)", stderr_write.getvalue())

    def test_storage_validation_and_put_object_only_contract(self):
        class PutOnlyS3:
            def __init__(self) -> None:
                self.uploads: list[tuple[str, str, str]] = []

            def upload_file(self, filename: str, bucket: str, key: str) -> None:
                self.uploads.append((Path(filename).name, bucket, key))

        # Completed directory with failure.json must be rejected
        dir_with_failure = self.temp_dir / "has-failure"
        dir_with_failure.mkdir()
        for name in (*RESULT_FILENAMES, FAILURE_FILENAME):
            (dir_with_failure / name).write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ResultUploadError, "failure.json"):
            upload_result_directory(dir_with_failure, "bucket", "run-1", s3_client=PutOnlyS3())

        # PutObject-only client succeeds and never uploads upload-complete.json via failure upload
        dir_partial = self.temp_dir / "partial-dir"
        dir_partial.mkdir()
        (dir_partial / FAILURE_FILENAME).write_text('{"status":"ERROR"}', encoding="utf-8")
        (dir_partial / "profile.yaml").write_text("test: {}", encoding="utf-8")
        (dir_partial / UPLOAD_COMPLETE_FILENAME).write_text("{}", encoding="utf-8")
        put_only = PutOnlyS3()
        keys = upload_failure_directory(dir_partial, "bucket", "run-2", s3_client=put_only)
        self.assertEqual(
            [
                "performance/failures/run-2/failure.json",
                "performance/failures/run-2/profile.yaml",
            ],
            keys,
        )
        with self.assertRaises(ResultUploadError):
            upload_failure_directory(dir_partial, "   ", "run-2", s3_client=put_only)
        with self.assertRaises(ResultUploadError):
            upload_failure_directory(self.temp_dir, "bucket", "run-2", s3_client=put_only)

    def test_secret_redaction_and_dry_run_safety(self):
        raw = (
            "https://alice:s3cr3t@api.example.test/v1 "
            "Authorization: Bearer top-jwt-token "
            "AWS_SECRET_ACCESS_KEY=raw-aws-secret "
            "password='pw-value' "
            "정상 한국어 진단 메시지 (exit 1)"
        )
        scrubbed = sanitize_message(raw)
        for secret in ("alice:s3cr3t", "top-jwt-token", "raw-aws-secret", "pw-value"):
            self.assertNotIn(secret, scrubbed)
        self.assertIn("https://***@api.example.test/v1", scrubbed)
        self.assertIn("Bearer ***", scrubbed)
        self.assertIn("정상 한국어 진단 메시지 (exit 1)", scrubbed)

        # Secrets in environment are never serialized into failure.json
        fake_s3, api, _, _, _, _ = self._enter_runner_mocks(
            env_overrides={
                "AWS_SECRET_ACCESS_KEY": "super-secret-aws-key",
                "AWS_SESSION_TOKEN": "super-secret-session-token",
                "PERF_API_TOKEN": "super-secret-api-token",
            }
        )
        api.create_suite.side_effect = cli.ApiError(
            "failed calling https://admin:secretpw@api.example.test with Bearer my-token-123"
        )
        with self.assertRaises(cli.ApiError):
            cli.execute(self._args(self.result_dir))
        raw_failure_text = (self.result_dir / FAILURE_FILENAME).read_text(encoding="utf-8")
        for secret in (
            "super-secret-aws-key",
            "super-secret-session-token",
            "super-secret-api-token",
            "admin:secretpw",
            "my-token-123",
        ):
            self.assertNotIn(secret, raw_failure_text)
        self.assertTrue(fake_s3.objects)

        # --dry-run failure must not write failure.json or touch S3
        dry_s3 = FakeS3Client()
        dry_dir = self.temp_dir / "dry-run-fail"
        with (
            patch("performance.runner.storage.boto3.client", return_value=dry_s3),
            patch.dict(os.environ, {"PERFORMANCE_RESULTS_BUCKET": "perf-results-bucket"}, clear=True),
        ):
            args = cli.parser().parse_args(["--experiment", str(self.temp_dir / "missing.yaml"),
                                            "--dry-run", "--result-dir", str(dry_dir)])
            with self.assertRaises(ConfigurationError):
                cli.execute(args)
        self.assertFalse(dry_dir.exists())
        self.assertEqual({}, dry_s3.objects)

    def test_main_exits_2_on_unexpected_exception(self):
        self._enter_runner_mocks()
        with (
            patch.object(cli, "execute", side_effect=RuntimeError("token=secret-value boom")),
            redirect_stderr(io.StringIO()) as err,
        ):
            code = cli.main(self._argv(self.result_dir))
        self.assertEqual(2, code)
        self.assertIn("ERROR: RuntimeError:", err.getvalue())
        self.assertNotIn("secret-value", err.getvalue())

    def test_analysis_bundle_workflow_script_requires_markers_hashes_and_rejects_legacy(self):
        workflow = yaml.safe_load(
            (REPO_ROOT / ".github/workflows/performance-analysis-bundle.yml").read_text(encoding="utf-8")
        )
        steps = workflow["jobs"]["collect"]["steps"]
        download_step = next(step for step in steps if step.get("name") == "Download canonical Runner results from S3")
        script = download_step["run"]

        fake_aws_script = """#!/usr/bin/env python3
import os, pathlib, shutil, sys
args = sys.argv[1:]
if args[:2] != ["s3", "cp"]:
    sys.exit(2)
src, dst = args[2], args[3]
s3_root = pathlib.Path(os.environ["FAKE_S3_ROOT"])
rel = src.removeprefix("s3://test-bucket/")
target = s3_root / rel
if "--recursive" in args:
    dst_path = pathlib.Path(dst)
    dst_path.mkdir(parents=True, exist_ok=True)
    if target.is_dir():
        for item in target.iterdir():
            if item.is_file():
                shutil.copy2(item, dst_path / item.name)
    sys.exit(0)
if target.is_file():
    pathlib.Path(dst).parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target, dst)
    sys.exit(0)
sys.exit(1)
"""

        def run_download(setup_fn):
            with tempfile.TemporaryDirectory() as run_tmp:
                root = Path(run_tmp)
                s3_root = root / "s3"
                work_dir = root / "work"
                bin_dir = root / "bin"
                s3_root.mkdir()
                work_dir.mkdir()
                bin_dir.mkdir()
                aws_bin = bin_dir / "aws"
                aws_bin.write_text(fake_aws_script, encoding="utf-8")
                aws_bin.chmod(0o755)
                summary_file = root / "step-summary.md"
                summary_file.write_text("", encoding="utf-8")

                run_id = "smoke-run-1"
                results_prefix = s3_root / "performance" / "results" / run_id
                failures_prefix = s3_root / "performance" / "failures" / run_id
                setup_fn(results_prefix, failures_prefix, run_id)

                env = dict(
                    os.environ,
                    PATH=f"{bin_dir}:{os.environ['PATH']}",
                    FAKE_S3_ROOT=str(s3_root),
                    RESULTS_BUCKET="test-bucket",
                    RUN_ID=run_id,
                    GITHUB_STEP_SUMMARY=str(summary_file),
                )
                completed = subprocess.run(
                    ["bash", "-c", script],
                    cwd=work_dir,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                return completed, summary_file.read_text(encoding="utf-8")

        def write_canonical(prefix: Path, run_id: str, *, result_doc: dict | None = None, include_marker: bool = True,
                            include_hashes: bool = True, omit_file: str | None = None, undeclared: str | None = None,
                            corrupt_hash: str | None = None):
            prefix.mkdir(parents=True, exist_ok=True)
            contents = {
                "experiment.yaml": "schema_version: 1\nid: exp-1\n",
                "profile.yaml": "test: {}",
                "dataset.yaml": "id: baseline",
                "execution-plan.json": '{"schema_version": 1}\n',
                "k6-summary.json": "{}",
                "aws-metrics.json": "{}",
                "result.json": json.dumps(result_doc if result_doc is not None else {"schema_version": 2, "run_id": run_id}),
                "report.md": "# report",
            }
            for name, content in contents.items():
                if name != omit_file:
                    (prefix / name).write_text(content, encoding="utf-8")
            if not include_marker:
                return
            declared = [name for name in RESULT_FILENAMES if name != undeclared]
            marker = {"schema_version": 1, "run_id": run_id, "status": "COMPLETE",
                      "completed_at": "2026-11-20T00:00:00Z", "files": declared}
            if include_hashes:
                marker["file_sha256"] = {
                    name: hashlib.sha256((prefix / name).read_bytes()).hexdigest() if (prefix / name).is_file() else "0" * 64
                    for name in declared
                }
                if corrupt_hash:
                    marker["file_sha256"][corrupt_hash] = "f" * 64
            (prefix / UPLOAD_COMPLETE_FILENAME).write_text(json.dumps(marker), encoding="utf-8")

        def expect_rejected(setup, message):
            res, _ = run_download(setup)
            self.assertEqual(1, res.returncode, res.stderr)
            self.assertIn(message, res.stderr)

        # 1. Complete schema_version=2 publication with marker and matching digests -> bundled
        res, summary = run_download(lambda rp, fp, rid: write_canonical(rp, rid))
        self.assertEqual(0, res.returncode, res.stderr)
        self.assertIn("Downloaded s3://test-bucket/performance/results/smoke-run-1/", summary)

        # 2. Missing marker, mismatched run_id or missing file_sha256 -> rejected
        expect_rejected(lambda rp, fp, rid: write_canonical(rp, rid, include_marker=False),
                        "upload-complete.json is missing")
        expect_rejected(lambda rp, fp, rid: write_canonical(rp, "other-run", result_doc={"schema_version": 2, "run_id": rid}),
                        "upload-complete.json is invalid")
        expect_rejected(lambda rp, fp, rid: write_canonical(rp, rid, include_hashes=False),
                        "upload-complete.json is invalid")

        # 3. Schema-less or malformed result.json is no longer analyzed, even with a marker
        for result_doc in ({"run_id": "smoke-run-1"}, {"schema_version": None, "run_id": "smoke-run-1"},
                           {"schema_version": 1, "run_id": "smoke-run-1"}):
            with self.subTest(result_doc=result_doc):
                expect_rejected(lambda rp, fp, rid, doc=result_doc: write_canonical(rp, rid, result_doc=doc),
                                "schema-less results are not analyzed")

        # 4. Historical schema-less layout (4 files, no inputs or marker) -> rejected as incomplete
        def setup_legacy(rp: Path, fp: Path, rid: str):
            rp.mkdir(parents=True, exist_ok=True)
            (rp / "result.json").write_text(json.dumps({"run_id": rid}), encoding="utf-8")
            for name in ("report.md", "k6-summary.json", "aws-metrics.json"):
                (rp / name).write_text("{}", encoding="utf-8")

        expect_rejected(setup_legacy, "missing or empty canonical Runner files")

        # 5. Required run inputs missing from S3 or from the marker declaration -> rejected
        for name in ("experiment.yaml", "execution-plan.json"):
            with self.subTest(missing=name):
                expect_rejected(lambda rp, fp, rid, n=name: write_canonical(rp, rid, omit_file=n), name)
                expect_rejected(lambda rp, fp, rid, n=name: write_canonical(rp, rid, undeclared=n),
                                f"does not declare required artifact '{name}'")

        # 6. Digest mismatch on any declared file (core or input) -> rejected
        for name in ("result.json", "experiment.yaml"):
            with self.subTest(corrupt=name):
                expect_rejected(lambda rp, fp, rid, n=name: write_canonical(rp, rid, corrupt_hash=n),
                                f"SHA-256 mismatch for s3://test-bucket/performance/results/smoke-run-1/{name}")

        # 7. Incomplete canonical files + failure.json in failures prefix -> exits 1 with guidance
        def setup_failed_run(rp: Path, fp: Path, rid: str):
            fp.mkdir(parents=True, exist_ok=True)
            (fp / FAILURE_FILENAME).write_text(
                json.dumps({"phase": "k6", "error": {"type": "RunnerError", "message": "boom"}}),
                encoding="utf-8",
            )

        res, summary = run_download(setup_failed_run)
        self.assertEqual(1, res.returncode)
        self.assertIn("performance/failures/smoke-run-1/", res.stderr)
        self.assertIn("phase=k6", res.stderr)
        self.assertIn("error.type=RunnerError", res.stderr)
        self.assertIn("phase: `k6`", summary)
        self.assertIn("error: `RunnerError`", summary)

        # 8. Canonical prefix containing failure.json -> rejected
        def setup_contaminated(rp: Path, fp: Path, rid: str):
            write_canonical(rp, rid)
            (rp / FAILURE_FILENAME).write_text("{}", encoding="utf-8")

        expect_rejected(setup_contaminated, "contains failure.json")

    def test_completed_upload_requires_run_inputs_and_hashes_every_file(self):
        def populate(directory: Path, *, skip: str | None = None) -> None:
            directory.mkdir()
            for name in RESULT_FILENAMES:
                if name == skip:
                    continue
                content = '{"schema_version": 2}' if name == "result.json" else f"content:{name}"
                (directory / name).write_text(content, encoding="utf-8")

        # 1. Every required artifact is uploaded before the marker, which hashes each of them
        complete_dir = self.temp_dir / "complete"
        populate(complete_dir)
        s3_client = FakeS3Client()
        upload_result_directory(complete_dir, "bucket", "run-complete", s3_client=s3_client)
        self.assertEqual([*RESULT_FILENAMES, UPLOAD_COMPLETE_FILENAME], [name for name, _, _ in s3_client.calls])
        marker = json.loads(
            s3_client.objects[f"performance/results/run-complete/{UPLOAD_COMPLETE_FILENAME}"].decode("utf-8")
        )
        self.assertEqual(list(RESULT_FILENAMES), marker["files"])
        self.assertEqual(2, marker["result_schema_version"])
        for name in RESULT_FILENAMES:
            self.assertEqual(hashlib.sha256((complete_dir / name).read_bytes()).hexdigest(), marker["file_sha256"][name])

        # 2. Missing run inputs or core files fail strictly and nothing is uploaded
        for name in ("experiment.yaml", "execution-plan.json", "aws-metrics.json"):
            with self.subTest(missing=name):
                missing_dir = self.temp_dir / f"missing-{name}"
                populate(missing_dir, skip=name)
                s3_missing = FakeS3Client()
                with self.assertRaisesRegex(ResultUploadError, name):
                    upload_result_directory(missing_dir, "bucket", "run-bad", s3_client=s3_missing)
                self.assertEqual([], s3_missing.calls)

    def test_experiment_metadata_and_optional_artifacts_in_failure_preservation(self):
        # Case A: Early failure of an incomplete manifest passed to cli.execute()
        exp_file = self.temp_dir / "incomplete-experiment.yaml"
        exp_file.write_text("schema_version: 1\nid: exp-early\n", encoding="utf-8")
        fake_s3_a = FakeS3Client()
        dir_a = self.temp_dir / "exp-early-dir"
        args = cli.parser().parse_args(["--experiment", str(exp_file), "--result-dir", str(dir_a)])
        with (
            patch("performance.runner.storage.boto3.client", return_value=fake_s3_a),
            patch.dict(
                os.environ,
                {"APP_REVISION": "app-sha", "PERFORMANCE_RESULTS_BUCKET": "perf-results-bucket"},
                clear=True,
            ),
        ):
            with self.assertRaises(ConfigurationError):
                cli.execute(args)

        failure_a = self._read_failure(dir_a)
        self.assertEqual(str(exp_file.resolve()), failure_a["inputs"]["experiment_path"])
        self.assertTrue((dir_a / "experiment.yaml").is_file())
        self.assertIn("experiment.yaml", failure_a["available_artifacts"])
        self.assertIn(f"performance/failures/{failure_a['run_id']}/experiment.yaml", fake_s3_a.objects)

        # Case B: RunContext.record_execution_plan populates experiment & infra metadata cleanly
        ctx_plan = RunContext(run_id="run-plan", started_at=datetime.now(timezone.utc))
        ctx_plan.record_execution_plan({
            "experiment": {"id": "exp-plan", "mode": "synthetic"},
            "infrastructure": {"id": "infra-fixture", "revision": "infra-rev-1"},
            "sources": {"experiment": {"path": str(exp_file)}},
            "profile": {"test": {"id": "PERF-OP-01", "type": "SMOKE"}},
            "dataset": {"id": "baseline-v1", "test_case_count": 78},
        })
        self.assertEqual(str(exp_file.resolve()), ctx_plan.inputs["experiment_path"])
        self.assertEqual("exp-plan", ctx_plan.inputs["experiment_id"])
        self.assertNotIn("experiment_mode", ctx_plan.inputs)
        self.assertEqual("infra-fixture", ctx_plan.inputs["infrastructure_id"])
        self.assertEqual("infra-rev-1", ctx_plan.inputs["infrastructure_revision"])
        self.assertEqual("PERF-OP-01", ctx_plan.inputs["profile_id"])
        self.assertEqual(78, ctx_plan.inputs["test_case_count"])

        # Case C: Backfill from execution-plan.json without overwriting existing inputs, plus partial S3 upload
        dir_b = self.temp_dir / "exp-backfill-dir"
        dir_b.mkdir()
        (dir_b / "experiment.yaml").write_text("schema_version: 1\nid: exp-backfill\n", encoding="utf-8")
        (dir_b / "execution-plan.json").write_text(
            json.dumps({
                "schema_version": 1,
                "experiment": {"id": "from-plan", "mode": "synthetic"},
                "infrastructure": {"id": "fixture-infra", "revision": "fixture-v1"},
                "sources": {"experiment": {"path": str(exp_file)}},
            }),
            encoding="utf-8",
        )
        (dir_b / UPLOAD_COMPLETE_FILENAME).write_text("{}", encoding="utf-8")
        ctx_b = RunContext(
            run_id="run-backfill-1",
            started_at=datetime.now(timezone.utc),
            result_dir=dir_b,
            result_dir_owned=True,
            phase="k6",
        )
        ctx_b.record_experiment(experiment_id="explicit-id")
        fake_s3_b = FakeS3Client()
        failure_path, preservation_errors = preserve_failure(
            ctx_b,
            cli.RunnerError("k6 boom"),
            bucket="perf-results-bucket",
            s3_client=fake_s3_b,
        )
        self.assertEqual([], preservation_errors)
        self.assertIsNotNone(failure_path)
        failure_b = self._read_failure(dir_b)
        self.assertEqual("explicit-id", failure_b["inputs"]["experiment_id"])
        self.assertEqual(str(exp_file), failure_b["inputs"]["experiment_path"])
        self.assertNotIn("experiment_mode", failure_b["inputs"])
        self.assertEqual("fixture-infra", failure_b["inputs"]["infrastructure_id"])
        self.assertEqual("fixture-v1", failure_b["inputs"]["infrastructure_revision"])
        self.assertIn("experiment.yaml", failure_b["available_artifacts"])
        self.assertIn("execution-plan.json", failure_b["available_artifacts"])
        self.assertIn("performance/failures/run-backfill-1/experiment.yaml", fake_s3_b.objects)
        self.assertIn("performance/failures/run-backfill-1/execution-plan.json", fake_s3_b.objects)
        self.assertNotIn("performance/failures/run-backfill-1/upload-complete.json", fake_s3_b.objects)


if __name__ == "__main__":
    unittest.main()
