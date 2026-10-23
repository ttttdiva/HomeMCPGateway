from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import unittest
from unittest.mock import patch

import httpx
import anyio
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from home_mcp_gateway import (
    aoitalk, aoitalk_agent_tasks, aoitalk_cli_sessions, aoitalk_manage as manage, aoitalk_tasks,
)


TASK_ID = "11111111-1111-4111-8111-111111111111"
OPERATION_ID = "44444444-4444-4444-8444-444444444444"
STAMP = "2026-10-23T03:00:00+00:00"
SECRET = "manage-fixture-secret-value"


def settings() -> aoitalk.Settings:
    return aoitalk.Settings("http://127.0.0.1:3000", "fixture-user", SECRET)


class ManageClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_accepts_2xx_array_and_empty_204(self):
        statuses = [200, 201, 202, 204]

        def handler(request: httpx.Request):
            status = statuses.pop(0)
            content = b"" if status == 204 else (b'["a","b"]' if status == 200 else b'{"accepted":true}')
            return httpx.Response(status, content=content, headers={"X-AoiTalk-Has-More": "true"})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(base_url=settings().base_url, transport=transport) as http:
            client = manage.ManageClient(settings(), http=http)
            status, data, headers = await client.call("GET", "/array")
            self.assertEqual(status, 200)
            self.assertEqual(data, ["a", "b"])
            self.assertEqual(headers.get("x-aoitalk-has-more"), "true")
            for expected in (201, 202):
                status, data, _ = await client.call("PUT", "/operation")
                self.assertEqual(status, expected)
                self.assertTrue(data["accepted"])
            status, data, _ = await client.call("PUT", "/empty")
            self.assertEqual((status, data), (204, {}))

    async def test_code_less_old_server_404_is_endpoint_unavailable_for_put_and_get(self):
        calls = []

        def handler(request: httpx.Request):
            calls.append((request.method, request.url.path))
            return httpx.Response(404, json={"detail": "Not Found"})

        async with httpx.AsyncClient(base_url=settings().base_url, transport=httpx.MockTransport(handler)) as http:
            client = manage.ManageClient(settings(), http=http)
            for method in ("PUT", "GET"):
                with self.assertRaises(manage.ManageError) as caught:
                    await client.call(method, f"/api/tasks/operations/{OPERATION_ID}",
                                      operation_endpoint=True, json={"action": "delete"})
                self.assertEqual(caught.exception.code, "endpoint_unavailable")
                self.assertFalse(caught.exception.uncertain)
        self.assertEqual([method for method, _ in calls], ["PUT", "GET"])
        self.assertEqual(len(calls), 2)  # neither operation is redirected to legacy CRUD

    async def test_structured_codes_and_operation_error_contract(self):
        bodies = [
            (409, {"error": "operation_conflict", "detail": "same operation id has other args"}),
            (409, {"error": "task_cas_stale"}),
            (404, {"error": "task_not_found"}),
            (404, {"error": "receipt_not_found"}),
            (410, {"error": "not_observed"}),
        ]

        def handler(request: httpx.Request):
            status, body = bodies.pop(0)
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(base_url=settings().base_url, transport=httpx.MockTransport(handler)) as http:
            client = manage.ManageClient(settings(), http=http)
            for expected, status in (("operation_conflict", 409), ("task_cas_stale", 409),
                                     ("task_not_found", 404), ("receipt_not_found", 404),
                                     ("not_observed", 410)):
                with self.assertRaises(manage.ManageError) as caught:
                    await client.call("PUT", "/api/tasks/operations/id", operation_endpoint=True)
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.http_status, status)

        async with httpx.AsyncClient(base_url=settings().base_url,
                                     transport=httpx.MockTransport(lambda req: httpx.Response(400, json={"detail": "bad"}))) as http:
            client = manage.ManageClient(settings(), http=http)
            with self.assertRaises(manage.ManageError) as caught:
                await client.call("PUT", "/api/tasks/operations/id", operation_endpoint=True)
            self.assertEqual(caught.exception.code, "invalid_response")

    async def test_nested_fastapi_detail_code_is_preserved(self):
        bodies = [
            (409, {"detail": {"code": "close_incomplete_subtasks_confirmation_required", "message": "confirm"}}),
            (404, {"detail": {"code": "task_not_found", "message": "missing"}}),
            (404, {"detail": {"error": "not_observed", "message": "pending"}}),
        ]

        def handler(request: httpx.Request):
            status, body = bodies.pop(0)
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(base_url=settings().base_url, transport=httpx.MockTransport(handler)) as http:
            client = manage.ManageClient(settings(), http=http)
            for expected, status in (
                ("close_incomplete_subtasks_confirmation_required", 409),
                ("task_not_found", 404),
                ("not_observed", 404),
            ):
                with self.assertRaises(manage.ManageError) as caught:
                    await client.call("PUT", "/api/tasks/operations/" + OPERATION_ID,
                                      operation_endpoint=True, json={})
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.http_status, status)
                self.assertIsInstance(caught.exception.detail, dict)
                self.assertEqual(caught.exception.detail.get("message"),
                                 {"close_incomplete_subtasks_confirmation_required": "confirm",
                                  "task_not_found": "missing", "not_observed": "pending"}[expected])

    async def test_rate_limit_retry_after_is_typed(self):
        async with httpx.AsyncClient(
            base_url=settings().base_url,
            transport=httpx.MockTransport(lambda req: httpx.Response(429, json={"error": "rate_limited"}, headers={"Retry-After": "17"})),
        ) as http:
            client = manage.ManageClient(settings(), http=http)
            with self.assertRaises(manage.ManageError) as caught:
                await client.call("GET", "/api/tasks")
        self.assertEqual(caught.exception.code, "rate_limited")
        self.assertEqual(caught.exception.retry_after, 17)

    async def test_read_post_timeout_is_not_reported_as_an_uncertain_mutation(self):
        async def run(mutation):
            async with httpx.AsyncClient(
                base_url=settings().base_url,
                transport=httpx.MockTransport(lambda req: (_ for _ in ()).throw(httpx.ReadTimeout("fixture timeout"))),
            ) as http:
                client = manage.ManageClient(settings(), http=http)
                with self.assertRaises(manage.ManageError) as caught:
                    await client.call("POST", "/api/knowledge/plugin/search", mutation=mutation, json={"query": "x"})
                return caught.exception

        self.assertTrue((await run(True)).uncertain)
        self.assertFalse((await run(False)).uncertain)


class ManagementToolTests(unittest.IsolatedAsyncioTestCase):
    async def _use_client(self, client, callback, *, knowledge=False):
        async def session(fn, *, knowledge=False):
            return manage._safe_data(await fn(client), client.settings, knowledge=knowledge)
        with patch.object(manage, "_session", session):
            return await callback()

    async def test_task_update_uses_only_operations_and_preserves_null(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.request = (method, path, kwargs)
                return 202, {"accepted": True, "effects": "pending", "operation_id": OPERATION_ID}, httpx.Headers()

        client = Client()
        result = await self._use_client(client, lambda: manage.aoitalk_task_update(
            OPERATION_ID, TASK_ID, STAMP, {"description": None, "status": "in_progress"},
        ))
        method, path, kwargs = client.request
        self.assertEqual(method, "PUT")
        self.assertEqual(path, f"/api/tasks/operations/{OPERATION_ID}")
        self.assertEqual(kwargs["json"], {
            "action": "update",
            "task_id": TASK_ID,
            "expected_updated_at": STAMP,
            "changes": {"description": None, "status": "in_progress"},
        })
        self.assertTrue(result["accepted"])
        self.assertEqual(result["effects"], "pending")

    async def test_task_delete_restore_and_create_payload_shapes(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.requests.append((method, path, kwargs))
                return 200, {"accepted": True}, httpx.Headers()

            def __init__(self):
                self.requests = []

        client = Client()
        await self._use_client(client, lambda: manage.aoitalk_task_create(OPERATION_ID, {"title": "new"}))
        await self._use_client(client, lambda: manage.aoitalk_task_delete(OPERATION_ID, TASK_ID, STAMP))
        batch = "22222222-2222-4222-8222-222222222222"
        await self._use_client(client, lambda: manage.aoitalk_task_restore(OPERATION_ID, TASK_ID, batch))
        self.assertEqual([r[1] for r in client.requests], [f"/api/tasks/operations/{OPERATION_ID}"] * 3)
        self.assertEqual(client.requests[0][2]["json"], {"action": "create", "payload": {"title": "new"}})
        self.assertEqual(client.requests[1][2]["json"], {
            "action": "delete", "task_id": TASK_ID, "expected_updated_at": STAMP,
        })
        self.assertEqual(client.requests[2][2]["json"], {
            "action": "restore", "task_id": TASK_ID, "deletion_batch_id": batch,
        })

    async def test_task_update_rejects_create_only_source_and_operation_id_is_uuid(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.request = (method, path, kwargs)
                return 202, {"accepted": True}, httpx.Headers()

        client = Client()
        invalid_null = await self._use_client(
            client,
            lambda: manage.aoitalk_task_update(OPERATION_ID, TASK_ID, STAMP, {"source": None}),
        )
        self.assertFalse(invalid_null["ok"])
        self.assertEqual(invalid_null["error"], "invalid_input")
        self.assertIn("outside the task catalog", invalid_null["detail"])
        self.assertFalse(hasattr(client, "request"))

        invalid_text = await self._use_client(
            client,
            lambda: manage.aoitalk_task_update(
                OPERATION_ID, TASK_ID, STAMP, {"source": "remote"},
            ),
        )
        self.assertFalse(invalid_text["ok"])
        self.assertEqual(invalid_text["error"], "invalid_input")
        self.assertIn("outside the task catalog", invalid_text["detail"])
        self.assertFalse(hasattr(client, "request"))

        invalid_id = await self._use_client(
            client,
            lambda: manage.aoitalk_task_operation_status("opaque-operation-id"),
        )
        self.assertFalse(invalid_id["ok"])
        self.assertEqual(invalid_id["error"], "invalid_input")

    async def test_operation_status_keeps_effect_state_and_stored_snapshot(self):
        class Client:
            settings = settings()
            effects_state = "warning"

            async def call(self, method, path, **kwargs):
                self.request = (method, path)
                return 200, {"state": "done", "result_snapshot": {"title": "at commit"},
                             "current_task": {"title": "now"}, "effects": {"state": self.effects_state}}, httpx.Headers()

        client = Client()
        result = await self._use_client(client, lambda: manage.aoitalk_task_operation_status(OPERATION_ID))
        self.assertEqual(client.request, ("GET", f"/api/tasks/operations/{OPERATION_ID}"))
        self.assertEqual(result["result_snapshot"]["title"], "at commit")
        self.assertEqual(result["current_task"]["title"], "now")
        self.assertEqual(result["effects"]["state"], "warning")
        for state in ("done", "warning", "unconfirmed"):
            client.effects_state = state
            status = await self._use_client(client, lambda: manage.aoitalk_task_operation_status(OPERATION_ID))
            self.assertEqual(status["effects"]["state"], state)

    async def test_uncertain_write_exposes_same_key_only_recovery(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                raise manage.ManageError("connection_error", uncertain=True)

        result = await self._use_client(Client(), lambda: manage.aoitalk_task_delete(OPERATION_ID, TASK_ID, STAMP))
        self.assertTrue(result["submission_uncertain"])
        self.assertEqual(result["operation_id"], OPERATION_ID)
        self.assertIn("same operation_id", result["next_step"])

    async def test_task_list_uses_server_page_header_without_filtering(self):
        page = [{"id": f"task-{index}"} for index in range(4)]

        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.method = method
                self.path = path
                self.request = kwargs
                return 200, page, httpx.Headers({"X-AoiTalk-Has-More": "true"})

        client = Client()
        result = await self._use_client(client, lambda: manage.aoitalk_tasks_list(
            limit=4, offset=8, status="in_progress", project_id=TASK_ID,
            space_id="22222222-2222-4222-8222-222222222222",
            assignee_id="33333333-3333-4333-8333-333333333333",
            search="meeting", due_before="2026-10-23T03:00:00",
            updated_before="2026-10-23T03:00:00.123456",
        ))
        self.assertEqual((client.method, client.path), ("GET", "/api/tasks"))
        self.assertEqual(client.request["params"], {
            "projection": "compact", "limit": 4, "offset": 8,
            "status": "in_progress", "project_id": TASK_ID,
            "space_id": "22222222-2222-4222-8222-222222222222",
            "assignee_id": "33333333-3333-4333-8333-333333333333",
            "search": "meeting", "due_before": "2026-10-23T03:00:00",
            "updated_before": "2026-10-23T03:00:00.123456",
        })
        self.assertEqual(result["tasks"], page)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["offset"], 8)
        self.assertEqual(result["next_offset"], 12)

    async def test_task_read_defaults_to_summary_and_exposes_bounded_sections(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.request = (method, path, kwargs)
                if kwargs.get("params", {}).get("section") == "task":
                    return 200, {
                        "section": "task",
                        "task": {"id": TASK_ID, "updated_at": "2026-10-23T03:00:00.123456"},
                    }, httpx.Headers()
                if kwargs.get("params", {}).get("section") == "description":
                    return 200, {
                        "section": "description", "content": "part", "offset": 20000,
                        "next_offset": 20004, "has_more": True, "fingerprint": "a" * 64,
                    }, httpx.Headers()
                return 200, {"id": TASK_ID, "updated_at": "2026-10-23T03:00:00.123456"}, httpx.Headers()

        client = Client()
        summary = await self._use_client(client, lambda: manage.aoitalk_task_read(TASK_ID))
        self.assertEqual(client.request, (
            "GET", f"/api/tasks/{TASK_ID}",
            {"params": {"section": "task", "limit": 50, "offset": 0, "max_chars": 4000}},
        ))
        self.assertEqual(summary["task"]["updated_at"], "2026-10-23T03:00:00.123456")

        bounded = await self._use_client(client, lambda: manage.aoitalk_task_read(
            TASK_ID, section="description", offset=20000, max_chars=4, fingerprint="a" * 64,
        ))
        self.assertEqual(client.request, (
            "GET", f"/api/tasks/{TASK_ID}",
            {"params": {
                "section": "description", "limit": 50, "offset": 20000,
                "max_chars": 4, "fingerprint": "a" * 64,
            }},
        ))
        self.assertTrue(bounded["has_more"])
        self.assertEqual(bounded["next_offset"], 20004)

    async def test_docs_search_returns_only_id_title_candidates(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.request = (method, path, kwargs)
                return 200, {"results": [
                    {"id": TASK_ID, "title": "Visible title", "excerpt": "invented excerpt", "body": "secret text"},
                    {"id": "no-title"},
                ]}, httpx.Headers()

        client = Client()
        result = await self._use_client(client, lambda: manage.aoitalk_docs_search("meeting", limit=50))
        self.assertEqual(client.request[0:2], ("GET", "/api/docs/search"))
        self.assertEqual(result["candidates"], [{"id": TASK_ID, "title": "Visible title"}])

    async def test_edit_read_with_more_pages_never_returns_write_token(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                return 200, {"has_more": True, "next_cursor": "opaque-next", "write_token": "22222222-2222-4222-8222-222222222222"}, httpx.Headers()

        result = await self._use_client(
            Client(), lambda: manage.aoitalk_docs_prepare_edit(TASK_ID, cursor="opaque-page")
        )
        self.assertTrue(result["has_more"])
        self.assertEqual(result["next_cursor"], "opaque-next")
        self.assertNotIn("write_token", result)

        class CompleteClient:
            settings = settings()

            async def call(self, method, path, **kwargs):
                return 201, {"coverage_complete": True, "has_more": False,
                             "write_token": "22222222-2222-4222-8222-222222222222"}, httpx.Headers()

        complete = await self._use_client(CompleteClient(), lambda: manage.aoitalk_docs_prepare_edit(TASK_ID))
        self.assertEqual(complete["write_token"], "22222222-2222-4222-8222-222222222222")

    async def test_edit_read_timeout_keeps_same_cursor_recovery_not_operation_status(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                raise manage.ManageError("connection_error")

        result = await self._use_client(Client(), lambda: manage.aoitalk_docs_prepare_edit(TASK_ID, "cursor-1"))
        self.assertTrue(result["cursor_advance_blocked"])
        self.assertNotIn("submission_uncertain", result)
        self.assertIn("exact same page arguments and cursor", result["next_step"])

    async def test_docs_mutate_keeps_write_token_and_operation_id(self):
        write_token = "22222222-2222-4222-8222-222222222222"
        changeset = {"intent": "revise_section", "operations": [{"op": "update", "node_id": TASK_ID}]}

        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.request = (method, path, kwargs)
                return 202, {"accepted": True, "effects": "pending"}, httpx.Headers()

        client = Client()
        result = await self._use_client(client, lambda: manage.aoitalk_docs_mutate(
            TASK_ID, write_token, OPERATION_ID, changeset,
        ))
        method, path, kwargs = client.request
        self.assertEqual((method, path), ("POST", "/api/docs/changesets"))
        self.assertEqual(kwargs["json"], {
            "root_id": TASK_ID, "project_id": None,
            "write_token": write_token, "operation_id": OPERATION_ID,
            "changeset": changeset,
        })
        self.assertEqual(result["write_token"], write_token)
        self.assertEqual(result["operation_id"], OPERATION_ID)
        self.assertEqual(result["effects"], "pending")

    async def test_knowledge_projection_drops_private_provenance_keys(self):
        projected = manage._safe_data({
            "results": [{"source_id": TASK_ID, "title": "safe", "provenance": {
                "source": "project", "root_path": "D:/private", "token": "bearer private", "access_policy": {"admin": True},
            }}],
            "body_fingerprint": "sha256:fixture", "next_cursor": "opaque-next",
        }, settings(), knowledge=True)
        self.assertEqual(projected["results"][0]["provenance"], {"source": "project"})
        self.assertEqual(projected["body_fingerprint"], "sha256:fixture")
        self.assertEqual(projected["next_cursor"], "opaque-next")
        self.assertNotIn("D:/private", json.dumps(projected))
        self.assertNotIn("bearer private", json.dumps(projected))

    async def test_knowledge_tool_uses_only_plugin_api(self):
        class Client:
            settings = settings()

            async def call(self, method, path, **kwargs):
                self.request = (method, path, kwargs)
                return 200, {"results": [{"source_id": TASK_ID, "title": "source", "root_path": "private",
                                          "token": "secret-token", "access_policy": {"admin": True}}],
                             "body_fingerprint": "sha256:fixture"}, httpx.Headers()

        client = Client()
        result = await self._use_client(client, lambda: manage.aoitalk_knowledge_search("query"), knowledge=True)
        self.assertEqual(client.request[1], "/api/knowledge/plugin/search")
        for private in ("root_path", "token", "access_policy", "private", "secret-token"):
            self.assertNotIn(private, json.dumps(result))
        self.assertEqual(result["body_fingerprint"], "sha256:fixture")


class DedicatedServerTests(unittest.TestCase):
    def test_catalog_has_exact_35_tools_and_annotations(self):
        from home_mcp_gateway import aoitalk_plugin_server
        expected = {
            *(fn.__name__ for fn in aoitalk.TOOLS),
            *(fn.__name__ for fn in aoitalk_tasks.TOOLS),
            *(fn.__name__ for fn in aoitalk_agent_tasks.TOOLS),
            *(fn.__name__ for fn in aoitalk_cli_sessions.TOOLS),
            *(fn.__name__ for fn in manage.TOOLS),
        }
        self.assertEqual(len(expected), 35)
        self.assertFalse({"run_command", "job_start", "call_mcp_tool"} & expected)
        self.assertIn("aoitalk_cli_session_reconnect", expected)
        self.assertIn("aoitalk_task_create", expected)
        self.assertNotIn("from .server", (Path(__file__).resolve().parents[1]
                                          / "home_mcp_gateway" / "aoitalk_plugin_server.py").read_text(encoding="utf-8"))
        self.assertNotIn("home_mcp_gateway.server", (Path(__file__).resolve().parents[1]
                                                      / "scripts" / "aoitalk_plugin_stdio.py").read_text(encoding="utf-8"))

        async def check():
            listing = {tool.name: tool for tool in await aoitalk_plugin_server.mcp.list_tools()}
            self.assertEqual(set(listing), expected)
            self.assertTrue(listing["aoitalk_cli_session_reconnect"].annotations.read_only_hint)
            self.assertFalse(listing["aoitalk_docs_prepare_edit"].annotations.read_only_hint)
            self.assertFalse(listing["aoitalk_docs_prepare_edit"].annotations.destructive_hint)
            self.assertEqual(set(listing["aoitalk_task_create"].input_schema["required"]), {"operation_id", "payload"})
            create_schema = listing["aoitalk_task_create"].input_schema
            create_payload = create_schema["$defs"]["TaskCreatePayload"]
            self.assertEqual(create_payload["required"], ["title"])
            update_schema = listing["aoitalk_task_update"].input_schema
            self.assertIn("expected_updated_at", update_schema["required"])
            self.assertIn("description", update_schema["$defs"]["TaskChanges"]["properties"])
            restore_schema = listing["aoitalk_task_restore"].input_schema
            self.assertIn("deletion_batch_id", restore_schema["required"])
            self.assertIn("aoitalk_catalog", listing)
        asyncio.run(check())

    def test_stdio_catalog_and_existing_21_schemas_match_generic(self):
        root = Path(__file__).resolve().parents[1]
        generic = root / "scripts" / "gateway_stdio.py"
        dedicated = root / "scripts" / "aoitalk_plugin_stdio.py"

        async def list_tools(entry: Path, *, enable_generic: bool):
            child_env = os.environ.copy()
            child_env["HOME_MCP_TOOL_LOG"] = "0"
            if enable_generic:
                child_env["HOME_MCP_AOITALK_ENABLED"] = "true"
            params = StdioServerParameters(command=sys.executable, args=[str(entry)], cwd=str(root), env=child_env)
            with anyio.fail_after(40):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return {tool.name: tool for tool in (await session.list_tools()).tools}

        async def check():
            dedicated_tools = await list_tools(dedicated, enable_generic=False)
            generic_tools = await list_tools(generic, enable_generic=True)
            expected = {fn.__name__ for fn in (
                *aoitalk.TOOLS, *aoitalk_tasks.TOOLS, *aoitalk_agent_tasks.TOOLS,
                *aoitalk_cli_sessions.TOOLS,
            )}
            self.assertEqual(len(dedicated_tools), 35)
            self.assertEqual(len(generic_tools), 97)
            self.assertEqual(set(dedicated_tools), expected | {fn.__name__ for fn in manage.TOOLS})
            for name in expected:
                self.assertEqual(dedicated_tools[name].input_schema, generic_tools[name].input_schema, name)
                self.assertEqual(dedicated_tools[name].output_schema, generic_tools[name].output_schema, name)
                self.assertEqual(dedicated_tools[name].annotations, generic_tools[name].annotations, name)
            self.assertNotIn("run_command", dedicated_tools)
            self.assertNotIn("job_start", dedicated_tools)
            self.assertTrue(dedicated_tools["aoitalk_cli_session_reconnect"].annotations.read_only_hint)

        anyio.run(check)

    def test_stdio_entry_scrubs_runtime_credentials_and_rejects_scoped_profile(self):
        entry = Path(__file__).resolve().parents[1] / "scripts" / "aoitalk_plugin_stdio.py"
        with patch.dict(os.environ, {
            "HOME_MCP_AOITALK_RUNTIME_API_KEY": SECRET,
            "CONTROL_PLANE_API_KEY": SECRET,
            "OPENAI_ADMIN_KEY": SECRET,
            "HOME_MCP_PROFILE": "local-agent-readonly",
        }, clear=True):
            with self.assertRaises(SystemExit):
                runpy.run_path(str(entry))

        with patch.dict(os.environ, {
            "HOME_MCP_AOITALK_RUNTIME_API_KEY": SECRET,
            "CONTROL_PLANE_API_KEY": SECRET,
            "OPENAI_ADMIN_KEY": SECRET,
            "HOME_MCP_TOOL_LOG_DIR": "unexpected-tool-log-path",
        }, clear=True):
            runpy.run_path(str(entry))
            for name in ("HOME_MCP_AOITALK_RUNTIME_API_KEY", "CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY"):
                self.assertNotIn(name, os.environ)
            self.assertEqual(os.environ["HOME_MCP_TOOL_LOG_DIR"],
                             str(entry.parents[1] / ".runtime" / "aoitalk" / "tool_logs"))

        probe_env = os.environ.copy()
        probe_env["HOME_MCP_TOOL_LOG"] = "0"
        probe_env.pop("HOME_MCP_PROFILE", None)
        probe = subprocess.run(
            [sys.executable, "-c",
             "import runpy,sys; runpy.run_path(sys.argv[1]); print('GENERIC_IMPORTED=' + str('home_mcp_gateway.server' in sys.modules))",
             str(entry)],
            cwd=entry.parents[1], env=probe_env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("GENERIC_IMPORTED=False", probe.stdout)

        generic_entry = Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py"
        with patch.dict(os.environ, {
            "HOME_MCP_AOITALK_RUNTIME_API_KEY": SECRET,
            "CONTROL_PLANE_API_KEY": SECRET,
            "OPENAI_ADMIN_KEY": SECRET,
        }, clear=True):
            runpy.run_path(str(generic_entry))
            for name in ("HOME_MCP_AOITALK_RUNTIME_API_KEY", "CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY"):
                self.assertNotIn(name, os.environ)

    def test_tunnel_scripts_keep_home_default_and_bind_separate_target_paths(self):
        root = Path(__file__).resolve().parents[1]
        connect = (root / "scripts" / "connect_tunnel.ps1").read_text(encoding="utf-8")
        install = (root / "scripts" / "install_autostart.ps1").read_text(encoding="utf-8")
        self.assertIn("[string]$Target = 'home-mcp'", connect)
        self.assertIn("HOME_MCP_AOITALK_TUNNEL_ID", connect)
        self.assertIn("aoitalk_plugin_stdio.py", connect)
        self.assertIn(".runtime\\aoitalk", connect)
        self.assertIn("'supervisor-status.json'", connect)
        self.assertIn("-Target {1}", install)
        self.assertIn("Home MCP Gateway - AoiTalk", install)
        self.assertIn("-WindowStyle Hidden", install)
        self.assertIn("does not match this repository and target", install)
        generic_entry = (root / "scripts" / "gateway_stdio.py").read_text(encoding="utf-8")
        self.assertIn("HOME_MCP_AOITALK_RUNTIME_API_KEY", generic_entry)


if __name__ == "__main__":
    unittest.main()
