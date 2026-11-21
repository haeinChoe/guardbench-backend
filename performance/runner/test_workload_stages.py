"""Configuration and actual k6 checks for fixed-infrastructure workload stages."""
import copy
import json
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import yaml

from performance.runner.acceptance import evaluate
from performance.runner.cli import _k6_environment, run_k6
from performance.runner.config import ConfigurationError, load_yaml, validate_profile
from performance.runner.plan import load_execution_plan
from performance.runner.test_runner import acceptance_profile, k6_profile, passing_summary


ROOT = Path(__file__).resolve().parents[1]
K6 = shutil.which(os.environ.get("K6_BIN", "k6"))


def staged_profile(name="staged-operating"):
    profile = load_yaml(ROOT / f"fixtures/profiles/{name}.yaml")
    validate_profile(profile)
    return profile


def staged_passing_summary():
    summary = copy.deepcopy(passing_summary())
    summary["metrics"]["test_run_iterations_started"] = {"values": {"count": 1}}
    summary["metrics"]["test_run_iterations_completed"] = {"values": {"count": 1}}
    return summary


def iteration_check(summary):
    metrics = {"status": "COLLECTED", "metrics": [
        {"id": prefix, "values": [1]} for prefix in ("ecs_", "sqs_", "rds_", "sagemaker_")
    ]}
    result = evaluate(staged_profile(), summary, {"passed": True, "duration_seconds": 0}, [], metrics)
    return result, next(check for check in result["checks"] if check["name"] == "completion.iterations")


class WorkloadStagesTest(unittest.TestCase):
    def test_operating_and_stress_fixtures_preserve_declared_curve_and_acceptance(self):
        for name in ("staged-operating", "staged-stress"):
            with self.subTest(name=name):
                profile = staged_profile(name)
                original = copy.deepcopy(profile)
                environment = _k6_environment(profile, "fixture", 1, "http://localhost")
                self.assertEqual(profile["workload"]["stages"], json.loads(environment["PERF_STAGES_JSON"]))
                self.assertNotIn("PERF_RAMP_UP_SECONDS", environment)
                self.assertNotIn("PERF_DURATION_SECONDS", environment)
                self.assertEqual(max(stage["concurrent_test_runs"] for stage in profile["workload"]["stages"]),
                                 profile["workload"]["concurrent_test_runs"])
                self.assertEqual(original, profile)

    def test_invalid_stages_fail_before_k6(self):
        invalid = [None, {}, [], [None], [{}],
                   [{"duration_seconds": 1, "concurrent_test_runs": 0}],
                   [{"duration_seconds": 1, "concurrent_test_runs": 3}],
                   [{"duration_seconds": 1, "concurrent_test_runs": 1, "unknown": 1}]]
        for duration in (0, -1, True, "1", None, float("nan"), float("inf"), -float("inf")):
            invalid.append([{"duration_seconds": duration, "concurrent_test_runs": 2}])
        for target in (-1, True, 1.5, "2", None, float("nan"), float("inf")):
            invalid.append([{"duration_seconds": 1, "concurrent_test_runs": target}])
        for stages in invalid:
            with self.subTest(stages=stages):
                profile = staged_profile()
                profile["workload"]["stages"] = stages
                with self.assertRaises(ConfigurationError):
                    validate_profile(profile)
        for field in ("ramp_up_seconds", "duration_seconds"):
            with self.subTest(mixed_field=field):
                profile = staged_profile()
                profile["workload"][field] = 1
                with self.assertRaises(ConfigurationError):
                    validate_profile(profile)

    def test_legacy_workload_is_not_overridden_by_inherited_stages(self):
        profile = staged_profile()
        del profile["workload"]["stages"]
        profile["workload"].update(ramp_up_seconds=0, duration_seconds=1)
        validate_profile(profile)
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.json"
            summary.write_text("{}")
            with patch.dict(os.environ, {"PERF_STAGES_JSON": "invalid"}), patch(
                    "performance.runner.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)) as run:
                run_k6(profile, "legacy", 1, "http://localhost", summary, ROOT.parent)
            self.assertNotIn("PERF_STAGES_JSON", run.call_args.kwargs["env"])
            self.assertEqual("1", run.call_args.kwargs["env"]["PERF_DURATION_SECONDS"])

    def test_interrupted_missing_empty_or_inconsistent_iteration_counts_fail_closed(self):
        for started, completed in ((2, 1), (None, 1), (1, None), (0, 0), (1, 2), (-1, -1),
                                   (float("nan"), 1), (1, float("inf")), (1.5, 1.5), (True, True)):
            with self.subTest(started=started, completed=completed):
                summary = staged_passing_summary()
                summary["metrics"]["test_run_iterations_started"] = {"count": started}
                summary["metrics"]["test_run_iterations_completed"] = {"count": completed}
                result, check = iteration_check(summary)
                self.assertFalse(check["passed"])
                self.assertEqual("FAIL", result["status"])
        summary = staged_passing_summary()
        summary["metrics"]["test_run_iterations_started"] = {"count": 2}
        result, check = iteration_check(summary)
        self.assertEqual(1, check["actual"]["interrupted"])

        summary_int = staged_passing_summary()
        summary_int["metrics"]["test_run_iterations_started"] = {"count": 2}
        summary_int["metrics"]["test_run_iterations_completed"] = {"count": 1}
        _, check_int = iteration_check(summary_int)
        self.assertIs(type(check_int["actual"]["started"]), int)
        self.assertIs(type(check_int["actual"]["completed"]), int)
        self.assertIs(type(check_int["actual"]["interrupted"]), int)
        self.assertEqual({"started": 2, "completed": 1, "interrupted": 1}, check_int["actual"])

    def test_complete_iterations_pass_for_current_and_nested_summary_shapes(self):
        for nested in (True, False):
            summary = staged_passing_summary()
            for name in ("test_run_iterations_started", "test_run_iterations_completed"):
                summary["metrics"][name] = {"values": {"count": 2}} if nested else {"count": 2}
            result, check = iteration_check(summary)
            self.assertTrue(check["passed"])
            self.assertEqual("PASS", result["status"])

    def test_legacy_profile_skips_completion_iterations_check(self):
        metrics = {"status": "COLLECTED", "metrics": [
            {"id": prefix, "values": [1]} for prefix in ("ecs_", "sqs_", "rds_", "sagemaker_")
        ]}
        for label, profile in (("acceptance_only", acceptance_profile()), ("ramp_up_duration", k6_profile())):
            with self.subTest(label=label):
                result = evaluate(profile, passing_summary(), {"passed": True, "duration_seconds": 0}, [], metrics)
                self.assertEqual("PASS", result["status"])
                self.assertNotIn("completion.iterations", [check["name"] for check in result["checks"]])

    def test_fixed_infrastructure_manifest_supports_operating_and_stress_stage_profiles(self):
        base_manifest = yaml.safe_load(
            (ROOT / "fixtures/experiments/mock-small-operating.yaml").read_text(encoding="utf-8")
        )
        dataset_path = str((ROOT / "fixtures/datasets/mock-two-cases.yaml").resolve())
        with tempfile.TemporaryDirectory() as directory:
            tmp = Path(directory)
            plans = {}
            for name in ("staged-operating", "staged-stress"):
                manifest = copy.deepcopy(base_manifest)
                manifest["id"] = f"fixture-{name}"
                manifest["profile"] = str((ROOT / f"fixtures/profiles/{name}.yaml").resolve())
                manifest["dataset"] = dataset_path
                manifest_path = tmp / f"{name}-experiment.yaml"
                manifest_path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
                plans[name] = load_execution_plan(manifest_path).to_dict()

            op_plan = plans["staged-operating"]
            stress_plan = plans["staged-stress"]
            self.assertEqual(op_plan["infrastructure"], stress_plan["infrastructure"])
            self.assertEqual(4, len(op_plan["profile"]["workload"]["stages"]))
            self.assertEqual(2, op_plan["profile"]["workload"]["concurrent_test_runs"])
            self.assertEqual(5, len(stress_plan["profile"]["workload"]["stages"]))
            self.assertEqual(3, stress_plan["profile"]["workload"]["concurrent_test_runs"])

            bad_profile_path = tmp / "bad-staged-profile.yaml"
            bad_profile = staged_profile("staged-operating")
            bad_profile["workload"]["stages"] = [{"duration_seconds": 0, "concurrent_test_runs": 2}]
            bad_profile_path.write_text(yaml.safe_dump(bad_profile), encoding="utf-8")
            bad_manifest = copy.deepcopy(base_manifest)
            bad_manifest["profile"] = str(bad_profile_path)
            bad_manifest["dataset"] = dataset_path
            bad_manifest_path = tmp / "bad-experiment.yaml"
            bad_manifest_path.write_text(yaml.safe_dump(bad_manifest), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_execution_plan(bad_manifest_path)

    def test_acceptance_threshold_breach_fails_without_mutating_profile(self):
        profile = staged_profile("staged-stress")
        snapshot = copy.deepcopy(profile)
        metrics = {"status": "COLLECTED", "metrics": [
            {"id": prefix, "values": [1]} for prefix in ("ecs_", "sqs_", "rds_", "sagemaker_")
        ]}
        summary = staged_passing_summary()
        failed_by_k6 = evaluate(
            profile, summary, {"passed": True, "duration_seconds": 0}, [], metrics,
            k6_threshold_failed=True,
        )
        self.assertEqual("FAIL", failed_by_k6["status"])
        self.assertEqual(snapshot, profile)

        breached_summary = copy.deepcopy(summary)
        breached_summary["metrics"]["test_run_create_latency"]["values"]["p(95)"] = 9999
        failed_by_latency = evaluate(
            profile, breached_summary, {"passed": True, "duration_seconds": 0}, [], metrics,
        )
        self.assertEqual("FAIL", failed_by_latency["status"])
        self.assertEqual(snapshot, profile)


@unittest.skipUnless(K6, "actual k6 binary is not installed")
class ActualK6StagesTest(unittest.TestCase):
    def test_inspect_uses_exact_fixture_stages_and_grace(self):
        for name in ("staged-operating", "staged-stress"):
            with self.subTest(name=name):
                profile = staged_profile(name)
                environment = self._environment(profile)
                result = subprocess.run([K6, "inspect", "--include-system-env-vars", str(ROOT / "k6/test-run.js")], env=environment,
                                        capture_output=True, text=True, timeout=15, check=True)
                inspected = json.loads(result.stdout)
                scenario = inspected["scenarios"]["workload"]
                self.assertEqual([{"duration": f"{stage['duration_seconds']}s", "target": stage["concurrent_test_runs"]}
                                  for stage in profile["workload"]["stages"]], scenario["stages"])
                expected_grace = (
                    f"{profile['workload']['completion_timeout_seconds'] + max(1, math.ceil(profile['workload']['polling_interval_seconds']))}s"
                )
                self.assertEqual(expected_grace, scenario["gracefulRampDown"])
                self.assertEqual(expected_grace, scenario["gracefulStop"])
                api_acc = profile["acceptance"]["api"]
                comp_acc = profile["acceptance"]["completion"]
                expected_thresholds = {
                    "test_run_create_latency": [
                        f"p(50)<{api_acc['create_latency_ms']['p50']}",
                        f"p(95)<{api_acc['create_latency_ms']['p95']}",
                        f"p(99)<{api_acc['create_latency_ms']['p99']}",
                    ],
                    "test_run_create_errors": [f"rate<={api_acc['error_rate']}"],
                    "test_run_completion_failures": [f"rate<={comp_acc['failure_rate']}"],
                    "test_run_completion_duration": [f"p(95)<={comp_acc['max_seconds']}"],
                }
                self.assertEqual(expected_thresholds, inspected["thresholds"])

    @staticmethod
    def _environment(profile, base_url="http://localhost"):
        environment = dict(os.environ)
        for name in ("PERF_STAGES_JSON", "PERF_RAMP_UP_SECONDS", "PERF_DURATION_SECONDS"):
            environment.pop(name, None)
        environment.update(_k6_environment(profile, "local-k6", 1, base_url))
        environment.update(K6_NO_USAGE_REPORT="true", K6_WEB_DASHBOARD="false")
        return environment

    def _run_local_api(self, delay, *, status="FINISHED", polling_interval=None, failure_rate=0):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, data):
                body = json.dumps({"data": data}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                requests.append((self.path, body, self.headers.get("Idempotency-Key")))
                self.respond(202, {"id": len(requests)})

            def do_GET(self):
                time.sleep(delay)
                payload = {"status": status}
                if status == "FINISHED":
                    payload["executionOutcome"] = "COMPLETED"
                self.respond(200, payload)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            profile = staged_profile()
            profile["workload"].update(concurrent_test_runs=1, max_iterations_per_vu=1,
                                       completion_timeout_seconds=1, stages=[
                                           {"duration_seconds": 0.1, "concurrent_test_runs": 1},
                                           {"duration_seconds": 0.1, "concurrent_test_runs": 0},
                                       ])
            if polling_interval is not None:
                profile["workload"]["polling_interval_seconds"] = polling_interval
            profile["acceptance"]["completion"]["failure_rate"] = failure_rate
            validate_profile(profile)
            environment = self._environment(profile, f"http://127.0.0.1:{server.server_port}")
            with tempfile.TemporaryDirectory() as directory:
                summary_path = Path(directory) / "summary.json"
                result = subprocess.run([K6, "run", "--quiet", "--address", "", "--summary-export",
                                         str(summary_path), str(ROOT / "k6/test-run.js")],
                                        env=environment, capture_output=True, text=True, timeout=15)
                self.assertIn(result.returncode, (0, 99), result.stdout + result.stderr)
                summary = json.loads(summary_path.read_text())
            self.assertTrue(requests, result.stdout + result.stderr)
            self.assertEqual("/api/v1/test-runs", requests[0][0])
            self.assertEqual(1, requests[0][1]["testSuiteId"])
            self.assertTrue(requests[0][2].startswith("perf-local-k6-"))
            return summary
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_ramp_down_allows_in_flight_completion(self):
        summary = self._run_local_api(delay=0.3)
        result, check = iteration_check(summary)
        self.assertTrue(check["passed"], check)
        self.assertGreater(check["actual"]["completed"], 0)
        self.assertEqual(0, check["actual"]["interrupted"])
        self.assertEqual("PASS", result["status"], result)

    def test_timeout_during_ramp_down_records_completion_failure_not_interruption(self):
        summary = self._run_local_api(delay=0.02, status="RUNNING", polling_interval=0.3, failure_rate=1.0)
        profile = staged_profile()
        profile["acceptance"]["completion"]["failure_rate"] = 1.0
        metrics = {"status": "COLLECTED", "metrics": [
            {"id": prefix, "values": [1]} for prefix in ("ecs_", "sqs_", "rds_", "sagemaker_")
        ]}
        result = evaluate(profile, summary, {"passed": True, "duration_seconds": 0}, [], metrics)
        by_name = {check["name"]: check for check in result["checks"]}
        self.assertTrue(by_name["completion.iterations"]["passed"], by_name["completion.iterations"])
        self.assertGreater(by_name["completion.iterations"]["actual"]["completed"], 0)
        self.assertEqual(0, by_name["completion.iterations"]["actual"]["interrupted"])
        self.assertTrue(by_name["completion.failure_rate"]["passed"], by_name["completion.failure_rate"])
        self.assertEqual(1.0, by_name["completion.failure_rate"]["actual"])

    def test_forced_interruption_cannot_pass_completion_acceptance(self):
        summary = self._run_local_api(delay=3)
        result, check = iteration_check(summary)
        self.assertFalse(check["passed"], check)
        self.assertGreater(check["actual"]["interrupted"], 0)
        self.assertEqual("FAIL", result["status"])


if __name__ == "__main__":
    unittest.main()
