"""AoiTalk CLI-session bridge tests; no live AoiTalk or provider process."""
from __future__ import annotations

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import anyio
import httpx
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from home_mcp_gateway import aoitalk
from home_mcp_gateway import aoitalk_cli_sessions as cli


USER = "fixture-user"
PASSWORD = "fixture-password-${LITERAL}-#-秘密"
SETTINGS = aoitalk.Settings("http://127.0.0.1:3000", USER, PASSWORD)
AGENT_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE = r"D:\Dev\80_HomeMCPGateway"
SESSION_ID = "11111111-1111-4111-8111-111111111111"


class API:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.session = {
            "session_id": SESSION_ID,
            "status": "active",
            "provider": "codex-cli",
            "model": "gpt-5-codex",
            "working_directory": WORKSPACE,
            "native_session_id": "must-not-return",
            "lease_token": "must-not-return",
        }
        self.fail_send = False
        self.fail_reconnect = False
        self.large_event = False
        self.unicode_large_event = False
        self.error_code: str | None = None
        self.create_bodies: list[dict] = []
        self.sent_keys: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/api/auth/login":
            return httpx.Response(
                200,
                json={"authenticated": True, "user": {"password_reset_required": False}},
                headers={"Set-Cookie": "session=fixture; Path=/; HttpOnly"},
            )
        if "session=fixture" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"error": "authentication_failed"})
        if path == "/api/cli-sessions" and request.method == "POST":
            if self.error_code:
                return httpx.Response(409, json={"code": self.error_code})
            body = json.loads(request.content)
            self.create_bodies.append(body)
            return httpx.Response(202, json={**self.session, "success": True, "accepted": True,
                                             "created": True, "idempotency_key": body["idempotency_key"]})
        if path == "/api/cli-sessions" and request.method == "GET":
            return httpx.Response(200, json={"sessions": [self.session]})
        if path == "/api/cli-sessions/collect":
            return httpx.Response(200, json={"sessions": [{
                "session_id": SESSION_ID,
                "work": {"lease_token": "private", "native_session_id": "private",
                         "native_handle": "private", "session_handle": "private",
                         "continuation_handle": "private", "provider_session_id": "private",
                         "conversation_id": "private", "metadata": {"secret": "private"}},
                "events": [{"cursor": "c1", "type": "assistant_text", "text": "あ" * 1000}],
                "next_cursor": "c2",
            } ]})
        if path == f"/api/cli-sessions/{SESSION_ID}":
            return httpx.Response(200, json={**self.session, "success": True, "session": None, "accepted": None})
        if path == f"/api/cli-sessions/{SESSION_ID}/events":
            if self.unicode_large_event:
                text = "あ" * 30_000
            else:
                text = "line one\nline two" if not self.large_event else "x" * 80_000
            return httpx.Response(200, json={"session_id": SESSION_ID, "status": "active",
                                             "events": [{"cursor": "c2", "type": "assistant_text", "text": text}],
                                             "next_cursor": "c3"})
        if path == f"/api/cli-sessions/{SESSION_ID}/messages":
            body = json.loads(request.content)
            if not isinstance(body.get("message"), str):
                return httpx.Response(422, json={"code": "invalid_input", "detail": "message required"})
            key = request.headers.get("idempotency-key", "")
            self.sent_keys.add(key)
            payload = {"session_id": SESSION_ID, "message_id": "msg-1", "status": "running",
                       "idempotency_key": body["idempotency_key"]}
            if self.fail_send:
                self.fail_send = False
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(202, json=payload)
        if path == f"/api/cli-sessions/{SESSION_ID}/cancel":
            return httpx.Response(202, json={"session_id": SESSION_ID, "status": "cancelling"})
        if path == f"/api/cli-sessions/{SESSION_ID}/reconnect":
            if self.fail_reconnect:
                self.fail_reconnect = False
                raise httpx.ReadTimeout("response lost")
            return httpx.Response(202, json={"session_id": SESSION_ID, "status": "reconnecting",
                                             "native_handle": "confirmed", "session_handle": "raw",
                                             "continuation_handle": "raw", "provider_session_id": "raw",
                                             "conversation_id": "raw", "reconciled": True,
                                             "safe_to_send": True})
        return httpx.Response(404, json={"error": "session_not_found"})


@contextmanager
def fixture_server(api: API):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            request = httpx.Request(
                self.command,
                f"http://{self.headers.get('Host', '127.0.0.1')}{urlsplit(self.path).path}",
                headers=dict(self.headers),
                content=raw,
            )
            try:
                response = api.handler(request)
            except httpx.HTTPError:
                self.send_response(500)
                self.end_headers()
                return
            self.send_response(response.status_code)
            for name, value in response.headers.items():
                if name.lower() in {"content-length", "transfer-encoding", "connection"}:
                    continue
                self.send_header(name, value)
            body = response.content
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = do_request
        do_POST = do_request

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class CliSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = API()
        original = httpx.AsyncClient

        def factory(**kwargs):
            return original(transport=httpx.MockTransport(self.api.handler), **kwargs)

        self.settings_patch = patch.object(aoitalk, "load_settings", return_value=SETTINGS)
        self.client_patch = patch.object(aoitalk.httpx, "AsyncClient", side_effect=factory)
        self.settings_patch.start()
        self.client_patch.start()
        self.addCleanup(self.settings_patch.stop)
        self.addCleanup(self.client_patch.stop)

    async def test_create_send_and_read_tools_are_bounded(self):
        created = await cli.aoitalk_cli_session_create(
            AGENT_ID, "codex-cli", "gpt-5-codex", WORKSPACE, "create-1"
        )
        self.assertTrue(created["ok"], created)
        self.assertEqual(created["session_id"], SESSION_ID)
        self.assertNotIn("must-not-return", json.dumps(created))

        sent = await cli.aoitalk_cli_session_send(SESSION_ID, "inspect the repository\n", "send-1")
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(sent["message_id"], "msg-1")
        listed = await cli.aoitalk_cli_session_list()
        self.assertEqual(listed["sessions"][0]["session_id"], SESSION_ID)
        status = await cli.aoitalk_cli_session_status(SESSION_ID)
        self.assertEqual(status["status"], "active")
        tail = await cli.aoitalk_cli_session_tail(SESSION_ID, after="c1", limit=1)
        self.assertEqual(tail["next_cursor"], "c3")
        self.assertEqual(tail["events"][0]["text"], "line one\nline two")
        collected = await cli.aoitalk_cli_session_collect({SESSION_ID: "c1"}, limit_per_session=1)
        self.assertEqual(collected["sessions"][0]["next_cursor"], "c2")
        self.assertNotIn("lease_token", json.dumps(collected))
        cancelled = await cli.aoitalk_cli_session_cancel(SESSION_ID)
        self.assertEqual(cancelled["status"], "cancelling")

    async def test_lost_send_response_is_uncertain_and_same_key_is_safe(self):
        self.api.fail_send = True
        lost = await cli.aoitalk_cli_session_send(SESSION_ID, "continue", "same-key")
        self.assertFalse(lost["ok"])
        self.assertTrue(lost["submission_uncertain"])
        self.assertIn("same idempotency_key", lost["next_step"])
        retry = await cli.aoitalk_cli_session_send(SESSION_ID, "continue", "same-key")
        self.assertTrue(retry["ok"], retry)
        self.assertEqual(retry["idempotency_key"], "same-key")

    async def test_permission_mode_is_forwarded_without_local_fallback(self):
        result = await cli.aoitalk_cli_session_create(
            AGENT_ID, "claude-cli", "claude-sonnet", WORKSPACE, "permission-1",
            permission_mode="accept_edits",
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.api.create_bodies[-1]["permission_mode"], "accept_edits")

    async def test_send_key_preserves_raw_whitespace_for_service_fingerprint(self):
        result = await cli.aoitalk_cli_session_send(SESSION_ID, "raw", " key ")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["idempotency_key"], " key ")

    async def test_reconnect_timeout_is_not_replayed(self):
        self.api.fail_reconnect = True
        lost = await cli.aoitalk_cli_session_reconnect(SESSION_ID)
        self.assertFalse(lost["ok"])
        self.assertTrue(lost["submission_uncertain"])
        self.assertIn("do not resend reconnect", lost["next_step"])

    async def test_reconnect_redacts_all_provider_handle_aliases(self):
        result = await cli.aoitalk_cli_session_reconnect(SESSION_ID)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["reconciled"])
        self.assertTrue(result["safe_to_send"])
        for marker in ("confirmed", '"raw"'):
            self.assertNotIn(marker, json.dumps(result))

    async def test_large_event_blocks_cursor_advance(self):
        self.api.large_event = True
        tail = await cli.aoitalk_cli_session_tail(SESSION_ID, after="c1", limit=1)
        self.assertTrue(tail["output_truncated"])
        self.assertTrue(tail["cursor_advance_blocked"])
        self.assertNotIn("next_cursor", tail)
        self.assertLessEqual(
            len(json.dumps(tail, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            cli.MAX_OUTPUT_BYTES,
        )

    async def test_unicode_wire_byte_budget_blocks_cursor_advance(self):
        self.api.unicode_large_event = True
        tail = await cli.aoitalk_cli_session_tail(SESSION_ID, after="c1", limit=1)
        self.assertTrue(tail["output_truncated"])
        self.assertTrue(tail["cursor_advance_blocked"])
        self.assertNotIn("next_cursor", tail)
        self.assertLessEqual(
            len(json.dumps(tail, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
            cli.MAX_OUTPUT_BYTES,
        )

    async def test_canonical_service_errors_remain_actionable(self):
        for code in ("canonical_runtime_unavailable", "agent_route_mismatch", "workspace_not_allowed",
                     "permission_mode_unsupported", "provider_cache_root_reparse_denied",
                     "provider_state_root_changed"):
            self.api.error_code = code
            result = await cli.aoitalk_cli_session_create(
                AGENT_ID, "codex-cli", "gpt-5-codex", WORKSPACE, "error-key-" + code
            )
            self.assertEqual(result["error"], code)
            self.assertFalse(result["submission_uncertain"])
        self.api.error_code = None

    async def test_validation_happens_before_network(self):
        self.assertEqual(
            (await cli.aoitalk_cli_session_create(AGENT_ID, "codex-cli", "model", "relative", "x"))["error"],
            "invalid_input",
        )
        self.assertEqual((await cli.aoitalk_cli_session_status("../escape"))["error"], "invalid_input")
        self.assertEqual((await cli.aoitalk_cli_session_tail(SESSION_ID, after="bad cursor"))["error"], "invalid_input")
        self.assertEqual((await cli.aoitalk_cli_session_list(limit=0))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_cancelling_status_is_a_valid_list_filter(self):
        result = await cli.aoitalk_cli_session_list(status="cancelling")
        self.assertTrue(result["ok"], result)


class StdioCliSessionTests(unittest.TestCase):
    def test_tools_are_discoverable_with_structured_schema_over_stdio(self):
        async def check(url: str, env_path: str, cwd: str):
            launcher = Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py"
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(launcher)],
                cwd=cwd,
                env={"HOME_MCP_AOITALK_ENV_FILE": env_path,
                     "HOME_MCP_AOITALK_URL": url,
                     "HOME_MCP_AOITALK_ENABLED": "true"},
            )
            with anyio.fail_after(30):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listing = {tool.name: tool for tool in (await session.list_tools()).tools}
                        expected = {fn.__name__ for fn in cli.TOOLS}
                        self.assertTrue(expected <= set(listing))
                        self.assertEqual(len(listing), 92)
                        for name in expected:
                            self.assertEqual(listing[name].output_schema["type"], "object")
                            self.assertIn("ok", listing[name].output_schema["required"])
                        create_schema = listing["aoitalk_cli_session_create"].input_schema
                        self.assertEqual(
                            create_schema["required"],
                            ["agent_id", "provider", "model", "working_directory", "idempotency_key"],
                        )
                        self.assertEqual(
                            create_schema["properties"]["provider"]["enum"],
                            ["codex-cli", "claude-cli", "grok-cli", "antigravity-cli"],
                        )
                        self.assertEqual(
                            create_schema["properties"]["permission_mode"]["enum"],
                            ["native_default", "standard", "accept_edits"],
                        )
                        self.assertTrue(listing["aoitalk_cli_session_status"].annotations.read_only_hint)
                        self.assertTrue(listing["aoitalk_cli_session_reconnect"].annotations.read_only_hint)
                        self.assertFalse(listing["aoitalk_cli_session_send"].annotations.read_only_hint)
                        self.assertFalse(listing["aoitalk_cli_session_cancel"].annotations.read_only_hint)
                        started = await session.call_tool("aoitalk_cli_session_create", {
                            "agent_id": AGENT_ID,
                            "provider": "codex-cli",
                            "model": "gpt-5-codex",
                            "working_directory": WORKSPACE,
                            "idempotency_key": "stdio-create",
                        })
                        self.assertFalse(started.is_error)
                        self.assertEqual(started.structured_content["session_id"], SESSION_ID)
                        status = await session.call_tool("aoitalk_cli_session_status", {"session_id": SESSION_ID})
                        self.assertFalse(status.is_error, status)
                        self.assertTrue(status.structured_content["ok"])
                        self.assertIsNone(status.structured_content["session"])
                        collected = await session.call_tool("aoitalk_cli_session_collect", {
                            "cursors": {SESSION_ID: "c1"},
                            "limit_per_session": 1,
                        })
                        self.assertFalse(collected.is_error, collected)
                        self.assertTrue(collected.structured_content["ok"])
                        self.assertNotIn("ok", collected.structured_content["sessions"][0])

        api = API()
        with tempfile.TemporaryDirectory() as cwd, fixture_server(api) as url:
            env_path = Path(cwd) / "app.env"
            env_path.write_text(
                f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\n"
                f"AOITALK_BOOTSTRAP_ADMIN_PASSWORD='{PASSWORD}'\n",
                encoding="utf-8",
            )
            anyio.run(check, url, str(env_path), cwd)


if __name__ == "__main__":
    unittest.main()
