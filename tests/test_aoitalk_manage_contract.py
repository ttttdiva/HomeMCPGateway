from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import httpx

from home_mcp_gateway import aoitalk
from home_mcp_gateway import aoitalk_manage as manage


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = ROOT / "tests" / "fixtures" / "aoitalk_41_management_contract.v3.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

ROOT_ID = "11111111-1111-4111-8111-111111111111"
PROJECT_ID = "22222222-2222-4222-8222-222222222222"
WRITE_TOKEN = "33333333-3333-4333-8333-333333333333"
OPERATION_ID = "44444444-4444-4444-8444-444444444444"
DOCUMENT_ID = "55555555-5555-4555-8555-555555555555"
TASK_ID = "66666666-6666-4666-8666-666666666666"
DELETION_BATCH_ID = "77777777-7777-4777-8777-777777777777"
STAMP = "2026-10-23T03:00:00+00:00"
SECRET = "contract-fixture-secret"


def _settings() -> aoitalk.Settings:
    return aoitalk.Settings("http://127.0.0.1:3000", "fixture-user", SECRET)


class _Recorder:
    settings = _settings()

    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []

    async def call(self, method: str, path: str, **kwargs: object):
        self.requests.append({"method": method, "path": path, "kwargs": kwargs})
        if path == f"/api/tasks/{TASK_ID}":
            params = kwargs.get("params") or {}
            if params.get("section") == "task":
                return 200, {
                    "section": "task",
                    "task": {
                        "id": TASK_ID,
                        "updated_at": "2026-10-23T03:00:00.123456",
                    },
                }, httpx.Headers()
            return 200, {
                "id": TASK_ID,
                "updated_at": "2026-10-23T03:00:00.123456",
            }, httpx.Headers()
        if path == "/api/docs/edit-reads":
            return 200, {"coverage_complete": True, "has_more": False, "next_cursor": None}, httpx.Headers()
        if path == "/api/knowledge/plugin/search":
            return 200, {"results": []}, httpx.Headers()
        if "/api/knowledge/plugin/documents/" in path:
            params = kwargs.get("params") or {}
            return 200, {
                "content": "fixture",
                "offset": params.get("offset", 0),
                "content_fingerprint": params.get("fingerprint", "a" * 64),
                "has_more": True,
            }, httpx.Headers()
        return 200, {"accepted": True}, httpx.Headers()


async def _run_with_recorder(callback):
    recorder = _Recorder()

    async def session(fn, *, knowledge: bool = False):
        return await fn(recorder)

    with patch.object(manage, "_session", session):
        result = await callback()
    return recorder, result


def _contract_python() -> tuple[Path, Path] | None:
    source_value = os.environ.get("AOITALK_CONTRACT_SOURCE", FIXTURE["source"]["absolute_root"])
    source = Path(source_value)
    candidates = (source / "venv" / "Scripts" / "python.exe", source / "venv" / "bin" / "python")
    for candidate in candidates:
        if candidate.exists():
            return source, candidate
    return None


def _validate_in_41(cases: list[dict[str, object]], testcase: unittest.TestCase) -> None:
    resolved = _contract_python()
    if resolved is None:
        testcase.skipTest(
            "41 contract source/venv is unavailable; set AOITALK_CONTRACT_SOURCE for strict DTO validation"
        )
    source, python = resolved
    validator = r"""
import json
import sys
from pydantic import TypeAdapter, ValidationError
from src.api.docs_routes import (
    DocsChangesetPayload,
    DocsProtocolChangesetRequest,
    DocsProtocolEditReadRequest,
    DocsProtocolReadRequest,
)
from src.api.knowledge_routes import KnowledgePluginSearchPayload
from src.api.routes.tasks.operations_routes import _OperationRequest, _task_operation_id
from src.api.routes.tasks._shared import CreateTaskPayload, UpdateTaskPayload

adapters = {
    "DocsProtocolReadRequest": TypeAdapter(DocsProtocolReadRequest),
    "DocsProtocolEditReadRequest": TypeAdapter(DocsProtocolEditReadRequest),
    "DocsProtocolChangesetRequest": TypeAdapter(DocsProtocolChangesetRequest),
    "DocsChangesetPayload": TypeAdapter(DocsChangesetPayload),
    "KnowledgePluginSearchPayload": TypeAdapter(KnowledgePluginSearchPayload),
    "TaskOperationRequest": TypeAdapter(_OperationRequest),
    "CreateTaskPayload": TypeAdapter(CreateTaskPayload),
    "UpdateTaskPayload": TypeAdapter(UpdateTaskPayload),
}
from src.api.datetime_utils import parse_utc_naive_datetime
from src.api.routes.tasks.read_projection import _iso
from datetime import datetime
failures = []
for case in json.load(sys.stdin):
    if "operation_id" in case:
        try:
            _task_operation_id(case["operation_id"])
            path_valid = True
        except Exception:
            path_valid = False
        if path_valid != case.get("path_valid", case["valid"]):
            failures.append({
                "path": case["operation_id"],
                "expected": case.get("path_valid", case["valid"]),
                "actual": path_valid,
            })
    if "expected_updated_at" in case:
        try:
            parse_utc_naive_datetime(case["expected_updated_at"])
            timestamp_valid = True
        except (TypeError, ValueError):
            timestamp_valid = False
        if timestamp_valid != case.get("timestamp_valid", True):
            failures.append({
                "timestamp": case["expected_updated_at"],
                "expected": case.get("timestamp_valid", True),
                "actual": timestamp_valid,
            })
    if "read_timestamp" in case:
        actual = _iso(datetime.fromisoformat(case["read_timestamp"]))
        if actual != case["read_timestamp"]:
            failures.append({"read_timestamp": case["read_timestamp"], "actual": actual})
    if "model" not in case:
        continue
    try:
        adapters[case["model"]].validate_python(case["value"])
        valid = True
    except ValidationError:
        valid = False
    if valid != case["valid"]:
        failures.append({"model": case["model"], "expected": case["valid"], "actual": valid})
if failures:
    print(json.dumps(failures, ensure_ascii=False))
    raise SystemExit(1)
"""
    completed = subprocess.run(
        [str(python), "-W", "ignore", "-c", validator],
        cwd=source,
        input=json.dumps(cases, ensure_ascii=False).encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    testcase.assertEqual(
        completed.returncode,
        0,
        completed.stdout.decode("utf-8", errors="replace")
        + completed.stderr.decode("utf-8", errors="replace"),
    )


def _validate_knowledge_paging_in_41(testcase: unittest.TestCase) -> None:
    """Exercise the committed plugin read service's offset/fingerprint rule."""
    resolved = _contract_python()
    if resolved is None:
        testcase.skipTest(
            "41 contract source/venv is unavailable; set AOITALK_CONTRACT_SOURCE for paging validation"
        )
    source, python = resolved
    validator = r"""
import asyncio
import hashlib
from uuid import UUID
from src.knowledge.service import KnowledgeService
from src.services import knowledge_plugin_access as access

async def fake_read_document(*args, **kwargs):
    return {
        "content": "abcd",
        "source": {"id": "11111111-1111-4111-8111-111111111111", "name": "fixture", "source_type": "local_dir"},
        "document": {"id": "55555555-5555-4555-8555-555555555555", "title": "fixture", "path": "docs/a.md"},
    }

async def main():
    try:
        await access.read_plugin_document(
            session=None,
            actor_user_id=UUID("11111111-1111-4111-8111-111111111111"),
            document_id=UUID("55555555-5555-4555-8555-555555555555"),
            view="body", max_chars=2, offset=1,
        )
    except ValueError as exc:
        rejected = "fingerprint is required" in str(exc)
    else:
        rejected = False
    KnowledgeService.read_document = staticmethod(fake_read_document)
    fingerprint = hashlib.sha256(b"abcd").hexdigest()
    accepted = await access.read_plugin_document(
        session=None,
        actor_user_id=UUID("11111111-1111-4111-8111-111111111111"),
        document_id=UUID("55555555-5555-4555-8555-555555555555"),
        view="body", max_chars=2, offset=1, fingerprint=fingerprint,
    )
    if not rejected or accepted["offset"] != 1 or accepted["content_fingerprint"] != fingerprint or not accepted["has_more"]:
        raise SystemExit(1)

asyncio.run(main())
"""
    completed = subprocess.run(
        [str(python), "-W", "ignore", "-c", validator],
        cwd=source,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=45,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        check=False,
    )
    testcase.assertEqual(
        completed.returncode,
        0,
        completed.stdout.decode("utf-8", errors="replace")
        + completed.stderr.decode("utf-8", errors="replace"),
    )


def _knowledge_query_is_valid(params: dict[str, object]) -> bool:
    """Validate the GET query from the captured 41 OpenAPI parameter schema."""
    route = FIXTURE["openapi_paths"]["/api/knowledge/plugin/documents/{document_id}"]["get"]
    query = {
        parameter["name"]: parameter["schema"]
        for parameter in route["parameters"]
        if parameter["in"] == "query"
    }
    if set(params) - set(query):
        return False
    view = params.get("view", query["view"]["default"])
    if not isinstance(view, str) or not re.fullmatch(query["view"]["pattern"], view):
        return False
    max_chars = params.get("max_chars", query["max_chars"]["default"])
    if type(max_chars) is not int or not query["max_chars"]["minimum"] <= max_chars <= query["max_chars"]["maximum"]:
        return False
    offset = params.get("offset", query["offset"]["default"])
    if type(offset) is not int or offset < query["offset"]["minimum"]:
        return False
    if "fingerprint" in params:
        fingerprint = params["fingerprint"]
        if not isinstance(fingerprint, str) or not re.fullmatch(query["fingerprint"]["anyOf"][0]["pattern"], fingerprint):
            return False
    if offset > 0 and not params.get("fingerprint"):
        return False
    return True


def _task_list_query_is_valid(params: dict[str, object]) -> bool:
    contract = FIXTURE["request_contracts"]["task_list"]
    if set(params) - set(contract["query_keys"]):
        return False
    if params.get("projection") != "compact":
        return False
    limit = params.get("limit")
    offset = params.get("offset")
    return (
        type(limit) is int
        and contract["bounds"]["limit"][0] <= limit <= contract["bounds"]["limit"][1]
        and type(offset) is int
        and offset >= contract["bounds"]["offset_min"]
    )


def _task_read_query_is_valid(params: dict[str, object]) -> bool:
    contract = FIXTURE["request_contracts"]["task_read"]
    if set(params) - set(contract["query_keys"]):
        return False
    section = params.get("section")
    if section not in contract["sections"]:
        return False
    limit = params.get("limit")
    offset = params.get("offset")
    max_chars = params.get("max_chars")
    bounds = contract["bounds"]
    if type(limit) is not int or not bounds["limit"][0] <= limit <= bounds["limit"][1]:
        return False
    if type(offset) is not int or offset < bounds["offset_text_sections_min"]:
        return False
    if section not in {"description", "metadata"} and offset > bounds["offset_collection_max"]:
        return False
    if type(max_chars) is not int or not bounds["max_chars"][0] <= max_chars <= bounds["max_chars"][1]:
        return False
    if "fingerprint" in params and section not in {"description", "metadata"}:
        return False
    if section in {"description", "metadata"} and offset > 0 and not params.get("fingerprint"):
        return False
    return True


def _contract_file_hash_mismatches(source: Path, expected_hashes: dict[str, str]) -> list[str]:
    mismatches = []
    for relative, expected in expected_hashes.items():
        try:
            actual = hashlib.sha256((source / relative).read_bytes()).hexdigest()
        except OSError:
            actual = None
        if actual != expected:
            mismatches.append(relative)
    return mismatches


class ContractFixtureTests(unittest.IsolatedAsyncioTestCase):
    def test_fixture_records_only_frozen_41_contracts(self):
        self.assertEqual(FIXTURE["source"]["commit"], "b6571a8623d08b2323fce64b8d8a8cd318410c64")
        self.assertEqual(FIXTURE["freeze"]["docs"], "confirmed_current_worktree")
        self.assertEqual(FIXTURE["freeze"]["knowledge"], "confirmed_current_worktree")
        self.assertEqual(FIXTURE["freeze"]["tasks"], "confirmed_current_worktree")
        self.assertEqual(FIXTURE["freeze"]["task_read"], "confirmed_current_worktree")
        self.assertEqual(FIXTURE["freeze"]["task_list"], "confirmed_current_worktree")
        self.assertIn("TaskOperationRequest", FIXTURE["models"])
        self.assertIn("CreateTaskPayload", FIXTURE["models"])
        self.assertIn("UpdateTaskPayload", FIXTURE["models"])
        hashes = FIXTURE["source"]["contract_file_sha256"]
        self.assertIn("src/api/routes/tasks/read_projection.py", hashes)
        self.assertIn("src/api/routes/tasks/tasks_routes.py", hashes)

    def test_fixture_source_commit_and_hashes_match_41_worktree(self):
        source = Path(FIXTURE["source"]["absolute_root"])
        if not source.exists():
            self.skipTest("41 contract source is unavailable")
        self.assertEqual(
            _contract_file_hash_mismatches(source, FIXTURE["source"]["contract_file_sha256"]),
            [],
        )

    def test_contract_hashes_ignore_unrelated_head_advancement_and_detect_file_changes(self):
        relative = "src/api/routes/tasks/read_projection.py"
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            contract_file = source / relative
            contract_file.parent.mkdir(parents=True)
            contract_file.write_text("frozen contract", encoding="utf-8")
            expected_hashes = {relative: hashlib.sha256(b"frozen contract").hexdigest()}

            def git(*args: str) -> str:
                completed = subprocess.run(
                    ["git", *args], cwd=source, text=True,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15,
                    creationflags=creationflags, check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
                return completed.stdout.strip()

            git("init", "--quiet")
            git("config", "user.email", "contract-test@example.invalid")
            git("config", "user.name", "Contract Test")
            git("add", relative)
            git("commit", "--quiet", "-m", "freeze contract")
            frozen_head = git("rev-parse", "HEAD")

            (source / "unrelated.txt").write_text("parallel development", encoding="utf-8")
            git("add", "unrelated.txt")
            git("commit", "--quiet", "-m", "advance unrelated HEAD")
            self.assertNotEqual(git("rev-parse", "HEAD"), frozen_head)
            self.assertEqual(_contract_file_hash_mismatches(source, expected_hashes), [])

            contract_file.write_text("changed contract", encoding="utf-8")
            self.assertEqual(
                _contract_file_hash_mismatches(source, expected_hashes), [relative],
            )

    def test_openapi_entries_and_bounds_are_recorded_from_41(self):
        paths = FIXTURE["openapi_paths"]
        self.assertEqual(paths["/api/docs/reads"]["post"]["requestBody"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/DocsProtocolReadRequest"})
        self.assertEqual(paths["/api/docs/edit-reads"]["post"]["requestBody"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/DocsProtocolEditReadRequest"})
        self.assertEqual(paths["/api/docs/changesets"]["post"]["requestBody"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/DocsProtocolChangesetRequest"})
        self.assertEqual(paths["/api/knowledge/plugin/search"]["post"]["requestBody"]["content"]["application/json"]["schema"],
                         {"$ref": "#/components/schemas/KnowledgePluginSearchPayload"})
        self.assertEqual(paths["/api/knowledge/plugin/documents/{document_id}"]["get"]["parameters"][0]["name"],
                         "document_id")
        self.assertEqual(FIXTURE["request_contracts"]["docs_read"]["bounds"]["page_chars"], [4096, 32000])
        self.assertEqual(FIXTURE["request_contracts"]["docs_edit_read"]["bounds"]["cursor_max_length"], 128)
        self.assertEqual(FIXTURE["request_contracts"]["knowledge_read"]["query_keys"],
                         ["view", "max_chars", "offset", "fingerprint"])
        self.assertEqual(FIXTURE["openapi_paths"]["/api/tasks"]["get"]["operationId"], "list_tasks_api_tasks_get")
        self.assertEqual(FIXTURE["openapi_paths"]["/api/tasks/{task_id}"]["get"]["operationId"], "get_task_api_tasks__task_id__get")

    def test_task_read_and_list_contracts_are_recorded_from_current_source(self):
        self.assertIn("task_operation", FIXTURE["request_contracts"])
        self.assertIn("task_operation_status", FIXTURE["request_contracts"])
        task_list = FIXTURE["request_contracts"]["task_list"]
        self.assertEqual((task_list["method"], task_list["path"], task_list["projection"]),
                         ("GET", "/api/tasks", "compact"))
        self.assertEqual(task_list["bounds"]["limit"], [1, 100])
        self.assertEqual(task_list["pagination"]["response_header"], "X-AoiTalk-Has-More")
        self.assertFalse(task_list["pagination"]["cursor"])
        task_read = FIXTURE["request_contracts"]["task_read"]
        self.assertEqual((task_read["method"], task_read["path"]), ("GET", "/api/tasks/{task_id}"))
        self.assertEqual(task_read["bounds"]["max_chars"], [1, 20000])
        self.assertTrue(task_read["paging"]["offset_gt_zero_requires_fingerprint"])

    async def test_task_operations_match_frozen_41_union_and_uuid_paths(self):
        create_payload = {
            "title": "Contract task",
            "status": "todo",
            "source": "local",
            "assignee_ids": [],
            "tag_ids": [],
            "estimated_hours": 1.5,
        }
        changes = {"status": "in_progress", "description": None}
        calls = (
            (
                "create",
                lambda: manage.aoitalk_task_create(OPERATION_ID, create_payload),
                {"action": "create", "payload": create_payload},
            ),
            (
                "update",
                lambda: manage.aoitalk_task_update(OPERATION_ID, TASK_ID, STAMP, changes),
                {
                    "action": "update",
                    "task_id": TASK_ID,
                    "expected_updated_at": STAMP,
                    "changes": changes,
                },
            ),
            (
                "delete",
                lambda: manage.aoitalk_task_delete(OPERATION_ID, TASK_ID, STAMP),
                {"action": "delete", "task_id": TASK_ID, "expected_updated_at": STAMP},
            ),
            (
                "restore",
                lambda: manage.aoitalk_task_restore(OPERATION_ID, TASK_ID, DELETION_BATCH_ID),
                {
                    "action": "restore",
                    "task_id": TASK_ID,
                    "deletion_batch_id": DELETION_BATCH_ID,
                },
            ),
        )
        bodies: list[dict[str, object]] = []
        for action, callback, expected_body in calls:
            recorder, result = await _run_with_recorder(callback)
            request = recorder.requests[-1]
            self.assertEqual((request["method"], request["path"]),
                             ("PUT", f"/api/tasks/operations/{OPERATION_ID}"))
            self.assertEqual(request["kwargs"]["json"], expected_body)
            self.assertTrue(result["ok"], action)
            bodies.append(expected_body)

        recorder, result = await _run_with_recorder(
            lambda: manage.aoitalk_task_operation_status(OPERATION_ID)
        )
        request = recorder.requests[-1]
        self.assertEqual((request["method"], request["path"]),
                         ("GET", f"/api/tasks/operations/{OPERATION_ID}"))
        self.assertTrue(result["ok"])

        positive_cases = [
            {
                "model": "TaskOperationRequest",
                "value": body,
                "valid": True,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            }
            for body in bodies
        ]
        positive_cases.extend([
            {"model": "CreateTaskPayload", "value": create_payload, "valid": True},
            {"model": "UpdateTaskPayload", "value": changes, "valid": True},
            {
                "model": "TaskOperationRequest",
                "value": {"action": "create", "payload": {**create_payload, "kind": "legacy"}},
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "update",
                    "task_id": TASK_ID,
                    "expected_updated_at": STAMP,
                    "changes": {**changes, "kind": "legacy"},
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "update",
                    "task_id": TASK_ID,
                    "expected_updated_at": STAMP,
                    "changes": {**changes, "source": "remote"},
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "update",
                    "payload": {
                        "task_id": TASK_ID,
                        "expected_updated_at": STAMP,
                        "changes": changes,
                    },
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "delete",
                    "payload": {"task_id": TASK_ID, "expected_updated_at": STAMP},
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "delete",
                    "task_id": TASK_ID,
                    "expected_updated_at": STAMP,
                    "extra": "forbidden",
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "restore",
                    "payload": {"task_id": TASK_ID, "deletion_batch_id": DELETION_BATCH_ID},
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": {
                    "action": "restore",
                    "task_id": TASK_ID,
                    "deletion_batch_id": DELETION_BATCH_ID,
                    "extra": "forbidden",
                },
                "valid": False,
                "operation_id": OPERATION_ID,
                "path_valid": True,
            },
            {
                "model": "TaskOperationRequest",
                "value": bodies[0],
                "valid": True,
                "operation_id": "legacy-opaque-operation-id",
                "path_valid": False,
            },
        ])
        _validate_in_41(positive_cases, self)

    async def test_task_compact_list_and_bounded_read_match_current_route_contract(self):
        recorder, result = await _run_with_recorder(
            lambda: manage.aoitalk_tasks_list(limit=3, offset=6, status="todo")
        )
        request = recorder.requests[-1]
        self.assertEqual((request["method"], request["path"]), ("GET", "/api/tasks"))
        list_params = request["kwargs"]["params"]
        self.assertEqual(list_params, {
            "projection": "compact", "limit": 3, "offset": 6, "status": "todo",
        })
        self.assertTrue(_task_list_query_is_valid(list_params))
        self.assertFalse(_task_list_query_is_valid({**list_params, "cursor": "imaginary"}))
        self.assertFalse(_task_list_query_is_valid({**list_params, "kind": "imaginary"}))
        self.assertFalse(_task_list_query_is_valid({**list_params, "limit": 101}))
        self.assertEqual(result["offset"], 6)

        recorder, _ = await _run_with_recorder(
            lambda: manage.aoitalk_task_read(
                TASK_ID,
                section="description",
                limit=2,
                offset=12000,
                max_chars=8192,
                fingerprint="a" * 64,
            )
        )
        request = recorder.requests[-1]
        self.assertEqual((request["method"], request["path"]), ("GET", f"/api/tasks/{TASK_ID}"))
        read_params = request["kwargs"]["params"]
        self.assertEqual(read_params, {
            "section": "description", "limit": 2, "offset": 12000,
            "max_chars": 8192, "fingerprint": "a" * 64,
        })
        self.assertTrue(_task_read_query_is_valid(read_params))
        self.assertFalse(_task_read_query_is_valid({key: value for key, value in read_params.items() if key != "fingerprint"}))
        self.assertFalse(_task_read_query_is_valid({**read_params, "cursor": "imaginary"}))
        self.assertFalse(_task_read_query_is_valid({**read_params, "offset": 10001, "section": "comments"}))

        _validate_in_41(
            [
                {
                    "model": "TaskOperationRequest",
                    "value": {
                        "action": action,
                        "task_id": TASK_ID,
                        "expected_updated_at": STAMP,
                        **({} if action == "delete" else {"changes": {"status": "todo"}}),
                    },
                    "valid": True,
                    "operation_id": OPERATION_ID,
                    "expected_updated_at": STAMP,
                    "timestamp_valid": True,
                }
                for action in ("update", "delete")
            ],
            self,
        )

    async def test_default_task_read_uses_summary_section_without_full_response_query(self):
        from home_mcp_gateway import aoitalk_plugin_server

        tools = await aoitalk_plugin_server.mcp.list_tools()
        task_read_schema = next(tool for tool in tools if tool.name == "aoitalk_task_read").input_schema
        section_schema = task_read_schema["properties"]["section"]
        self.assertEqual(section_schema["default"], "task")
        self.assertNotIn("null", json.dumps(section_schema))

        recorder, task = await _run_with_recorder(lambda: manage.aoitalk_task_read(TASK_ID))
        request = recorder.requests[-1]
        self.assertEqual((request["method"], request["path"]), ("GET", f"/api/tasks/{TASK_ID}"))
        self.assertEqual(request["kwargs"]["params"], {
            "section": "task", "limit": 50, "offset": 0, "max_chars": 4000,
        })
        self.assertEqual(task["task"]["updated_at"], "2026-10-23T03:00:00.123456")

    async def test_task_read_naive_updated_at_flows_to_update_delete_and_other_iso_forms_stay_exact(self):
        naive = "2026-10-23T03:00:00.123456"
        recorder, task = await _run_with_recorder(lambda: manage.aoitalk_task_read(TASK_ID))
        read_request = recorder.requests[-1]
        self.assertEqual((read_request["method"], read_request["path"]), ("GET", f"/api/tasks/{TASK_ID}"))
        self.assertEqual(read_request["kwargs"]["params"]["section"], "task")
        self.assertEqual(task["task"]["updated_at"], naive)

        timestamps = (task["task"]["updated_at"], "2026-10-23T03:00:00.123456Z", "2026-10-23T12:00:00.123456+09:00")
        cases: list[dict[str, object]] = [{"read_timestamp": naive}]
        for timestamp in timestamps:
            for action in ("update", "delete"):
                if action == "update":
                    recorder, _ = await _run_with_recorder(
                        lambda timestamp=timestamp: manage.aoitalk_task_update(
                            OPERATION_ID, TASK_ID, timestamp, {"status": "todo"}
                        )
                    )
                else:
                    recorder, _ = await _run_with_recorder(
                        lambda timestamp=timestamp: manage.aoitalk_task_delete(
                            OPERATION_ID, TASK_ID, timestamp
                        )
                    )
                request = recorder.requests[-1]
                body = request["kwargs"]["json"]
                self.assertEqual(body["expected_updated_at"], timestamp)
                cases.append({
                    "model": "TaskOperationRequest",
                    "value": body,
                    "valid": True,
                    "operation_id": OPERATION_ID,
                    "expected_updated_at": timestamp,
                    "timestamp_valid": True,
                })

        for action in ("update", "delete"):
            if action == "update":
                bad_call = lambda: manage.aoitalk_task_update(
                    OPERATION_ID, TASK_ID, "not-a-timestamp", {"status": "todo"}
                )
            else:
                bad_call = lambda: manage.aoitalk_task_delete(
                    OPERATION_ID, TASK_ID, "not-a-timestamp"
                )
            recorder, result = await _run_with_recorder(bad_call)
            self.assertEqual(result["error"], "invalid_input")
            self.assertEqual(recorder.requests, [])
        cases.append({
            "model": "TaskOperationRequest",
            "value": {
                "action": "delete", "task_id": TASK_ID,
                "expected_updated_at": "not-a-timestamp",
            },
            "valid": True,
            "operation_id": OPERATION_ID,
            "expected_updated_at": "not-a-timestamp",
            "timestamp_valid": False,
        })
        _validate_in_41(cases, self)

    async def test_task_operation_rejects_opaque_operation_id_and_extra_payload_before_http(self):
        for callback in (
            lambda: manage.aoitalk_task_create("legacy-opaque-operation-id", {"title": "x"}),
            lambda: manage.aoitalk_task_create(OPERATION_ID, {"title": "x", "kind": "legacy"}),
            lambda: manage.aoitalk_task_update(
                OPERATION_ID, TASK_ID, STAMP, {"status": "todo", "kind": "legacy"}
            ),
            lambda: manage.aoitalk_task_update(
                OPERATION_ID, TASK_ID, STAMP, {"status": "todo", "source": "remote"}
            ),
        ):
            recorder, result = await _run_with_recorder(callback)
            self.assertEqual(result["error"], "invalid_input")
            self.assertEqual(recorder.requests, [])

    async def _docs_requests(self):
        recorder, _ = await _run_with_recorder(
            lambda: manage.aoitalk_docs_read(
                ROOT_ID,
                project_id=PROJECT_ID,
                view="outline",
                cursor="page-1",
                depth=4,
                page_chars=8192,
            )
        )
        read_request = recorder.requests[-1]
        self.assertEqual((read_request["method"], read_request["path"]), ("POST", "/api/docs/reads"))
        self.assertEqual(
            read_request["kwargs"]["json"],
            {
                "root_id": ROOT_ID,
                "project_id": PROJECT_ID,
                "view": "outline",
                "cursor": "page-1",
                "depth": 4,
                "page_chars": 8192,
            },
        )

        recorder, _ = await _run_with_recorder(
            lambda: manage.aoitalk_docs_prepare_edit(
                ROOT_ID,
                project_id=PROJECT_ID,
                cursor="edit-page",
                depth=6,
                page_chars=16384,
            )
        )
        edit_request = recorder.requests[-1]
        self.assertEqual((edit_request["method"], edit_request["path"]), ("POST", "/api/docs/edit-reads"))
        self.assertEqual(
            edit_request["kwargs"]["json"],
            {
                "root_id": ROOT_ID,
                "project_id": PROJECT_ID,
                "cursor": "edit-page",
                "depth": 6,
                "page_chars": 16384,
            },
        )

        changeset = {
            "intent": "revise_section",
            "operations": [
                {"op": "update", "node_id": ROOT_ID, "title": "Updated", "content": None}
            ],
        }
        recorder, _ = await _run_with_recorder(
            lambda: manage.aoitalk_docs_mutate(
                ROOT_ID, WRITE_TOKEN, OPERATION_ID, changeset, project_id=PROJECT_ID
            )
        )
        mutate_request = recorder.requests[-1]
        self.assertEqual((mutate_request["method"], mutate_request["path"]), ("POST", "/api/docs/changesets"))
        self.assertEqual(
            mutate_request["kwargs"]["json"],
            {
                "root_id": ROOT_ID,
                "project_id": PROJECT_ID,
                "write_token": WRITE_TOKEN,
                "operation_id": OPERATION_ID,
                "changeset": changeset,
            },
        )
        return read_request["kwargs"]["json"], edit_request["kwargs"]["json"], mutate_request["kwargs"]["json"]

    async def test_confirmed_docs_requests_match_route_and_strict_dtos(self):
        read_body, edit_body, mutate_body = await self._docs_requests()
        _validate_in_41(
            [
                {"model": "DocsProtocolReadRequest", "value": read_body, "valid": True},
                {"model": "DocsProtocolEditReadRequest", "value": edit_body, "valid": True},
                {"model": "DocsProtocolChangesetRequest", "value": mutate_body, "valid": True},
                {"model": "DocsChangesetPayload", "value": mutate_body["changeset"], "valid": True},
                {
                    "model": "DocsProtocolReadRequest",
                    "value": {**read_body, "node_id": ROOT_ID},
                    "valid": False,
                },
                {
                    "model": "DocsProtocolEditReadRequest",
                    "value": {**edit_body, "target": ROOT_ID},
                    "valid": False,
                },
                {
                    "model": "DocsProtocolChangesetRequest",
                    "value": {**mutate_body, "target": ROOT_ID},
                    "valid": False,
                },
                {
                    "model": "DocsProtocolChangesetRequest",
                    "value": {
                        **mutate_body,
                        "changeset": {
                            **mutate_body["changeset"],
                            "operations": [
                                {"op": "update", "node_id": ROOT_ID, "extra": "forbidden"}
                            ],
                        },
                    },
                    "valid": False,
                },
            ],
            self,
        )

    async def test_docs_mutation_rejects_invalid_typed_changesets_before_http(self):
        invalid_changesets = (
            {"intent": "revise_section", "operations": []},
            {"intent": "unknown", "operations": [{"op": "archive", "node_id": ROOT_ID}]},
            {"intent": "revise_section", "operations": [{"op": "unknown", "node_id": ROOT_ID}]},
            {
                "intent": "revise_section",
                "operations": [{"op": "update", "node_id": ROOT_ID, "extra": "forbidden"}],
            },
        )
        for changeset in invalid_changesets:
            recorder, result = await _run_with_recorder(
                lambda changeset=changeset: manage.aoitalk_docs_mutate(
                    ROOT_ID, WRITE_TOKEN, OPERATION_ID, changeset,
                )
            )
            self.assertEqual(result["error"], "invalid_input")
            self.assertEqual(recorder.requests, [])

    async def test_docs_page_bounds_reject_before_http(self):
        for callback in (
            lambda: manage.aoitalk_docs_read(ROOT_ID, page_chars=4095),
            lambda: manage.aoitalk_docs_read(ROOT_ID, cursor="x" * 513),
            lambda: manage.aoitalk_docs_prepare_edit(ROOT_ID, page_chars=32001),
            lambda: manage.aoitalk_docs_prepare_edit(ROOT_ID, cursor="x" * 129),
        ):
            recorder, result = await _run_with_recorder(callback)
            self.assertEqual(result["error"], "invalid_input")
            self.assertEqual(recorder.requests, [])

    async def test_confirmed_knowledge_requests_match_route_and_strict_dtos(self):
        recorder, _ = await _run_with_recorder(
            lambda: manage.aoitalk_knowledge_search(
                query="query",
                limit=7,
                source_id=DOCUMENT_ID,
                project_id=PROJECT_ID,
                tags=["docs", "meeting"],
                extension=".md",
                path_prefix="docs/",
            )
        )
        search_request = recorder.requests[-1]
        self.assertEqual((search_request["method"], search_request["path"]),
                         ("POST", "/api/knowledge/plugin/search"))
        search_body = search_request["kwargs"]["json"]
        self.assertEqual(
            search_body,
            {
                "query": "query",
                "limit": 7,
                "source_id": DOCUMENT_ID,
                "project_id": PROJECT_ID,
                "tags": ["docs", "meeting"],
                "extension": ".md",
                "path_prefix": "docs/",
            },
        )
        self.assertNotIn("cursor", search_body)

        recorder, result = await _run_with_recorder(
            lambda: manage.aoitalk_knowledge_read(
                DOCUMENT_ID,
                view="outline",
                max_chars=20000,
                offset=128,
                fingerprint="a" * 64,
            )
        )
        read_request = recorder.requests[-1]
        self.assertEqual(
            (read_request["method"], read_request["path"]),
            ("GET", f"/api/knowledge/plugin/documents/{DOCUMENT_ID}"),
        )
        read_params = read_request["kwargs"]["params"]
        self.assertEqual(
            read_params,
            {"view": "outline", "max_chars": 20000, "offset": 128, "fingerprint": "a" * 64},
        )
        self.assertTrue(_knowledge_query_is_valid(read_params))
        self.assertEqual(result["offset"], 128)
        self.assertEqual(result["content_fingerprint"], "a" * 64)
        self.assertTrue(result["has_more"])

        _validate_in_41(
            [
                {"model": "KnowledgePluginSearchPayload", "value": search_body, "valid": True},
                {"model": "KnowledgePluginSearchPayload", "value": {**search_body, "cursor": "forbidden"}, "valid": False},
            ],
            self,
        )

        self.assertFalse(_knowledge_query_is_valid({"view": "outline", "max_chars": 20000, "offset": 128}))
        self.assertFalse(_knowledge_query_is_valid({**read_params, "cursor": "forbidden"}))
        self.assertFalse(_knowledge_query_is_valid({**read_params, "view": "record"}))
        self.assertFalse(_knowledge_query_is_valid({**read_params, "max_chars": 32001}))
        self.assertFalse(_knowledge_query_is_valid({**read_params, "fingerprint": "a" * 63}))
        _validate_knowledge_paging_in_41(self)

    async def test_knowledge_read_bounds_reject_before_http(self):
        for callback in (
            lambda: manage.aoitalk_knowledge_read(DOCUMENT_ID, max_chars=0),
            lambda: manage.aoitalk_knowledge_read(DOCUMENT_ID, max_chars=32001),
            lambda: manage.aoitalk_knowledge_read(DOCUMENT_ID, offset=-1),
            lambda: manage.aoitalk_knowledge_read(DOCUMENT_ID, offset=1),
            lambda: manage.aoitalk_knowledge_read(DOCUMENT_ID, fingerprint="A" * 64),
        ):
            recorder, result = await _run_with_recorder(callback)
            self.assertEqual(result["error"], "invalid_input")
            self.assertEqual(recorder.requests, [])

    async def test_knowledge_search_does_not_expose_cursor_argument(self):
        with self.assertRaises(TypeError):
            await manage.aoitalk_knowledge_search("query", cursor="legacy")

    async def test_catalog_uses_real_knowledge_sources_endpoint(self):
        recorder = _Recorder()

        async def session(fn, *, knowledge: bool = False):
            return await fn(recorder)

        async def call(method: str, path: str, **kwargs: object):
            recorder.requests.append({"method": method, "path": path, "kwargs": kwargs})
            if path == "/api/projects":
                return 200, {"projects": [{"id": PROJECT_ID, "name": "Project"}]}, httpx.Headers()
            if path == "/api/tasks":
                return 200, {"tasks": []}, httpx.Headers()
            if path == f"/api/projects/{PROJECT_ID}/tags":
                return 200, {"tags": []}, httpx.Headers()
            if path == "/api/knowledge/plugin/sources":
                return 200, {"sources": []}, httpx.Headers()
            raise AssertionError(path)

        recorder.call = call  # type: ignore[method-assign]
        with patch.object(manage, "_session", session):
            result = await manage.aoitalk_catalog()
        self.assertTrue(result["ok"])
        paths = [request["path"] for request in recorder.requests]
        self.assertIn("/api/projects", paths)
        self.assertIn(f"/api/projects/{PROJECT_ID}/tags", paths)
        self.assertIn("/api/knowledge/plugin/sources", paths)
        self.assertNotIn("/api/tasks/catalog", paths)
        self.assertNotIn("kind", result["catalog"]["task_create_fields"])

    async def test_nested_structured_errors_preserve_details_and_code_less_404_is_unavailable(self):
        responses = [
            (409, {"detail": {"code": "close_incomplete_subtasks_confirmation_required", "message": "confirm"}}),
            (404, {"detail": {"code": "task_not_found", "message": "missing"}}),
            (404, {"detail": {"error": "not_observed", "message": "not observable"}}),
            (404, {"detail": "Not Found"}),
        ]

        def handler(request: httpx.Request):
            status, body = responses.pop(0)
            return httpx.Response(status, json=body)

        async with httpx.AsyncClient(
            base_url=_settings().base_url,
            transport=httpx.MockTransport(handler),
        ) as http:
            client = manage.ManageClient(_settings(), http=http)
            with self.assertRaises(manage.ManageError) as caught:
                await client.call("PUT", "/api/tasks/operations/" + OPERATION_ID,
                                  operation_endpoint=True, json={})
            self.assertEqual(caught.exception.code, "close_incomplete_subtasks_confirmation_required")
            self.assertEqual(caught.exception.detail["code"], "close_incomplete_subtasks_confirmation_required")
            with self.assertRaises(manage.ManageError) as caught:
                await client.call("GET", "/api/tasks/operations/" + OPERATION_ID,
                                  operation_endpoint=True)
            self.assertEqual(caught.exception.code, "task_not_found")
            self.assertEqual(caught.exception.detail["code"], "task_not_found")
            with self.assertRaises(manage.ManageError) as caught:
                await client.call("GET", "/api/tasks/operations/" + OPERATION_ID,
                                  operation_endpoint=True)
            self.assertEqual(caught.exception.code, "not_observed")
            self.assertEqual(caught.exception.detail["error"], "not_observed")
            with self.assertRaises(manage.ManageError) as caught:
                await client.call("GET", "/api/tasks/operations/" + OPERATION_ID,
                                  operation_endpoint=True)
            self.assertEqual(caught.exception.code, "endpoint_unavailable")


if __name__ == "__main__":
    unittest.main()
