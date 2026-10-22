"""AoiTalk CLI-session bridge tests; no live AoiTalk or provider process."""
from __future__ import annotations

import json
import importlib
import io
import os
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

import anyio
import httpx
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from home_mcp_gateway import aoitalk
from home_mcp_gateway import aoitalk_agent_tasks as agent_tasks
from home_mcp_gateway import aoitalk_cli_sessions as cli


USER = "fixture-user"
PASSWORD = "fixture-password-${LITERAL}-#-秘密"
SETTINGS = aoitalk.Settings("http://127.0.0.1:3000", USER, PASSWORD)
AGENT_ID = "11111111-1111-1111-1111-111111111111"
WORKSPACE = r"C:\path\to\HomeMCPGateway"
SESSION_ID = "11111111-1111-4111-8111-111111111111"
HTTPX_ASYNC_CLIENT = httpx.AsyncClient


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
        self.send_bodies: list[dict] = []
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
            self.send_bodies.append(body)
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

    async def test_mock_non_codex_session_workflow_forwards_synthetic_repository_without_spawning_cli(self):
        scratch_root = Path(__file__).resolve().parents[1] / ".local"
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cli-session-synthetic-", dir=scratch_root) as directory:
            repository = Path(directory)
            (repository / "README.md").write_text("# Synthetic Fixture\nStatus: ready\n", encoding="utf-8")
            workspace = str(repository)
            self.api.session.update(provider="claude-cli", model="fixture-model", working_directory=workspace)
            message = "Review README.md and report the synthetic project status.\n"
            with patch.object(subprocess, "Popen", side_effect=AssertionError("the Gateway must not spawn a CLI")) as popen:
                created = await cli.aoitalk_cli_session_create(
                    AGENT_ID, "claude-cli", "fixture-model", workspace, "synthetic-create",
                    title="Synthetic repository review", permission_mode="standard",
                )
                sent = await cli.aoitalk_cli_session_send(SESSION_ID, message, "synthetic-send")
                status = await cli.aoitalk_cli_session_status(SESSION_ID)
                tail = await cli.aoitalk_cli_session_tail(SESSION_ID, after="c1", limit=1)
                cancelled = await cli.aoitalk_cli_session_cancel(SESSION_ID)
            popen.assert_not_called()

        self.assertTrue(created["ok"], created)
        self.assertEqual(created["provider"], "claude-cli")
        self.assertTrue(sent["ok"], sent)
        self.assertEqual(status["status"], "active")
        self.assertEqual(tail["events"][0]["text"], "line one\nline two")
        self.assertEqual(cancelled["status"], "cancelling")
        self.assertEqual(self.api.create_bodies[-1]["provider"], "claude-cli")
        self.assertEqual(self.api.create_bodies[-1]["model"], "fixture-model")
        self.assertEqual(self.api.create_bodies[-1]["working_directory"], workspace)
        self.assertEqual(self.api.create_bodies[-1]["title"], "Synthetic repository review")
        self.assertEqual(self.api.send_bodies[-1], {"message": message, "idempotency_key": "synthetic-send"})
        self.assertEqual(self.api.calls, [
            ("POST", "/api/auth/login"), ("POST", "/api/cli-sessions"),
            ("POST", "/api/auth/login"), ("POST", f"/api/cli-sessions/{SESSION_ID}/messages"),
            ("POST", "/api/auth/login"), ("GET", f"/api/cli-sessions/{SESSION_ID}"),
            ("POST", "/api/auth/login"), ("GET", f"/api/cli-sessions/{SESSION_ID}/events"),
            ("POST", "/api/auth/login"), ("POST", f"/api/cli-sessions/{SESSION_ID}/cancel"),
        ])

    async def test_actual_41_asgi_router_and_work_adapter_use_only_fake_backend(self):
        """Route+adapter contract: actual 41 routes/adapter with in-memory rows and fake backend.

        This does not exercise durable CLIDevelopmentSessionService admission or
        AgentWorkCoordinator settlement.
        """
        source_root_value = os.environ.get("AOITALK_SOURCE_WORKTREE")
        if not source_root_value:
            self.skipTest("AOITALK_SOURCE_WORKTREE is required for the cross-worktree contract test")
        source_root = Path(source_root_value).resolve()
        if not (source_root / "src" / "api" / "cli_development_session_routes.py").is_file():
            self.fail(f"41 CLI-session router was not found under {source_root}")
        source_text = str(source_root)
        sys.path.insert(0, source_text)
        try:
            from src.api.cli_development_session_routes import create_cli_development_session_router
            from src.memory.models import AgentRun, AgentWorkItem, ConversationMessage, ConversationSession
            from src.services.agent_work_runtime import WorkClaim
            from src.services.cli_development_sessions import (
                CLI_DEVELOPMENT_CAPABILITIES,
                CLI_DEVELOPMENT_CONTEXT_KEY,
                CLI_SESSION_ADAPTER_KEY,
                CLI_SESSION_SOURCE_TYPE,
            )
            from src.services.cli_session_work_adapter import CLISessionWorkAdapter
        finally:
            sys.path.remove(source_text)

        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse

        owner_id = "11111111-1111-4111-8111-111111111111"
        agent_revision_id = "22222222-2222-4222-8222-222222222222"
        binding_id = "33333333-3333-4333-8333-333333333333"
        session_uuid = uuid.UUID(SESSION_ID)
        work_uuid = uuid.uuid4()
        run_uuid = uuid.uuid4()
        message_uuid = uuid.uuid4()
        request_log: list[tuple[str, str]] = []

        class MemoryDB:
            def __init__(self, service):
                self.service = service

            async def get(self, model, _identifier):
                return {
                    AgentWorkItem: self.service.work,
                    AgentRun: self.service.run,
                    ConversationSession: self.service.conversation,
                    ConversationMessage: self.service.trigger,
                }.get(model)

        class SessionStore:
            async def acquire(self, scope):
                return SimpleNamespace(scope=scope, action="start", native_session_id=None)

            async def record_native_session(self, _lease, native_id):
                self.native_session_id = native_id
                return True

            async def release(self, _lease):
                return True

        class RepositoryBackend:
            def __init__(self, root):
                self.root = Path(root)
                self._last_native_session_id = "fake-native-session"
                self.received_prompt = None

            def execute_prompt(self, prompt, *, cwd, **_kwargs):
                self.received_prompt = prompt
                root = Path(cwd)
                source = root / "calculator.py"
                readme = root / "README.md"
                if "fix the intentional test failure" not in prompt.casefold():
                    return False, "fixture did not receive the expected synthetic repository request"
                if "Status: draft" not in readme.read_text(encoding="utf-8"):
                    return False, "synthetic README did not start in the expected state"
                if "return left + right - 1" not in source.read_text(encoding="utf-8"):
                    return False, "synthetic calculator did not start with the expected defect"

                sys.path.insert(0, str(root))
                try:
                    calculator = importlib.import_module("calculator")
                    test_module = importlib.import_module("test_calculator")
                    first_stream = io.StringIO()
                    first_result = unittest.TextTestRunner(stream=first_stream, verbosity=2).run(
                        unittest.defaultTestLoader.loadTestsFromModule(test_module)
                    )
                    first_output = first_stream.getvalue()
                finally:
                    sys.path.remove(str(root))
                if first_result.wasSuccessful() or "FAIL" not in first_output or "AssertionError" not in first_output:
                    return False, f"synthetic test did not fail as intended: {first_output}"

                inspected_source = source.read_text(encoding="utf-8")
                if "return left + right - 1" not in inspected_source or "AssertionError" not in first_output:
                    return False, "the failing test result was not inspected before the repair"
                source.write_text(
                    inspected_source.replace("return left + right - 1", "return left + right"),
                    encoding="utf-8",
                )
                sys.path.insert(0, str(root))
                try:
                    calculator = importlib.reload(calculator)
                    test_module.add = calculator.add
                    second_stream = io.StringIO()
                    second_result = unittest.TextTestRunner(stream=second_stream, verbosity=2).run(
                        unittest.defaultTestLoader.loadTestsFromModule(test_module)
                    )
                    second_output = second_stream.getvalue()
                finally:
                    sys.path.remove(str(root))
                    sys.modules.pop("test_calculator", None)
                    sys.modules.pop("calculator", None)
                if not second_result.wasSuccessful() or "OK" not in second_output:
                    return False, f"synthetic test did not pass after repair: {second_output}"

                contents = readme.read_text(encoding="utf-8")
                readme.write_text(contents.replace("Status: draft", "Status: reviewed"), encoding="utf-8")
                return True, (
                    "Synthetic stdlib unittest failed as expected and the failure was inspected:\n"
                    f"{first_output}\nRepaired calculator.py, then reran the test successfully:\n{second_output}"
                )

            def get_last_cli_diagnostics(self):
                return {"status": "success"}

            def get_last_invocation_config(self):
                return {"model": "fixture-model", "cwd": str(self.root)}

        class InMemorySessionService:
            """Route facade; the actual Work adapter owns the fake-backend turn."""

            def __init__(self, root):
                self.root = Path(root)
                self.owner_id = owner_id
                self.status = "ready"
                self.runtime_events: list[dict] = []
                self.backend_instances: list[RepositoryBackend] = []
                cache_root = self.root / ".fake-provider-cache"
                cache_root.mkdir()
                self.context = {
                    "schema_version": 1,
                    "owner_id": owner_id,
                    "agent_id": AGENT_ID,
                    "agent_revision_id": agent_revision_id,
                    "binding_id": binding_id,
                    "binding_snapshot_hash": "a" * 64,
                    "provider": "claude-cli",
                    "model": "fixture-model",
                    "effort": "",
                    "working_directory": str(self.root),
                    "permission_mode": "standard",
                    "native_state": "unsupported",
                    "native_scope_key": "",
                    "provider_cache_root": str(cache_root),
                    "project_id": None,
                    "authority": {},
                    "policy": {},
                }
                self.conversation = SimpleNamespace(
                    id=session_uuid,
                    user_id=owner_id,
                    agent_id=AGENT_ID,
                    context={CLI_DEVELOPMENT_CONTEXT_KEY: self.context},
                    message_count=1,
                    development_status="working",
                )
                self.work = SimpleNamespace(
                    id=work_uuid,
                    conversation_session_id=session_uuid,
                    active_agent_run_id=run_uuid,
                    source_type=CLI_SESSION_SOURCE_TYPE,
                    execution_adapter=CLI_SESSION_ADAPTER_KEY,
                    state="running",
                    attempt_count=1,
                    lease_owner="fixture-lease-owner",
                    lease_token="fixture-lease-token",
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
                    metadata_json={"owner_id": owner_id},
                    assigned_agent_id=AGENT_ID,
                    agent_revision_id=agent_revision_id,
                )
                self.run = SimpleNamespace(
                    id=run_uuid,
                    session_id=session_uuid,
                    work_item_id=work_uuid,
                    work_item_attempt=1,
                    status="running",
                    run_type=CLI_SESSION_SOURCE_TYPE,
                    trigger_message_id=message_uuid,
                    user_id=owner_id,
                )
                self.trigger = SimpleNamespace(
                    id=message_uuid, session_id=session_uuid, role="user", content=""
                )
                self.db = MemoryDB(self)
                self.session_store = SessionStore()
                self.adapter = CLISessionWorkAdapter(
                    self,
                    backend_factory=self._backend_factory,
                    session_store=self.session_store,
                )

            def _backend_factory(self, *, provider, model, effort, execution_options):
                if (provider, model, effort) != ("claude-cli", "fixture-model", ""):
                    raise AssertionError("the route must forward the fixed non-Codex fixture label")
                backend = RepositoryBackend(execution_options.cwd)
                self.backend_instances.append(backend)
                return backend

            async def _session(self):
                return self.db

            async def _close(self, _db):
                return None

            @staticmethod
            def _context(row):
                raw = row.context.get(CLI_DEVELOPMENT_CONTEXT_KEY, {})
                return dict(raw)

            async def _binding_for_row(self, _db, _conversation):
                return SimpleNamespace(route_snapshot={"roles": {"operator": {
                    "provider": "claude-cli", "model": "fixture-model", "effort": "", "backend": "cli",
                }}})

            def _policy_for_request(self, *, working_directory, **_kwargs):
                return SimpleNamespace(), working_directory

            @staticmethod
            def _validate_permission_mode(_policy, requested):
                return requested

            @staticmethod
            def _validate_provider_permission_mode(_provider, _mode):
                return None

            def _provider_cache_root(self, _policy, _provider):
                return self.context["provider_cache_root"]

            async def _resolve_authority(self, **_kwargs):
                return {}

            async def record_native_scope(self, _session_id, *, scope_key, state):
                self.context["native_scope_key"] = scope_key
                self.context["native_state"] = state

            async def record_runtime_event(self, **event):
                self.runtime_events.append(event)

            async def record_execution_result(self, **result):
                self.status = "completed" if result.get("classification") == "succeeded" else "failed"
                output = str(result.get("output") or "")
                self.runtime_events.append({
                    "event_type": "assistant_text",
                    "message": output,
                    "payload": {"text": output},
                })
                return {"discarded": False, "assistant_message_id": "fixture-assistant-message"}

            async def finalize_cancelled_run(self, **_kwargs):
                return None

            def _session_projection(self, status=None):
                return {
                    "session_id": SESSION_ID,
                    "owner_id": owner_id,
                    "agent_id": AGENT_ID,
                    "agent_revision_id": agent_revision_id,
                    "binding_id": binding_id,
                    "provider": self.context["provider"],
                    "model": self.context["model"],
                    "working_directory": str(self.root),
                    "native_state": self.context["native_state"],
                    "run_id": str(run_uuid),
                    "work_item_id": str(work_uuid),
                    "status": status or self.status,
                }

            async def create_session(self, *, request, owner_id, actor_role):
                if owner_id != self.owner_id or actor_role != "admin":
                    raise AssertionError("unexpected route principal")
                self.context.update({
                    "agent_id": request["agent_id"],
                    "provider": request["provider"],
                    "model": request["model"],
                    "effort": request.get("effort") or "",
                    "working_directory": request["working_directory"],
                    "permission_mode": request["permission_mode"],
                })
                return {"session": self._session_projection("ready"), "created": True}

            async def send(self, *, session_id, request, owner_id):
                if session_id != SESSION_ID or owner_id != self.owner_id:
                    raise AssertionError("unexpected session owner or id")
                self.trigger.content = request["content"]
                claim = WorkClaim(
                    work_item_id=str(work_uuid),
                    lease_token=self.work.lease_token,
                    lease_owner=self.work.lease_owner,
                    attempt=1,
                    source_type=CLI_SESSION_SOURCE_TYPE,
                    source_id=SESSION_ID,
                    source_revision="fixture-revision",
                    intent_key="turn",
                    domain="cli",
                    assigned_agent_id=AGENT_ID,
                    agent_revision_id=agent_revision_id,
                    execution_adapter=CLI_SESSION_ADAPTER_KEY,
                    required_capabilities=tuple(CLI_DEVELOPMENT_CAPABILITIES),
                    project_id=None,
                    task_id=None,
                    persona_id=None,
                    concurrency_key=None,
                    conversation_session_id=SESSION_ID,
                    metadata={"owner_id": owner_id},
                )
                outcome = await self.adapter.execute(claim, coordinator=None, run_id=str(run_uuid))
                if outcome.classification != "succeeded":
                    raise AssertionError(f"fake Work adapter returned {outcome.classification}: {outcome.error_code}")
                return {"session_id": SESSION_ID, "run_id": str(run_uuid), "status": self.status,
                        "message_id": "fixture-assistant-message"}

            async def get_session(self, *, session_id, owner_id):
                if session_id != SESSION_ID or owner_id != self.owner_id:
                    raise AssertionError("unexpected session owner or id")
                return {"session": self._session_projection(), "run_id": str(run_uuid),
                        "run": {"id": str(run_uuid)}, "work": {"id": str(work_uuid)}}

            async def list_events(self, *, session_id, owner_id, after_cursor, limit):
                if session_id != SESSION_ID or owner_id != self.owner_id:
                    raise AssertionError("unexpected session owner or id")
                events = [{
                    "event_id": f"run:fixture-{index}",
                    "source": "run",
                    "run_id": str(run_uuid),
                    "sequence": index + 1,
                    "event_type": item.get("event_type", "cli.status"),
                    "status": self.status,
                    "message": item.get("message", ""),
                    "payload": item.get("payload") or {},
                } for index, item in enumerate(self.runtime_events[-limit:])]
                return {"events": events, "next_cursor": "fixture-cursor-2", "has_more": False}

            async def cancel(self, *, session_id, run_id, owner_id):
                if session_id != SESSION_ID or owner_id != self.owner_id or run_id != str(run_uuid):
                    raise AssertionError("unexpected cancellation binding")
                self.status = "cancelling"
                return {"accepted": True, "session_id": SESSION_ID, "run_id": run_id,
                        "work_item_id": str(work_uuid), "status": "cancelling",
                        "session": self._session_projection("cancelling")}

        scratch_root = Path(__file__).resolve().parents[1] / ".local"
        scratch_root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cli-work-asgi-", dir=scratch_root) as directory:
            repository = Path(directory)
            readme = repository / "README.md"
            readme.write_text("# Synthetic Project\nStatus: draft\n", encoding="utf-8")
            (repository / "calculator.py").write_text(
                "def add(left, right):\n    return left + right - 1\n",
                encoding="utf-8",
            )
            (repository / "test_calculator.py").write_text(
                "import unittest\nfrom calculator import add\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
                "if __name__ == '__main__':\n    unittest.main()\n",
                encoding="utf-8",
            )
            service = InMemorySessionService(repository)
            config = {"cli_development_sessions": {"enabled": True}}
            app = FastAPI()

            @app.middleware("http")
            async def record_request(request: Request, call_next):
                request_log.append((request.method, request.url.path))
                return await call_next(request)

            @app.post("/api/auth/login")
            async def login(payload: dict):
                if payload != {"username": USER, "password": PASSWORD, "credential_source": "local"}:
                    return JSONResponse(status_code=401, content={"authenticated": False})
                return JSONResponse(
                    status_code=200,
                    content={"authenticated": True, "user": {"password_reset_required": False}},
                    headers={"Set-Cookie": "session=fixture; Path=/; HttpOnly"},
                )

            async def require_admin():
                return None

            def get_user_from_request(_request: Request):
                return {"id": owner_id, "role": "admin"}

            app.include_router(create_cli_development_session_router(
                require_admin=require_admin,
                get_user_from_request=get_user_from_request,
                service=service,
                get_config=lambda: config,
            ))

            def asgi_client_factory(**kwargs):
                self.assertFalse(kwargs["trust_env"])
                self.assertFalse(kwargs["follow_redirects"])
                return HTTPX_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), **kwargs)

            message = (
                "Fix the intentional test failure in calculator.py. Inspect the failed stdlib test, "
                "repair the function, rerun the test, and update README.md to Status: reviewed."
            )
            with patch.object(subprocess, "Popen", side_effect=AssertionError("no CLI process may be started")) as popen:
                with patch.object(aoitalk.httpx, "AsyncClient", side_effect=asgi_client_factory):
                    created = await cli.aoitalk_cli_session_create(
                        AGENT_ID, "claude-cli", "fixture-model", str(repository), "asgi-create",
                        permission_mode="standard",
                    )
                    sent = await cli.aoitalk_cli_session_send(SESSION_ID, message, "asgi-send")
                    status = await cli.aoitalk_cli_session_status(SESSION_ID)
                    tail = await cli.aoitalk_cli_session_tail(SESSION_ID, after="fixture-cursor-1", limit=10)
                    cancelled = await cli.aoitalk_cli_session_cancel(SESSION_ID)
                popen.assert_not_called()

            self.assertTrue(created["ok"], created)
            self.assertTrue(sent["ok"], sent)
            self.assertEqual(len(service.backend_instances), 1, service.runtime_events)
            self.assertEqual(readme.read_text(encoding="utf-8"), "# Synthetic Project\nStatus: reviewed\n")
            self.assertEqual(
                (repository / "calculator.py").read_text(encoding="utf-8"),
                "def add(left, right):\n    return left + right\n",
            )
            self.assertEqual(status["status"], "completed")
            tail_json = json.dumps(tail, ensure_ascii=False)
            self.assertIn("AssertionError", tail_json)
            self.assertIn("FAIL", tail_json)
            self.assertIn("OK", tail_json)
            self.assertEqual(cancelled["status"], "cancelling")
            self.assertEqual(service.backend_instances[0].received_prompt, message)
            self.assertEqual([path for _method, path in request_log if path != "/api/auth/login"], [
                "/api/cli-sessions",
                f"/api/cli-sessions/{SESSION_ID}/messages",
                f"/api/cli-sessions/{SESSION_ID}",
                f"/api/cli-sessions/{SESSION_ID}/events",
                f"/api/cli-sessions/{SESSION_ID}/cancel",
            ])

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
                        # 92 pre-existing tools plus the generic Agent task tools.
                        agent_task_tools = {"aoitalk_agent_task_start", "aoitalk_agent_task_status",
                                            "aoitalk_agent_task_tail", "aoitalk_agent_task_result",
                                            "aoitalk_agent_task_cancel"}
                        self.assertEqual(agent_task_tools, {fn.__name__ for fn in agent_tasks.TOOLS})
                        self.assertTrue(agent_task_tools <= set(listing))
                        self.assertEqual(len(listing), 92 + len(agent_task_tools))
                        result_tool = listing["aoitalk_agent_task_result"]
                        self.assertTrue(result_tool.annotations.read_only_hint)
                        self.assertFalse(result_tool.annotations.destructive_hint)
                        self.assertEqual(result_tool.input_schema["required"], ["task_id"])
                        self.assertEqual(result_tool.input_schema["properties"]["limit"]["maximum"], 8000)
                        self.assertEqual(result_tool.output_schema["properties"]["content"]["type"], "string")
                        self.assertIn("content_sha256", result_tool.output_schema["properties"])
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
