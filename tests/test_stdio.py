"""Exercise the installed gateway over its real MCP stdio transport."""
import json
import base64
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import time

import anyio
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image
from home_mcp_gateway import android, aoitalk, aoitalk_tasks, browser_qa, desktop, git_qa, image_library, jobs, readiness
from home_mcp_gateway import server
from qa_test_support import local_server


FULL_MCP_BASELINE = {
    "desktop_observe", "desktop_act", "system_info", "get_environment", "set_environment", "local_read_plan",
    "list_directory", "glob_paths", "search_text", "path_info", "read_text", "write_text",
    "append_text", "read_file_base64", "write_file_base64", "make_directory", "copy_path",
    "move_path", "delete_path", "hash_file", "run_command", "run_powershell", "run_python",
    "start_process", "list_processes", "process_status", "kill_process", "http_request",
    "download_url", "screenshot", "tool_log_info", "tool_log_summary", "git_find", "git_resolve", "git_status", "git_fetch",
    "git_remote_head", "git_worktree_create", "git_worktree_list", "git_worktree_status",
    "git_worktree_diff", "git_apply_patch", "git_changed_files", "git_worktree_cleanup",
    "job_start", "job_status", "job_tail", "job_wait", "job_stop", "job_list", "adb_devices",
    "adb_shell", "adb_screenshot", "adb_logcat", "adb_tap", "adb_swipe", "adb_text",
    "adb_keyevent", "adb_install", "adb_start", "adb_stop", "adb_package_info", "wait_tcp",
    "wait_http", "browser_start", "browser_run_plan", "browser_navigate", "browser_page_text",
    "browser_find", "browser_click", "browser_fill", "browser_keyboard", "browser_screenshot",
    "browser_errors", "browser_close", "image_library_register_context",
}

AOITALK_TOOLS = {fn.__name__ for fn in (*aoitalk.TOOLS, *aoitalk_tasks.TOOLS)}


class StdioTests(unittest.TestCase):
    def test_discovery_and_python_call(self):
        async def check():
            launcher = Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py"
            with tempfile.TemporaryDirectory() as cwd:
                params = StdioServerParameters(
                    command=sys.executable,
                    args=[str(launcher)],
                    cwd=cwd,
                    env={"CONTROL_PLANE_API_KEY": "test-only-sentinel", "HOME_MCP_TOOL_LOG_DIR": str(Path(cwd) / "tool-logs"),
                         "HOME_MCP_AOITALK_ENABLED": "false"},
                )
                with anyio.fail_after(30):
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            info = await session.initialize()
                            self.assertEqual(info.server_info.name, "Home MCP Gateway")
                            listing = await session.list_tools()
                            ordered_names = [tool.name for tool in listing.tools]
                            names = set(ordered_names)
                            self.assertEqual(len(names), 76)
                            self.assertTrue(FULL_MCP_BASELINE.issubset(names), FULL_MCP_BASELINE - names)
                            self.assertFalse(AOITALK_TOOLS & names)
                            self.assertEqual(ordered_names[:2], ["desktop_observe", "desktop_act"])
                            self.assertFalse({"list_monitors", "screenshot_image", "list_windows", "capture_window"} & names)
                            self.assertIn("run_python", names)
                            self.assertIn("read_text", names)
                            plan_path = Path(cwd) / "plan.txt"
                            plan_path.write_text("stdio plan marker\n", encoding="utf-8")
                            batch = await session.call_tool("local_read_plan", {
                                "steps": [
                                    {"operation": "path_info", "args": {"path": str(plan_path)}},
                                    {"operation": "read_text", "args": {"path": str(plan_path), "max_chars": 256}},
                                    {"operation": "glob_paths", "args": {
                                        "pattern": str(Path(cwd) / "*.txt"),
                                        "recursive": False,
                                        "max_results": 8,
                                        "timeout_sec": 1,
                                    }},
                                ],
                            })
                            self.assertFalse(batch.is_error, batch)
                            batch_payload = batch.structured_content
                            if batch_payload is None:
                                batch_payload = json.loads(batch.content[0].text)
                            self.assertEqual(batch_payload["status"], "completed")
                            self.assertEqual(batch_payload["steps_completed"], 3)
                            self.assertEqual(batch_payload["steps"][1]["result"]["text"], "stdio plan marker\n")
                            search_tool = next(tool for tool in listing.tools if tool.name == "search_text")
                            self.assertIsNone(search_tool.input_schema["properties"]["max_files"]["default"])
                            searched = await session.call_tool("search_text", {
                                "root": str(cwd),
                                "query": "stdio plan marker",
                                "file_glob": "*.txt",
                                "max_results": 4,
                                "timeout_sec": 2,
                            })
                            self.assertFalse(searched.is_error, searched)
                            search_payload = searched.structured_content
                            if search_payload is None:
                                search_payload = json.loads(searched.content[0].text)
                            self.assertFalse(search_payload["timed_out"])
                            self.assertTrue(search_payload["results"])
                            process_tool = next(tool for tool in listing.tools if tool.name == "list_processes")
                            self.assertIn("detailed", process_tool.input_schema["properties"])
                            self.assertIn("include_details", process_tool.input_schema["properties"])
                            processes = await session.call_tool("list_processes", {
                                "name_contains": "python",
                                "detailed": False,
                            })
                            self.assertFalse(processes.is_error, processes)
                            process_payload = processes.structured_content
                            if process_payload is None:
                                process_payload = json.loads(processes.content[0].text)
                            self.assertIsInstance(process_payload["processes"], list)
                            if process_payload["processes"]:
                                self.assertNotIn("status", process_payload["processes"][0])
                            self.assertIn("browser_run_plan", names)
                            for module in (android, browser_qa, desktop, git_qa, image_library, jobs, readiness):
                                self.assertTrue({fn.__name__ for fn in module.TOOLS}.issubset(names))
                            result = await session.call_tool("run_python", {
                                "code": "import os; print(6 * 7); print('CONTROL_PLANE_API_KEY' in os.environ)",
                            })
                            self.assertFalse(result.is_error)
                            payload = result.structured_content
                            if payload is None:
                                payload = json.loads(result.content[0].text)
                            self.assertEqual(payload["returncode"], 0)
                            self.assertEqual(payload["stdout"].splitlines(), ["42", "False"])
                            log_files = list((Path(cwd) / "tool-logs").glob("*/*.jsonl"))
                            self.assertEqual(len(log_files), 1)
                            events = [json.loads(line) for line in log_files[0].read_text(encoding="utf-8").splitlines()]
                            run_event = next(event for event in events if event["tool"] == "run_python")
                            self.assertIsNotNone(run_event["mcp_request_id"])
                            self.assertEqual(run_event["mcp_method"], "tools/call")

        anyio.run(check)

    def test_aoitalk_enable_precedence_and_repo_env_fallback(self):
        with tempfile.TemporaryDirectory() as root:
            repo = Path(root)
            (repo / ".env").write_text("\ufeffHOME_MCP_AOITALK_ENABLED=YeS\n", encoding="utf-8")
            with patch.object(server, "GATEWAY_ROOT", repo), patch.dict(os.environ, {}, clear=True):
                self.assertTrue(server._aoitalk_enabled())
            with patch.object(server, "GATEWAY_ROOT", repo), patch.dict(
                    os.environ, {"HOME_MCP_AOITALK_ENABLED": "off"}, clear=True):
                self.assertFalse(server._aoitalk_enabled())

    def test_jev_only_discovery_and_tunnel_entrypoint(self):
        async def check():
            root = Path(__file__).resolve().parents[1]
            launcher = root / "scripts" / "jev_browser_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)])
            with anyio.fail_after(30):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        info = await session.initialize()
                        self.assertEqual(info.server_info.name, "Home MCP Jev Browser")
                        listing = await session.list_tools()
                        self.assertEqual([tool.name for tool in listing.tools], ["browser_run_plan"])

            tunnel = (root / "scripts" / "connect_tunnel.ps1").read_text(encoding="utf-8")
            self.assertIn("gateway_stdio.py", tunnel)
            self.assertNotIn("jev_browser_stdio.py", tunnel)

        anyio.run(check)

    def test_images_and_browser_over_real_mcp(self):
        async def check(url):
            fixture = Path(__file__).with_name("stdio_capture_fixture.py").resolve()
            params = StdioServerParameters(command=sys.executable, args=[str(fixture)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    for name, args in (("desktop_observe", {"mode": "desktop"}), ("desktop_observe", {"mode": "monitor", "monitor": 1}),
                                       ("desktop_observe", {"mode": "window", "hwnd": 999}), ("adb_screenshot", {"serial": "fake"})):
                        result = await session.call_tool(name, args)
                        self.assertFalse(result.is_error, result)
                        block = next(c for c in result.content if c.type == "image")
                        self.assertEqual(block.mime_type, "image/png")
                        with Image.open(io.BytesIO(base64.b64decode(block.data))) as picture:
                            picture.load()
                            self.assertEqual(picture.size, (64, 48))
                        self.assertEqual(result.structured_content["width"], 64)
                    missing = await session.call_tool("adb_devices", {"adb_path": "__missing_adb_qa__"})
                    self.assertEqual(missing.structured_content["error"], "adb_not_found")
                    bad = await session.call_tool("desktop_observe", {"mode": "window", "hwnd": 999, "expected_pid": 7})
                    self.assertTrue(bad.is_error)
                    self.assertIn("ownership changed", bad.content[0].text)
                    ready = await session.call_tool("wait_http", {"url": url, "body_contains": "Local QA"})
                    self.assertTrue(ready.structured_content["ready"])
                    start = await session.call_tool("browser_start", {})
                    if start.is_error and "Executable doesn't exist" in start.content[0].text:
                        return  # OS-independent image tests above still ran; browser install is optional.
                    self.assertFalse(start.is_error, start)
                    sid = start.structured_content["session_id"]
                    for name, args in (("browser_navigate", {"url": url}), ("browser_fill", {"selector": "#name", "value": "MCP QA"}),
                                       ("browser_click", {"selector": "#go"})):
                        result = await session.call_tool(name, {"session_id": sid, **args})
                        self.assertFalse(result.is_error, result)
                    text = await session.call_tool("browser_page_text", {"session_id": sid, "selector": "#result"})
                    self.assertEqual(text.structured_content["text"], "MCP QA")
                    screenshot = await session.call_tool("browser_screenshot", {"session_id": sid})
                    self.assertFalse(screenshot.is_error, screenshot)
                    self.assertTrue(any(c.type == "image" for c in screenshot.content))
                    self.assertEqual(screenshot.structured_content["width"], 1280)
                    await session.call_tool("browser_close", {"session_id": sid})
        with local_server() as (url, _):
            anyio.run(check, url)

    def test_job_reconnect_after_gateway_shutdown(self):
        async def check(runtime):
            launcher = Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)])
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    started = await session.call_tool("job_start", {"command": [sys.executable, "-u", "-c",
                        "import time; print('before restart'); time.sleep(2); print('after restart')"], "runtime_dir": runtime})
                    self.assertFalse(started.is_error, started)
                    job_id = started.structured_content["job_id"]
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("job_wait", {"job_id": job_id, "runtime_dir": runtime, "timeout_sec": 10})
                    self.assertEqual(result.structured_content["exit_code"], 0, result.structured_content)
                    logs = await session.call_tool("job_tail", {"job_id": job_id, "runtime_dir": runtime})
                    self.assertIn("after restart", logs.structured_content["stdout"])
                    state = result.structured_content
            deadline = time.monotonic() + 5
            while jobs.identity(state["worker_pid"], state["worker_create_time"]) == "alive" and time.monotonic() < deadline:
                await anyio.sleep(.05)
        with tempfile.TemporaryDirectory() as runtime:
            anyio.run(check, runtime)


if __name__ == "__main__":
    unittest.main()
