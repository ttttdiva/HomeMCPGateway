"""Scoped Gateway tests: no AoiTalk, model, external network, or live desktop."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from home_mcp_gateway.local_agent_profile import (
    PROFILE, TOOLS, ReadOnlyScope, ScopeError, clean_environment,
)


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "workspace"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()
        (self.root / "readme.txt").write_text("hello\nneedle 日本語\n", encoding="utf-8", newline="")
        (self.outside / "private.txt").write_text("outside secret", encoding="utf-8")
        self.environment = {"HOME_MCP_PROFILE": PROFILE, "HOME_MCP_WORKSPACE": str(self.root),
                            "HOME_MCP_AGENT_RUN_ID": "run-a", "HOME_MCP_OWNER_ID": "owner-a"}
        self.scope = ReadOnlyScope.from_environment(self.environment)

    def test_scope_required_unknown_profile_rejected(self):
        for field in self.environment:
            env = dict(self.environment)
            del env[field]
            with self.subTest(field=field), self.assertRaises(ScopeError):
                ReadOnlyScope.from_environment(env)
        for profile in ["normal", "default", "invalid", " "]:
            with self.subTest(profile=profile), self.assertRaises(ScopeError):
                ReadOnlyScope.from_environment({**self.environment, "HOME_MCP_PROFILE": profile})
        with self.assertRaises(ScopeError):
            ReadOnlyScope.from_environment({**self.environment, "HOME_MCP_WORKSPACE": "relative"})

    def test_environment_does_not_inherit_credentials(self):
        env = clean_environment({**self.environment, "PATH": "safe", "AOITALK_BOOTSTRAP_ADMIN_PASSWORD": "secret",
                                 "HOME_MCP_AOITALK_ENABLED": "1", "CONTROL_PLANE_API_KEY": "secret",
                                 "OPENAI_API_KEY": "secret", "PYTHONPATH": "untrusted"})
        self.assertEqual(env["HOME_MCP_AOITALK_ENABLED"], "0")
        self.assertEqual(env["PATH"], "safe")
        self.assertNotIn("secret", json.dumps(env))
        self.assertNotIn("PYTHONPATH", env)

    def test_only_read_only_operations_are_callable(self):
        self.assertEqual(TOOLS, ("list_directory", "path_info", "read_text", "search_text"))
        for name in ["write_text", "run_command", "run_powershell", "run_python", "job_start", "job_cancel",
                     "browser_run_plan", "desktop_click", "http_request", "get_environment", "set_environment",
                     "aoitalk_local_task_start", "aoitalk_clip_ingest", "call_mcp_tool", "local_read_plan"]:
            self.assertEqual(self.scope.call(name, {})["error"], "tool_not_allowed")
        self.assertTrue(self.scope.call("read_text", {"path": "readme.txt"})["ok"])
        self.assertEqual(self.scope.call("read_text", {"path": "readme.txt"})["text"], "hello\nneedle 日本語\n")
        self.assertTrue(self.scope.call("path_info", {"path": "readme.txt"})["ok"])
        self.assertEqual(len(self.scope.call("list_directory", {"path": "."})["entries"]), 1)
        self.assertEqual(len(self.scope.call("search_text", {"root": ".", "query": "needle"})["matches"]), 1)

    def test_traversal_outside_credentials_devices_and_ads_denied(self):
        for name in [".env", ".env.production", "private.key", "credentials.json", ".npmrc",
                     "secrets.yaml", "secret.toml", "vault.kdbx", "data.secret", "id_rsa.pub"]:
            (self.root / name).write_text("credential fixture", encoding="utf-8")
            self.assertEqual(self.scope.call("read_text", {"path": name})["error"], "scope_denied")
        for value in [str(self.outside / "private.txt"), "../outside/private.txt", "readme.txt:secret",
                      "readme.txt.", "readme.txt ", "\\\\?\\C:\\secret", "//server/share/file"]:
            self.assertEqual(self.scope.call("read_text", {"path": value})["error"], "scope_denied", value)
        names = [Path(row["path"]).name for row in self.scope.call("list_directory", {"path": "."})["entries"]]
        self.assertEqual(names, ["readme.txt"])
        self.assertEqual(self.scope.call("search_text", {"root": ".", "query": "credential"})["matches"], [])

    def test_explicit_workspace_below_dot_local_is_allowed_but_appdata_is_not(self):
        scratch = self.base / ".local" / "fixture-workspace"
        scratch.mkdir(parents=True)
        (scratch / "safe.txt").write_text("safe", encoding="utf-8")
        (scratch / ".env").write_text("credential fixture", encoding="utf-8")
        scope = ReadOnlyScope.from_environment({**self.environment, "HOME_MCP_WORKSPACE": str(scratch)})
        self.assertEqual(scope.call("read_text", {"path": "safe.txt"})["text"], "safe")
        self.assertEqual(scope.call("read_text", {"path": ".env"})["error"], "scope_denied")
        with self.assertRaises(ScopeError):
            ReadOnlyScope.from_environment({**self.environment, "HOME_MCP_WORKSPACE": str(scratch.parent)})
        (self.root / ".local").mkdir()
        (self.root / ".local" / "app.txt").write_text("private appdata", encoding="utf-8")
        self.assertEqual(self.scope.call("read_text", {"path": ".local/app.txt"})["error"], "scope_denied")

    def test_symlinks_denied(self):
        try:
            (self.root / "linked").symlink_to(self.outside, target_is_directory=True)
            (self.root / "same-root-link").symlink_to(self.root / "readme.txt")
        except OSError:
            self.skipTest("symbolic links unavailable for this test account")
        for value in ["linked/private.txt", "same-root-link"]:
            self.assertEqual(self.scope.call("read_text", {"path": value})["error"], "scope_denied")
    def test_hardlinks_denied(self):
        os.link(self.outside / "private.txt", self.root / "hardlinked.txt")
        self.assertEqual(self.scope.call("read_text", {"path": "hardlinked.txt"})["error"], "scope_denied")

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_windows_junction_denied(self):
        junction = self.root / "junction"
        created = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(self.outside)],
                                 capture_output=True, text=True, timeout=10)
        if created.returncode:
            self.skipTest("junction creation unavailable for this test account")
        try:
            self.assertEqual(self.scope.call("read_text", {"path": "junction/private.txt"})["error"], "scope_denied")
        finally:
            os.rmdir(junction)

    def test_binary_and_image_content_are_not_stringified(self):
        (self.root / "image.png").write_bytes(b"\x89PNG\x00\x00binary")
        self.assertEqual(self.scope.call("read_text", {"path": "image.png"})["error"], "unsupported_binary")
        (self.root / "bad.bin").write_bytes(b"\xff\xfeinvalid")
        self.assertEqual(self.scope.call("read_text", {"path": "bad.bin"})["error"], "unsupported_text_encoding")

    def test_opened_file_is_checked_before_read(self):
        with patch.object(ReadOnlyScope, "_opened_path", return_value=self.outside / "private.txt"), \
                patch("home_mcp_gateway.local_agent_profile.os.read") as read:
            self.assertEqual(self.scope.call("read_text", {"path": "readme.txt"})["error"], "scope_denied")
            read.assert_not_called()

    def test_bounds_and_scope_cannot_be_widened_through_arguments(self):
        for value in [0, -1, 24001, True, "10"]:
            self.assertEqual(self.scope.call("read_text", {"path": "readme.txt", "max_chars": value})["error"], "invalid_input")
        for arguments in [
            {"path": "readme.txt", "workspace": str(self.outside)},
            {"path": "readme.txt", "owner_id": "other"},
            {"path": "readme.txt", "encoding": "utf-8"},
        ]:
            self.assertEqual(self.scope.call("read_text", arguments)["error"], "invalid_input")
        result = self.scope.call("read_text", {"path": "readme.txt", "max_chars": 2})
        self.assertEqual(result["text"], "he")
        self.assertTrue(result["truncated"])
        self.assertEqual(self.scope.call("search_text", {"root": ".", "query": "hello", "timeout_sec": 0})["error"], "invalid_input")

    def test_two_run_scopes_cannot_read_each_other(self):
        other = ReadOnlyScope.from_environment({**self.environment, "HOME_MCP_WORKSPACE": str(self.outside),
                                               "HOME_MCP_OWNER_ID": "owner-b", "HOME_MCP_AGENT_RUN_ID": "run-b"})
        self.assertEqual(self.scope.call("read_text", {"path": str(self.outside / "private.txt")})["error"], "scope_denied")
        self.assertEqual(other.call("read_text", {"path": str(self.root / "readme.txt")})["error"], "scope_denied")

    def test_launcher_rejects_unknown_profile_before_importing_normal_server(self):
        launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
        result = subprocess.run([sys.executable, str(launcher)], env={**os.environ, "HOME_MCP_PROFILE": "bad"},
                                capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("invalid HOME_MCP_PROFILE", result.stderr)


class ProfileStdioTests(unittest.TestCase):
    def test_real_stdio_catalog_call_and_direct_denials(self):
        import anyio
        from mcp.client import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client

        async def check(workspace):
            launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)], cwd=workspace,
                env={"HOME_MCP_PROFILE": PROFILE, "HOME_MCP_WORKSPACE": workspace,
                     "HOME_MCP_AGENT_RUN_ID": "fixture-run", "HOME_MCP_OWNER_ID": "fixture-owner",
                     "HOME_MCP_AOITALK_ENABLED": "1", "OPENAI_API_KEY": "must-not-propagate"})
            with anyio.fail_after(20):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = await session.list_tools()
                        self.assertEqual([tool.name for tool in listed.tools], list(TOOLS))
                        result = await session.call_tool("read_text", {"path": "fixture.txt"})
                        self.assertFalse(result.is_error)
                        self.assertEqual(result.structured_content["text"], "fixture")
                        for tool in ("run_command", "aoitalk_local_task_start", "aoitalk_clip_ingest", "call_mcp_tool"):
                            denied = await session.call_tool(tool, {})
                            self.assertTrue(denied.is_error, tool)
                        denied = await session.call_tool("read_text", {"path": "../escape.txt"})
                        self.assertFalse(denied.structured_content["ok"])
                        self.assertEqual(denied.structured_content["error"], "scope_denied")

        with tempfile.TemporaryDirectory() as workspace:
            (Path(workspace) / "fixture.txt").write_text("fixture", encoding="utf-8")
            anyio.run(check, workspace)


if __name__ == "__main__":
    unittest.main()
