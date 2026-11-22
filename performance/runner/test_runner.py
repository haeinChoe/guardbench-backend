import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from performance.runner.acceptance import evaluate
from performance.runner.cli import K6_THRESHOLD_FAILURE_EXIT_CODE, RunnerError, run_k6


ROOT = Path(__file__).resolve().parents[1]


def acceptance_scenario() -> dict:
    return {
        "acceptance": {
            "api": {"create_latency_ms": {"p50": 1000, "p95": 1000, "p99": 1000}, "error_rate": 0},
            "completion": {"max_seconds": 10, "failure_rate": 0},
        }
    }


def passing_summary() -> dict:
    return {
        "metrics": {
            "test_run_create_latency": {"values": {"p(50)": 1, "p(95)": 1, "p(99)": 1}},
            "test_run_create_errors": {"values": {"rate": 0}},
            "test_run_completion_failures": {"values": {"rate": 0}},
            "test_run_completion_duration": {"values": {"p(95)": 1}},
        }
    }


def k6_scenario() -> dict:
    scenario = acceptance_scenario()
    scenario.update({
        "workload": {
            "concurrent_test_runs": 1,
            "ramp_up_seconds": 1,
            "duration_seconds": 1,
            "completion_timeout_seconds": 1,
            "polling_interval_seconds": 1,
        },
        "target": {"type": "HTTP_ENDPOINT"},
    })
    return scenario


def k6_connection() -> dict:
    return {
        "api_url": "http://localhost",
        "target": {
            "type": "HTTP_ENDPOINT",
            "url": "https://target.example.test/v1/chat/completions",
            "model": "test-model",
            "revision": "rev-1",
        },
    }


class PerformanceRunnerTest(unittest.TestCase):
    def test_acceptance_passes_on_app_level_metrics(self):
        result = evaluate(acceptance_scenario(), passing_summary())

        self.assertEqual("PASS", result["status"])
        self.assertEqual(
            [
                "api.create_latency.p(50)",
                "api.create_latency.p(95)",
                "api.create_latency.p(99)",
                "api.create_error_rate",
                "completion.failure_rate",
                "completion.duration.p95",
                "k6.thresholds",
            ],
            [check["name"] for check in result["checks"]],
        )

    def test_acceptance_fails_on_latency_error_or_completion_breach(self):
        breach_latency = passing_summary()
        breach_latency["metrics"]["test_run_create_latency"]["values"]["p(95)"] = 1500
        self.assertEqual("FAIL", evaluate(acceptance_scenario(), breach_latency)["status"])

        breach_error = passing_summary()
        breach_error["metrics"]["test_run_create_errors"]["values"]["rate"] = 0.05
        self.assertEqual("FAIL", evaluate(acceptance_scenario(), breach_error)["status"])

        breach_duration = passing_summary()
        breach_duration["metrics"]["test_run_completion_duration"]["values"]["p(95)"] = 15
        self.assertEqual("FAIL", evaluate(acceptance_scenario(), breach_duration)["status"])

        breach_failure = passing_summary()
        breach_failure["metrics"]["test_run_completion_failures"]["values"]["rate"] = 0.1
        self.assertEqual("FAIL", evaluate(acceptance_scenario(), breach_failure)["status"])

    def test_acceptance_fails_when_k6_thresholds_fail(self):
        result = evaluate(acceptance_scenario(), passing_summary(), k6_threshold_failed=True)

        self.assertEqual("FAIL", result["status"])
        self.assertIn(
            {"name": "k6.thresholds", "actual": "FAIL", "expected": "PASS", "passed": False},
            result["checks"],
        )

    def test_k6_threshold_exit_preserves_summary_and_passes_connection_env(self):
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "k6-summary.json"
            summary_path.write_text("{}", encoding="utf-8")
            completed = subprocess.CompletedProcess([], K6_THRESHOLD_FAILURE_EXIT_CODE)

            with patch("performance.runner.cli.subprocess.run", return_value=completed) as run:
                exit_code = run_k6(
                    k6_scenario(),
                    k6_connection(),
                    "run-id",
                    1,
                    summary_path,
                    ROOT.parent,
                )

        self.assertEqual(K6_THRESHOLD_FAILURE_EXIT_CODE, exit_code)
        self.assertFalse(run.call_args.kwargs["check"])
        passed_env = run.call_args.kwargs["env"]
        self.assertEqual("http://localhost", passed_env["PERF_BASE_URL"])
        self.assertEqual("https://target.example.test/v1/chat/completions", passed_env["PERF_TARGET_URL"])
        self.assertEqual("test-model", passed_env["PERF_TARGET_MODEL"])
        self.assertEqual("rev-1", passed_env["PERF_TARGET_REVISION"])

    def test_k6_unexpected_exit_is_runner_error(self):
        with tempfile.TemporaryDirectory() as directory:
            summary_path = Path(directory) / "k6-summary.json"
            completed = subprocess.CompletedProcess([], 1)

            with patch("performance.runner.cli.subprocess.run", return_value=completed):
                with self.assertRaises(RunnerError):
                    run_k6(k6_scenario(), k6_connection(), "run-id", 1, summary_path, ROOT.parent)

    def test_k6_version_and_requirements_match_contract(self):
        k6_version = (ROOT / "k6" / "VERSION").read_text(encoding="utf-8").strip()
        requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

        self.assertEqual("0.55.0", k6_version)
        self.assertIn("PyYAML", requirements)


if __name__ == "__main__":
    unittest.main()
