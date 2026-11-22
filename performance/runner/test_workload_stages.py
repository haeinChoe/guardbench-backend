"""Configuration and actual k6 checks for workload stages."""
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
from performance.runner.config import ConfigurationError, load_yaml, validate_scenario
from performance.runner.plan import load_execution_plan
from performance.runner.test_runner import acceptance_scenario, k6_connection, k6_scenario, passing_summary


ROOT = Path(__file__).resolve().parents[1]
K6 = shutil.which(os.environ.get("K6_BIN", "k6"))


def staged_scenario(name="staged-stress"):
    scenario = load_yaml(ROOT / f"fixtures/profiles/{name}.yaml")
    validate_scenario(scenario)
    return scenario


def staged_passing_summary():
    summary = copy.deepcopy(passing_summary())
    summary["metrics"]["test_run_iterations_started"] = {"values": {"count": 1}}
    summary["metrics"]["test_run_iterations_completed"] = {"values": {"count": 1}}
    return summary


def iteration_check(summary):
    result = evaluate(staged_scenario(), summary)
    return result, next(check for check in result["checks"] if check["name"] == "completion.iterations")


class WorkloadStagesTest(unittest.TestCase):
    def test_stress_fixture_preserves_declared_curve_and_acceptance(self):
        scenario = staged_scenario("staged-stress")
        connection = k6_connection()
        original = copy.deepcopy(scenario)
        environment = _k6_environment(scenario, connection, "fixture", 1)
        self.assertEqual(scenario["workload"]["stages"], json.loads(environment["PERF_STAGES_JSON"]))
        self.assertNotIn("PERF_RAMP_UP_SECONDS", environment)
        self.assertNotIn("PERF_DURATION_SECONDS", environment)
        self.assertEqual(
            max(stage["concurrent_test_runs"] for stage in scenario["workload"]["stages"]),
            scenario["workload"]["concurrent_test_runs"],
        )
        self.assertEqual(original, scenario)

    def test_invalid_stages_fail_before_k6(self):
        invalid = [
            None, {}, [], [None], [{}],
            [{"duration_seconds": 1, "concurrent_test_runs": 0}],
            [{"duration_seconds": 1, "concurrent_test_runs": 4}],
            [{"duration_seconds": 1, "concurrent_test_runs": 1, "unknown": 1}],
        ]
        for duration in (0, -1, True, "1", None, float("nan"), float("inf"), -float("inf")):
            invalid.append([{"duration_seconds": duration, "concurrent_test_runs": 2}])
        for target in (-1, True, 1.5, "2", None, float("nan"), float("inf")):
            invalid.append([{"duration_seconds": 1, "concurrent_test_runs": target}])
        for stages in invalid:
            with self.subTest(stages=stages):
                scenario = staged_scenario()
                scenario["workload"]["stages"] = stages
                with self.assertRaises(ConfigurationError):
                    validate_scenario(scenario)
        for field in ("ramp_up_seconds", "duration_seconds"):
            with self.subTest(mixed_field=field):
                scenario = staged_scenario()
                scenario["workload"][field] = 1
                with self.assertRaises(ConfigurationError):
                    validate_scenario(scenario)

    def test_ramp_up_workload_is_not_overridden_by_inherited_stages(self):
        scenario = staged_scenario()
        del scenario["workload"]["stages"]
        scenario["workload"].update(ramp_up_seconds=0, duration_seconds=1)
        validate_scenario(scenario)
        with tempfile.TemporaryDirectory() as directory:
            summary = Path(directory) / "summary.json"
            summary.write_text("{}")
            with patch.dict(os.environ, {"PERF_STAGES_JSON": "invalid"}), patch(
                "performance.runner.cli.subprocess.run", return_value=subprocess.CompletedProcess([], 0)
            ) as run:
                run_k6(scenario, k6_connection(), "ramp-up", 1, summary, ROOT.parent)
            self.assertNotIn("PERF_STAGES_JSON", run.call_args.kwargs["env"])
            self.assertEqual("1", run.call_args.kwargs["env"]["PERF_DURATION_SECONDS"])

    def test_interrupted_missing_empty_or_inconsistent_iteration_counts_fail_closed(self):
        for started, completed in (
            (2, 1), (None, 1), (1, None), (0, 0), (1, 2), (-1, -1),
            (float("nan"), 1), (1, float("inf")), (1.5, 1.5), (True, True),
        ):
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

    def test_ramp_up_scenario_skips_completion_iterations_check(self):
        for label, scenario in (("acceptance_only", acceptance_scenario()), ("ramp_up_duration", k6_scenario())):
            with self.subTest(label=label):
                result = evaluate(scenario, passing_summary())
                self.assertEqual("PASS", result["status"])
                self.assertNotIn("completion.iterations", [check["name"] for check in result["checks"]])

    def test_execution_plan_supports_single_target_and_staged_scenarios(self):
        dataset_path = (ROOT / "fixtures/datasets/two-cases.yaml").resolve()
        plans = {
            name: load_execution_plan(
                api_url="https://api.example.test",
                target_url="https://target.example.test/v1/chat/completions",
                target_model="fixture-model",
                scenario_path=(ROOT / f"fixtures/profiles/{name}.yaml").resolve(),
                dataset_path=dataset_path,
            ).to_dict()
            for name in ("smoke", "staged-stress")
        }

        single_plan = plans["smoke"]
        stress_plan = plans["staged-stress"]
        self.assertEqual(single_plan["connection"], stress_plan["connection"])
        self.assertNotIn("stages", single_plan["scenario"]["workload"])
        self.assertEqual(5, len(stress_plan["scenario"]["workload"]["stages"]))
        self.assertEqual(3, stress_plan["scenario"]["workload"]["concurrent_test_runs"])

        with tempfile.TemporaryDirectory() as directory:
            bad_scenario_path = Path(directory) / "bad-staged-scenario.yaml"
            bad_scenario = staged_scenario("staged-stress")
            bad_scenario["workload"]["stages"] = [{"duration_seconds": 0, "concurrent_test_runs": 2}]
            bad_scenario_path.write_text(yaml.safe_dump(bad_scenario), encoding="utf-8")
            with self.assertRaises(ConfigurationError):
                load_execution_plan(
                    api_url="https://api.example.test",
                    target_url="https://target.example.test/v1/chat/completions",
                    target_model="fixture-model",
                    scenario_path=bad_scenario_path,
                    dataset_path=dataset_path,
                )

    def test_acceptance_threshold_breach_fails_without_mutating_scenario(self):
        scenario = staged_scenario("staged-stress")
        snapshot = copy.deepcopy(scenario)
        summary = staged_passing_summary()
        failed_by_k6 = evaluate(scenario, summary, k6_threshold_failed=True)
        self.assertEqual("FAIL", failed_by_k6["status"])
        self.assertEqual(snapshot, scenario)

        breached_summary = copy.deepcopy(summary)
        breached_summary["metrics"]["test_run_create_latency"]["values"]["p(95)"] = 9999
        failed_by_latency = evaluate(scenario, breached_summary)
        self.assertEqual("FAIL", failed_by_latency["status"])
        self.assertEqual(snapshot, scenario)


@unittest.skipUnless(K6, "actual k6 binary is not installed")
class ActualK6StagesTest(unittest.TestCase):
    def test_inspect_uses_exact_fixture_stages_and_grace(self):
        scenario = staged_scenario("staged-stress")
        environment = self._environment(scenario)
        result = subprocess.run(
            [K6, "inspect", "--include-system-env-vars", str(ROOT / "k6/test-run.js")],
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        inspected = json.loads(result.stdout)
        k6_scenario_cfg = inspected["scenarios"]["workload"]
        self.assertEqual(
            [
                {"duration": f"{stage['duration_seconds']}s", "target": stage["concurrent_test_runs"]}
                for stage in scenario["workload"]["stages"]
            ],
            k6_scenario_cfg["stages"],
        )
        expected_grace = (
            f"{scenario['workload']['completion_timeout_seconds'] + max(1, math.ceil(scenario['workload']['polling_interval_seconds']))}s"
        )
        self.assertEqual(expected_grace, k6_scenario_cfg["gracefulRampDown"])
        self.assertEqual(expected_grace, k6_scenario_cfg["gracefulStop"])
        api_acc = scenario["acceptance"]["api"]
        comp_acc = scenario["acceptance"]["completion"]
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
    def _environment(scenario, base_url="http://localhost"):
        environment = dict(os.environ)
        for name in ("PERF_STAGES_JSON", "PERF_RAMP_UP_SECONDS", "PERF_DURATION_SECONDS"):
            environment.pop(name, None)
        connection = k6_connection()
        connection["api_url"] = base_url
        environment.update(_k6_environment(scenario, connection, "local-k6", 1))
        environment.update(K6_NO_USAGE_REPORT="true", K6_WEB_DASHBOARD="false")
        return environment

    def _run_local_api(self, delay, *, status="FINISHED", polling_interval=None, failure_rate=0):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status_code, data):
                body = json.dumps({"data": data}).encode()
                self.send_response(status_code)
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
            scenario = staged_scenario()
            scenario["workload"].update(
                concurrent_test_runs=1,
                max_iterations_per_vu=1,
                completion_timeout_seconds=1,
                stages=[
                    {"duration_seconds": 0.1, "concurrent_test_runs": 1},
                    {"duration_seconds": 0.1, "concurrent_test_runs": 0},
                ],
            )
            if polling_interval is not None:
                scenario["workload"]["polling_interval_seconds"] = polling_interval
            scenario["acceptance"]["completion"]["failure_rate"] = failure_rate
            validate_scenario(scenario)
            environment = self._environment(scenario, f"http://127.0.0.1:{server.server_port}")
            with tempfile.TemporaryDirectory() as directory:
                summary_path = Path(directory) / "summary.json"
                result = subprocess.run(
                    [K6, "run", "--quiet", "--address", "", "--summary-export", str(summary_path), str(ROOT / "k6/test-run.js")],
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
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
        scenario = staged_scenario()
        scenario["acceptance"]["completion"]["failure_rate"] = 1.0
        result = evaluate(scenario, summary)
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
