"""Unit coverage for the bounded static local-read batch tool."""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from home_mcp_gateway import local_plan


class LocalReadPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.text_path = self.root / "notes.txt"
        self.text_path.write_text("needle\nsecond line\n", encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def step(operation, **args):
        return {"operation": operation, "args": args}

    def test_unknown_and_dangerous_operations_are_rejected(self):
        for operation in (
            "run_command", "run_python", "write_text", "http_request", "system_info",
            "job_status", "job_tail", "unknown",
        ):
            with self.subTest(operation=operation), self.assertRaises(local_plan.LocalReadPlanValidationError):
                local_plan.local_read_plan([self.step(operation, command="echo unsafe")])

    def test_all_steps_are_validated_before_any_execution(self):
        called = Mock(return_value={"exists": True})
        with patch.object(local_plan, "_DISPATCH", {"path_info": called}):
            with self.assertRaises(local_plan.LocalReadPlanValidationError):
                local_plan.local_read_plan([
                    self.step("path_info", path=str(self.text_path)),
                    self.step("run_command", command="echo must-not-run"),
                ])
        called.assert_not_called()

    def test_step_limit_has_default_and_hard_bound(self):
        steps = [self.step("path_info", path=str(self.text_path)) for _ in range(local_plan.DEFAULT_MAX_STEPS + 1)]
        with self.assertRaises(local_plan.LocalReadPlanValidationError):
            local_plan.local_read_plan(steps)

        result = local_plan.local_read_plan(
            [self.step("path_info", path=str(self.text_path)) for _ in range(local_plan.HARD_MAX_STEPS)],
            max_steps=local_plan.HARD_MAX_STEPS,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps_completed"], local_plan.HARD_MAX_STEPS)

    def test_success_runs_multiple_read_only_steps_in_order(self):
        result = local_plan.local_read_plan([
            self.step("path_info", path=str(self.text_path)),
            self.step("read_text", path=str(self.text_path), max_chars=128),
            self.step(
                "search_text",
                root=str(self.root),
                query="needle",
                file_glob="*.txt",
                max_results=4,
                max_files=20,
                timeout_sec=2,
            ),
            self.step(
                "glob_paths",
                pattern=str(self.root / "*.txt"),
                recursive=False,
                max_results=4,
                timeout_sec=2,
            ),
        ])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["steps_completed"], 4)
        self.assertEqual([item["operation"] for item in result["steps"]], [
            "path_info", "read_text", "search_text", "glob_paths",
        ])
        self.assertIn("needle", result["steps"][1]["result"]["text"])

    def test_failure_stops_and_retains_completed_prefix(self):
        first = Mock(return_value={"exists": True})
        second = Mock(side_effect=RuntimeError("SECRET_EXCEPTION_ARGUMENT"))
        third = Mock(return_value={"matches": []})
        dispatch = {"path_info": first, "read_text": second, "glob_paths": third}
        with patch.object(local_plan, "_DISPATCH", dispatch):
            result = local_plan.local_read_plan([
                self.step("path_info", path=str(self.text_path)),
                self.step("read_text", path=str(self.text_path), max_chars=32),
                self.step("glob_paths", pattern=str(self.root / "*"), max_results=2),
            ])
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["failed_step"], 2)
        self.assertEqual(result["steps_completed"], 1)
        self.assertEqual(len(result["steps"]), 2)
        self.assertEqual(result["steps"][0]["status"], "completed")
        self.assertEqual(result["steps"][1]["error"], "step_failed")
        third.assert_not_called()
        self.assertNotIn("SECRET_EXCEPTION_ARGUMENT", json.dumps(result))

    def test_search_timeout_is_propagated_and_native_timeout_result_stops_plan(self):
        search = Mock(return_value={"timed_out": True, "partial": True, "results": []})
        later = Mock(return_value={"exists": True})
        with patch.object(local_plan.core, "search_text", search), patch.object(
            local_plan, "_DISPATCH", {"search_text": local_plan._invoke_search_text, "path_info": later}
        ):
            result = local_plan.local_read_plan([
                self.step(
                    "search_text",
                    root=str(self.root),
                    query="needle",
                    max_results=4,
                    max_files=20,
                    timeout_sec=12,
                ),
                self.step("path_info", path=str(self.text_path)),
            ], step_timeout_sec=1.25)
        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["timeout_sec"], 1.25)
        self.assertEqual(search.call_args.kwargs["max_files"], 20)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["reason"], "step_timeout")
        later.assert_not_called()

    def test_search_and_glob_receive_remaining_total_budget(self):
        search = Mock(return_value={"results": []})
        glob = Mock(return_value={"matches": []})
        with patch.object(local_plan.core, "search_text", search), patch.object(local_plan.core, "glob_paths", glob):
            result = local_plan.local_read_plan([
                self.step("search_text", root=str(self.root), query="needle", max_results=2, max_files=10),
                self.step("glob_paths", pattern=str(self.root / "*.txt"), max_results=2),
            ], total_timeout_sec=0.1, step_timeout_sec=5)
        self.assertEqual(result["status"], "completed")
        self.assertLessEqual(search.call_args.kwargs["timeout_sec"], 0.1)
        self.assertLessEqual(glob.call_args.kwargs["timeout_sec"], 0.1)

    def test_output_cap_marks_current_result_without_dropping_prefix(self):
        self.text_path.write_text("x" * 8_000, encoding="utf-8")
        cap = 512
        result = local_plan.local_read_plan([
            self.step("path_info", path=str(self.text_path)),
            self.step("read_text", path=str(self.text_path), max_chars=8_000),
        ], max_result_bytes=cap)
        self.assertTrue(result["output_truncated"])
        self.assertGreaterEqual(len(result["steps"]), 1)
        self.assertLessEqual(local_plan._json_size(result), cap)
        self.assertEqual(result["output_bytes"], local_plan._json_size(result))
        self.assertLessEqual(result["steps_completed"], len(result["steps"]))
        self.assertEqual(result["steps"][0]["operation"], "path_info")

    def test_result_cap_has_a_minimum_and_environment_reads_are_specific(self):
        with self.assertRaises(local_plan.LocalReadPlanValidationError):
            local_plan.local_read_plan(
                [self.step("path_info", path=str(self.text_path))],
                max_result_bytes=local_plan.MIN_MAX_RESULT_BYTES - 1,
            )
        with self.assertRaises(local_plan.LocalReadPlanValidationError):
            local_plan.local_read_plan([self.step("read_text", path=str(self.text_path))])
        with self.assertRaises(local_plan.LocalReadPlanValidationError):
            local_plan.local_read_plan([self.step("read_text", path=str(self.text_path), max_chars=0)])
        with self.assertRaises(local_plan.LocalReadPlanValidationError):
            local_plan.local_read_plan([self.step("get_environment", name="")])

        result = local_plan.local_read_plan([
            self.step("get_environment", name="PATH"),
        ])
        self.assertEqual(result["status"], "completed")

    def test_streaming_read_stops_on_cooperative_deadline(self):
        self.text_path.write_text("x" * 100_000, encoding="utf-8")
        real_monotonic = time.monotonic
        ticks = iter([10.0, 10.0, 10.0, 10.2])
        with patch.object(local_plan.time, "monotonic", side_effect=lambda: next(ticks, 10.2)):
            started = real_monotonic()
            result = local_plan.local_read_plan(
                [self.step("read_text", path=str(self.text_path), max_chars=local_plan.MAX_READ_CHARS)],
                step_timeout_sec=0.1,
            )
            elapsed = real_monotonic() - started
        self.assertLess(elapsed, 1.0)
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["reason"], "step_timeout")

    def test_synthetic_huge_result_is_bounded_before_json_serialization(self):
        huge = {"text": "x" * 5_000_000}
        fake = Mock(return_value=huge)
        with patch.object(local_plan, "_DISPATCH", {"path_info": fake}):
            result = local_plan.local_read_plan(
                [self.step("path_info", path=str(self.text_path))],
                max_result_bytes=local_plan.MIN_MAX_RESULT_BYTES,
            )
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["steps"][0]["result_truncated"])
        self.assertLessEqual(len(result["steps"][0]["result"]["text"]), local_plan.MIN_MAX_RESULT_BYTES // 4)
        self.assertEqual(result["output_bytes"], local_plan._json_size(result))

    def test_deadline_is_rechecked_after_result_conversion(self):
        fake = Mock(return_value={"text": "bounded"})
        ticks = iter([10.0, 10.0, 10.0, 10.01, 10.2])
        with patch.object(local_plan, "_DISPATCH", {"path_info": fake}), patch.object(
            local_plan.time, "monotonic", side_effect=lambda: next(ticks, 10.2)
        ):
            result = local_plan.local_read_plan(
                [self.step("path_info", path=str(self.text_path))],
                step_timeout_sec=0.1,
            )
        self.assertEqual(result["status"], "timed_out")
        self.assertEqual(result["reason"], "step_timeout")
        self.assertEqual(result["steps"][0]["error"], "step_timeout")


if __name__ == "__main__":
    unittest.main()
