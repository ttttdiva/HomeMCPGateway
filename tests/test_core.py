import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import psutil

from home_mcp_gateway import core


class CoreTests(unittest.TestCase):
    def test_file_roundtrip_and_hash(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "a" / "hello.txt"
            core.write_text(str(p), "hello\nworld")
            self.assertEqual(core.read_text(str(p))["text"], "hello\nworld")
            self.assertEqual(len(core.hash_file(str(p))["digest"]), 64)

    def test_copy_move_delete(self):
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a.txt"
            b = Path(td) / "b.txt"
            c = Path(td) / "c.txt"
            a.write_text("x", encoding="utf-8")
            core.copy_path(str(a), str(b))
            self.assertTrue(b.exists())
            core.move_path(str(b), str(c))
            self.assertFalse(b.exists())
            self.assertTrue(c.exists())
            core.delete_path(str(c))
            self.assertFalse(c.exists())

    def test_run_python(self):
        result = core.run_python("print(6 * 7)")
        self.assertEqual(result["returncode"], 0)
        self.assertIn("42", result["stdout"])

    def test_run_command_normal_preserves_shell_cwd_env_and_output_limit(self):
        with tempfile.TemporaryDirectory() as td:
            result = core.run_command(
                "cd && echo %HOME_MCP_CORE_TEST_ENV%",
                cwd=td,
                env={"HOME_MCP_CORE_TEST_ENV": "present"},
            )
            limited = core.run_command("echo 123456789", max_output_chars=3)
            failed = core.run_command(
                subprocess.list2cmdline([sys.executable, "-c", "import sys; print('failure', file=sys.stderr); sys.exit(7)"])
            )
        self.assertEqual(result["returncode"], 0)
        self.assertIn("present", result["stdout"])
        self.assertTrue(limited["stdout_truncated"])
        self.assertEqual(result["cwd"], td)
        self.assertEqual(failed["returncode"], 7)
        self.assertIn("failure", failed["stderr"])

    def test_run_command_timeout_returns_promptly_with_stdout_and_stderr(self):
        code = (
            "import sys,time; print('before-stdout', flush=True); "
            "print('before-stderr', file=sys.stderr, flush=True); time.sleep(5)"
        )
        command = subprocess.list2cmdline([sys.executable, "-c", code])
        started = time.monotonic()
        result = core.run_command(command, timeout_sec=1)
        elapsed = time.monotonic() - started
        self.assertTrue(result["timed_out"])
        self.assertLess(elapsed, 3.0)
        self.assertIn("before-stdout", result["stdout"])
        self.assertIn("before-stderr", result["stderr"])

    def test_run_command_timeout_kills_child_process_tree(self):
        child_code = "import time; time.sleep(15)"
        parent_code = (
            "import subprocess,sys,time; "
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "print(child.pid, flush=True); time.sleep(15)"
        )
        command = subprocess.list2cmdline([sys.executable, "-c", parent_code])
        result = core.run_command(command, timeout_sec=1)
        self.assertTrue(result["timed_out"])
        child_pids = [int(token) for token in result["stdout"].split() if token.isdigit()]
        self.assertTrue(child_pids)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(psutil.pid_exists(pid) for pid in child_pids):
            time.sleep(0.05)
        self.assertFalse(any(psutil.pid_exists(pid) for pid in child_pids))

    def test_run_command_timeout_kills_pipe_holder_after_parent_exits(self):
        # The direct Python parent exits immediately, but its child inherits
        # stdout and sleeps.  This is the Windows shell/pipe case where a
        # plain psutil child walk can no longer see the descendant at timeout.
        child_code = "import time; time.sleep(15)"
        parent_code = (
            "import subprocess,sys; "
            f"child=subprocess.Popen([sys.executable, '-c', {child_code!r}]); "
            "print(child.pid, flush=True); sys.exit(0)"
        )
        command = subprocess.list2cmdline([sys.executable, "-c", parent_code])
        started = time.monotonic()
        result = core.run_command(command, timeout_sec=1)
        elapsed = time.monotonic() - started
        self.assertTrue(result["timed_out"])
        self.assertLess(elapsed, 3.0)
        child_pids = [int(token) for token in result["stdout"].split() if token.isdigit()]
        self.assertTrue(child_pids)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(psutil.pid_exists(pid) for pid in child_pids):
            time.sleep(0.05)
        self.assertFalse(any(psutil.pid_exists(pid) for pid in child_pids))

    def test_environment(self):
        name = "HOME_MCP_GATEWAY_TEST_VAR"
        core.set_environment(name, "abc")
        self.assertEqual(core.get_environment(name)["value"], "abc")
        core.set_environment(name, None)
        self.assertIsNone(core.get_environment(name)["value"])

    def test_search_text(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "x.txt"
            p.write_text("alpha\nbeta\ngamma\n", encoding="utf-8")
            result = core.search_text(td, "beta", "*.txt")
            self.assertEqual(result["results"][0]["line"], 2)


    def test_search_text_skips_generated_directories(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "visible.txt").write_text("needle\n", encoding="utf-8")
            ignored = root / ".venv"
            ignored.mkdir()
            (ignored / "hidden.txt").write_text("needle\n", encoding="utf-8")
            result = core.search_text(td, "needle", "*.txt")
            self.assertEqual(len(result["results"]), 1)
            self.assertTrue(result["results"][0]["path"].endswith("visible.txt"))
            self.assertIn(result["backend"], {"ripgrep", "python"})

    def test_search_text_global_limit_reports_truncation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a.txt").write_text("needle\nneedle\n", encoding="utf-8")
            result = core.search_text(td, "needle", "*.txt", max_results=1)
            self.assertEqual(len(result["results"]), 1)
            self.assertTrue(result["truncated"])

    def test_search_text_rg_stream_stops_high_output_at_max_results(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            payload = "needle\n" * 200
            for index in range(40):
                (root / f"{index}.txt").write_text(payload, encoding="utf-8")
            started = time.monotonic()
            result = core.search_text(td, "needle", "*.txt", max_results=1, timeout_sec=5)
            elapsed = time.monotonic() - started
        self.assertEqual(result["backend"], "ripgrep")
        self.assertEqual(len(result["results"]), 1)
        self.assertTrue(result["truncated"])
        self.assertGreaterEqual(result["scanned_files"], 1)
        self.assertLess(elapsed, 3.0)

    def test_search_text_zero_max_results_has_internal_result_cap(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "many.txt").write_text("needle\n" * 4, encoding="utf-8")
            with patch.object(core, "_DEFAULT_SEARCH_MAX_RESULTS", 2):
                result = core.search_text(td, "needle", "*.txt", max_results=0, timeout_sec=2)
        self.assertLessEqual(len(result["results"]), 2)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["partial"])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "many.txt").write_text("needle\n" * 4, encoding="utf-8")
            with patch.object(core, "_DEFAULT_SEARCH_MAX_RESULTS", 2), \
                 patch.object(core.shutil, "which", return_value=None):
                fallback = core.search_text(td, "needle", "*.txt", max_results=0, timeout_sec=2)
        self.assertLessEqual(len(fallback["results"]), 2)
        self.assertTrue(fallback["truncated"])
        self.assertTrue(fallback["partial"])

    def test_search_text_python_caps_serialized_result_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "large.txt").write_text(("needle " + "x" * 65500 + "\n") * 200, encoding="utf-8")
            with patch.object(core.shutil, "which", return_value=None):
                result = core.search_text(td, "needle", "*.txt", max_results=0, timeout_sec=5)
        serialized_bytes = sum(
            len(item["path"].encode("utf-8")) + len(item["text"].encode("utf-8")) + 64
            for item in result["results"]
        )
        self.assertLessEqual(serialized_bytes, core._RG_STREAM_MAX_OUTPUT_BYTES)
        self.assertTrue(result["truncated"])
        self.assertTrue(result["partial"])

    def test_search_text_rg_timeout_kills_pipe_holding_descendant(self):
        if os.name != "nt":
            self.skipTest("Windows Job Object regression")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            pid_path = root / "child.pid"
            fake_rg = root / "fake-rg.cmd"
            child_code = f"import os,time; open(r'{pid_path}','w').write(str(os.getpid())); time.sleep(15)"
            fake_rg.write_text(
                f'@echo off\r\nstart "" /b "{sys.executable}" -c "{child_code}"\r\n',
                encoding="utf-8",
            )
            with patch.object(core.shutil, "which", return_value=str(fake_rg)):
                started = time.monotonic()
                result = core.search_text(td, "needle", timeout_sec=1)
                elapsed = time.monotonic() - started
            self.assertTrue(result["timed_out"])
            self.assertLess(elapsed, 3.0)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not pid_path.exists():
                time.sleep(0.05)
            self.assertTrue(pid_path.exists())
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            while time.monotonic() < deadline and psutil.pid_exists(child_pid):
                time.sleep(0.05)
            self.assertFalse(psutil.pid_exists(child_pid))

    def test_search_text_python_fallback_is_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(5):
                (root / f"{index}.txt").write_text("needle\n", encoding="utf-8")
            with patch.object(core.shutil, "which", return_value=None):
                result = core.search_text(td, "needle", "*.txt", max_files=2)
        self.assertEqual(result["backend"], "python")
        self.assertLessEqual(result["scanned_files"], 2)
        self.assertTrue(result["partial"])

    def test_search_text_max_files_avoids_unbounded_rg_scan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(5):
                (root / f"{index}.txt").write_text("needle\n", encoding="utf-8")
            result = core.search_text(td, "needle", "*.txt", max_files=2, timeout_sec=1)
        self.assertEqual(result["backend"], "ripgrep")
        self.assertLessEqual(result["scanned_files"], 2)
        self.assertTrue(result["partial"])

    def test_search_text_server_style_max_files_keeps_rg_backend(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "one.txt").write_text("needle\n", encoding="utf-8")
            result = core.search_text(td, "needle", "*.txt", max_files=10000, timeout_sec=2)
        self.assertEqual(result["backend"], "ripgrep")
        self.assertEqual(result["scanned_files"], 1)
        self.assertFalse(result["timed_out"])

    def test_search_text_without_max_files_disables_file_cap(self):
        fake_result = {
            "root": tempfile.gettempdir(),
            "scanned_files": 0,
            "results": [],
            "truncated": False,
            "backend": "ripgrep",
            "timed_out": False,
            "partial": False,
        }
        with patch.object(core.shutil, "which", return_value="rg"), \
             patch.object(core, "_run_rg_stream", return_value=(fake_result, 0)) as runner:
            result = core.search_text(tempfile.gettempdir(), "needle", timeout_sec=1)
        self.assertEqual(result["backend"], "ripgrep")
        self.assertIsNone(runner.call_args.args[3])

    def test_search_text_max_files_caps_nonmatching_files_before_search(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for index in range(5):
                (root / f"{index:02d}-no-match.txt").write_text("ordinary\n", encoding="utf-8")
            (root / "99-match.txt").write_text("needle\n", encoding="utf-8")
            bounded_paths = [str(root / f"{index:02d}-no-match.txt") for index in range(5)]
            with patch.object(core.shutil, "which", return_value="rg"), \
                 patch.object(core, "_enumerate_rg_files", return_value=(bounded_paths, False, True, -9, True)) as enumerate_files:
                result = core.search_text(td, "needle", "*.txt", max_files=5, timeout_sec=2)
        self.assertEqual(result["backend"], "ripgrep")
        self.assertEqual(result["scanned_files"], 5)
        self.assertEqual(result["results"], [])
        self.assertTrue(result["partial"])
        self.assertEqual(enumerate_files.call_args.args[1], 5)

    def test_search_text_rg_uses_fixed_argv_and_process_group(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "one.txt").write_text("needle\n", encoding="utf-8")
            with patch.object(core.subprocess, "Popen", wraps=core.subprocess.Popen) as popen:
                result = core.search_text(td, "needle", "*.txt", timeout_sec=2)
        self.assertEqual(result["backend"], "ripgrep")
        kwargs = popen.call_args.kwargs
        self.assertFalse(kwargs["shell"])
        if os.name != "nt":
            self.assertTrue(kwargs["start_new_session"])

    def test_search_text_rg_timeout_does_not_fallback(self):
        timed_out = {
            "root": tempfile.gettempdir(),
            "scanned_files": 0,
            "results": [],
            "truncated": False,
            "backend": "ripgrep",
            "timed_out": True,
            "partial": True,
        }
        with patch.object(core.shutil, "which", return_value="rg"), \
             patch.object(core, "_run_rg_stream", return_value=(timed_out, None)), \
             patch.object(core, "_search_text_python", side_effect=AssertionError("unexpected fallback")):
            result = core.search_text(tempfile.gettempdir(), "needle", timeout_sec=0.1)
        self.assertEqual(result["backend"], "ripgrep")
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["partial"])

    def test_search_text_nonzero_rg_fallback_keeps_absolute_deadline(self):
        timeout = 0.01
        fallback_calls = []

        def slow_nonzero(*args, **kwargs):
            time.sleep(0.05)
            return ({
                "root": str(args[1]),
                "scanned_files": 0,
                "results": [],
                "truncated": False,
                "backend": "ripgrep",
                "timed_out": False,
                "partial": False,
            }, 2)

        def bounded_fallback(*args, **kwargs):
            absolute_deadline = args[-1]
            fallback_calls.append(absolute_deadline)
            expired = time.monotonic() >= absolute_deadline
            return {
                "root": str(args[0]),
                "scanned_files": 0,
                "results": [],
                "truncated": False,
                "backend": "python",
                "timed_out": expired,
                "partial": expired,
            }

        with patch.object(core.shutil, "which", return_value="rg"), \
             patch.object(core, "_run_rg_stream", side_effect=slow_nonzero), \
             patch.object(core, "_search_text_python", side_effect=bounded_fallback):
            started = time.monotonic()
            result = core.search_text(tempfile.gettempdir(), "needle", timeout_sec=timeout)
            elapsed = time.monotonic() - started
        self.assertTrue(fallback_calls)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["partial"])
        self.assertLess(elapsed, 0.2)

    def test_glob_paths_recursive_max_results_and_timeout_shape(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "a").mkdir()
            (root / "a" / "one.txt").write_text("1", encoding="utf-8")
            (root / "a" / "two.txt").write_text("2", encoding="utf-8")
            (root / "a" / "three.txt").write_text("3", encoding="utf-8")
            result = core.glob_paths(str(root / "**" / "*.txt"), max_results=2, timeout_sec=1)
        self.assertEqual(len(result["matches"]), 2)
        self.assertTrue(result["truncated"])
        self.assertFalse(result["timed_out"])
        self.assertTrue(result["partial"])

    def test_glob_paths_explicit_hidden_and_symlink_cycle_safe(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            hidden = root / ".hidden.txt"
            hidden.write_text("hidden", encoding="utf-8")
            visible = root / "visible.txt"
            visible.write_text("visible", encoding="utf-8")
            link = root / "loop"
            try:
                link.symlink_to(root, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("directory symlinks are unavailable")

            wildcard = core.glob_paths(str(root / "*.txt"), max_results=10, timeout_sec=1)
            explicit = core.glob_paths(str(hidden), max_results=10, timeout_sec=1)
            recursive = core.glob_paths(str(root / "**" / "*.txt"), max_results=10, timeout_sec=1)
        self.assertNotIn(str(hidden), wildcard["matches"])
        self.assertIn(str(hidden), explicit["matches"])
        self.assertIn(str(visible), recursive["matches"])
        self.assertFalse(recursive["timed_out"])

    def test_list_processes_is_lightweight_and_filterable(self):
        result = core.list_processes()
        self.assertIsInstance(result["processes"], list)
        self.assertTrue(result["processes"])
        expected_light = {"pid", "name", "exe", "cmdline", "username"}
        self.assertTrue(expected_light.issubset(result["processes"][0]))
        self.assertTrue({"ppid", "status", "create_time"}.isdisjoint(result["processes"][0]))
        current_name = psutil.Process(os.getpid()).name()
        filtered = core.list_processes(current_name)
        self.assertTrue(any(item["pid"] == os.getpid() for item in filtered["processes"]))
        fake = type("FakeProcess", (), {"info": {
            "pid": 1, "name": "fake", "exe": "fake.exe", "cmdline": [], "username": "u",
            "ppid": 0, "status": "running", "create_time": 0,
        }})()
        with patch.object(core.psutil, "process_iter", return_value=[fake]):
            detailed = core.list_processes(include_details=True)
        self.assertIn("status", detailed["processes"][0])

    def test_system_info_reports_the_live_gateway_implementation(self):
        from unittest.mock import patch
        from home_mcp_gateway import desktop
        with patch.object(desktop, 'IMPLEMENTATION', 'loaded-test-marker'), \
             patch.object(core.shutil, 'which', return_value=None), \
             patch.object(core.psutil, 'disk_partitions', return_value=[]):
            info = core.system_info()['gateway']
        self.assertEqual(info['pid'], os.getpid())
        self.assertEqual(info['desktop_implementation'], 'loaded-test-marker')
        self.assertEqual(info['desktop_tools'], ['desktop_observe', 'desktop_act'])
        self.assertEqual(info['uia_backend'], 'UIAutomationCore.COM')


if __name__ == "__main__":
    unittest.main()
