"""Regression tests for failure diagnostics and local partial artifact preservation."""

from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import yaml

from performance.runner import cli
from performance.runner.config import ConfigurationError
from performance.runner.failure import FAILURE_FILENAME, RunContext, preserve_failure, sanitize_message
from performance.runner.results import RESULT_FILENAMES
from performance.runner.test_runner import passing_summary


FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class FailureArtifactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.temp_dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.result_dir = self.temp_dir / "run-result"
        self.scenario_path = self.temp_dir / "scenario.yaml"
        self.dataset_path = self.temp_dir / "dataset.yaml"
        shutil.copyfile(FIXTURES / "profiles/smoke.yaml", self.scenario_path)
        shutil.copyfile(FIXTURES / "datasets/two-cases.yaml", self.dataset_path)

    def _argv(self, result_dir: Path, *extra: str, scenario_path: Path | None = None, dataset_path: Path | None = None) -> list[str]:
        return [
            "--api-url", "https://api.example.test",
            "--target-url", "https://target.example.test/v1/chat/completions",
            "--target-model", "fixture-model",
            "--target-revision", "rev-1",
            "--scenario", str(scenario_path or self.scenario_path),
            "--dataset", str(dataset_path or self.dataset_path),
            "--result-dir", str(result_dir),
            *extra,
        ]

    def _args(self, result_dir: Path, *extra: str, scenario_path: Path | None = None, dataset_path: Path | None = None):
        return cli.parser().parse_args(
            self._argv(result_dir, *extra, scenario_path=scenario_path, dataset_path=dataset_path)
        )

    def _enter_runner_mocks(self, *, env_overrides: dict[str, str] | None = None):
        env = dict(env_overrides or {})
        self.stack.enter_context(patch.dict(os.environ, env, clear=True))

        api = self.stack.enter_context(patch.object(cli, "ApiClient")).return_value
        api.list_runs.return_value = []
        api.create_suite.return_value = 7
        api.get_run.return_value = {"id": 1, "status": "FINISHED", "executionOutcome": "COMPLETED"}

        def default_k6(*args):
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return 0

        k6 = self.stack.enter_context(patch.object(cli, "run_k6", side_effect=default_k6))
        self.stack.enter_context(patch.object(cli.subprocess, "run", side_effect=AssertionError("unexpected process")))
        return api, k6

    def _read_failure(self, result_dir: Path | None = None) -> dict:
        target = result_dir or self.result_dir
        return json.loads((target / FAILURE_FILENAME).read_text(encoding="utf-8"))

    def test_early_missing_scenario_writes_failure_json_preserving_dataset(self):
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        missing_scenario = self.temp_dir / "does-not-exist.yaml"
        args = self._args(self.result_dir, scenario_path=missing_scenario)

        with self.assertRaises(ConfigurationError):
            cli.execute(args)

        failure = self._read_failure()
        self.assertEqual(1, failure["schema_version"])
        self.assertEqual("ERROR", failure["status"])
        self.assertEqual("preflight", failure["phase"])
        self.assertTrue(failure["run_id"].startswith("preflight-error-"))
        self.assertEqual("ConfigurationError", failure["error"]["type"])
        self.assertEqual(str(missing_scenario.resolve()), failure["inputs"]["scenario_path"])
        self.assertEqual(["dataset.yaml", "failure.json"], failure["available_artifacts"])
        self.assertEqual([], failure["preservation_errors"])
        for missing_name in ("scenario.yaml", "execution-plan.json", "k6-summary.json", "result.json", "report.md"):
            self.assertFalse((self.result_dir / missing_name).exists())

    def test_early_invalid_plan_inputs_preserve_existing_source_files_and_failure_json(self):
        malformed_scenario = self.temp_dir / "malformed.yaml"
        malformed_scenario.write_text("test: [\n", encoding="utf-8")
        env_scenario = yaml.safe_load(self.scenario_path.read_text(encoding="utf-8"))
        env_scenario["test"]["name"] = "${UNRESOLVED_SCENARIO_NAME}"
        env_scenario_path = self.temp_dir / "env-scenario.yaml"
        env_scenario_path.write_text(yaml.safe_dump(env_scenario), encoding="utf-8")

        cases = [
            ("malformed_yaml", malformed_scenario, self.dataset_path),
            ("unresolved_env", env_scenario_path, self.dataset_path),
            ("missing_dataset", self.scenario_path, self.temp_dir / "missing-dataset.yaml"),
        ]
        for label, s_path, d_path in cases:
            with self.subTest(label=label):
                sub_dir = self.temp_dir / f"early-{label}"
                with patch.dict(os.environ, {}, clear=True):
                    args = self._args(sub_dir, scenario_path=s_path, dataset_path=d_path)
                    with self.assertRaises(ConfigurationError):
                        cli.execute(args)

                failure = self._read_failure(sub_dir)
                self.assertEqual("preflight", failure["phase"])
                self.assertTrue(failure["run_id"].startswith("preflight-error-"))
                self.assertIn("failure.json", failure["available_artifacts"])
                self.assertIn("scenario.yaml", failure["available_artifacts"])
                self.assertEqual(s_path.read_bytes(), (sub_dir / "scenario.yaml").read_bytes())
                self.assertFalse((sub_dir / "result.json").exists())

    def test_existing_result_dir_is_never_overwritten_and_prints_both_errors_to_stderr(self):
        existing = self.temp_dir / "existing-early"
        existing.mkdir()
        sentinel = existing / "sentinel.txt"
        sentinel.write_text("do-not-overwrite", encoding="utf-8")

        missing_scenario = self.temp_dir / "missing-scenario.yaml"
        stderr_buf = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr_buf):
            code = cli.main(self._argv(existing, scenario_path=missing_scenario))

        self.assertEqual(2, code)
        self.assertEqual("do-not-overwrite", sentinel.read_text(encoding="utf-8"))
        self.assertFalse((existing / FAILURE_FILENAME).exists())
        err_text = stderr_buf.getvalue()
        self.assertIn("ERROR (original, phase=preflight", err_text)
        self.assertIn("ConfigurationError", err_text)
        self.assertIn("WARNING (preservation failed): create_result_dir(FileExistsError", err_text)
        self.assertIn("ERROR:", err_text)

    def test_preflight_and_seed_failures_preserve_failure_json(self):
        # Preflight failure (active run)
        api, k6 = self._enter_runner_mocks()
        api.list_runs.return_value = [{"id": 1}]
        pre_dir = self.temp_dir / "preflight-active"
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(pre_dir))
        k6.assert_not_called()
        failure_pre = self._read_failure(pre_dir)
        self.assertEqual("preflight", failure_pre["phase"])
        self.assertEqual("RunnerError", failure_pre["error"]["type"])
        self.assertNotIn("k6-summary.json", failure_pre["available_artifacts"])
        self.assertNotIn("suite_id", failure_pre["inputs"])

        # Seed failures (ApiError and non-positive suite id)
        api.list_runs.return_value = []
        for label, side_effect, return_value, exc_type in (
            ("api_error", cli.ApiError("seed HTTP 500"), None, cli.ApiError),
            ("non_positive_id", None, 0, cli.RunnerError),
        ):
            with self.subTest(label=label):
                sub_dir = self.temp_dir / f"seed-{label}"
                api.create_suite.side_effect = side_effect
                api.create_suite.return_value = return_value
                with self.assertRaises(exc_type):
                    cli.execute(self._args(sub_dir))
                failure = self._read_failure(sub_dir)
                self.assertEqual("seed", failure["phase"])
                self.assertEqual("FIXTURE-SMOKE", failure["inputs"]["scenario_id"])
                self.assertEqual("SMOKE", failure["inputs"]["scenario_type"])
                self.assertEqual("fixture-two-cases-v1", failure["inputs"]["dataset_id"])
                self.assertEqual(2, failure["inputs"]["test_case_count"])
                for name in ("scenario.yaml", "dataset.yaml", "execution-plan.json", "failure.json"):
                    self.assertIn(name, failure["available_artifacts"])
                self.assertNotIn("suite_id", failure["inputs"])

    def test_k6_and_report_failures_preserve_prior_artifacts_only(self):
        api, k6 = self._enter_runner_mocks()

        # Case 1: k6 exit 1 without summary
        dir_a = self.temp_dir / "k6-no-summary"
        k6.side_effect = cli.RunnerError("k6 script 또는 실행 환경 오류로 종료되었습니다 (exit 1).")
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(dir_a))
        failure_a = self._read_failure(dir_a)
        self.assertEqual("k6", failure_a["phase"])
        self.assertEqual(7, failure_a["inputs"]["suite_id"])
        self.assertNotIn("k6-summary.json", failure_a["available_artifacts"])

        # Case 2: k6 exit 1 with partial summary
        dir_b = self.temp_dir / "k6-partial-summary"
        def partial_k6(*args):
            args[4].write_text('{"partial": true}', encoding="utf-8")
            raise cli.RunnerError("k6 script 또는 실행 환경 오류로 종료되었습니다 (exit 1).")
        k6.side_effect = partial_k6
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(dir_b))
        failure_b = self._read_failure(dir_b)
        self.assertEqual("k6", failure_b["phase"])
        self.assertIn("k6-summary.json", failure_b["available_artifacts"])

        # Case 3: k6 corrupt JSON summary
        dir_c = self.temp_dir / "k6-corrupt-summary"
        def corrupt_k6(*args):
            args[4].write_text("{corrupt", encoding="utf-8")
            return 0
        k6.side_effect = corrupt_k6
        with self.assertRaises(cli.RunnerError):
            cli.execute(self._args(dir_c))
        failure_c = self._read_failure(dir_c)
        self.assertEqual("k6", failure_c["phase"])
        self.assertEqual("{corrupt", (dir_c / "k6-summary.json").read_text(encoding="utf-8"))
        self.assertFalse((dir_c / "result.json").exists())

        # Case 4: report diagnostics error
        def healthy_k6(*args):
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return 0
        k6.side_effect = healthy_k6
        api.list_runs.side_effect = [[], cli.ApiError("diagnostics failed")]
        dir_rep_api = self.temp_dir / "report-api-fail"
        with self.assertRaises(cli.ApiError):
            cli.execute(self._args(dir_rep_api))
        failure_rep_api = self._read_failure(dir_rep_api)
        self.assertEqual("report", failure_rep_api["phase"])
        self.assertIn("k6-summary.json", failure_rep_api["available_artifacts"])
        self.assertFalse((dir_rep_api / "result.json").exists())
        self.assertFalse((dir_rep_api / "report.md").exists())

        # Case 5: report formatting error
        api.list_runs.side_effect = None
        api.list_runs.return_value = []
        dir_rep_fmt = self.temp_dir / "report-fmt-fail"
        with patch.object(cli, "render_report", side_effect=RuntimeError("template boom")):
            with self.assertRaises(RuntimeError):
                cli.execute(self._args(dir_rep_fmt))
        failure_rep_fmt = self._read_failure(dir_rep_fmt)
        self.assertEqual("report", failure_rep_fmt["phase"])
        self.assertIn("k6-summary.json", failure_rep_fmt["available_artifacts"])
        self.assertFalse((dir_rep_fmt / "result.json").exists())
        self.assertFalse((dir_rep_fmt / "report.md").exists())

    def test_compound_local_write_failure_preserves_original_cause(self):
        _, k6 = self._enter_runner_mocks()
        k6.side_effect = cli.RunnerError("k6 original cause failure")
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

    def test_secret_redaction_and_dry_run_safety(self):
        raw = (
            "https://alice:s3cr3t@api.example.test/v1 "
            "Authorization: Bearer top-jwt-token "
            "API_SECRET_KEY=raw-secret-value "
            "ACCESS_TOKEN=raw-access-token "
            "password='pw-value' "
            "정상 한국어 진단 메시지 (exit 1)"
        )
        scrubbed = sanitize_message(raw)
        for secret in ("alice:s3cr3t", "top-jwt-token", "raw-secret-value", "raw-access-token", "pw-value"):
            self.assertNotIn(secret, scrubbed)
        self.assertIn("https://***@api.example.test/v1", scrubbed)
        self.assertIn("Bearer ***", scrubbed)
        self.assertIn("정상 한국어 진단 메시지 (exit 1)", scrubbed)

        api, _ = self._enter_runner_mocks(
            env_overrides={
                "API_SECRET_KEY": "super-secret-api-key",
                "SESSION_TOKEN": "super-secret-session-token",
                "PERF_API_TOKEN": "super-secret-perf-token",
            }
        )
        api.create_suite.side_effect = cli.ApiError(
            "failed calling https://admin:secretpw@api.example.test with Bearer my-token-123"
        )
        with self.assertRaises(cli.ApiError):
            cli.execute(self._args(self.result_dir))
        raw_failure_text = (self.result_dir / FAILURE_FILENAME).read_text(encoding="utf-8")
        for secret in (
            "super-secret-api-key",
            "super-secret-session-token",
            "super-secret-perf-token",
            "admin:secretpw",
            "my-token-123",
        ):
            self.assertNotIn(secret, raw_failure_text)

        # --dry-run failure writes no failure.json and creates no directory
        dry_dir = self.temp_dir / "dry-run-fail"
        args = self._args(dry_dir, "--dry-run", scenario_path=self.temp_dir / "missing.yaml")
        with self.assertRaises(ConfigurationError):
            cli.execute(args)
        self.assertFalse(dry_dir.exists())

    def test_main_exit_codes_for_pass_fail_and_error(self):
        _, k6 = self._enter_runner_mocks()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(0, cli.main(self._argv(self.temp_dir / "main-pass")))

        def threshold_fail(*args):
            args[4].write_text(json.dumps(passing_summary()), encoding="utf-8")
            return cli.K6_THRESHOLD_FAILURE_EXIT_CODE

        k6.side_effect = threshold_fail
        fail_dir = self.temp_dir / "main-fail"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(1, cli.main(self._argv(fail_dir)))
        for name in RESULT_FILENAMES:
            self.assertTrue((fail_dir / name).is_file())
        self.assertFalse((fail_dir / FAILURE_FILENAME).exists())

        with (
            patch.object(cli, "execute", side_effect=RuntimeError("token=secret-value boom")),
            redirect_stderr(io.StringIO()) as err,
        ):
            code = cli.main(self._argv(self.temp_dir / "main-error"))
        self.assertEqual(2, code)
        self.assertIn("ERROR: RuntimeError:", err.getvalue())
        self.assertNotIn("secret-value", err.getvalue())

    def test_run_context_record_and_backfill_from_execution_plan(self):
        ctx_plan = RunContext(run_id="run-plan", started_at=datetime.now(timezone.utc))
        ctx_plan.record_execution_plan({
            "connection": {
                "api_url": "https://user:pw@api.example.test",
                "target": {
                    "type": "HTTP_ENDPOINT",
                    "url": "https://alice:secret@target.example.test/v1",
                    "model": "fixture-model",
                    "revision": "rev-9",
                },
            },
            "sources": {
                "scenario": {"path": str(self.scenario_path)},
                "dataset": {"path": str(self.dataset_path)},
            },
            "scenario": {"test": {"id": "FIXTURE-SMOKE", "type": "SMOKE"}},
            "dataset": {"id": "baseline-v1", "test_case_count": 78},
        })
        self.assertEqual("https://***@api.example.test", ctx_plan.inputs["api_url"])
        self.assertEqual("https://***@target.example.test/v1", ctx_plan.inputs["target_url"])
        self.assertEqual("FIXTURE-SMOKE", ctx_plan.inputs["scenario_id"])
        self.assertEqual(78, ctx_plan.inputs["test_case_count"])

        # Backfill missing inputs without overwriting existing inputs
        backfill_dir = self.temp_dir / "backfill-dir"
        backfill_dir.mkdir()
        (backfill_dir / "scenario.yaml").write_text("test: {}", encoding="utf-8")
        (backfill_dir / "execution-plan.json").write_text(
            json.dumps({
                "schema_version": 1,
                "connection": {
                    "api_url": "https://api.example.test",
                    "target": {"url": "https://target.example.test/v1", "model": "from-plan"},
                },
                "scenario": {"test": {"id": "FROM-PLAN", "type": "STRESS"}},
                "dataset": {"id": "two-cases", "test_case_count": 2},
                "sources": {
                    "scenario": {"path": str(self.scenario_path)},
                    "dataset": {"path": str(self.dataset_path)},
                },
            }),
            encoding="utf-8",
        )
        ctx_b = RunContext(
            run_id="run-backfill-1",
            started_at=datetime.now(timezone.utc),
            result_dir=backfill_dir,
            result_dir_owned=True,
            phase="k6",
        )
        ctx_b.record_input("scenario_id", "EXPLICIT-ID")
        failure_path, preservation_errors = preserve_failure(ctx_b, cli.RunnerError("k6 boom"))
        self.assertEqual([], preservation_errors)
        self.assertIsNotNone(failure_path)
        failure_b = self._read_failure(backfill_dir)
        self.assertEqual("EXPLICIT-ID", failure_b["inputs"]["scenario_id"])
        self.assertEqual("STRESS", failure_b["inputs"]["scenario_type"])
        self.assertEqual("from-plan", failure_b["inputs"]["target_model"])
        self.assertIn("scenario.yaml", failure_b["available_artifacts"])
        self.assertIn("execution-plan.json", failure_b["available_artifacts"])


if __name__ == "__main__":
    unittest.main()
