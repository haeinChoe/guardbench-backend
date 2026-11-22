import unittest
from datetime import datetime, timezone
from pathlib import Path

from performance.runner.acceptance import evaluate
from performance.runner.cli import _collect_run_diagnostics, _threshold_inputs

ROOT = Path(__file__).resolve().parents[1]


def scenario() -> dict:
    return {
        "workload": {
            "concurrent_test_runs": 1,
            "ramp_up_seconds": 0,
            "duration_seconds": 10,
            "max_iterations_per_vu": 1,
            "completion_timeout_seconds": 300,
            "polling_interval_seconds": 2,
        },
        "target": {"type": "HTTP_ENDPOINT"},
        "acceptance": {
            "api": {
                "create_latency_ms": {"p50": 1000, "p95": 2000, "p99": 3000},
                "error_rate": 0,
            },
            "completion": {
                "max_seconds": 300,
                "failure_rate": 0,
            },
        },
    }


def connection() -> dict:
    return {
        "api_url": "https://api.example.test",
        "target": {
            "type": "HTTP_ENDPOINT",
            "url": "https://target.example.test/v1/chat/completions",
            "model": "test-model",
            "revision": "target-revision",
        },
    }


class PerformanceDiagnosticsTest(unittest.TestCase):
    def test_acceptance_reads_k6_055_summary_export_shape(self):
        summary = {
            "metrics": {
                "test_run_create_latency": {
                    "avg": 1979.597112,
                    "med": 1979.597112,
                    "p(50)": 1979.597112,
                    "p(90)": 1979.597112,
                    "p(95)": 1979.597112,
                    "p(99)": 1979.597112,
                },
                "test_run_create_errors": {"rate": 0},
                "test_run_completion_failures": {"rate": 0},
                "test_run_completion_duration": {"p(95)": 212.967},
            }
        }

        result = evaluate(scenario(), summary)
        by_name = {check["name"]: check for check in result["checks"]}

        self.assertEqual(1979.597112, by_name["api.create_latency.p(95)"]["actual"])
        self.assertTrue(by_name["api.create_latency.p(95)"]["passed"])
        self.assertEqual(0.0, by_name["api.create_error_rate"]["actual"])
        self.assertEqual(0.0, by_name["completion.failure_rate"]["actual"])
        self.assertEqual(212.967, by_name["completion.duration.p95"]["actual"])
        self.assertTrue(by_name["completion.duration.p95"]["passed"])

    def test_acceptance_reads_rate_value_from_real_smoke_shape(self):
        summary = {
            "metrics": {
                "test_run_create_latency": {
                    "p(50)": 365.577834,
                    "p(95)": 365.577834,
                    "p(99)": 365.577834,
                },
                "test_run_create_errors": {
                    "passes": 0,
                    "fails": 1,
                    "thresholds": {"rate<=0": False},
                    "value": 0,
                },
                "test_run_completion_failures": {
                    "passes": 0,
                    "fails": 1,
                    "thresholds": {"rate<=0": False},
                    "value": 0,
                },
                "test_run_completion_duration": {"p(95)": 201.131},
            }
        }

        result = evaluate(scenario(), summary)
        by_name = {check["name"]: check for check in result["checks"]}

        self.assertEqual(0.0, by_name["api.create_error_rate"]["actual"])
        self.assertTrue(by_name["api.create_error_rate"]["passed"])
        self.assertEqual(0.0, by_name["completion.failure_rate"]["actual"])
        self.assertTrue(by_name["completion.failure_rate"]["passed"])
        self.assertEqual("PASS", result["status"])

    def test_acceptance_keeps_nested_values_fixture_compatibility(self):
        summary = {
            "metrics": {
                "test_run_create_latency": {"values": {"p(50)": 1, "p(95)": 1, "p(99)": 1}},
                "test_run_create_errors": {"values": {"rate": 0}},
                "test_run_completion_failures": {"values": {"rate": 0}},
                "test_run_completion_duration": {"values": {"p(95)": 1}},
            }
        }

        result = evaluate(scenario(), summary)

        self.assertEqual("PASS", result["status"])

    def test_k6_exports_all_acceptance_percentiles(self):
        script = (ROOT / "k6" / "test-run.js").read_text(encoding="utf-8")
        self.assertIn("summaryTrendStats", script)
        self.assertIn("'p(50)'", script)
        self.assertIn("'p(95)'", script)
        self.assertIn("'p(99)'", script)

    def test_threshold_inputs_are_preserved_from_scenario(self):
        inputs = _threshold_inputs(scenario(), connection(), "run-1", 1)
        self.assertEqual("1000", inputs["PERF_API_P50_MS"])
        self.assertEqual("2000", inputs["PERF_API_P95_MS"])
        self.assertEqual("3000", inputs["PERF_API_P99_MS"])
        self.assertEqual("300", inputs["PERF_COMPLETION_MAX_SECONDS"])

    def test_failed_run_diagnostics_preserve_ids_and_error_summary(self):
        class FakeApi:
            def list_runs(self, **kwargs):
                return [
                    {"id": 41, "testSuiteId": 7},
                    {"id": 42, "testSuiteId": 7},
                    {"id": 99, "testSuiteId": 8},
                ]

            def get_run(self, run_id):
                if run_id == 41:
                    return {"id": 41, "status": "FINISHED", "executionOutcome": "COMPLETED"}
                if run_id == 42:
                    return {"id": 42, "status": "FINISHED", "executionOutcome": "ERROR"}
                raise AssertionError("other suite must not be queried")

            def list_run_results(self, run_id):
                if run_id == 42:
                    return [
                        {"error": {"stage": "EVALUATOR", "code": "PROVIDER_TIMEOUT", "message": "safe"}},
                        {"error": {"stage": "EVALUATOR", "code": "PROVIDER_TIMEOUT", "message": "safe"}},
                    ]
                return []

        now = datetime.now(timezone.utc)
        diagnostics = _collect_run_diagnostics(FakeApi(), 7, now, now)

        self.assertEqual(2, diagnostics["test_run_count"])
        self.assertEqual([42], diagnostics["failed_test_run_ids"])
        self.assertEqual({"EVALUATOR:PROVIDER_TIMEOUT": 2}, diagnostics["error_summary"])
        self.assertEqual("ERROR", diagnostics["failed_runs"][0]["executionOutcome"])


if __name__ == "__main__":
    unittest.main()
