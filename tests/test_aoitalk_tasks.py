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
from pydantic import TypeAdapter
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from home_mcp_gateway import aoitalk
from home_mcp_gateway import aoitalk_tasks as tasks
from home_mcp_gateway.aoitalk_task_schema import ListReply, StartReply

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
                previous = self.tasks[self.keys[key]]
                if previous["body"] != body:
                    return 409, {"error": "idempotency_conflict"}, {}
                return 200, {"task": self.view(previous), "created": False}, {}
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
    def test_budget_truncation_preserves_output_types_and_task_identity(self):
        base = {"task_id": "a" * 32, "status": "completed", "agent_id": "b" * 36,
                "agent_revision_id": "c" * 36, "agent_run_id": "d" * 36, "work_id": "e" * 36,
                "session_id": "f" * 36, "requested_route": {},
                "resolved_route": {"provider": "openai_compatible_local", "model": "qwen3.8-flash-next-iq3_xxs", "effort": "thinking"},
                "execution_state": "succeeded", "created_at": "2026-10-13T12:00:00+00:00",
                "counters": {"llm_calls": 3, "tool_calls": 2, "commands": 0}, "is_terminal": True}
        # Sweep budget boundaries, including a previously reproduced numeric
        # counter replaced by a truncation string at goal length 75.
        for size in range(1, 301):
            payload = {"ok": True, "tasks": [{**base, "goal": "g" * size} for _ in range(100)]}
            bounded = tasks._sanitize(payload, SETTINGS)
            TypeAdapter(ListReply).validate_python(bounded)
            for task in bounded["tasks"]:
                self.assertEqual(task["task_id"], "a" * 32)
                self.assertEqual(task["status"], "completed")
            if len(bounded["tasks"]) < 100:
                self.assertTrue(bounded["output_truncated"])

    def test_short_credentials_do_not_corrupt_public_protocol_fields(self):
        task_id = "a" * 32
        agent_id = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        payload = {"ok": True, "tasks": [{"task_id": task_id, "status": "completed",
                   "agent_id": agent_id, "execution_state": "succeeded",
                   "resolved_route": {"provider": "openai_compatible_local", "model": "qwen3.8-flash-next-iq3_xxs", "effort": "thinking"},
                   "result": {"checks": {"note": "private-password", "status": "private-password"}}}]}
        for username in ("ok", "id", "a", "completed", "openai_compatible_local"):
            settings = aoitalk.Settings("http://127.0.0.1:9", username, "private-password")
            bounded = tasks._sanitize(payload, settings)
            TypeAdapter(ListReply).validate_python(bounded)
            task = bounded["tasks"][0]
            self.assertEqual(task["task_id"], task_id)
            self.assertEqual(task["agent_id"], agent_id)
            self.assertEqual(task["status"], "completed")
            self.assertEqual(task["resolved_route"], payload["tasks"][0]["resolved_route"])
            self.assertNotIn("private-password", json.dumps(bounded))
        business = tasks._sanitize({"ok": False, "error": "permission_denied", "message": "private-password"},
                                   aoitalk.Settings("http://127.0.0.1:9", "permission_denied", "private-password"))
        self.assertEqual(business["error"], "permission_denied")
        self.assertNotIn("private-password", json.dumps(business))
        for sensitive in ({"ok": True, "idempotency_key": "retry-private-password-1"},
                          {"ok": True, "resolved_route": {"model": "private-password-model"}},
                          {"ok": True, "status": "completed"}):
            password = "completed" if "status" in sensitive else "private-password"
            bounded = tasks._sanitize(sensitive, aoitalk.Settings("http://127.0.0.1:9", "fixture-user", password))
            TypeAdapter(StartReply).validate_python(bounded)
            self.assertNotIn(password, json.dumps(bounded))
            self.assertTrue(bounded["output_truncated"])

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


    async def test_same_key_rejects_semantically_different_requests(self):
        agent_id = str(uuid4())
        first = await self.start(idempotency_key="route-key", agent_id=agent_id,
                                 provider="openai_compatible_local")
        for change in ({"agent_id": str(uuid4())}, {"model": "other-model"},
                       {"workspace": "C:\\different"}, {"allow_commands": False}, {"mode": "mutate"}):
            arguments = {"agent_id": agent_id, "provider": "openai_compatible_local", **change}
            result = await self.start(idempotency_key="route-key", **arguments)
            self.assertEqual(result["error"], "idempotency_conflict")
            self.assertEqual(result["idempotency_key"], "route-key")
            self.assertFalse(result["submission_uncertain"])
        self.assertEqual(len(self.api.tasks), 1)
        self.assertIn(first["task_id"], self.api.tasks)

    async def test_lost_acceptance_response_reuses_same_task_on_retry(self):
        handler = self.api.handler
        def lost_response(request):
            response = handler(request)
            if request.method == "POST" and request.url.path == "/api/local-agent/tasks":
                raise httpx.ReadTimeout("response lost")
            return response
        self.api.handler = lost_response
        failed = await self.start(idempotency_key="lost-response")
        self.assertTrue(failed["submission_uncertain"])
        self.assertEqual(failed["idempotency_key"], "lost-response")
        self.assertEqual(len(self.api.tasks), 1)
        existing_id = next(iter(self.api.tasks))
        self.api.handler = handler
        retried = await self.start(idempotency_key="lost-response")
        self.assertEqual(retried["task_id"], existing_id)
        self.assertFalse(retried["created"])
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


    async def test_route_expectations_and_agent_forwarded_without_default_changes(self):
        result = await self.start(agent_id="agent-qwen", provider="openai_compatible_local",
                                  model="qwen3.8-flash-next-iq3_xxs", effort="medium", allow_commands=False)
        body = self.api.tasks[result["task_id"]]["body"]
        for name, expected in (("agent_id", "agent-qwen"), ("provider", "openai_compatible_local"),
                               ("model", "qwen3.8-flash-next-iq3_xxs"), ("effort", "medium")):
            self.assertEqual(body[name], expected)
        self.assertFalse(body["allow_commands"])
        explicit = await self.start(allow_commands=True)
        self.assertTrue(self.api.tasks[explicit["task_id"]]["body"]["allow_commands"])
        legacy = await self.start()
        legacy_body = self.api.tasks[legacy["task_id"]]["body"]
        self.assertNotIn("allow_commands", legacy_body)
        for name in ("agent_id", "provider", "model", "effort"):
            self.assertNotIn(name, legacy_body)

    async def test_invalid_expectations_fail_before_network(self):
        for name in ("agent_id", "provider", "model", "effort"):
            for value in ("", " ", "x" * 257, "line\nfeed", True, {}):
                self.assertEqual((await self.start(**{name: value}))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_identity_routes_and_exact_execution_state_are_visible(self):
        task_id = (await self.start())["task_id"]
        route = {"provider": "openai_compatible_local", "model": "qwen3.8-flash-next-iq3_xxs", "effort": None}
        self.api.tasks[task_id].update(agent_id="agent-a", agent_revision_id="revision-a",
            agent_run_id="run-a", work_id="work-a", session_id="session-a", status="waiting",
            execution_state="loading_model", requested_route=route,
            resolved_route={**route, "api_key": "must-not-return"})
        value = await tasks.aoitalk_local_task_status(task_id)
        self.assertEqual(value["resolved_route"], route)
        self.assertEqual(value["agent_run_id"], "run-a")
        self.assertEqual(value["status"], "waiting")
        self.assertEqual(value["execution_state"], "loading_model")
        self.assertIn("selected model", value["next_step"])
        listed = await tasks.aoitalk_local_task_list()
        self.assertEqual(listed["tasks"][0]["agent_revision_id"], "revision-a")
        self.assertNotIn("must-not-return", json.dumps(listed))

    async def test_approval_interruption_and_unknown_states(self):
        task_id = (await self.start())["task_id"]
        for status in ("waiting_approval", "interrupted"):
            self.api.tasks[task_id]["status"] = status
            result = await tasks.aoitalk_local_task_status(task_id)
            self.assertTrue(result["ok"])
            self.assertIn("approval" if status == "waiting_approval" else "done", result["next_step"])
            self.assertTrue((await tasks.aoitalk_local_task_tail(task_id))["ok"])
        for status in ("unrecognized", ["running"]):
            self.api.tasks[task_id]["status"] = status
            self.assertEqual((await tasks.aoitalk_local_task_status(task_id))["error"], "invalid_response")
            self.assertEqual((await tasks.aoitalk_local_task_list())["error"], "invalid_response")

    async def test_canonical_agent_errors_remain_actionable(self):
        for code in ("agent_not_configured", "agent_not_found", "agent_inactive", "agent_route_mismatch",
                     "agent_revision_missing", "permission_denied", "canonical_runtime_unavailable"):
            self.api.post_error = (409, {"error": code})
            result = await self.start()
            self.assertEqual(result["error"], code)
            self.assertFalse(result["submission_uncertain"])

    async def test_command_runtime_unavailable_is_actionable_and_not_retried(self):
        self.api.post_error = (409, {"error": "command_runtime_unavailable",
                                    "detail": "sandbox unavailable " + PASSWORD})
        result = await self.start(mode="mutate", allow_commands=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "command_runtime_unavailable")
        self.assertEqual(result["http_status"], 409)
        self.assertFalse(result["submission_uncertain"])
        self.assertEqual(result["next_step"], "resolve_error")
        self.assertEqual(self.api.calls.count(("POST", "/api/local-agent/tasks")), 1)
        self.assertEqual(self.api.tasks, {})
        self.assert_private(result)

    async def test_success_payload_is_sanitized_and_bounded(self):
        task_id = (await self.start())["task_id"]
        self.api.tasks[task_id].update(summary_text=PASSWORD + " session=fixture Bearer abcdefghijklmnop",
            llm={"password": PASSWORD, "api_key": "private-api-value", "model": "fixture"},
            result={"checks": {"token": "token=private-token-value", "image_base64": "A" * 1000000},
                    "report": "x" * 200000})
        result = await tasks.aoitalk_local_task_status(task_id)
        self.assertTrue(result["ok"])
        self.assert_private(result)
        self.assertNotIn("private-api-value", json.dumps(result))
        self.assertNotIn("private-token-value", json.dumps(result))
        self.assertNotIn("image_base64", json.dumps(result))
        self.assertLess(len(json.dumps(result)), 65000)


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
                            self.assertEqual(listing[fn.__name__].output_schema["type"], "object")
                            self.assertIn("ok", listing[fn.__name__].output_schema["required"])
                        start_schema = listing["aoitalk_local_task_start"].input_schema
                        self.assertEqual(start_schema["required"], ["goal", "workspace"])
                        self.assertEqual(start_schema["properties"]["mode"]["enum"], ["read_only", "mutate"])
                        self.assertEqual(start_schema["properties"]["goal"]["maxLength"], 8000)
                        arrays = start_schema["properties"]["constraints"]["anyOf"]
                        self.assertEqual(arrays[0]["items"]["type"], "string")
                        self.assertEqual(arrays[0]["items"]["maxLength"], 1000)
                        self.assertIn({"type": "null"}, arrays)
                        self.assertEqual(arrays[0]["maxItems"], 20)
                        self.assertEqual(start_schema["properties"]["allow_commands"]["default"], None)
                        states = listing["aoitalk_local_task_list"].input_schema["properties"]["status"]["anyOf"]
                        self.assertEqual(set(states[0]["enum"]), tasks.STATES)
                        output = listing["aoitalk_local_task_list"].output_schema
                        self.assertIn("$defs", output)
                        self.assertEqual(output["properties"]["tasks"]["type"], "array")
                        task_ref = output["properties"]["tasks"]["items"]["$ref"]
                        task_schema = output["$defs"][task_ref.rsplit("/", 1)[-1]]
                        self.assertEqual(set(task_schema["properties"]["status"]["enum"]), tasks.STATES)
                        route = task_schema["properties"]["resolved_route"]["anyOf"]
                        self.assertIn({"type": "null"}, route)
                        self.assertIn("$ref", route[0])
                        self.assertIn("error", output["properties"])
                        self.assertIn("events", listing["aoitalk_local_task_tail"].output_schema["properties"])
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
                        self.assertNotIn("error", value)  # no optional defaults inserted
                        self.assertEqual(json.loads(started.content[0].text), value)
                        record = api.tasks[value["task_id"]]
                        record.update(resolved_route={"provider": "openai_compatible_local", "model": "fixture-model", "effort": None},
                                      changed_files=[], result={"checks": {"nested": [None, {"passed": True}]},
                                                                "commands": [{"stdout": "fixture", "exit_code": 0}]})
                        status = await session.call_tool("aoitalk_local_task_status", {"task_id": value["task_id"]})
                        self.assertEqual(status.structured_content["status"], "queued")
                        self.assertEqual(status.structured_content["result"], record["result"])
                        self.assertIsNone(status.structured_content["resolved_route"]["effort"])
                        self.assertEqual(status.structured_content["changed_files"], [])
                        tail = await session.call_tool("aoitalk_local_task_tail", {"task_id": value["task_id"], "limit": 3})
                        self.assertEqual(len(tail.structured_content["events"]), 3)
                        listed = await session.call_tool("aoitalk_local_task_list", {})
                        self.assertEqual(listed.structured_content["tasks"][0]["task_id"], value["task_id"])
                        cancel = await session.call_tool("aoitalk_local_task_cancel", {"task_id": value["task_id"]})
                        self.assertEqual(cancel.structured_content["status"], "cancelled")
                        api.post_error = (409, {"error": "agent_route_mismatch"})
                        rejected = await session.call_tool("aoitalk_local_task_start", {
                            "goal": "inspect", "workspace": WORKSPACE, "idempotency_key": "stdio-error"})
                        self.assertFalse(rejected.is_error)
                        self.assertFalse(rejected.structured_content["ok"])
                        self.assertEqual(rejected.structured_content["error"], "agent_route_mismatch")
                        self.assertEqual(json.loads(rejected.content[0].text), rejected.structured_content)
                        calls_before = len(api.calls)
                        invalid = await session.call_tool("aoitalk_local_task_start", {
                            "goal": "inspect", "workspace": WORKSPACE, "mode": "arbitrary"})
                        self.assertTrue(invalid.is_error)
                        self.assertEqual(len(api.calls), calls_before)
                        # The bounded reply must remain a valid MCP result even
                        # when a legitimate large task list exhausts its budget.
                        for index in range(100):
                            task_id = f"{index:032x}"
                            api.tasks[task_id] = {**record, "task_id": task_id, "goal": "g" * 8000}
                        bounded = await session.call_tool("aoitalk_local_task_list", {"limit": 100})
                        self.assertFalse(bounded.is_error)
                        self.assertTrue(bounded.structured_content["ok"])
                        self.assertTrue(bounded.structured_content["output_truncated"])
                        for item in bounded.structured_content["tasks"]:
                            self.assertRegex(item["task_id"], tasks.TASK_ID_RE)
                            self.assertIn(item["status"], tasks.STATES)
                        for result in [started, status, tail, listed, cancel]:
                            self.assertNotIn(PASSWORD, result.model_dump_json())
                            self.assertNotIn("session=fixture", result.model_dump_json())

        api = API()
        with tempfile.TemporaryDirectory() as cwd, fixture_server(api) as url:
            path = Path(cwd) / "app.env"
            path.write_text(f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\nAOITALK_BOOTSTRAP_ADMIN_PASSWORD='{PASSWORD}'\n",
                            encoding="utf-8")
            anyio.run(check, url, str(path), cwd)
        self.assertEqual(len(api.tasks), 101)

    def test_idempotent_start_survives_stdio_reconnect(self):
        async def check(url, env_path, cwd):
            launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)], cwd=cwd,
                env={"HOME_MCP_AOITALK_ENV_FILE": env_path, "HOME_MCP_AOITALK_URL": url,
                     "HOME_MCP_AOITALK_ENABLED": "true"})
            task_id = None
            for attempt in range(2):
                with anyio.fail_after(20):
                    async with stdio_client(params) as (read, write):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            reply = await session.call_tool("aoitalk_local_task_start", {
                                "goal": "inspect", "workspace": WORKSPACE, "idempotency_key": "reconnect-1"})
                            self.assertFalse(reply.is_error)
                            result = reply.structured_content
                            self.assertTrue(result["ok"])
                            self.assertEqual(result["created"], attempt == 0)
                            if task_id is None:
                                task_id = result["task_id"]
                            else:
                                self.assertEqual(result["task_id"], task_id)
                            listed = await session.call_tool("aoitalk_local_task_list", {})
                            self.assertEqual(listed.structured_content["tasks"][0]["task_id"], task_id)

        api = API()
        with tempfile.TemporaryDirectory() as cwd, fixture_server(api) as url:
            path = Path(cwd) / "app.env"
            path.write_text(f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\nAOITALK_BOOTSTRAP_ADMIN_PASSWORD='{PASSWORD}'\n",
                            encoding="utf-8")
            anyio.run(check, url, str(path), cwd)
        self.assertEqual(len(api.tasks), 1)


if __name__ == "__main__":
    unittest.main()
