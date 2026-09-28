"""ClipIngest contract/error tests. No live AoiTalk or paid LLM calls."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

import anyio
import httpx
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from home_mcp_gateway import aoitalk as clip

USER = "fixture-user"
PASSWORD = "fixture-password-${LITERAL}-#-秘密"
SOURCE = "# テスト\n\nリード文。\n\n- 要点\n"
NODE_ID = str(uuid4())
RECEIPT_ID = str(uuid4())
SETTINGS = clip.Settings("http://127.0.0.1:3000", USER, PASSWORD)


class API:
    """Minimal wire-compatible durable jobs/receipt/node API in memory."""
    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.bodies = []
        self.new_status = "succeeded"
        self.action = "create"
        self.login_status = 200
        self.reset = False
        self.cookie = True
        self.lookup_error = False
        self.lose_enqueue_response = False
        self.poll_error = False
        self.finish_on_poll = False
        self.node_missing = False
        self.bad_receipt = False
        self.raw_error = None
        self.cookie_path = "/"

    def route(self, method, path, headers, body):
        self.calls.append((method, path))
        if path == "/api/auth/login":
            assert body["username"] == USER and body["password"] == PASSWORD
            assert body["credential_source"] == "local"
            return self.login_status, {"authenticated": True, "user": {
                "password_reset_required": self.reset}}, ({"Set-Cookie": f"session=fixture; Path={self.cookie_path}; HttpOnly"} if self.cookie else {})
        assert "session=fixture" in headers.get("cookie", "")
        if self.raw_error is not None:
            return 500, self.raw_error, {}
        if path == "/api/docs/ingest/jobs" and method == "GET":
            return 200, {"jobs": list(self.jobs.values())[:1]}, {}
        if "/by-idempotency-key/" in path:
            if self.lookup_error:
                raise httpx.ReadTimeout("SECRET " + PASSWORD)
            key = path.rsplit("/", 1)[-1]
            return (200, self.jobs[key], {}) if key in self.jobs else (404, {"detail": "Not found"}, {})
        if path == "/api/docs/ingest/jobs" and method == "POST":
            key = headers["idempotency-key"]
            self.bodies.append(body)
            self.jobs[key] = {"job_id": str(uuid4()), "status": self.new_status,
                "idempotency_key": key, "target_node_id": body.get("target_node_id"),
                "source_sha256": hashlib.sha256(body["source"].encode("utf-8")).hexdigest(),
                "receipt_id": RECEIPT_ID, "result": {"open_node_id": NODE_ID,
                    "action": self.action}, "error": {"code": "llm_unavailable", "message": PASSWORD}}
            if self.lose_enqueue_response:
                self.lose_enqueue_response = False
                raise httpx.ReadTimeout("SECRET " + PASSWORD)
            return 202, self.jobs[key], {}
        if path.startswith("/api/docs/ingest/jobs/"):
            if self.poll_error:
                raise httpx.ReadTimeout("SECRET " + PASSWORD)
            job = next((v for v in self.jobs.values() if v["job_id"] == path.rsplit("/", 1)[-1]), None)
            if job:
                if self.finish_on_poll:
                    job["status"] = "succeeded"
                return 200, job, {}
            return 404, {}, {}
        if path == f"/api/docs/nodes/{NODE_ID}":
            if self.node_missing:
                return 404, {}, {}
            return 200, {"node": {"id": NODE_ID, "title": "保存タイトル"}}, {}
        if path == f"/api/docs/clip-ingest-receipts/{RECEIPT_ID}":
            job = list(self.jobs.values())[-1]
            return 200, {"receipt": {"id": RECEIPT_ID, "topic_node_id": NODE_ID,
                "source_sha256": "0" * 64 if self.bad_receipt else job["source_sha256"],
                "action": job["result"]["action"], "source_text": "never echo " + PASSWORD}}, {}
        return 404, {}, {}

    def handler(self, request):
        raw = request.content
        body = json.loads(raw) if raw else None
        status, data, headers = self.route(request.method, request.url.path,
                                          dict(request.headers), body)
        return httpx.Response(status, json=data, headers=headers)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "HomeMCPGateway"
        self.root.mkdir()
        self.app = self.root.parent / "41_AoiTalk"
        self.app.mkdir()
        self.patch_root = patch.object(clip, "GATEWAY_ROOT", self.root)
        self.patch_env = patch.dict(os.environ, {}, clear=True)
        self.patch_root.start(); self.patch_env.start()
        self.addCleanup(self.patch_root.stop); self.addCleanup(self.patch_env.stop)

    def credentials(self):
        (self.app / ".env").write_text(
            f'\ufeffAOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\nAOITALK_BOOTSTRAP_ADMIN_PASSWORD=\'{PASSWORD}\'\n', encoding="utf-8")

    def test_missing_credentials(self):
        with self.assertRaises(clip.ClipError) as cm:
            clip.load_settings()
        self.assertEqual(cm.exception.code, "configuration_error")

    def test_defaults_bom_literal_password_and_no_environment_mutation(self):
        self.credentials()
        before = dict(os.environ)
        settings = clip.load_settings()
        self.assertEqual(settings.base_url, "http://127.0.0.1:3000")
        self.assertEqual(settings.password, PASSWORD)
        self.assertEqual(dict(os.environ), before)
        self.assertNotIn(PASSWORD, repr(settings))
        self.assertNotIn(USER, repr(settings))

    def test_gateway_env_overrides_default_and_relative_file_resolution(self):
        (self.root / "config").mkdir()
        (self.root / "config/app.env").write_text(
            f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\nAOITALK_BOOTSTRAP_ADMIN_PASSWORD=abc\n", encoding="utf-8")
        (self.root / ".env").write_text(
            "HOME_MCP_AOITALK_ENV_FILE=config/app.env\nHOME_MCP_AOITALK_URL=https://example.test\n"
            f"HOME_MCP_AOITALK_TARGET_NODE_ID={NODE_ID}\n", encoding="utf-8")
        value = clip.load_settings()
        self.assertEqual(value.target_node_id, NODE_ID)
        self.assertEqual(value.base_url, "https://example.test")
        os.environ["HOME_MCP_AOITALK_URL"] = "http://[::1]:3000"
        self.assertEqual(clip.load_settings().base_url, "http://[::1]:3000")

    def test_unsafe_or_ambiguous_endpoint_configuration(self):
        self.credentials()
        for url in ["http://example.test", "https://user:pass@example.test", "https://example.test?q=x",
                    "https://example.test#fragment", "https://example.test/api", "file:///tmp/local", "not-a-url"]:
            with self.subTest(url=url), patch.dict(os.environ, {"HOME_MCP_AOITALK_URL": url}):
                with self.assertRaises(clip.ClipError):
                    clip.load_settings()

    def test_invalid_default_target(self):
        self.credentials()
        with patch.dict(os.environ, {"HOME_MCP_AOITALK_TARGET_NODE_ID": "../other"}):
            with self.assertRaises(clip.ClipError):
                clip.load_settings()


class ClipTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = API()
        original = httpx.AsyncClient
        def factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return original(transport=httpx.MockTransport(self.api.handler), **kwargs)
        for p in [patch.object(clip, "load_settings", return_value=SETTINGS),
                  patch.object(clip.httpx, "AsyncClient", side_effect=factory),
                  patch.object(clip, "POLL_INTERVAL", .001)]:
            p.start(); self.addCleanup(p.stop)

    async def ingest(self, **kwargs):
        return await clip.aoitalk_clip_ingest(kwargs.pop("source", SOURCE), wait_seconds=kwargs.pop("wait_seconds", 0), **kwargs)

    def assert_private(self, result):
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(PASSWORD, text)
        self.assertNotIn("session=fixture", text)
        self.assertNotIn("never echo", text)

    async def test_success_requires_receipt_and_node_readback_and_default_flags(self):
        result = await self.ingest()
        self.assertTrue(result["saved"], result)
        self.assertTrue(result["verified"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["action"], "create")
        self.assertEqual(self.api.bodies[0], {"source": SOURCE, "upload_ids": [],
            "skip_image_recognition": True, "enable_external_research": False, "target_node_id": None})
        self.assertIn(("GET", f"/api/docs/nodes/{NODE_ID}"), self.api.calls)
        self.assertIn(("GET", f"/api/docs/clip-ingest-receipts/{RECEIPT_ID}"), self.api.calls)
        self.assert_private(result)

    async def test_public_base_path_cookie_is_sent_to_api_root(self):
        # AOITALK_PUBLIC_BASE_PATH=/at scopes the session cookie to /at while
        # the adapter calls the loopback FastAPI root (/api/...).
        self.api.cookie_path = "/at"
        connection = await clip.aoitalk_clip_connection()
        self.assertTrue(connection["ok"], connection)
        result = await self.ingest()
        self.assertTrue(result["saved"], result)
        self.assert_private(result)

    async def test_connection_check_does_not_write_clips(self):
        result = await clip.aoitalk_clip_connection()
        self.assertTrue(result["ok"])
        self.assertFalse(result["worker_write_tested"])
        self.assertFalse(self.api.bodies)
        self.assert_private(result)

    async def test_same_source_reuses_job_after_restart_without_client_state(self):
        first, second = await self.ingest(), await self.ingest()
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        self.assertEqual(len(self.api.bodies), 1)

    async def test_newlines_only_canonicalized(self):
        first = await self.ingest(source=SOURCE.replace("\n", "\r\n"))
        second = await self.ingest()
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        third = await self.ingest(source=SOURCE + " \t")
        self.assertNotEqual(third["idempotency_key"], second["idempotency_key"])
        self.assertTrue(self.api.bodies[-1]["source"].endswith(" \t"))

    async def test_target_and_research_flags_change_default_key(self):
        first = await self.ingest()
        second = await self.ingest(target_node_id=NODE_ID)
        third = await self.ingest(enable_external_research=True)
        self.assertEqual(len({x["idempotency_key"] for x in [first, second, third]}), 3)
        self.assertTrue(self.api.bodies[-1]["enable_external_research"])

    async def test_configured_target_is_used(self):
        with patch.object(clip, "load_settings", return_value=clip.Settings(SETTINGS.base_url, USER, PASSWORD, NODE_ID)):
            result = await self.ingest()
        self.assertTrue(result["saved"])
        self.assertEqual(self.api.bodies[0]["target_node_id"], NODE_ID)

    async def test_explicit_key_cannot_reuse_different_source_or_explicit_target(self):
        await self.ingest(idempotency_key="fixture-key")
        changed = await self.ingest(idempotency_key="fixture-key", source="different")
        target = await self.ingest(idempotency_key="fixture-key", target_node_id=NODE_ID)
        self.assertEqual(changed["error"], "idempotency_conflict")
        self.assertEqual(target["error"], "idempotency_conflict")
        self.assertEqual(len(self.api.bodies), 1)

    async def test_queued_is_not_saved(self):
        self.api.new_status = "queued"
        result = await self.ingest()
        self.assertTrue(result["ok"])
        self.assertFalse(result["saved"])
        self.assertEqual(result["status"], "queued")

    async def test_polling_to_success(self):
        self.api.new_status = "queued"; self.api.finish_on_poll = True
        result = await self.ingest(wait_seconds=.1)
        self.assertTrue(result["saved"], result)

    async def test_bounded_wait_returns_running(self):
        self.api.new_status = "running"
        result = await self.ingest(wait_seconds=.01)
        self.assertFalse(result["saved"])
        self.assertEqual(result["status"], "running")

    async def test_status_reads_by_id_without_reenqueue(self):
        self.api.new_status = "queued"
        result = await self.ingest()
        self.api.finish_on_poll = True
        status = await clip.aoitalk_clip_status(job_id=result["job_id"], wait_seconds=0)
        self.assertTrue(status["saved"])
        self.assertEqual(len(self.api.bodies), 1)

    async def test_enqueue_response_lost_recovers_by_key_without_second_post(self):
        self.api.lose_enqueue_response = True
        result = await self.ingest()
        self.assertEqual(result["error"], "connection_error")
        self.assertTrue(result["submission_uncertain"])
        status = await clip.aoitalk_clip_status(idempotency_key=result["idempotency_key"], wait_seconds=0)
        self.assertTrue(status["saved"], status)
        self.assertTrue((await self.ingest())["saved"])
        self.assertEqual(len(self.api.bodies), 1)
        self.assert_private(result)

    async def test_lookup_failure_does_not_fall_through_to_post(self):
        self.api.lookup_error = True
        result = await self.ingest()
        self.assertEqual(result["error"], "connection_error")
        self.assertFalse(result["submission_uncertain"])
        self.assertFalse(self.api.bodies)

    async def test_poll_failure_preserves_job_id_and_key(self):
        self.api.new_status = "running"; self.api.poll_error = True
        result = await self.ingest(wait_seconds=.1)
        self.assertFalse(result["saved"])
        self.assertIn("job_id", result)
        self.assertIn("idempotency_key", result)
        self.assertEqual(result["status"], "running")
        self.assert_private(result)

    async def test_failed_job_never_retries_or_claims_success(self):
        self.api.new_status = "failed"
        result = await self.ingest()
        self.assertEqual(result["error"], "job_failed")
        self.assertEqual(result["server_error_code"], "llm_unavailable")
        self.assertFalse((await self.ingest())["saved"])
        self.assertEqual(len(self.api.bodies), 1)
        self.assert_private(result)

    async def test_unknown_status_is_not_success(self):
        self.api.new_status = "completed"
        result = await self.ingest()
        self.assertEqual(result["error"], "invalid_response")
        self.assertFalse(result["saved"])

    async def test_missing_node_and_mismatched_receipt_are_not_saved(self):
        for name in ["node_missing", "bad_receipt"]:
            with self.subTest(name=name):
                setattr(self.api, name, True)
                result = await self.ingest()
                self.assertEqual(result["error"], "verification_failed")
                self.assertEqual(result["status"], "succeeded")
                self.assertFalse(result["saved"])
                setattr(self.api, name, False)

    async def test_duplicate_skip_is_distinguished(self):
        self.api.action = "duplicate_skip"
        result = await self.ingest()
        self.assertTrue(result["saved"])
        self.assertEqual(result["action"], "duplicate_skip")

    async def test_authentication_failure_no_job_calls(self):
        self.api.login_status = 401
        result = await self.ingest()
        self.assertEqual(result["error"], "authentication_failed")
        self.assertEqual(self.api.calls, [("POST", "/api/auth/login")])
        self.assert_private(result)

    async def test_password_reset_is_not_bypassed(self):
        self.api.reset = True
        result = await self.ingest()
        self.assertEqual(result["error"], "password_reset_required")
        self.assertFalse(self.api.bodies)

    async def test_no_cookie_is_not_authenticated(self):
        self.api.cookie = False
        result = await self.ingest()
        self.assertEqual(result["error"], "authentication_failed")
        self.assertFalse(self.api.bodies)

    async def test_server_error_body_never_returned(self):
        self.api.raw_error = {"detail": PASSWORD, "Authorization": "Bearer secret", "source_text": "private"}
        result = await self.ingest()
        self.assertEqual(result["error"], "server_error")
        self.assert_private(result)
        self.assertNotIn("Bearer secret", str(result))

    async def test_no_redirect_is_followed(self):
        self.api.login_status = 307
        result = await self.ingest()
        self.assertEqual(result["error"], "configuration_error")
        self.assertEqual(len(self.api.calls), 1)

    async def test_input_validation_before_login(self):
        for args in [{"source": " "}, {"source": "x" * 100001}, {"target_node_id": "../node"},
                     {"idempotency_key": "bad/key"}, {"idempotency_key": "x" * 129},
                     {"enable_external_research": "false"}, {"wait_seconds": -1},
                     {"wait_seconds": 31}, {"wait_seconds": float("nan")}, {"wait_seconds": True}]:
            with self.subTest(args=list(args)):
                self.assertEqual((await self.ingest(**args))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_status_requires_exactly_one_identifier(self):
        for args in [{}, {"job_id": NODE_ID, "idempotency_key": "key"}, {"job_id": "../../"}]:
            self.assertEqual((await clip.aoitalk_clip_status(**args))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_missing_status_does_not_create(self):
        result = await clip.aoitalk_clip_status(idempotency_key="missing", wait_seconds=0)
        self.assertEqual(result["error"], "not_found")
        self.assertFalse(self.api.bodies)


@contextmanager
def fixture_server(api):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def respond(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            body = json.loads(raw) if raw else None
            status, data, headers = api.route(self.command, self.path.split("?", 1)[0],
                {k.lower(): v for k, v in self.headers.items()}, body)
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


class StdioClipTests(unittest.TestCase):
    def test_new_tools_over_real_stdio_and_local_http(self):
        async def check(url, env_path, cwd):
            launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)], cwd=cwd,
                env={"HOME_MCP_AOITALK_ENV_FILE": env_path, "HOME_MCP_AOITALK_URL": url,
                     "HOME_MCP_AOITALK_TARGET_NODE_ID": "", "HOME_MCP_AOITALK_ENABLED": "true"})
            with anyio.fail_after(35):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listing = {t.name: t for t in (await session.list_tools()).tools}
                        for fn in clip.TOOLS:
                            self.assertIn(fn.__name__, listing)
                            self.assertNotIn("password", str(listing[fn.__name__].input_schema))
                        self.assertFalse(listing["aoitalk_clip_ingest"].annotations.read_only_hint)
                        self.assertTrue(listing["aoitalk_clip_status"].annotations.read_only_hint)
                        connection = await session.call_tool("aoitalk_clip_connection", {})
                        self.assertFalse(connection.is_error)
                        self.assertTrue(connection.structured_content["ok"])
                        saved = await session.call_tool("aoitalk_clip_ingest", {"source": SOURCE, "wait_seconds": 0})
                        self.assertFalse(saved.is_error)
                        self.assertTrue(saved.structured_content["saved"], saved.structured_content)
                        value = saved.structured_content
                        again = await session.call_tool("aoitalk_clip_ingest", {"source": SOURCE, "wait_seconds": 0})
                        self.assertEqual(value["job_id"], again.structured_content["job_id"])
                        status = await session.call_tool("aoitalk_clip_status", {"job_id": value["job_id"], "wait_seconds": 0})
                        self.assertTrue(status.structured_content["verified"])
                        for result in [connection, saved, again, status]:
                            self.assertNotIn(PASSWORD, result.model_dump_json())
                            self.assertNotIn("session=fixture", result.model_dump_json())
        api = API()
        with tempfile.TemporaryDirectory() as cwd, fixture_server(api) as url:
            path = Path(cwd) / "app.env"
            path.write_text(f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\nAOITALK_BOOTSTRAP_ADMIN_PASSWORD='{PASSWORD}'\n", encoding="utf-8")
            anyio.run(check, url, str(path), cwd)
        self.assertEqual(len(api.bodies), 1)


if __name__ == "__main__":
    unittest.main()
