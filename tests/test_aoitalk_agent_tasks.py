"""Generic AoiTalk Agent task bridge tests using an in-memory HTTPX transport."""
from __future__ import annotations

import importlib
import hashlib
import inspect
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs
from uuid import UUID

import httpx
from pydantic import TypeAdapter, ValidationError

from home_mcp_gateway import aoitalk
from home_mcp_gateway import aoitalk_agent_tasks as tasks
from home_mcp_gateway import aoitalk_tasks as legacy_tasks
from home_mcp_gateway.aoitalk_agent_task_schema import (
    ResultReply, StartReply, TailReply, TaskReply, structured_agent_task_tool,
)

USER = "fixture-user"
PASSWORD = "fixture-password-${LITERAL}-#-秘密"
SETTINGS = aoitalk.Settings("http://127.0.0.1:3000", USER, PASSWORD)
TASK_ID = "a" * 32
AGENT_ID = "11111111-2222-3333-4444-555555555555"
WORKSPACE = "C:\\workspace\\fixture-repo"
HTTPX_ASYNC_CLIENT = httpx.AsyncClient


class API:
    def __init__(self):
        self.calls: list[dict] = []
        self.tasks: dict[str, dict] = {}
        self.keys: dict[str, str] = {}
        self.legacy_tasks: dict[str, dict] = {}
        self.legacy_keys: dict[str, str] = {}
        self.fail_after_create = False
        self.error_response = None
        self.status_after_gets: int | None = None
        self.get_count = 0
        self.large_events = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        call = {
            "method": request.method,
            "path": request.url.path,
            "query": parse_qs(request.url.query.decode()),
            "headers": dict(request.headers),
            "body": body,
        }
        self.calls.append(call)
        if request.url.path == "/api/auth/login":
            assert body == {"username": USER, "password": PASSWORD, "credential_source": "local"}
            return httpx.Response(200, json={"authenticated": True, "user": {"password_reset_required": False}},
                                  headers={"Set-Cookie": "session=fixture; Path=/; HttpOnly"})

        assert "session=fixture" in request.headers.get("cookie", "")
        if self.error_response is not None:
            status, payload = self.error_response
            return httpx.Response(status, json=payload)

        if request.method == "POST" and request.url.path == "/api/agent-tasks":
            key = request.headers.get("idempotency-key")
            assert key
            if key in self.keys:
                task_id = self.keys[key]
                previous = self.tasks[task_id]
                if previous["body"] != body:
                    return httpx.Response(409, json={"error": "idempotency_conflict"})
                return httpx.Response(200, json={"task": self.view(previous), "created": False})
            task_id = f"{len(self.tasks) + 1:032x}"
            task = {
                "task_id": task_id,
                "status": "queued",
                "work_item_id": "work-123",
                "agent_run_id": "run-456",
                "agent_id": body["agent_id"],
                "agent_revision_id": "revision-789",
                "cancellation_requested": False,
                "body": body,
            }
            self.tasks[task_id] = task
            self.keys[key] = task_id
            if self.fail_after_create:
                self.fail_after_create = False
                raise httpx.ReadTimeout("fixture response lost", request=request)
            return httpx.Response(202, json={"task": self.view(task), "created": True})

        if request.method == "POST" and request.url.path == "/api/local-agent/tasks":
            key = request.headers.get("idempotency-key")
            assert key
            if key in self.legacy_keys:
                task_id = self.legacy_keys[key]
                previous = self.legacy_tasks[task_id]
                if previous["body"] != body:
                    return httpx.Response(409, json={"error": "idempotency_conflict"})
                return httpx.Response(200, json={"task": self.view(previous), "created": False})
            task_id = f"legacy-{len(self.legacy_tasks) + 1:032x}"[-32:]
            task = {"task_id": task_id, "status": "queued", "executor": "aoitalk_local",
                    "workspace": body["workspace"], "mode": body["mode"], "goal": body["goal"],
                    "body": body}
            self.legacy_tasks[task_id] = task
            self.legacy_keys[key] = task_id
            return httpx.Response(202, json={"task": self.view(task), "created": True})

        parts = request.url.path.split("/")
        if len(parts) >= 5 and parts[1:4] == ["api", "local-agent", "tasks"]:
            task = self.legacy_tasks.get(parts[4])
            if task is None:
                return httpx.Response(404, json={"detail": "task not found"})
            if request.method == "GET" and len(parts) == 5:
                return httpx.Response(200, json={"task": self.view(task)})
            if request.method == "GET" and parts[5:] == ["tail"]:
                return httpx.Response(200, json={"task_id": task["task_id"], "status": task["status"],
                                                  "events": [{"message": "legacy-event"}],
                                                  "truncated": False, "event_count": 1})
            if request.method == "POST" and parts[5:] == ["cancel"]:
                task["status"] = "cancelled"
                return httpx.Response(200, json={"task": self.view(task)})
        if len(parts) >= 4 and parts[1:3] == ["api", "agent-tasks"]:
            task = self.tasks.get(parts[3])
            if task is None:
                return httpx.Response(404, json={"detail": "private " + PASSWORD})
            if request.method == "GET" and len(parts) == 4:
                self.get_count += 1
                if self.status_after_gets is not None and self.get_count >= self.status_after_gets:
                    task["status"] = "completed"
                    task["is_terminal"] = True
                return httpx.Response(200, json={"task": self.view(task)})
            if request.method == "GET" and parts[4:] == ["tail"]:
                query = parse_qs(request.url.query.decode())
                limit = int(query["limit"][0])
                events = [{"sequence": index,
                           "message": "x" * 4000 if self.large_events else f"event-{index}"}
                          for index in range(limit + 2)]
                events[0]["api_key"] = "hidden-api-key"
                events[0]["private_key"] = "hidden-private-key"
                return httpx.Response(200, json={"task_id": task["task_id"], "status": task["status"],
                                                  "events": events, "truncated": True, "event_count": 500})
            if request.method == "POST" and parts[4:] == ["cancel"]:
                task["cancellation_requested"] = True
                return httpx.Response(200, json={"task": self.view(task), "cancellation_requested": True})
        return httpx.Response(404, json={"error": "not_found"})

    @staticmethod
    def view(task: dict) -> dict:
        return {key: value for key, value in task.items() if key != "body"}


class AgentTaskTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = API()
        self._start_counter = 0
        def client_factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return HTTPX_ASYNC_CLIENT(transport=httpx.MockTransport(self.api.handler), **kwargs)

        for mock in (
            patch.object(aoitalk, "load_settings", return_value=SETTINGS),
            patch.object(aoitalk.httpx, "AsyncClient", side_effect=client_factory),
            patch.object(tasks, "POLL_INTERVAL", 0.001),
        ):
            mock.start()
            self.addCleanup(mock.stop)

    async def start(self, **kwargs):
        self._start_counter += 1
        defaults = {
            "goal": "inspect the workspace",
            "workspace": WORKSPACE,
            "agent_id": AGENT_ID,
            "idempotency_key": f"gw-test-{self._start_counter}",
        }
        defaults.update(kwargs)
        return await tasks.aoitalk_agent_task_start(**defaults)

    def assert_private(self, result):
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn(PASSWORD, encoded)
        self.assertNotIn("session=fixture", encoded)
        self.assertNotIn("hidden-api-key", encoded)
        self.assertNotIn("hidden-private-key", encoded)
        self.assertNotIn("Bearer abcdefghijklmnop", encoded)

    async def test_start_only_calls_authenticated_generic_http_endpoint_and_never_spawns_cli(self):
        with patch.object(subprocess, "Popen", side_effect=AssertionError("CLI/process spawn is forbidden")) as popen:
            result = await self.start(
                constraints=["preserve user changes"],
                completion_criteria=["report findings"],
                mode="read_only",
                allow_commands=False,
                provider="anthropic",
                model="claude-sonnet-4",
                effort="High",
                required_capabilities=["workspace_read"],
                idempotency_key="generic-api-task-1",
            )
        popen.assert_not_called()
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "queued")
        self.assertEqual(result["idempotency_key"], "generic-api-task-1")
        self.assertEqual([call["path"] for call in self.api.calls], ["/api/auth/login", "/api/agent-tasks"])
        post = self.api.calls[1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["headers"]["idempotency-key"], "generic-api-task-1")
        self.assertEqual(post["body"], {
            "goal": "inspect the workspace",
            "workspace": WORKSPACE,
            "constraints": ["preserve user changes"],
            "completion_criteria": ["report findings"],
            "mode": "read_only",
            "allow_commands": False,
            "agent_id": str(UUID(AGENT_ID)),
            "provider": "anthropic",
            "model": "claude-sonnet-4",
            "effort": "High",
            "required_capabilities": ["workspace_read"],
        })
        self.assertNotIn("provider_url", post["body"])
        self.assertNotIn("api_key", post["body"])
        self.assert_private(result)
        TypeAdapter(StartReply).validate_python(result)

    async def test_opencode_ip_rotator_route_is_only_forwarded_as_an_expectation(self):
        result = await self.start(
            provider="opencode-ip-rotator",
            model="zen/example-free",
            effort="high",
            idempotency_key="opencode-route-expectation",
        )

        self.assertTrue(result["ok"], result)
        post = next(
            call for call in self.api.calls
            if call["method"] == "POST" and call["path"] == "/api/agent-tasks"
        )
        self.assertEqual(post["body"]["provider"], "opencode-ip-rotator")
        self.assertEqual(post["body"]["model"], "zen/example-free")
        self.assertEqual(post["body"]["effort"], "high")
        self.assertNotIn("base_url", post["body"])
        self.assertNotIn("api_key", post["body"])
        self.assert_private(result)

    async def test_tool_calling_unsupported_error_keeps_safe_code_and_guidance(self):
        self.api.error_response = (
            409,
            {"error": "tool_calling_unsupported", "detail": "raw-provider-exception"},
        )

        result = await self.start(
            provider="opencode-ip-rotator",
            model="zen/example-free",
            idempotency_key="unsupported-tool-calling",
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "tool_calling_unsupported")
        self.assertEqual(result["http_status"], 409)
        self.assertEqual(
            result["message"],
            "対象modelはtool callingに対応していません。対応modelへ変更してください。",
        )
        self.assertNotIn("raw-provider-exception", json.dumps(result, ensure_ascii=False))
        self.assert_private(result)

    async def test_403_keeps_server_code_and_safe_detail_without_secrets(self):
        self.api.error_response = (
            403,
            {"error": "permission_denied",
             "detail": f"Current Agent authority does not permit command_execute token=abc123 {PASSWORD}"},
        )
        result = await self.start(idempotency_key="denied-detail")
        self.assertEqual(result["error"], "permission_denied")
        self.assertEqual(result["http_status"], 403)
        self.assertEqual(result["error_source"], "server_error_code")
        self.assertEqual(result["server_error_code"], "permission_denied")
        self.assertIn("command_execute", result["server_detail"])
        encoded = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("abc123", encoded)
        self.assertNotIn(PASSWORD, encoded)

    async def test_403_without_server_code_is_marked_status_only(self):
        self.api.error_response = (403, {"detail": "An ordinary human session is required"})
        result = await self.start(idempotency_key="denied-status-only")
        self.assertEqual(result["error"], "permission_denied")
        self.assertEqual(result["error_source"], "http_status_only")
        self.assertNotIn("server_error_code", result)
        self.assertEqual(result["server_detail"], "An ordinary human session is required")

    async def test_unlisted_403_server_code_is_reported_not_swallowed(self):
        self.api.error_response = (403, {"error": "future_code_x", "detail": "x"})
        result = await self.start(idempotency_key="denied-unlisted")
        self.assertEqual(result["error"], "permission_denied")
        self.assertEqual(result["server_error_code"], "future_code_x")

    async def test_agent_work_unavailable_error_keeps_safe_code_and_guidance(self):
        self.api.error_response = (
            503,
            {"error": "agent_work_unavailable", "detail": "upstream-runtime-exception"},
        )

        result = await self.start(idempotency_key="agent-work-runtime-unavailable")

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "agent_work_unavailable")
        self.assertEqual(result["http_status"], 503)
        self.assertEqual(
            result["message"],
            "AoiTalkのAgentWork runtimeを利用できません。runtimeの稼働状態を確認してください。",
        )
        self.assertNotIn("upstream-runtime-exception", json.dumps(result, ensure_ascii=False))
        self.assert_private(result)

    def test_agent_task_api_error_codes_have_fixed_messages_and_keep_legacy_codes(self):
        agent_task_api_codes = (
            "invalid_input",
            "invalid_workspace",
            "workspace_not_allowed",
            "permission_denied",
            "command_permission_required",
            "cli_route_not_supported",
            "agent_route_unavailable",
            "agent_route_mismatch",
            "tool_capability_unknown",
            "tool_calling_unsupported",
            "command_tool_unavailable",
            "agent_work_unavailable",
            "unknown_capability",
            "not_found",
            "invalid_idempotency_key",
            "idempotency_conflict",
            "agent_revision_missing",
            "lease_lost",
            "timeout",
            "cancelled",
            "execution_binding_mismatch",
            "command_runtime_unavailable",
            "agent_binding_mismatch",
            "audit_write_failed",
        )
        legacy_codes = (
            "agent_not_configured",
            "agent_not_found",
            "agent_inactive",
            "required_capability_missing",
            "capability_not_granted",
        )

        for code in (*agent_task_api_codes, *legacy_codes):
            with self.subTest(code=code):
                self.assertIn(code, tasks.SERVER_CODES)

        for code in tasks.SERVER_CODES:
            with self.subTest(code=code):
                self.assertIn(code, tasks.MESSAGES)
                self.assertIsInstance(tasks.MESSAGES[code], str)
                self.assertTrue(tasks.MESSAGES[code].strip())

    async def test_uncertain_submit_is_not_retried_and_same_key_replay_returns_same_task(self):
        self.api.fail_after_create = True
        uncertain = await self.start(idempotency_key="same-replay-key")
        self.assertFalse(uncertain["ok"])
        self.assertTrue(uncertain["submission_uncertain"])
        self.assertEqual(uncertain["idempotency_key"], "same-replay-key")
        self.assertIn("same idempotency_key", uncertain["next_step"])
        self.assertEqual(len(self.api.tasks), 1)
        existing_task_id = next(iter(self.api.tasks))
        self.assertEqual([c["path"] for c in self.api.calls].count("/api/agent-tasks"), 1)

        replay = await self.start(idempotency_key="same-replay-key")
        self.assertTrue(replay["ok"], replay)
        self.assertFalse(replay["created"])
        self.assertEqual(replay["task_id"], existing_task_id)
        self.assertEqual(len(self.api.tasks), 1)
        posts = [c for c in self.api.calls if c["path"] == "/api/agent-tasks"]
        self.assertEqual([call["headers"]["idempotency-key"] for call in posts],
                         ["same-replay-key", "same-replay-key"])

    async def test_actual_41_router_normalizes_capabilities_before_admission(self):
        source_root_value = os.environ.get("AOITALK_SOURCE_WORKTREE")
        if not source_root_value:
            self.skipTest("AOITALK_SOURCE_WORKTREE is required for the cross-worktree contract test")
        source_root = Path(source_root_value).resolve()
        route_file = source_root / "src" / "api" / "agent_task_routes.py"
        if not route_file.is_file():
            self.fail(f"41 API-Agent router was not found at {route_file}")
        source_text = str(source_root)
        sys.path.insert(0, source_text)
        try:
            create_agent_task_router = importlib.import_module("src.api.agent_task_routes").create_agent_task_router
        finally:
            sys.path.remove(source_text)

        from fastapi import FastAPI
        from fastapi.responses import JSONResponse

        class InactiveCoordinator:
            def __init__(self):
                self.runtime_checks = 0

            def _runtime_enabled(self):
                self.runtime_checks += 1
                return False

        coordinator = InactiveCoordinator()
        owner_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        config = {"local_agent_tasks": {"allowed_roots": [str(source_root)]}}
        app = FastAPI()
        captured_requests: list[dict] = []

        @app.post("/api/auth/login")
        async def login(payload: dict):
            if payload != {"username": USER, "password": PASSWORD, "credential_source": "local"}:
                return JSONResponse(status_code=401, content={"authenticated": False})
            return JSONResponse(
                status_code=200,
                content={"authenticated": True, "user": {"password_reset_required": False}},
                headers={"Set-Cookie": "session=fixture; Path=/; HttpOnly"},
            )

        async def require_auth_dependency():
            return None

        async def get_user_from_request(request):
            if request.url.path == "/api/agent-tasks":
                captured_requests.append(json.loads(await request.body()))
            return {"id": owner_id, "role": "admin", "actor_type": "human", "is_agent": False,
                    "_authority_source": "web_session"}

        app.include_router(create_agent_task_router(
            get_db_manager=lambda: object(),
            get_coordinator=lambda: coordinator,
            get_user_from_request=get_user_from_request,
            require_auth_dependency=require_auth_dependency,
            config=config,
        ))

        payload = {
            "goal": "inspect the synthetic AoiTalk worktree",
            "workspace": str(source_root),
            "constraints": [],
            "completion_criteria": [],
            "mode": "read_only",
            "allow_commands": False,
            "agent_id": AGENT_ID,
        }
        async with HTTPX_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://test") as raw:
            # This invalid capability is accepted by the route's Pydantic
            # request model, then rejected by the actual service normalization.
            rejected = await raw.post(
                "/api/agent-tasks",
                json={**payload, "required_capabilities": ["workspace.read"]},
                headers={"Idempotency-Key": "actual-normalizer-rejects-dot"},
            )
            duplicate = await raw.post(
                "/api/agent-tasks",
                json={**payload, "required_capabilities": ["workspace_read", "workspace_read"]},
                headers={"Idempotency-Key": "actual-normalizer-rejects-duplicate"},
            )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(rejected.json().get("error"), "invalid_input")
        self.assertEqual(duplicate.status_code, 422, duplicate.text)
        self.assertEqual(duplicate.json().get("error"), "invalid_input")
        self.assertEqual(coordinator.runtime_checks, 0)

        def asgi_client_factory(**kwargs):
            self.assertFalse(kwargs["trust_env"])
            self.assertFalse(kwargs["follow_redirects"])
            return HTTPX_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), **kwargs)

        with patch.object(aoitalk.httpx, "AsyncClient", side_effect=asgi_client_factory):
            multiline_goal = "Inspect the synthetic workspace\tthen review files.\r\nPreserve this line."
            multiline_constraints = ["Keep this constraint\r\nwith an embedded line break\tand tab."]
            multiline_criteria = ["Report the result\twith the same useful CRLF:\r\nready."]
            forwarded = await tasks.aoitalk_agent_task_start(
                multiline_goal, payload["workspace"], AGENT_ID,
                mode="read_only", allow_commands=False,
                provider="anthropic", model="claude-sonnet-4", effort="High",
                constraints=multiline_constraints,
                completion_criteria=multiline_criteria,
                required_capabilities=["workspace_read"],
                idempotency_key="actual-normalizer-accepts-underscore",
            )
        self.assertEqual(captured_requests[-1]["goal"], multiline_goal)
        self.assertEqual(captured_requests[-1]["constraints"], multiline_constraints)
        self.assertEqual(captured_requests[-1]["completion_criteria"], multiline_criteria)
        self.assertEqual(captured_requests[-1]["provider"], "anthropic")
        self.assertEqual(captured_requests[-1]["effort"], "High")
        # The valid 80 value passes 41's real normalization and reaches the
        # admission/runtime boundary; the inactive fixture rejects before a
        # durable task is submitted, so there is no uncertain replay.
        self.assertFalse(forwarded["ok"])
        self.assertEqual(forwarded["http_status"], 503)
        self.assertEqual(forwarded["error"], "agent_work_unavailable")
        self.assertFalse(forwarded["submission_uncertain"])
        self.assertEqual(coordinator.runtime_checks, 1)

    async def test_username_collision_omits_key_but_keeps_uncertain_replay_instructions(self):
        self.api.fail_after_create = True
        uncertain = await self.start(idempotency_key=USER)
        self.assertFalse(uncertain["ok"])
        self.assertTrue(uncertain["submission_uncertain"])
        self.assertNotIn("idempotency_key", uncertain)
        self.assertNotIn(USER, json.dumps(uncertain))
        self.assertIn("exactly the idempotency_key value supplied to the original call", uncertain["next_step"])
        self.assert_private(uncertain)

        replay = await self.start(idempotency_key=USER)
        self.assertTrue(replay["ok"], replay)
        self.assertFalse(replay["created"])
        self.assertNotIn("idempotency_key", replay)
        self.assertNotIn(USER, json.dumps(replay))
        posts = [call for call in self.api.calls if call["path"] == "/api/agent-tasks"]
        self.assertEqual([call["headers"]["idempotency-key"] for call in posts], [USER, USER])

    async def test_existing_local_task_start_keeps_legacy_route_body_and_key_reuse(self):
        first = await legacy_tasks.aoitalk_local_task_start(
            "legacy goal", WORKSPACE, idempotency_key="legacy-api-key"
        )
        second = await legacy_tasks.aoitalk_local_task_start(
            "legacy goal", WORKSPACE, idempotency_key="legacy-api-key"
        )
        self.assertTrue(first["ok"], first)
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(first["task_id"], second["task_id"])
        posts = [call for call in self.api.calls if call["path"] == "/api/local-agent/tasks"]
        self.assertEqual(len(posts), 2)
        self.assertEqual([call["headers"]["idempotency-key"] for call in posts],
                         ["legacy-api-key", "legacy-api-key"])
        expected_body = {
            "goal": "legacy goal",
            "workspace": WORKSPACE,
            "constraints": [],
            "completion_criteria": [],
            "mode": "read_only",
            "allow_adb": False,
            "allow_git_commit": False,
        }
        self.assertEqual(posts[0]["body"], expected_body)
        self.assertEqual(posts[1]["body"], expected_body)
        self.assertEqual([call["path"] for call in self.api.calls], [
            "/api/auth/login", "/api/local-agent/tasks", "/api/auth/login", "/api/local-agent/tasks",
        ])

    async def test_existing_local_task_start_omitted_provider_uses_pinned_agent_route(self):
        started = await legacy_tasks.aoitalk_local_task_start(
            "synthetic edit and test",
            WORKSPACE,
            mode="mutate",
            allow_commands=True,
            idempotency_key="pinned-agent-route",
            agent_id=AGENT_ID,
            provider=None,
        )

        self.assertTrue(started["ok"], started)
        self.assertEqual(started["status"], "queued")
        self.assertEqual(started["agent_id"], str(UUID(AGENT_ID)))
        self.assertEqual(started["agent_revision_id"], "revision-789")
        self.assertEqual(started["idempotency_key"], "pinned-agent-route")
        self.assertIn("aoitalk_agent_task_status", started["next_step"])
        self.assertEqual([call["path"] for call in self.api.calls], [
            "/api/auth/login", "/api/agent-tasks",
        ])
        post = self.api.calls[1]
        self.assertEqual(post["headers"]["idempotency-key"], "pinned-agent-route")
        self.assertEqual(post["body"], {
            "goal": "synthetic edit and test",
            "workspace": WORKSPACE,
            "constraints": [],
            "completion_criteria": [],
            "mode": "mutate",
            "allow_commands": True,
            "agent_id": str(UUID(AGENT_ID)),
        })
        self.assertNotIn("provider", post["body"])
        self.assertNotIn("allow_adb", post["body"])
        self.assertNotIn("allow_git_commit", post["body"])

    async def test_local_task_entry_reads_canonical_task_started_via_agent_id(self):
        # Reproduction: start(agent_id, provider omitted) created a canonical task,
        # but local status/tail only queried /api/local-agent/tasks and got 404.
        started = await legacy_tasks.aoitalk_local_task_start(
            "read-only check", WORKSPACE, mode="read_only", allow_commands=False,
            idempotency_key="canonical-readback", agent_id=AGENT_ID,
        )
        self.assertEqual(started["executor"], "aoitalk_agent_task")
        task_id = started["task_id"]
        task = self.api.tasks[task_id]
        task.update(status="completed", is_terminal=True, summary_text="final answer 44/15",
                    result={"final_response": "44/15", "report": {"duplicates": 0}})
        replay = await legacy_tasks.aoitalk_local_task_start(
            "read-only check", WORKSPACE, mode="read_only", allow_commands=False,
            idempotency_key="canonical-readback", agent_id=AGENT_ID,
        )
        self.assertEqual((replay["task_id"], replay["created"]), (task_id, False))
        self.assertEqual(len(self.api.tasks), 1)
        self.api.calls.clear()

        status = await legacy_tasks.aoitalk_local_task_status(task_id, wait_seconds=0.2)
        self.assertTrue(status["ok"], status)
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["executor"], "aoitalk_agent_task")
        self.assertEqual(status["work_id"], "work-123")
        self.assertEqual(status["agent_run_id"], "run-456")
        self.assertFalse(status["cancel_requested"])
        self.assertEqual(status["summary_text"], "final answer 44/15")
        self.assertEqual(status["result"], {"final_response": "44/15", "report": {"duplicates": 0}})
        self.assertIn("done", status["next_step"])
        LegacyTaskReply = importlib.import_module("home_mcp_gateway.aoitalk_task_schema").TaskReply
        TypeAdapter(LegacyTaskReply).validate_python(status)
        self.assertEqual([call["path"] for call in self.api.calls], [
            "/api/auth/login", f"/api/local-agent/tasks/{task_id}", f"/api/agent-tasks/{task_id}",
        ])

        tail = await legacy_tasks.aoitalk_local_task_tail(task_id, limit=3, max_bytes=2048)
        self.assertTrue(tail["ok"], tail)
        self.assertEqual(len(tail["events"]), 3)
        self.assertEqual(self.api.calls[-1]["path"], f"/api/agent-tasks/{task_id}/tail")
        self.assertEqual(self.api.calls[-1]["query"], {"limit": ["3"], "max_bytes": ["2048"]})
        self.assert_private(tail)

        task["status"] = "running"
        cancelled = await legacy_tasks.aoitalk_local_task_cancel(task_id)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertTrue(cancelled["cancel_requested"])
        self.assertEqual(self.api.calls[-1]["path"], f"/api/agent-tasks/{task_id}/cancel")
        self.assertEqual(len(self.api.tasks), 1)

    async def test_local_task_status_schema_accepts_null_result_in_every_state(self):
        # Regression: running canonical tasks carry result=None, which the published
        # legacy TaskReply schema rejected (INVALID_ARGUMENT on status only).
        LegacyTaskReply = importlib.import_module("home_mcp_gateway.aoitalk_task_schema").TaskReply
        started = await legacy_tasks.aoitalk_local_task_start(
            "read-only check", WORKSPACE, mode="read_only", allow_commands=False,
            idempotency_key="canonical-null-result", agent_id=AGENT_ID,
        )
        task_id = started["task_id"]
        terminal = {"completed", "failed", "cancelled"}
        for state in ("queued", "running", "waiting", "failed", "cancelled", "completed"):
            self.api.tasks[task_id].update(status=state, is_terminal=state in terminal, result=None)
            status = await legacy_tasks.aoitalk_local_task_status(task_id)
            self.assertTrue(status["ok"], (state, status))
            self.assertEqual(status["status"], state)
            TypeAdapter(LegacyTaskReply).validate_python(status)
            self.assertIsNone(status["result"], state)
        self.assertEqual(len(self.api.tasks), 1)

    async def test_local_task_entry_unknown_id_stays_not_found_without_extra_routes(self):
        unknown = "f" * 32
        for fn in (legacy_tasks.aoitalk_local_task_status, legacy_tasks.aoitalk_local_task_tail,
                   legacy_tasks.aoitalk_local_task_cancel):
            self.api.calls.clear()
            result = await fn(unknown)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error"], "not_found")
            self.assert_private(result)
            paths = [call["path"] for call in self.api.calls if call["path"] != "/api/auth/login"]
            self.assertEqual(len(paths), 2)
            self.assertTrue(paths[0].startswith(f"/api/local-agent/tasks/{unknown}"))
            self.assertTrue(paths[1].startswith(f"/api/agent-tasks/{unknown}"))

    async def test_local_task_entry_permission_error_is_not_rerouted(self):
        self.api.error_response = (403, {"error": "permission_denied"})
        result = await legacy_tasks.aoitalk_local_task_status("e" * 32)
        self.assertEqual(result["error"], "permission_denied")
        self.assertNotIn("/api/agent-tasks/" + "e" * 32, [call["path"] for call in self.api.calls])

    async def test_legacy_local_task_status_tail_cancel_stay_on_legacy_route(self):
        started = await legacy_tasks.aoitalk_local_task_start("legacy goal", WORKSPACE,
                                                              idempotency_key="legacy-readback")
        task_id = started["task_id"]
        self.api.legacy_tasks[task_id].update(status="completed", summary_text="legacy done")
        status = await legacy_tasks.aoitalk_local_task_status(task_id)
        tail = await legacy_tasks.aoitalk_local_task_tail(task_id)
        self.assertEqual((status["status"], status["executor"], status["summary_text"]),
                         ("completed", "aoitalk_local", "legacy done"))
        self.assertEqual(tail["events"], [{"message": "legacy-event"}])
        self.api.legacy_tasks[task_id]["status"] = "running"
        cancelled = await legacy_tasks.aoitalk_local_task_cancel(task_id)
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertFalse([call for call in self.api.calls if call["path"].startswith("/api/agent-tasks/")])

    async def test_status_wait_and_tail_values_are_bounded(self):
        started = await self.start(idempotency_key="bounded-task")
        task_id = started["task_id"]
        self.api.status_after_gets = 2
        status = await tasks.aoitalk_agent_task_status(task_id, wait_seconds=0.2)
        self.assertEqual(status["status"], "completed")
        self.assertTrue(status["is_terminal"])
        TypeAdapter(TaskReply).validate_python(status)

        tail = await tasks.aoitalk_agent_task_tail(task_id, limit=5, max_bytes=2048)
        call = self.api.calls[-1]
        self.assertEqual(call["path"], f"/api/agent-tasks/{task_id}/tail")
        self.assertEqual(call["query"], {"limit": ["5"], "max_bytes": ["2048"]})
        self.assertEqual(len(tail["events"]), 5)
        self.assertTrue(tail["truncated"])
        self.assert_private(tail)
        TypeAdapter(TailReply).validate_python(tail)

        self.api.large_events = True
        large_tail = await tasks.aoitalk_agent_task_tail(task_id, limit=200, max_bytes=64000)
        self.assertLessEqual(len(json.dumps(large_tail, ensure_ascii=False).encode("utf-8")),
                             tasks.MAX_OUTPUT_BYTES)
        self.assertTrue(large_tail["output_truncated"])

        before = len(self.api.calls)
        for wait in (-1, 20.01, float("nan"), True, "1"):
            self.assertEqual((await tasks.aoitalk_agent_task_status(task_id, wait_seconds=wait))["error"],
                             "invalid_input")
        for args in ({"limit": 0}, {"limit": 201}, {"max_bytes": 1023}, {"max_bytes": 64001}):
            self.assertEqual((await tasks.aoitalk_agent_task_tail(task_id, **args))["error"], "invalid_input")
        self.assertEqual(len(self.api.calls), before)

    async def test_status_relays_secret_free_failure_detail(self):
        task_id = (await self.start(idempotency_key="failure-task"))["task_id"]
        task = self.api.tasks[task_id]
        task["status"] = "failed"
        task["is_terminal"] = True
        task["failure"] = {
            "reason": "task_unresolved_tool_failure",
            "unresolved_tools": [
                {"tool": "get_repo_map", "denied": True, "reason": "agent_authority_denied",
                 "arguments": {"api_key": "hidden"}, "output": PASSWORD},
                {"tool": "bm25_search", "denied": False, "reason": "tool_execution_failed"},
            ],
            "successful_tool_count": 94,
            "successful_tools": ["read_file"],
        }
        status = await tasks.aoitalk_agent_task_status(task_id)
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["failure"]["reason"], "task_unresolved_tool_failure")
        self.assertEqual(status["failure"]["unresolved_tools"], [
            {"tool": "get_repo_map", "denied": True, "reason": "agent_authority_denied"},
            {"tool": "bm25_search", "denied": False, "reason": "tool_execution_failed"},
        ])
        self.assertEqual(status["failure"]["successful_tool_count"], 94)
        self.assertNotIn(PASSWORD, json.dumps(status))
        self.assertNotIn("hidden", json.dumps(status))
        TypeAdapter(TaskReply).validate_python(status)

        task["failure"]["successful_tools"] = ["t" * 81]
        task["failure"]["unresolved_tools"] = {"tool": "x"}
        degraded = await tasks.aoitalk_agent_task_status(task_id)
        self.assertEqual(degraded["status"], "failed")
        self.assertEqual(degraded["failure"]["successful_tools"], [])
        task["failure"]["reason"] = "x" * 150 + "p4ssw0rd-secret"
        long_reason = await tasks.aoitalk_agent_task_status(task_id)
        self.assertIsNone(long_reason["failure"]["reason"])
        self.assertNotIn("p4ssw0rd", json.dumps(long_reason))
        self.assertEqual(degraded["failure"]["unresolved_tools"], [])

        task["failure"] = "not-a-dict"
        self.assertEqual((await tasks.aoitalk_agent_task_status(task_id))["error"], "invalid_response")

    async def test_cancel_is_only_a_request_until_status_confirms_stop(self):
        started = await self.start(idempotency_key="cancel-task")
        task_id = started["task_id"]
        self.api.tasks[task_id]["status"] = "running"
        cancelled = await tasks.aoitalk_agent_task_cancel(task_id)
        self.assertTrue(cancelled["ok"], cancelled)
        self.assertTrue(cancelled["cancellation_requested"])
        self.assertEqual(cancelled["status"], "running")
        self.assertNotEqual(cancelled["status"], "cancelled")
        self.assertIn("only a request", cancelled["next_step"])

        self.api.tasks[task_id]["status"] = "stopped"
        status = await tasks.aoitalk_agent_task_status(task_id)
        self.assertEqual(status["status"], "stopped")
        self.assertTrue(status["cancellation_requested"])

    async def test_error_body_and_task_output_are_sanitized(self):
        self.api.error_response = (500, {"detail": PASSWORD, "Authorization": "Bearer abcdefghijklmnop"})
        result = await self.start(idempotency_key="server-error-key")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "server_error")
        self.assertTrue(result["submission_uncertain"])
        self.assertNotIn("detail", result)
        self.assert_private(result)

    async def test_status_preserves_result_code_and_reports_exact_string_clipping(self):
        task_id = (await self.start())["task_id"]
        code = 'def hello():\r\n\tif True:\r\n\t\treturn "秘密😀e\u0301"\r\n'
        task = self.api.tasks[task_id]
        task.update(status="completed", result={"response": code}, summary_text="a\r\n\tb")
        reply = await tasks.aoitalk_agent_task_status(task_id)
        self.assertEqual(reply["result"]["response"], code)
        self.assertEqual(reply["summary_text"], "ab")
        self.assertNotIn("output_truncated", reply)
        compile(reply["result"]["response"], "<answer>", "exec")
        for size in (3999, 4000, 4001, 33000, 48000):
            with self.subTest(size=size):
                task["result"]["response"] = "x" * size
                reply = await tasks.aoitalk_agent_task_status(task_id)
                self.assertEqual(len(reply["result"]["response"]), min(size, 4000))
                self.assertEqual(reply.get("output_truncated", False), size > 4000)
        # The display helper remains byte-for-byte compatible for short UI/errors.
        self.assertEqual(aoitalk._safe_text("a\r\n\tb\x00" + PASSWORD, SETTINGS), "ab[redacted]")

    async def test_final_result_reconstructs_large_unicode_code_with_stable_digest(self):
        task_id = (await self.start())["task_id"]
        code = 'def hello():\r\n\treturn "日本語😀e\u0301"\r\n' * 1500
        self.assertGreater(len(code), 33000)
        self.api.tasks[task_id].update(status="completed", result={
            "response": code, "reasoning": "hidden reasoning", "logs": ["raw log"],
            "api_key": "hidden-api-key"}, progress={"trace": "private trace"})
        pages, offset, digest = [], 0, None
        while True:
            reply = await tasks.aoitalk_agent_task_result(task_id, offset, 3799, digest)
            self.assertTrue(reply["ok"], reply)
            TypeAdapter(ResultReply).validate_python(reply)
            self.assertEqual(reply["offset"], offset)
            self.assertEqual(reply["total_chars"], len(code))
            self.assertEqual(reply["total_bytes"], len(code.encode("utf-8")))
            self.assertEqual(reply["content_sha256"], hashlib.sha256(code.encode("utf-8")).hexdigest())
            self.assertTrue(reply["output_truncated"])
            encoded = json.dumps(reply, ensure_ascii=False).encode("utf-8")
            self.assertLessEqual(len(encoded), tasks.MAX_OUTPUT_BYTES)
            for hidden in ("hidden reasoning", "raw log", "private trace", "hidden-api-key"):
                self.assertNotIn(hidden, encoded.decode())
            pages.append(reply["content"])
            digest = reply["content_sha256"]
            if reply["next_offset"] is None:
                break
            offset = reply["next_offset"]
        self.assertEqual("".join(pages), code)
        repeat = await tasks.aoitalk_agent_task_result(task_id, 0, 3799, digest)
        self.assertEqual(repeat["content"], pages[0])
        self.assertTrue(all(call["method"] == "GET" for call in self.api.calls[2:]
                            if call["path"] != "/api/auth/login"))

    async def test_result_redacts_full_secrets_before_splitting_at_every_boundary(self):
        task_id = (await self.start())["task_id"]
        secrets = (PASSWORD, USER, "Bearer abcdefghijklmnop", "aoitpat_fixture-token",
                   "sk-fixture-token", "token=cross-page-token", "api_key=hidden-api-key",
                   "private_key=hidden-private-key", "session=fixture")
        for secret in secrets:
            for split in range(1, len(secret)):
                with self.subTest(secret_type=secret.split("=")[0][:8], split=split):
                    prefix = "x" * (40 - split) + " "
                    raw = prefix + secret + "\r\n\tend秘密😀"
                    expected = prefix + "[redacted]\r\n\tend秘密😀"
                    self.api.tasks[task_id].update(status="completed", result={"response": raw})
                    first = await tasks.aoitalk_agent_task_result(task_id, limit=41)
                    content = first["content"]
                    if first["next_offset"] is not None:
                        second = await tasks.aoitalk_agent_task_result(
                            task_id, first["next_offset"], 8000, first["content_sha256"])
                        content += second["content"]
                    self.assertEqual(content, expected)
                    self.assertEqual(first["content_sha256"], hashlib.sha256(expected.encode()).hexdigest())
        # Credentials are redacted before ordinary status clipping too.
        self.api.tasks[task_id]["result"] = {"response": "x" * 3998 + PASSWORD + " end"}
        status = await tasks.aoitalk_agent_task_status(task_id)
        self.assertEqual(status["result"]["response"], "x" * 3998 + "[r")
        self.assertTrue(status["output_truncated"])

    async def test_result_exact_page_boundaries_empty_text_and_legacy_final_answer(self):
        task_id = (await self.start())["task_id"]
        for size in (0, 1, 3999, 4000, 4001, 7999, 8000, 8001):
            with self.subTest(size=size):
                text = "界" * size
                self.api.tasks[task_id].update(status="succeeded", result={"final_response": text})
                page = await tasks.aoitalk_agent_task_result(task_id, limit=4000)
                self.assertTrue(page["ok"], page)
                self.assertEqual(page["content"], text[:4000])
                self.assertEqual(page["next_offset"], 4000 if size > 4000 else None)
                self.assertEqual(page["output_truncated"], size > 4000)
                end = await tasks.aoitalk_agent_task_result(task_id, size, 1, page["content_sha256"])
                self.assertEqual(end["content"], "")
                self.assertIsNone(end["next_offset"])
                past_end = await tasks.aoitalk_agent_task_result(task_id, size + 1, 1, page["content_sha256"])
                self.assertEqual(past_end["error"], "invalid_input")

    async def test_result_validates_inputs_before_http_and_never_reuses_changed_content(self):
        task_id = (await self.start())["task_id"]
        before = len(self.api.calls)
        for bad_id in ("", "../" + task_id, "A" * 32, "a" * 31, task_id + "\n", None, 42):
            reply = await tasks.aoitalk_agent_task_result(bad_id)
            self.assertEqual(reply["error"], "invalid_input")
        for args in ({"offset": -1}, {"offset": True}, {"offset": 1.5}, {"offset": "0"},
                     {"offset": tasks.MAX_RESPONSE_BYTES + 1}, {"offset": 1},
                     {"limit": 0}, {"limit": 8001}, {"limit": True}, {"limit": 1.5},
                     {"expected_digest": "g" * 64}, {"expected_digest": 42}):
            self.assertEqual((await tasks.aoitalk_agent_task_result(task_id, **args))["error"], "invalid_input")
        self.assertEqual(len(self.api.calls), before)
        self.api.tasks[task_id].update(status="completed", result={"response": "first answer"})
        first = await tasks.aoitalk_agent_task_result(task_id, limit=3)
        self.api.tasks[task_id]["result"]["response"] = "changed answer"
        changed = await tasks.aoitalk_agent_task_result(task_id, 3, 3, first["content_sha256"])
        self.assertEqual(changed["error"], "result_changed")
        self.assertNotIn("content", changed)

    async def test_result_rejects_wrong_tasks_statuses_and_non_final_fields(self):
        task_id = (await self.start())["task_id"]
        task = self.api.tasks[task_id]
        for status in ("queued", "running", "waiting", "failed", "cancelled", "stopped", "interrupted", "unknown"):
            task.update(status=status, result={"response": "not final"})
            reply = await tasks.aoitalk_agent_task_result(task_id)
            self.assertEqual(reply["error"], "result_not_ready")
            self.assertNotIn("content", reply)
        task.update(status="completed", is_terminal=False)
        self.assertEqual((await tasks.aoitalk_agent_task_result(task_id))["error"], "invalid_response")
        task["is_terminal"] = True
        for bad_id in ("b" * 32, "invalid", None):
            task["task_id"] = bad_id
            self.assertEqual((await tasks.aoitalk_agent_task_result(task_id))["error"], "invalid_response")
        task["task_id"] = task_id
        for result, error in ((None, "result_unavailable"), ({}, "result_unavailable"),
                              ({"reasoning": "hidden", "logs": ["raw"]}, "result_unavailable"),
                              ({"response": None, "final_response": "do not fall back"}, "invalid_response"),
                              ("not a result object", "invalid_response")):
            task["result"] = result
            reply = await tasks.aoitalk_agent_task_result(task_id)
            self.assertEqual(reply["error"], error)
            self.assertNotIn("content", reply)
        task["status"] = "bad\nstatus"
        self.assertEqual((await tasks.aoitalk_agent_task_result(task_id))["error"], "invalid_response")

    async def test_result_response_caps_and_worst_case_json_page_size(self):
        task_id = (await self.start())["task_id"]
        task = self.api.tasks[task_id]
        # The wire cap includes ALL fields, even those not returned by this tool.
        task.update(status="completed", result={"response": "safe", "logs": "x" * tasks.MAX_RESPONSE_BYTES})
        self.assertEqual((await tasks.aoitalk_agent_task_result(task_id))["error"], "invalid_response")
        task["result"] = {"response": '\\"\t\r\n界😀' * 9000}
        page = await tasks.aoitalk_agent_task_result(task_id, limit=8000)
        self.assertTrue(page["ok"], page)
        self.assertEqual(len(page["content"]), 8000)
        self.assertLessEqual(len(json.dumps(page, ensure_ascii=False).encode()), tasks.MAX_OUTPUT_BYTES)
        # Exercise exact HTTP cap, not just a grossly oversized response.
        task["result"] = {"response": ""}
        overhead = len(json.dumps({"task": self.api.view(task)}, ensure_ascii=False, separators=(",", ":")).encode())
        task["result"]["response"] = "x" * (tasks.MAX_RESPONSE_BYTES - overhead)
        self.assertTrue((await tasks.aoitalk_agent_task_result(task_id))["ok"])
        task["result"]["response"] += "x"
        self.assertEqual((await tasks.aoitalk_agent_task_result(task_id))["error"], "invalid_response")
        # Escaped lone surrogates are legal JSON, but not valid UTF-8 answer text.
        task["result"]["response"] = "safe"
        raw = json.dumps({"task": self.api.view(task)}).replace('"safe"', '"\\ud800"').encode()
        with patch.object(tasks.AgentTaskClient, "call", return_value=(200, json.loads(raw))):
            self.assertEqual((await tasks.aoitalk_agent_task_result(task_id))["error"], "invalid_response")

    async def test_result_mcp_schema_and_envelope_preserve_content(self):
        task_id = (await self.start())["task_id"]
        code = 'def hello():\r\n\treturn "😀"\r\n' * 200
        self.api.tasks[task_id].update(status="completed", result={"response": code})
        tool = structured_agent_task_tool(tasks.aoitalk_agent_task_result)
        reply = await tool(task_id=task_id, limit=8000)
        self.assertEqual(reply.structured_content["content"], code)
        self.assertEqual(json.loads(reply.content[0].text), reply.structured_content)
        self.assertIn(tasks.aoitalk_agent_task_result, tasks.TOOLS)
        self.assertIn(tasks.aoitalk_agent_task_result, tasks.READ_ONLY_TOOLS)

    def test_sanitizer_marks_key_and_byte_clipping_and_keeps_redaction(self):
        for raw in ({"summary_text": "界" * 4001}, {"result": {"response": "\\\t\r\n😀" * 4000}},
                    {"k" * 101: "v"}, {"result": ["界" * 4000] * 20}):
            reply = tasks._sanitize(raw, SETTINGS)
            self.assertTrue(reply["output_truncated"])
            self.assertLessEqual(len(json.dumps(reply, ensure_ascii=False).encode()), tasks.MAX_OUTPUT_BYTES)
        reply = tasks._sanitize({"result": {"response": PASSWORD + " token=hidden-api-key\n",
                                            "api_key": "hidden-api-key"}}, SETTINGS)
        self.assert_private(reply)

    async def test_result_roundtrip_over_real_stdio_and_fixture_http(self):
        import tempfile
        import anyio
        from mcp.client import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from test_aoitalk_tasks import fixture_server

        task_id = (await self.start())["task_id"]
        text = 'def hello():\r\n\treturn "日本語😀"\r\n' * 350
        self.api.tasks[task_id].update(status="completed", result={"response": text})
        api = self.api

        class WireAPI:
            def route(self, method, path, headers, body, query):
                response = api.handler(httpx.Request(method, "http://fixture" + path,
                                                     headers=headers, json=body))
                cookies = {"Set-Cookie": response.headers["set-cookie"]} if "set-cookie" in response.headers else {}
                return response.status_code, response.json(), cookies

        with tempfile.TemporaryDirectory() as cwd, fixture_server(WireAPI()) as url:
            env_file = Path(cwd) / "fixture.env"
            env_file.write_text(f"AOITALK_BOOTSTRAP_ADMIN_USERNAME={USER}\n"
                                f"AOITALK_BOOTSTRAP_ADMIN_PASSWORD='{PASSWORD}'\n", encoding="utf-8")
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py")],
                cwd=cwd, env={"HOME_MCP_AOITALK_ENABLED": "true", "HOME_MCP_AOITALK_URL": url,
                              "HOME_MCP_AOITALK_ENV_FILE": str(env_file), "HOME_MCP_TOOL_LOG": "0"},
            )
            with anyio.fail_after(30):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        # Exercise SDK validation, which runs before the function's
                        # manual guards. Coercing bool/string/float to int would
                        # otherwise allow a login and task GET here.
                        before = len(api.calls)
                        digest = hashlib.sha256(text.encode()).hexdigest()
                        for parameter in ("offset", "limit"):
                            for malformed in (False, True, "0", "1", 0.0, 1.0):
                                with self.subTest(parameter=parameter, malformed=repr(malformed)):
                                    response = await session.call_tool("aoitalk_agent_task_result", {
                                        "task_id": task_id, "expected_digest": digest,
                                        parameter: malformed,
                                    })
                                    self.assertTrue(response.is_error, response)
                                    self.assertEqual(len(api.calls), before,
                                                     "Malformed types must fail before login/GET")
                        parts, args = [], {"task_id": task_id, "limit": 8000}
                        while True:
                            response = await session.call_tool("aoitalk_agent_task_result", args)
                            self.assertFalse(response.is_error, response)
                            page = response.structured_content
                            self.assertTrue(page["ok"], page)
                            self.assertEqual(json.loads(response.content[0].text), page)
                            parts.append(page["content"])
                            if page["next_offset"] is None:
                                break
                            args.update(offset=page["next_offset"], expected_digest=page["content_sha256"])
                        self.assertEqual("".join(parts), text)
                        self.assertEqual(hashlib.sha256("".join(parts).encode()).hexdigest(), page["content_sha256"])

    async def test_invalid_inputs_do_not_make_http_calls(self):
        for args in (
            {"agent_id": "not-a-uuid"},
            {"idempotency_key": "bad/key"},
            {"idempotency_key": ":leading"},
            {"idempotency_key": ".leading"},
            {"idempotency_key": "-leading"},
            {"idempotency_key": "_leading"},
            {"idempotency_key": None},
            {"required_capabilities": ["Unsafe Capability"]},
            {"required_capabilities": ["workspace.read"]},
            {"required_capabilities": ["workspace-read"]},
            {"required_capabilities": ["workspace_read", "workspace_read"]},
            {"goal": "invalid\x01control"},
            {"goal": "invalid\u0085control"},
            {"constraints": ["invalid\x02control"]},
            {"constraints": ["invalid\u0085control"]},
            {"workspace": WORKSPACE + "\n"},
            {"workspace": WORKSPACE + "\u0085"},
            {"constraints": ["x" * 1001]},
            {"completion_criteria": ["x"] * 21},
            {"provider": ""},
            {"provider": "anthropic:alt"},
            {"effort": "1high"},
        ):
            with self.subTest(args=list(args)):
                self.assertEqual((await self.start(**args))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_idempotency_key_is_required_by_the_mcp_signature(self):
        parameter = inspect.signature(tasks.aoitalk_agent_task_start, eval_str=True).parameters["idempotency_key"]
        self.assertIs(parameter.default, inspect.Parameter.empty)
        key_schema = TypeAdapter(parameter.annotation)
        key_schema.validate_python("A-key_1")
        for key in (":leading", ".leading", "-leading", "_leading"):
            with self.subTest(key=key), self.assertRaises(ValidationError):
                key_schema.validate_python(key)
        with self.assertRaises(TypeError):
            await tasks.aoitalk_agent_task_start("inspect", WORKSPACE, AGENT_ID)
        self.assertFalse(self.api.calls)

    async def test_route_expectation_schemas_match_41_identifier_syntax(self):
        parameters = inspect.signature(tasks.aoitalk_agent_task_start, eval_str=True).parameters
        provider_schema = TypeAdapter(parameters["provider"].annotation)
        effort_schema = TypeAdapter(parameters["effort"].annotation)
        self.assertEqual(provider_schema.validate_python("anthropic"), "anthropic")
        self.assertEqual(effort_schema.validate_python("High"), "High")
        for provider in ("anthropic:alt", "anthropic.alt", "https://provider.example/v1"):
            with self.subTest(provider=provider), self.assertRaises(ValidationError):
                provider_schema.validate_python(provider)

    async def test_read_only_mode_rejects_commands_before_transport(self):
        result = await self.start(mode="read_only", allow_commands=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_explicit_git_permissions_are_forwarded_only_with_mutating_commands(self):
        result = await self.start(
            mode="mutate",
            allow_commands=True,
            allow_git_commit=True,
            allow_git_push=True,
        )
        self.assertTrue(result["ok"], result)
        post = next(call for call in self.api.calls if call["path"] == "/api/agent-tasks")
        self.assertIs(post["body"]["allow_git_commit"], True)
        self.assertIs(post["body"]["allow_git_push"], True)

        self.api.calls.clear()
        denied = await self.start(
            mode="mutate",
            allow_commands=False,
            allow_git_commit=True,
        )
        self.assertFalse(denied["ok"])
        self.assertEqual(denied["error"], "invalid_input")
        self.assertFalse(self.api.calls)


    def posts(self):
        return [call for call in self.api.calls if call["method"] == "POST" and call["path"].startswith("/api/")
                and call["path"] != "/api/auth/login"]

    async def test_required_outputs_omitted_or_empty_keep_existing_payload(self):
        for index, extra in enumerate(({}, {"required_outputs": None}, {"required_outputs": []})):
            with self.subTest(extra=extra):
                self.api.calls.clear()
                result = await self.start(idempotency_key=f"ro-empty-{index}", **extra)
                self.assertTrue(result["ok"], result)
                self.assertEqual(self.posts()[0]["body"], {
                    "goal": "inspect the workspace", "workspace": WORKSPACE, "constraints": [],
                    "completion_criteria": [], "mode": "read_only", "allow_commands": False,
                    "agent_id": str(UUID(AGENT_ID)),
                })

    async def test_required_outputs_exact_canonical_payload_and_pinned_route(self):
        outputs = ["docs/report.md", "out\\result.json"]
        result = await self.start(idempotency_key="ro-valid", required_outputs=outputs,
                                  required_capabilities=["workspace_read"])
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["path"] for c in self.api.calls], ["/api/auth/login", "/api/agent-tasks"])
        post = self.api.calls[1]
        self.assertEqual(post["headers"]["idempotency-key"], "ro-valid")
        self.assertEqual(post["body"], {
            "goal": "inspect the workspace", "workspace": WORKSPACE, "constraints": [],
            "completion_criteria": [], "mode": "read_only", "allow_commands": False,
            "agent_id": str(UUID(AGENT_ID)), "required_capabilities": ["workspace_read"],
            "required_outputs": outputs,
        })
        # Same key + same contract replays; a changed contract conflicts.
        replay = await self.start(idempotency_key="ro-valid", required_outputs=outputs,
                                  required_capabilities=["workspace_read"])
        self.assertFalse(replay["created"])
        changed = await self.start(idempotency_key="ro-valid", required_outputs=["other.md"],
                                   required_capabilities=["workspace_read"])
        self.assertEqual(changed["error"], "idempotency_conflict")

    async def test_required_outputs_limits_and_type_validation_make_no_http_calls(self):
        too_many = [f"f{i}.txt" for i in range(21)]
        self.assertTrue((await self.start(idempotency_key="ro-max20",
                                          required_outputs=too_many[:20]))["ok"])
        self.api.calls.clear()
        for value in (too_many, "report.md", {"a": "b"}, [1], [None], [["a"]], [""], ["  "],
                      ["a.txt", "a.txt"], ["/abs.txt"], ["\\abs.txt"], ["C:\\abs.txt"], ["C:abs.txt"],
                      ["../up.txt"], ["a/../../up.txt"], ["a\\..\\up.txt"], ["a//b.txt"], ["./a.txt"],
                      [" a.txt"], ["a\x00b"], ["a\nb"], ["x" * 513]):
            with self.subTest(value=value):
                self.assertEqual((await self.start(required_outputs=value))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    def test_required_outputs_schema_is_optional_list_max20(self):
        for fn in (tasks.aoitalk_agent_task_start, legacy_tasks.aoitalk_local_task_start):
            parameter = inspect.signature(fn, eval_str=True).parameters["required_outputs"]
            self.assertIsNone(parameter.default)
            schema = TypeAdapter(parameter.annotation)
            schema.validate_python(None)
            schema.validate_python([])
            schema.validate_python(["a"] * 20)
            for bad in (["a"] * 21, "a", [""], ["x" * 513], [1]):
                with self.subTest(fn=fn.__name__, bad=bad), self.assertRaises(ValidationError):
                    schema.validate_python(bad)

    async def test_facade_forwards_required_outputs_only_to_canonical_route(self):
        started = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="ro-facade",
            required_outputs=["docs/report.md"])
        self.assertTrue(started["ok"], started)
        self.assertEqual(started["executor"], "aoitalk_agent_task")
        self.assertEqual([c["path"] for c in self.posts()], ["/api/agent-tasks"])
        post = self.posts()[0]
        self.assertEqual(post["headers"]["idempotency-key"], "ro-facade")
        self.assertEqual(post["body"]["agent_id"], str(UUID(AGENT_ID)))
        self.assertEqual(post["body"]["required_outputs"], ["docs/report.md"])
        self.api.calls.clear()
        plain = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="ro-facade-plain",
            required_outputs=[])
        self.assertTrue(plain["ok"], plain)
        self.assertNotIn("required_outputs", self.posts()[0]["body"])
        self.api.calls.clear()
        bad = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="ro-facade-bad",
            required_outputs=["../x"])
        self.assertEqual(bad["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    async def test_facade_rejects_required_outputs_on_legacy_route_without_http(self):
        for kwargs in ({}, {"agent_id": AGENT_ID, "provider": "openai_compatible_local"}):
            for outputs in (["docs/report.md"], "report.md"):
                with self.subTest(kwargs=list(kwargs), outputs=outputs):
                    self.api.calls.clear()
                    result = await legacy_tasks.aoitalk_local_task_start(
                        "legacy goal", WORKSPACE, idempotency_key="ro-legacy",
                        required_outputs=outputs, **kwargs)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "invalid_input")
                    self.assertIn("required_outputs", result["detail"])
                    self.assertFalse(self.api.calls)
        # Omitted/empty keeps the legacy call byte-identical.
        for outputs in (None, []):
            self.api.calls.clear()
            ok = await legacy_tasks.aoitalk_local_task_start(
                "legacy goal", WORKSPACE, idempotency_key="ro-legacy-ok", required_outputs=outputs)
            self.assertTrue(ok["ok"], ok)
            body = self.posts()[0]["body"]
            self.assertEqual(self.posts()[0]["path"], "/api/local-agent/tasks")
            self.assertNotIn("required_outputs", body)

    async def test_edit_targets_omitted_is_absent_empty_is_preserved_nonempty_forwarded(self):
        base = {"goal": "inspect the workspace", "workspace": WORKSPACE, "constraints": [],
                "completion_criteria": [], "mode": "read_only", "allow_commands": False,
                "agent_id": str(UUID(AGENT_ID))}
        for index, (extra, expected) in enumerate((
            ({}, {}),
            ({"edit_targets": None}, {}),
            ({"edit_targets": []}, {"edit_targets": []}),
            ({"edit_targets": ["src/app.py", "docs/a.md"]}, {"edit_targets": ["src/app.py", "docs/a.md"]}),
            ({"edit_targets": [], "required_outputs": []}, {"edit_targets": []}),
        )):
            with self.subTest(extra=extra):
                self.api.calls.clear()
                result = await self.start(idempotency_key=f"et-{index}", **extra)
                self.assertTrue(result["ok"], result)
                self.assertEqual([c["path"] for c in self.api.calls], ["/api/auth/login", "/api/agent-tasks"])
                self.assertEqual(self.posts()[0]["body"], {**base, **expected})
                self.assertEqual(self.posts()[0]["headers"]["idempotency-key"], f"et-{index}")

    async def test_edit_targets_validation_makes_no_http_calls(self):
        bs = chr(92)
        self.assertTrue((await self.start(idempotency_key="et-max20",
                                          edit_targets=[f"f{i}.py" for i in range(20)]))["ok"])
        self.api.calls.clear()
        for value in ([f"f{i}.py" for i in range(21)], "a.py", {"a": "b"}, [1], [None], [""], ["  "],
                      ["a.py", "a.py"], ["/abs.py"], [bs + "abs.py"], ["C:" + bs + "abs.py"],
                      ["../up.py"], ["a/../up.py"], ["a//b.py"], ["dir/"], ["./a.py"], [" a.py"],
                      ["a" + chr(0) + "b"], ["a" + chr(10) + "b"], ["x" * 513]):
            with self.subTest(value=value):
                self.assertEqual((await self.start(edit_targets=value))["error"], "invalid_input")
        self.assertFalse(self.api.calls)

    def test_edit_targets_schema_is_optional_list_max20(self):
        for fn in (tasks.aoitalk_agent_task_start, legacy_tasks.aoitalk_local_task_start):
            parameter = inspect.signature(fn, eval_str=True).parameters["edit_targets"]
            self.assertIsNone(parameter.default)
            schema = TypeAdapter(parameter.annotation)
            for good in (None, [], ["a"] * 20):
                self.assertEqual(schema.validate_python(good), good)
            for bad in (["a"] * 21, "a", [""], ["x" * 513], [1]):
                with self.subTest(fn=fn.__name__, bad=bad), self.assertRaises(ValidationError):
                    schema.validate_python(bad)

    async def test_facade_edit_targets_canonical_forward_and_legacy_reject(self):
        started = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="et-facade", edit_targets=[])
        self.assertTrue(started["ok"], started)
        self.assertEqual([c["path"] for c in self.posts()], ["/api/agent-tasks"])
        self.assertEqual(self.posts()[0]["body"]["edit_targets"], [])
        self.api.calls.clear()
        started = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="et-facade-2",
            edit_targets=["src/app.py"])
        self.assertEqual(self.posts()[0]["body"]["edit_targets"], ["src/app.py"])
        self.api.calls.clear()
        omitted = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="et-facade-3")
        self.assertTrue(omitted["ok"], omitted)
        self.assertNotIn("edit_targets", self.posts()[0]["body"])
        self.api.calls.clear()
        bad = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="et-facade-4",
            edit_targets=["../x"])
        self.assertEqual(bad["error"], "invalid_input")
        self.assertFalse(self.api.calls)
        for kwargs in ({}, {"agent_id": AGENT_ID, "provider": "openai_compatible_local"}):
            for value in ([], ["src/app.py"]):
                with self.subTest(kwargs=list(kwargs), value=value):
                    result = await legacy_tasks.aoitalk_local_task_start(
                        "legacy goal", WORKSPACE, idempotency_key="et-legacy", edit_targets=value, **kwargs)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "invalid_input")
                    self.assertIn("edit_targets", result["detail"])
                    self.assertFalse(self.api.calls)
        ok = await legacy_tasks.aoitalk_local_task_start(
            "legacy goal", WORKSPACE, idempotency_key="et-legacy-ok", edit_targets=None)
        self.assertTrue(ok["ok"], ok)
        self.assertEqual(self.posts()[0]["path"], "/api/local-agent/tasks")
        self.assertNotIn("edit_targets", self.posts()[0]["body"])


    async def test_requires_command_verification_omitted_absent_explicit_forwarded_exactly(self):
        for index, (extra, present, value) in enumerate((
            ({}, False, None),
            ({"requires_command_verification": None}, False, None),
            ({"requires_command_verification": False}, True, False),
            ({"requires_command_verification": True, "mode": "mutate", "allow_commands": True}, True, True),
        )):
            with self.subTest(extra=extra):
                self.api.calls.clear()
                result = await self.start(idempotency_key=f"rcv-{index}", **extra)
                self.assertTrue(result["ok"], result)
                body = self.posts()[0]["body"]
                self.assertEqual("requires_command_verification" in body, present)
                if present:
                    self.assertIs(body["requires_command_verification"], value)

    async def test_requires_command_verification_nonboolean_makes_no_http_calls(self):
        for value in (0, 1, "true", "false", [], {}, 1.0):
            with self.subTest(value=value):
                self.api.calls.clear()
                self.assertEqual(
                    (await self.start(requires_command_verification=value))["error"], "invalid_input")
                self.assertFalse(self.api.calls)

    def test_requires_command_verification_schema_is_strict_optional_bool(self):
        for fn in (tasks.aoitalk_agent_task_start, legacy_tasks.aoitalk_local_task_start):
            parameter = inspect.signature(fn, eval_str=True).parameters["requires_command_verification"]
            self.assertIsNone(parameter.default)
            schema = TypeAdapter(parameter.annotation)
            for good in (None, True, False):
                self.assertIs(schema.validate_python(good), good)
            for bad in (0, 1, "true", "false", []):
                with self.subTest(fn=fn.__name__, bad=bad), self.assertRaises(ValidationError):
                    schema.validate_python(bad)

    async def test_facade_requires_command_verification_canonical_forward_and_legacy_reject(self):
        for index, value in enumerate((False, True)):
            self.api.calls.clear()
            started = await legacy_tasks.aoitalk_local_task_start(
                "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key=f"rcv-facade-{index}",
                requires_command_verification=value)
            self.assertTrue(started["ok"], started)
            self.assertIs(self.posts()[0]["body"]["requires_command_verification"], value)
        self.api.calls.clear()
        omitted = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="rcv-facade-omit")
        self.assertTrue(omitted["ok"], omitted)
        self.assertNotIn("requires_command_verification", self.posts()[0]["body"])
        self.api.calls.clear()
        bad = await legacy_tasks.aoitalk_local_task_start(
            "facade goal", WORKSPACE, agent_id=AGENT_ID, idempotency_key="rcv-facade-bad",
            requires_command_verification="true")
        self.assertEqual(bad["error"], "invalid_input")
        self.assertFalse(self.api.calls)
        for kwargs in ({}, {"agent_id": AGENT_ID, "provider": "openai_compatible_local"}):
            for value in (False, True):
                with self.subTest(kwargs=list(kwargs), value=value):
                    result = await legacy_tasks.aoitalk_local_task_start(
                        "legacy goal", WORKSPACE, idempotency_key="rcv-legacy",
                        requires_command_verification=value, **kwargs)
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["error"], "invalid_input")
                    self.assertIn("requires_command_verification", result["detail"])
                    self.assertFalse(self.api.calls)


if __name__ == "__main__":
    unittest.main()
