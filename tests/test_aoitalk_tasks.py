"""AoiTalk local-task bridge tests. No live AoiTalk or LLM calls."""
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import anyio
import httpx
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from home_mcp_gateway import aoitalk
from home_mcp_gateway import aoitalk_tasks as tasks

USER = "fixture-user"
PASSWORD = "fixture-password-${LITERAL}-#-秘密"
SETTINGS = aoitalk.Settings("http://127.0.0.1:3000", USER, PASSWORD)
WORKSPACE = "C:\\workspace\\fixture-repo"


class API:
    """Wire-compatible in-memory /api/local-agent implementation."""

    def __init__(self):
        self.calls = []
        self.tasks = {}
        self.keys = {}
        self.login_status = 200
        self.cookie = True
        self.status_override = None
        self.malformed = False
        self.finish_after_polls = None
        self.polls = 0
        self.disabled = False
        self.post_error = None
        self.cookie_path = "/"

    def view(self, task):
        return dict(task)

    def route(self, method, path, headers, body, query):
        self.calls.append((method, path))
        if path == "/api/auth/login":
            assert body["username"] == USER and body["password"] == PASSWORD
            return self.login_status, {"authenticated": True, "user": {"password_reset_required": False}}, (
                {"Set-Cookie": f"session=fixture; Path={self.cookie_path}; HttpOnly"} if self.cookie else {})
        assert "session=fixture" in headers.get("cookie", "")
        if self.disabled:
            return 409, {"detail": "local agent tasks are disabled"}, {}
        if self.status_override:
            return self.status_override, {"detail": "internal " + PASSWORD, "trace": "Bearer abcdefghijklmnop"}, {}
        if self.malformed:
            return 200, {"task": {"task_id": "nope", "status": "weird"}, "tasks": "x"}, {}
        if path == "/api/local-agent/status":
            return 200, {"enabled": True, "running": True, "model": "fixture-model", "allowed_roots": ["C:\\workspace"],
                         "max_concurrency": 1, "limits": {"max_max_tool_rounds": 120}, "queued": 0}, {}
        if path == "/api/local-agent/tasks" and method == "POST":
            if self.post_error:
                status, payload = self.post_error
                return status, payload, {}
            key = headers.get("idempotency-key")
            if key in self.keys:
                return 200, {"task": self.view(self.tasks[self.keys[key]]), "created": False}, {}
            task_id = uuid4().hex
            self.tasks[task_id] = {"task_id": task_id, "status": "queued", "executor": "aoitalk_local",
                                   "workspace": body["workspace"], "mode": body["mode"], "goal": body["goal"],
                                   "counters": {}, "is_terminal": False, "body": body}
            self.keys[key] = task_id
            return 202, {"task": self.view(self.tasks[task_id]), "created": True}, {}
        if path == "/api/local-agent/tasks" and method == "GET":
            return 200, {"tasks": [self.view(t) for t in self.tasks.values()]}, {}
        parts = path.split("/")
        task = self.tasks.get(parts[4]) if len(parts) > 4 else None
        if task is None:
            return 404, {"detail": "task not found", "error": "not_found"}, {}
        if path.endswith("/tail"):
            limit = int(query.get("limit", ["50"])[0])
            events = [{"ts": i, "type": "command_end", "exit_code": 0} for i in range(100)][-limit:]
            return 200, {"task_id": task["task_id"], "status": task["status"], "events": events,
                         "truncated": True, "event_count": 100}, {}
        if path.endswith("/cancel"):
            task.update(status="cancelled", cancel_requested=True, is_terminal=True)
            return 200, {"task": self.view(task)}, {}
        self.polls += 1
        if self.finish_after_polls is not None and self.polls >= self.finish_after_polls:
            task.update(status="completed", is_terminal=True, summary_text="completed\ncommands: 3",
                        result={"checks": {"build": "pass"}, "final_message": "x" * 10, "stopped_reason": "final"})
        return 200, {"task": self.view(task)}, {}

    def handler(self, request):
        body = json.loads(request.content) if request.content else None
        query = parse_qs(request.url.query.decode() if isinstance(request.url.query, bytes) else request.url.query)
        status, data, headers = self.route(request.method, request.url.path, dict(request.headers), body, query)
        return httpx.Response(status, json=data, headers=headers)


class TaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = API()
        original = httpx.AsyncClient

        def factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return original(transport=httpx.MockTransport(self.api.handler), **kwargs)

        for p in [patch.object(aoitalk, "load_settings", return_value=SETTINGS),
                  patch.object(aoitalk.httpx, "AsyncClient", side_effect=factory),
                  patch.object(tasks, "POLL_INTERVAL", .001)]:
            p.start(); self.addCleanup(p.stop)

    def assert_private(self, result):
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("session=fixture", text)
        self.assertNotIn("Bearer abcdefghijklmnop", text)

    async def start(self, **kwargs):
        return await tasks.aoitalk_local_task_start(kwargs.pop("goal", "fix the build"),
                                                    kwargs.pop("workspace", WORKSPACE), **kwargs)

    async def test_connection_and_list(self):
        result = await tasks.aoitalk_local_task_list()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["service"]["model"], "fixture-model")
        self.assertEqual(result["tasks"], [])
        self.assert_private(result)

    async def test_start_returns_task_id_without_waiting(self):
        started = time.monotonic()
        result = await self.start(mode="mutate", constraints=["keep changes"],
                                  completion_criteria=["build passes"], allow_adb=True, max_tool_rounds=30)
        self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "queued")
        self.assertTrue(result["created"])
        self.assertTrue(result["idempotency_key"].startswith("gw-"))
        body = self.api.tasks[result["task_id"]]["body"]
        self.assertEqual(body["mode"], "mutate")
        self.assertEqual(body["max_tool_rounds"], 30)
        self.assertTrue(body["allow_adb"])
        self.assertNotIn("timeout_sec", body)
        self.assert_private(result)

    async def test_same_idempotency_key_reuses_task(self):
        first = await self.start(idempotency_key="chatgpt-1")
        second = await self.start(idempotency_key="chatgpt-1")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertFalse(second["created"])
        self.assertEqual(len(self.api.tasks), 1)

    async def test_status_bounded_wait_and_completion(self):
        task_id = (await self.start())["task_id"]
        running = await tasks.aoitalk_local_task_status(task_id, wait_seconds=0)
        self.assertEqual(running["status"], "queued")
        self.assertIn("poll", running["next_step"])
        self.api.finish_after_polls = self.api.polls + 3
        done = await tasks.aoitalk_local_task_status(task_id, wait_seconds=2)
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["result"]["checks"], {"build": "pass"})
        self.assertNotIn("final_message", done["result"])  # compact view only

    async def test_status_wait_is_bounded(self):
        task_id = (await self.start())["task_id"]
        started = time.monotonic()
        result = await tasks.aoitalk_local_task_status(task_id, wait_seconds=0.2)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual(result["status"], "queued")
        for bad in [-1, 21, float("nan"), True, "5"]:
            self.assertEqual((await tasks.aoitalk_local_task_status(task_id, wait_seconds=bad))["error"], "invalid_input")

    async def test_tail_and_cancel(self):
        task_id = (await self.start())["task_id"]
        tail = await tasks.aoitalk_local_task_tail(task_id, limit=5)
        self.assertTrue(tail["ok"])
        self.assertEqual(len(tail["events"]), 5)
        self.assertTrue(tail["truncated"])
        cancelled = await tasks.aoitalk_local_task_cancel(task_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertTrue(cancelled["cancel_requested"])

    async def test_invalid_ids_rejected_before_network(self):
        for fn in (tasks.aoitalk_local_task_status, tasks.aoitalk_local_task_tail, tasks.aoitalk_local_task_cancel):
            for bad in ["../../etc", "x" * 32, "", None]:
                self.assertEqual((await fn(bad))["error"], "invalid_input")
        self.assertEqual((await self.start(goal=" "))["error"], "invalid_input")
        self.assertEqual((await self.start(idempotency_key="bad/key"))["error"], "invalid_input")
        self.assertEqual((await tasks.aoitalk_local_task_list(limit=0))["error"], "invalid_input")
        self.assertEqual((await tasks.aoitalk_local_task_list(status="weird"))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_unknown_task_is_not_found(self):
        result = await tasks.aoitalk_local_task_status(uuid4().hex)
        self.assertEqual(result["error"], "not_found")

    async def test_server_validation_codes_are_surfaced(self):
        self.api.post_error = (403, {"detail": "workspace is outside local_agent_tasks.allowed_roots",
                                     "error": "workspace_not_allowed"})
        result = await self.start()
        self.assertEqual(result["error"], "workspace_not_allowed")
        self.assertFalse(result["submission_uncertain"])
        self.api.post_error = (422, {"detail": [{"loc": ["body", "mode"], "msg": "bad", "input": PASSWORD}]})
        result = await self.start()
        self.assertEqual(result["error"], "invalid_input")
        self.assertIn("mode", result["detail"])
        self.assert_private(result)

    async def test_public_base_path_cookie_is_sent_to_api_root(self):
        self.api.cookie_path = "/at"  # AOITALK_PUBLIC_BASE_PATH deployments
        result = await self.start()
        self.assertTrue(result["ok"], result)
        self.assertTrue((await tasks.aoitalk_local_task_status(result["task_id"]))["ok"])

    async def test_disabled_service(self):
        self.api.disabled = True
        self.assertEqual((await tasks.aoitalk_local_task_list())["error"], "disabled")

    async def test_authentication_failure(self):
        self.api.login_status = 401
        result = await self.start()
        self.assertEqual(result["error"], "authentication_failed")
        self.assertEqual(self.api.calls, [("POST", "/api/auth/login")])
        self.assertFalse(result["submission_uncertain"])
        self.api.login_status, self.api.cookie = 200, False
        self.assertEqual((await tasks.aoitalk_local_task_list())["error"], "authentication_failed")
        self.assert_private(result)

    async def test_server_error_body_never_returned(self):
        task_id = (await self.start())["task_id"]
        self.api.status_override = 500
        for result in [await tasks.aoitalk_local_task_status(task_id), await self.start()]:
            self.assertEqual(result["error"], "server_error")
            self.assert_private(result)
        self.assertTrue(result["submission_uncertain"])
        self.assertIn("same idempotency_key", result["next_step"])

    async def test_malformed_response(self):
        task_id = (await self.start())["task_id"]
        self.api.malformed = True
        self.assertEqual((await tasks.aoitalk_local_task_status(task_id))["error"], "invalid_response")
        self.assertEqual((await tasks.aoitalk_local_task_tail(task_id))["error"], "invalid_response")
        self.assertEqual((await tasks.aoitalk_local_task_list())["error"], "invalid_response")

    async def test_timeout_and_aoitalk_down(self):
        def timeout(request):
            raise httpx.ReadTimeout("SECRET " + PASSWORD)

        self.api.handler = timeout
        result = await self.start()
        self.assertEqual(result["error"], "connection_error")
        self.assert_private(result)

    async def test_configuration_error(self):
        with patch.object(aoitalk, "load_settings", side_effect=aoitalk.ClipError("configuration_error")):
            result = await tasks.aoitalk_local_task_list()
        self.assertEqual(result["error"], "configuration_error")


class RealAoiTalkDownTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_port_is_connection_error(self):
        with patch.object(aoitalk, "load_settings", return_value=aoitalk.Settings("http://127.0.0.1:9", USER, PASSWORD)):
            result = await tasks.aoitalk_local_task_list()
        self.assertEqual(result["error"], "connection_error")
        self.assertNotIn(PASSWORD, json.dumps(result, ensure_ascii=False))


@contextmanager
def fixture_server(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            parts = urlsplit(self.path)
            status, data, headers = api.route(self.command, parts.path,
                                              {k.lower(): v for k, v in self.headers.items()}, body,
                                              parse_qs(parts.query))
            encoded = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers(); self.wfile.write(encoded)

        do_GET = respond
        do_POST = respond

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


class StdioTaskTests(unittest.TestCase):
    def test_tools_listed_and_callable_over_real_stdio(self):
        async def check(url, env_path, cwd):
            launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)], cwd=cwd,
                env={"HOME_MCP_AOITALK_ENV_FILE": env_path, "HOME_MCP_AOITALK_URL": url,
                     "HOME_MCP_AOITALK_ENABLED": "true"})
            with anyio.fail_after(35):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listing = {t.name: t for t in (await session.list_tools()).tools}
                        for fn in tasks.TOOLS:
                            self.assertIn(fn.__name__, listing)
                            self.assertNotIn("password", str(listing[fn.__name__].input_schema))
                        for fn in ("aoitalk_clip_connection", "aoitalk_clip_ingest", "aoitalk_clip_status"):
                            self.assertIn(fn, listing)
                        self.assertFalse(listing["aoitalk_local_task_start"].annotations.read_only_hint)
                        self.assertTrue(listing["aoitalk_local_task_status"].annotations.read_only_hint)
                        self.assertTrue(listing["aoitalk_local_task_tail"].annotations.read_only_hint)
                        started = await session.call_tool("aoitalk_local_task_start", {
                            "goal": "inspect", "workspace": WORKSPACE, "idempotency_key": "stdio-1"})
                        self.assertFalse(started.is_error)
                        value = started.structured_content
                        self.assertTrue(value["ok"], value)
                        status = await session.call_tool("aoitalk_local_task_status", {"task_id": value["task_id"]})
                        self.assertEqual(status.structured_content["status"], "queued")
                        tail = await session.call_tool("aoitalk_local_task_tail", {"task_id": value["task_id"], "limit": 3})
                        self.assertEqual(len(tail.structured_content["events"]), 3)
                        listed = await session.call_tool("aoitalk_local_task_list", {})
                        self.assertEqual(listed.structured_content["tasks"][0]["task_id"], value["task_id"])
                        cancel = await session.call_tool("aoitalk_local_task_cancel", {"task_id": value["task_id"]})
                        self.assertEqual(cancel.structured_content["status"], "cancelled")
                        for result in [started, status, tail, listed, cancel]:
                            self.assertNotIn(PASSWORD, result.model_dump_json())
                            self.assertNotIn("session=fixture", result.model_dump_json())

        api = API()
        with tempfile.TemporaryDirectory() as cwd, fixture_server(api) as url:
            path = Path(cwd) / "app.env"
            path.write_text(f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\nAOITALK_BOOTSTRAP_ADMIN_PASSWORD='{PASSWORD}'\n",
                            encoding="utf-8")
            anyio.run(check, url, str(path), cwd)
        self.assertEqual(len(api.tasks), 1)


if __name__ == "__main__":
    unittest.main()
