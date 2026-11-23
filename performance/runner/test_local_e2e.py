"""Tests for the local E2E harness connecting real Runner and real k6."""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from performance.runner import local_e2e


class LocalE2EHarnessTest(unittest.TestCase):
    def test_missing_k6_exits_with_code_2_and_clear_message(self):
        stderr = io.StringIO()
        with patch.object(local_e2e, "resolve_k6_binary", return_value=None), redirect_stderr(stderr):
            code = local_e2e.main([])
        self.assertEqual(2, code)
        self.assertIn("k6", stderr.getvalue())

    def test_server_and_temp_dir_clean_up_on_scenario_failure(self):
        captured: dict[str, object] = {}

        def failing_scenario(scenario_path, dataset_path, result_dir):
            with local_e2e.LocalMockApiServer() as server:
                captured["server"] = server
                captured["result_dir"] = result_dir
                result_dir.mkdir(parents=True, exist_ok=True)
                self.assertTrue(server.is_running)
                raise RuntimeError("simulated harness failure")

        outcome = local_e2e._execute_scenario(
            "failure_cleanup",
            failing_scenario,
            local_e2e.DEFAULT_SCENARIO,
            local_e2e.DEFAULT_DATASET,
        )
        self.assertFalse(outcome.passed)
        self.assertIn("simulated harness failure", outcome.detail)
        self.assertFalse(captured["server"].is_running)
        self.assertFalse(outcome.work_dir.exists())

    def test_k6_iteration_limit_rejects_idle_vu_busy_loop(self):
        workload = {"concurrent_test_runs": 2, "ramp_up_seconds": 0, "duration_seconds": 1}
        limit = local_e2e.max_expected_k6_iterations(workload, started=2)
        self.assertEqual(8, limit)
        self.assertLessEqual(4, limit)
        self.assertGreater(597935, limit)
        staged = {"concurrent_test_runs": 2, "stages": [{"duration_seconds": 1}, {"duration_seconds": 2}]}
        self.assertEqual(2 + 2 * (3 + 2), local_e2e.max_expected_k6_iterations(staged, started=2))

    @unittest.skipUnless(local_e2e.resolve_k6_binary(), "actual k6 binary is not installed")
    def test_local_e2e_harness_executes_all_three_scenarios_with_real_k6(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code, outcomes = local_e2e.run_harness()

        output = stdout.getvalue()
        self.assertEqual(0, exit_code, output + stderr.getvalue())
        self.assertEqual(["normal_completion", "measurement_fail", "execution_error"], [o.name for o in outcomes])
        for outcome in outcomes:
            self.assertTrue(outcome.passed, outcome.detail)
            self.assertFalse(outcome.work_dir.exists(), f"temporary directory lingered: {outcome.work_dir}")
            self.assertIn(f"[PASS] {outcome.name}:", output)
        self.assertEqual([], local_e2e.list_child_k6_pids())
        for expected_line in (
            "실제 실행 구성 요소",
            "테스트 대역 범위",
            "실제 성능 Baseline 검증이 아닙니다",
            "Summary: 3/3 scenarios passed",
        ):
            self.assertIn(expected_line, output)


if __name__ == "__main__":
    unittest.main()
