"""Dedicated, authenticated AoiTalk task, Docs and Knowledge management tools.

The task mutation API is intentionally operation based.  This module never
falls back to the legacy task CRUD endpoints: operation IDs and their exact
payloads are part of the caller-visible recovery contract.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Literal, TypeAlias, Union
from uuid import UUID

import httpx
from typing_extensions import NotRequired, TypedDict

from . import aoitalk, aoitalk_auth

MIN_DOC_PAGE_CHARS = 4096
MAX_DOC_PAGE_CHARS = 32000
MAX_RESPONSE_BYTES = aoitalk.MAX_RESPONSE_BYTES
SERVER_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SECRET_KEY_RE = re.compile(r"(?i)(?:^|_)(?:password|secret|api_key|private_key|root_path|access_policy)$")
KNOWLEDGE_PRIVATE_KEYS = frozenset({"root_path", "token", "access_policy"})
DOCS_READ_VIEWS = frozenset({"document", "record", "outline"})
KNOWLEDGE_READ_VIEWS = frozenset({"body", "outline"})


MESSAGES = {
    "configuration_error": "AoiTalkの接続設定・認証情報をローカルの.envで確認してください。",
    "invalid_input": "入力値と、catalog/readで取得した現在の値を確認してください。",
    "authentication_failed": ".envの認証情報でログインできません。",
    "permission_denied": "AoiTalk側のアクセス権限を確認してください。権限の回避は行いません。",
    "endpoint_unavailable": "AoiTalkの専用Plugin APIが利用できません。旧CRUDへは切り替えていません。",
    "invalid_response": "AoiTalkの応答が契約に合いません。完了・cursor進行として扱いません。",
    "connection_error": "AoiTalkへ接続できません。送信結果が不明な場合は同じoperation_idと同じ引数で照合してください。",
    "rate_limited": "AoiTalkがリクエストを制限しています。Retry-Afterを確認してください。",
    "server_error": "AoiTalk APIがエラーを返しました。応答の構造化errorを確認してください。",
}


TASK_FIELDS = frozenset({
    "project_id", "knowledge_node_id", "title", "description", "status", "priority",
    "start_at", "end_at", "all_day", "auto_close_on_due", "reminder_offsets",
    "notifications_enabled", "estimated_hours", "parent_task_id", "source", "assignee_ids",
    "tag_ids", "recurrence_rrule", "recurrence_timezone", "task_metadata",
    "close_incomplete_subtasks",
})
TASK_UPDATE_FIELDS = TASK_FIELDS - {"source"}
TASK_CREATE_FIELDS = TASK_FIELDS - {"close_incomplete_subtasks"}


class TaskCreatePayload(TypedDict):
    title: str
    project_id: NotRequired[str | None]
    knowledge_node_id: NotRequired[str | None]
    description: NotRequired[str | None]
    status: NotRequired[str]
    priority: NotRequired[str | None]
    start_at: NotRequired[str | None]
    end_at: NotRequired[str | None]
    all_day: NotRequired[bool]
    auto_close_on_due: NotRequired[bool]
    reminder_offsets: NotRequired[list[int] | None]
    notifications_enabled: NotRequired[bool | None]
    estimated_hours: NotRequired[float | None]
    parent_task_id: NotRequired[str | None]
    source: NotRequired[str]
    assignee_ids: NotRequired[list[str]]
    tag_ids: NotRequired[list[str]]
    recurrence_rrule: NotRequired[str | None]
    recurrence_timezone: NotRequired[str]
    task_metadata: NotRequired[dict[str, Any] | None]


class TaskChanges(TypedDict, total=False):
    project_id: str | None
    knowledge_node_id: str | None
    title: str | None
    description: str | None
    status: str | None
    priority: str | None
    start_at: str | None
    end_at: str | None
    all_day: bool | None
    auto_close_on_due: bool | None
    reminder_offsets: list[int] | None
    notifications_enabled: bool | None
    estimated_hours: float | None
    parent_task_id: str | None
    assignee_ids: list[str] | None
    tag_ids: list[str] | None
    recurrence_rrule: str | None
    recurrence_timezone: str | None
    task_metadata: dict[str, Any] | None
    close_incomplete_subtasks: bool | None


class DocsUpdateOperation(TypedDict):
    op: Literal["update"]
    node_id: str
    title: NotRequired[str | None]
    description: NotRequired[str | None]
    content: NotRequired[str | None]


class DocsCreateOperation(TypedDict):
    op: Literal["create"]
    ref: str
    parent_id: str
    title: str
    description: NotRequired[str | None]
    content: NotRequired[str | None]
    block_type: NotRequired[Literal["paragraph", "markdown", "code"] | None]


class DocsSetFieldsOperation(TypedDict):
    op: Literal["set_fields"]
    node_id: str
    values: dict[str, Any]


class DocsAddTagOperation(TypedDict):
    op: Literal["add_tag"]
    node_id: str
    tag_id: str


class DocsRemoveTagOperation(TypedDict):
    op: Literal["remove_tag"]
    node_id: str
    tag_id: str


class DocsMoveOperation(TypedDict):
    op: Literal["move"]
    node_id: str
    parent_id: str
    leave_reference: NotRequired[bool | None]


class DocsArchiveOperation(TypedDict):
    op: Literal["archive"]
    node_id: str


DocsChangesetOperation: TypeAlias = Union[
    DocsUpdateOperation,
    DocsCreateOperation,
    DocsSetFieldsOperation,
    DocsAddTagOperation,
    DocsRemoveTagOperation,
    DocsMoveOperation,
    DocsArchiveOperation,
]


class DocsChangesetPayload(TypedDict):
    intent: Literal["revise_section", "upsert_record", "reorganize_subtree"]
    operations: list[DocsChangesetOperation]


class ManageError(Exception):
    def __init__(self, code: str, http_status: int | None = None, *, server_code: str | None = None,
                 detail: Any | None = None, retry_after: int | None = None,
                 local_hold: int | None = None, uncertain: bool = False):
        self.code = code if code in MESSAGES or SERVER_CODE_RE.fullmatch(code or "") else "server_error"
        self.http_status = http_status
        self.server_code = server_code
        self.detail = detail
        self.retry_after = retry_after
        self.local_hold = local_hold
        self.uncertain = uncertain
        super().__init__(MESSAGES.get(self.code, "AoiTalk APIが構造化エラーを返しました。"))


def _error(exc: ManageError, **state: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": exc.code,
                              "message": MESSAGES.get(exc.code, "AoiTalk APIが構造化エラーを返しました。"), **state}
    if exc.http_status is not None:
        result["http_status"] = exc.http_status
    if exc.server_code:
        result["server_error_code"] = exc.server_code
    if exc.detail is not None:
        result["detail"] = exc.detail
    if exc.retry_after is not None:
        result["retry_after_seconds"] = exc.retry_after
    if exc.local_hold is not None:
        result["local_cooldown_seconds"] = exc.local_hold
    if exc.uncertain:
        result["submission_uncertain"] = True
        result["next_step"] = "Inspect status and retry only with the same operation_id and identical arguments."
    return result


def _page_error(exc: ManageError) -> dict[str, Any]:
    result = _error(exc, cursor_advance_blocked=True)
    if not exc.uncertain:
        result["next_step"] = "Resolve the error, then retry with the exact same page arguments and cursor; do not advance it."
    return result


def _safe_data(value: Any, settings: aoitalk.Settings, *, knowledge: bool = False, depth: int = 0) -> Any:
    if depth > 24:
        raise ManageError("invalid_response")
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            name = str(key)
            lowered = name.casefold()
            if knowledge and lowered in KNOWLEDGE_PRIVATE_KEYS:
                continue
            if SECRET_KEY_RE.search(name):
                continue
            safe[name] = _safe_data(item, settings, knowledge=knowledge, depth=depth + 1)
        return safe
    if isinstance(value, list):
        return [_safe_data(item, settings, knowledge=knowledge, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return aoitalk._redact_text(value, settings)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ManageError("invalid_response")


def _structured_error_code(value: Any, *, depth: int = 0) -> str | None:
    """Find a bounded FastAPI ``detail.code``/``detail.error`` value."""
    if depth > 3:
        return None
    if isinstance(value, dict):
        for key in ("code", "error"):
            candidate = value.get(key)
            if isinstance(candidate, str) and SERVER_CODE_RE.fullmatch(candidate):
                return candidate
            if isinstance(candidate, dict):
                nested = _structured_error_code(candidate, depth=depth + 1)
                if nested is not None:
                    return nested
        for key in ("detail", "data"):
            nested = _structured_error_code(value.get(key), depth=depth + 1)
            if nested is not None:
                return nested
    elif isinstance(value, list):
        for item in value[:8]:
            nested = _structured_error_code(item, depth=depth + 1)
            if nested is not None:
                return nested
    return None


def _safe_error_detail(value: Any, settings: aoitalk.Settings) -> Any | None:
    """Keep structured error details while applying the normal redaction rules."""
    if value is None:
        return None
    try:
        return _safe_data(value, settings)
    except ManageError:
        # An unexpected error detail type must not be echoed across MCP.
        return None


class ManageClient(aoitalk.Client):
    """HTTP handling for the dedicated management API, using the shared auth jar."""

    async def call(self, method: str, path: str, *, operation_endpoint: bool = False,
                   mutation: bool | None = None,
                   **kwargs: Any) -> tuple[int, Any, httpx.Headers]:
        is_mutation = method.upper() in {"PUT", "POST", "PATCH", "DELETE"} if mutation is None else mutation
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                status = response.status_code
                headers = response.headers
                chunks: list[bytes] = []
                size = 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES:
                        raise ManageError("invalid_response", status,
                                          uncertain=is_mutation and status >= 500)
                    chunks.append(part)
        except ManageError:
            raise
        except (httpx.HTTPError, OSError):
            raise ManageError("connection_error", uncertain=is_mutation) from None

        if 300 <= status < 400:
            raise ManageError("configuration_error", status)

        if status == 204:
            data: Any = {}
        else:
            raw = b"".join(chunks)
            try:
                data = json.loads(raw) if raw else {}
            except (ValueError, UnicodeError):
                if status in {200, 201, 202}:
                    raise ManageError("invalid_response", status,
                                      uncertain=is_mutation) from None
                data = None

        if status in {200, 201, 202, 204}:
            if not isinstance(data, (dict, list)):
                raise ManageError("invalid_response", status)
            return status, data, headers

        raw_detail = data.get("detail") if isinstance(data, dict) else None
        code = _structured_error_code(data)
        if status == 404 and operation_endpoint and code is None:
            raise ManageError("endpoint_unavailable", status)
        if operation_endpoint and 400 <= status < 500 and code is None:
            raise ManageError("invalid_response", status)
        default_code = {400: "invalid_input", 401: "authentication_failed", 403: "permission_denied",
                        404: "not_found", 409: "conflict", 410: "operation_expired",
                        422: "invalid_input", 429: "rate_limited"}.get(status, "server_error")
        retry_after = aoitalk.parse_retry_after(headers.get("retry-after")) if status == 429 else None
        detail = _safe_error_detail(raw_detail, self.settings)
        if isinstance(detail, str):
            detail = detail[:300] or None
        elif status == 422 and isinstance(raw_detail, list):
            fields = sorted({".".join(str(part) for part in item.get("loc", [])[1:])
                             for item in raw_detail if isinstance(item, dict) and isinstance(item.get("loc"), list)})
            detail = {"message": "invalid fields: " + ", ".join(fields[:10])} if fields else detail
        server_code = code
        # Server-authored business codes are returned as-is so task_not_found,
        # operation_conflict, task_cas_stale, not_observed and receipt errors
        # remain distinguishable to the caller.
        error_code = code or default_code
        write_uncertain = is_mutation and status >= 500
        raise ManageError(error_code, status, server_code=server_code, detail=detail,
                          retry_after=retry_after, uncertain=write_uncertain)

    async def login_manage(self) -> None:
        try:
            await self.login()
        except aoitalk.ClipError as exc:
            raise _manage_auth_error(exc) from None


def _manage_auth_error(exc: aoitalk.ClipError) -> ManageError:
    return ManageError(exc.code if exc.code in MESSAGES else "server_error", exc.http_status,
                       retry_after=exc.retry_after, local_hold=exc.local_hold)


async def _session(fn, *, knowledge: bool = False):
    try:
        settings = aoitalk.load_settings()
    except aoitalk.ClipError:
        raise ManageError("configuration_error") from None
    async with aoitalk_auth.client_for(
        ManageClient, settings, lambda client: client.login_manage(), _manage_auth_error,
        lambda exc: isinstance(exc, ManageError) and exc.http_status == 401,
    ) as client:
        return _safe_data(await fn(client), settings, knowledge=knowledge)


def _text(value: Any, name: str, maximum: int, *, allow_empty: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value.strip())
            or any(ord(ch) < 32 for ch in value)):
        raise ManageError("invalid_input", detail=f"{name} must be bounded text")
    return value


def _operation_id(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ManageError("invalid_input", detail="operation_id must be a UUID") from None


def _task_id(value: Any) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ManageError("invalid_input", detail="task_id must be a UUID") from None


def _limit(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ManageError("invalid_input", detail=f"{name} must be between {low} and {high}")
    return value


def _cursor(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _text(value, "cursor", 2048)


def _payload(value: Any, *, create: bool) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 40:
        raise ManageError("invalid_input", detail="payload must be a bounded object")
    allowed = TASK_CREATE_FIELDS
    extra = set(value) - allowed
    if extra:
        raise ManageError("invalid_input", detail="payload contains fields outside the task catalog")
    if create and (not isinstance(value.get("title"), str) or not value["title"].strip()):
        raise ManageError("invalid_input", detail="payload.title is required")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ManageError("invalid_input", detail="payload must contain JSON values") from None
    return dict(value)


def _changes(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value or len(value) > 40:
        raise ManageError("invalid_input", detail="changes must be a nonempty object")
    extra = set(value) - TASK_UPDATE_FIELDS
    if extra:
        raise ManageError("invalid_input", detail="changes contains fields outside the task catalog")
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ManageError("invalid_input", detail="changes must contain JSON values") from None
    # Deliberately preserve explicit null values and omitted keys exactly.
    return dict(value)


def _has_more(headers: httpx.Headers) -> bool | None:
    raw = headers.get("x-aoitalk-has-more")
    if raw is None:
        return None
    value = raw.strip().casefold()
    if value in {"true", "1", "yes"}:
        return True
    if value in {"false", "0", "no"}:
        return False
    raise ManageError("invalid_response")


def _with_page_header(data: Any, headers: httpx.Headers, key: str | None = None) -> dict[str, Any]:
    has_more = _has_more(headers)
    if isinstance(data, list):
        result = {key or "items": data}
    elif isinstance(data, dict):
        result = dict(data)
    else:
        raise ManageError("invalid_response")
    if has_more is not None:
        result["has_more"] = has_more
    if key and key not in result and isinstance(result.get("items"), list):
        result[key] = result["items"]
    return result


def _uuid_text(value: Any, name: str) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ManageError("invalid_input", detail=f"{name} must be a UUID") from None


def _docs_changeset(value: Any) -> dict[str, Any]:
    """Validate the strict Docs changeset union before sending it to 41."""
    if not isinstance(value, dict) or set(value) != {"intent", "operations"}:
        raise ManageError("invalid_input", detail="changeset must contain only intent and operations")
    if value.get("intent") not in {"revise_section", "upsert_record", "reorganize_subtree"}:
        raise ManageError("invalid_input", detail="changeset.intent is invalid")
    operations = value.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise ManageError("invalid_input", detail="changeset.operations must contain 1-100 operations")
    allowed: dict[str, tuple[set[str], set[str]]] = {
        "update": ({"op", "node_id"}, {"title", "description", "content"}),
        "create": ({"op", "ref", "parent_id", "title"}, {"description", "content", "block_type"}),
        "set_fields": ({"op", "node_id", "values"}, set()),
        "add_tag": ({"op", "node_id", "tag_id"}, set()),
        "remove_tag": ({"op", "node_id", "tag_id"}, set()),
        "move": ({"op", "node_id", "parent_id"}, {"leave_reference"}),
        "archive": ({"op", "node_id"}, set()),
    }
    normalized: list[dict[str, Any]] = []
    for operation in operations:
        if not isinstance(operation, dict) or not isinstance(operation.get("op"), str):
            raise ManageError("invalid_input", detail="changeset operation must be a typed object")
        op = operation["op"]
        shape = allowed.get(op)
        if shape is None:
            raise ManageError("invalid_input", detail="changeset operation op is invalid")
        required, optional = shape
        keys = set(operation)
        if not required <= keys or keys - required - optional:
            raise ManageError("invalid_input", detail=f"changeset {op} operation fields are invalid")
        for key in required - {"op", "values"}:
            if not isinstance(operation.get(key), str):
                raise ManageError("invalid_input", detail=f"changeset {op}.{key} must be text")
        if op == "set_fields":
            if not isinstance(operation.get("values"), dict):
                raise ManageError("invalid_input", detail="changeset set_fields.values must be an object")
        if op == "create" and operation.get("block_type") not in (None, "paragraph", "markdown", "code"):
            raise ManageError("invalid_input", detail="changeset create.block_type is invalid")
        if op == "move" and "leave_reference" in operation and not (
            operation["leave_reference"] is None or isinstance(operation["leave_reference"], bool)
        ):
            raise ManageError("invalid_input", detail="changeset move.leave_reference must be boolean")
        for key in optional:
            if key in operation and operation[key] is not None and not isinstance(operation[key], str):
                if op != "move" or key != "leave_reference":
                    raise ManageError("invalid_input", detail=f"changeset {op}.{key} must be text or null")
        normalized.append(dict(operation))
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise ManageError("invalid_input", detail="changeset must contain JSON values") from None
    return {"intent": value["intent"], "operations": normalized}


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ManageError("invalid_input", detail=f"{name} must be non-negative")
    return value


async def aoitalk_catalog() -> dict[str, Any]:
    """Compose the stable Task field set and authorized Project/Tag/Knowledge candidates."""
    try:
        async def read(client: ManageClient):
            def collection(data: Any, *keys: str) -> list[Any]:
                if isinstance(data, list):
                    return data
                if isinstance(data, dict):
                    for key in keys:
                        value = data.get(key)
                        if isinstance(value, list):
                            return value
                return []

            _, project_data, _ = await client.call("GET", "/api/projects")
            projects = collection(project_data, "projects", "items")[:50]
            public_projects: list[dict[str, Any]] = []
            tags_by_project: dict[str, list[dict[str, Any]]] = {}
            for item in projects:
                if not isinstance(item, dict):
                    continue
                try:
                    project_id = _uuid_text(item.get("id"), "project_id")
                except ManageError:
                    continue
                public_projects.append({
                    "id": project_id,
                    "name": aoitalk._redact_text(str(item.get("name", "")), client.settings),
                })
                _, tag_data, _ = await client.call("GET", f"/api/projects/{project_id}/tags")
                raw_tags = collection(tag_data, "tags", "items")[:100]
                tags_by_project[project_id] = [
                    {
                        "id": str(tag.get("id")),
                        "name": aoitalk._redact_text(str(tag.get("name", "")), client.settings),
                        "color": tag.get("color"),
                    }
                    for tag in raw_tags
                    if isinstance(tag, dict) and isinstance(tag.get("id"), str)
                ]

            _, source_data, _ = await client.call("GET", "/api/knowledge/plugin/sources")
            raw_sources = collection(source_data, "sources", "items")[:100]
            sources = [
                {key: source[key] for key in ("id", "name", "type") if key in source}
                for source in raw_sources
                if isinstance(source, dict)
            ]
            catalog = {
                "task_fields": sorted(TASK_FIELDS),
                "task_create_fields": sorted(TASK_CREATE_FIELDS),
                "task_update_fields": sorted(TASK_UPDATE_FIELDS),
                "projects": public_projects,
                "tags_by_project": tags_by_project,
                "knowledge_sources": sources,
            }
            return {"ok": True, "catalog": catalog}
        return await _session(read)
    except ManageError as exc:
        return _error(exc)


async def aoitalk_tasks_list(
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    project_id: str | None = None,
    space_id: str | None = None,
    assignee_id: str | None = None,
    search: str | None = None,
    due_before: str | None = None,
    updated_before: str | None = None,
) -> dict[str, Any]:
    """List one SQL-bounded compact Task page; continue with next_offset while has_more is true."""
    try:
        count = _limit(limit, 1, 100, "limit")
        page_offset = _nonnegative_int(offset, "offset")
        params: dict[str, Any] = {
            "projection": "compact", "limit": count, "offset": page_offset,
        }
        for name, value in (
            ("project_id", project_id),
            ("space_id", space_id),
            ("assignee_id", assignee_id),
        ):
            if value is not None:
                params[name] = _uuid_text(value, name)
        if status is not None:
            params["status"] = _text(status, "status", 40)
        if search is not None:
            params["search"] = _text(search, "search", 2000, allow_empty=True)
        for name, value in (("due_before", due_before), ("updated_before", updated_before)):
            if value is not None:
                timestamp = _text(value, name, 80)
                _parse_timestamp(timestamp)
                params[name] = timestamp

        async def read(client: ManageClient):
            _, data, headers = await client.call("GET", "/api/tasks", params=params)
            result = _with_page_header(data, headers, "tasks")
            if isinstance(result.get("tasks"), list) and len(result["tasks"]) > count:
                raise ManageError("invalid_response")
            result.setdefault("ok", True)
            result.setdefault("limit", count)
            result.setdefault("offset", page_offset)
            if result.get("has_more") is True and isinstance(result.get("tasks"), list):
                result.setdefault("next_offset", page_offset + len(result["tasks"]))
            return result
        return await _session(read)
    except ManageError as exc:
        return _error(exc)


async def aoitalk_task_read(
    task_id: str,
    section: Literal[
        "task", "description", "metadata", "comments", "references",
        "activities", "occurrences", "time_entries",
    ] = "task",
    limit: int = 50,
    offset: int = 0,
    max_chars: int = 4000,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Read one bounded Task section; defaults to the task summary.

    Pass `task.updated_at` unchanged as `expected_updated_at` for the next mutation.
    Reuse text-section fingerprints when continuing description or metadata pages.
    """
    state = {"task_id": task_id}
    try:
        normalized = _task_id(task_id)
        section_name = _text(section, "section", 32)
        if section_name not in {
            "task", "description", "metadata", "comments", "references",
            "activities", "occurrences", "time_entries",
        }:
            raise ManageError("invalid_input", detail="section is not in the Task read projection")
        page_limit = _limit(limit, 1, 100, "limit")
        page_offset = _nonnegative_int(offset, "offset")
        if section_name not in {"description", "metadata"} and page_offset > 10000:
            raise ManageError("invalid_input", detail="offset must be at most 10000 for this section")
        page_chars = _limit(max_chars, 1, 20000, "max_chars")
        params: dict[str, Any] = {
            "section": section_name,
            "limit": page_limit,
            "offset": page_offset,
            "max_chars": page_chars,
        }
        if fingerprint is not None:
            if section_name not in {"description", "metadata"}:
                raise ManageError("invalid_input", detail="fingerprint is only valid for description or metadata")
            value = _text(fingerprint, "fingerprint", 64)
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ManageError("invalid_input", detail="fingerprint must be a lowercase SHA-256 value")
            params["fingerprint"] = value
        if section_name in {"description", "metadata"} and page_offset > 0 and fingerprint is None:
            raise ManageError("invalid_input", detail="fingerprint is required when offset is nonzero")

        async def read(client: ManageClient):
            _, data, _ = await client.call("GET", f"/api/tasks/{normalized}", params=params)
            if not isinstance(data, dict):
                raise ManageError("invalid_response")
            result = {"ok": True, **data, "task_id": normalized}
            if data.get("has_more") is True and isinstance(data.get("items"), list):
                result.setdefault("next_offset", offset + len(data["items"]))
            return result
        return await _session(read)
    except ManageError as exc:
        return _error(exc, **state)


async def _task_operation(action: str, operation_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    operation = _operation_id(operation_id)
    attempted = False
    try:
        async def submit(client: ManageClient):
            nonlocal attempted
            attempted = True
            request = {"action": action}
            if action == "create":
                request["payload"] = payload
            else:
                request.update(payload)
            _, data, _ = await client.call(
                "PUT", f"/api/tasks/operations/{operation}", operation_endpoint=True,
                json=request,
            )
            if not isinstance(data, dict):
                raise ManageError("invalid_response", uncertain=True)
            return {**data, "ok": data.get("ok", True), "operation_id": operation, "action": action}
        return await _session(submit)
    except ManageError as exc:
        return _error(exc, operation_id=operation, action=action,
                      submission_uncertain=exc.uncertain or (attempted and exc.http_status is None))


async def aoitalk_task_create(
    operation_id: UUID,
    payload: TaskCreatePayload,
) -> dict[str, Any]:
    """Create one Task through the operation ledger; reuse operation_id only with this exact payload, then read before another mutation."""
    try:
        operation = _operation_id(operation_id)
        clean = _payload(payload, create=True)
    except ManageError as exc:
        return _error(exc, operation_id=operation_id, action="create")
    return await _task_operation("create", operation, clean)


async def aoitalk_task_update(
    operation_id: UUID,
    task_id: str,
    expected_updated_at: str,
    changes: TaskChanges,
) -> dict[str, Any]:
    """Update through the operation ledger using task_read's expected_updated_at; omitted fields stay unchanged, null is explicit, and read again before another mutation."""
    try:
        operation = _operation_id(operation_id)
        task = _task_id(task_id)
        expected = _text(expected_updated_at, "expected_updated_at", 80)
        _parse_timestamp(expected)
        clean = _changes(changes)
    except ManageError as exc:
        return _error(exc, operation_id=operation_id, action="update", task_id=task_id)
    return await _task_operation("update", operation, {
        "task_id": task, "expected_updated_at": expected, "changes": clean,
    })


def _parse_timestamp(value: str) -> None:
    try:
        # Match 41's parse_utc_naive_datetime acceptance rules while retaining
        # the caller's exact input string for the Task CAS request body.
        datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        raise ManageError("invalid_input", detail="expected_updated_at must be an ISO timestamp from task_read") from None


async def aoitalk_task_delete(
    operation_id: UUID,
    task_id: str,
    expected_updated_at: str,
) -> dict[str, Any]:
    """Delete through the operation ledger using task_read's expected_updated_at and a caller-held operation_id."""
    try:
        operation = _operation_id(operation_id)
        task = _task_id(task_id)
        expected = _text(expected_updated_at, "expected_updated_at", 80)
        _parse_timestamp(expected)
    except ManageError as exc:
        return _error(exc, operation_id=operation_id, action="delete", task_id=task_id)
    return await _task_operation("delete", operation, {"task_id": task, "expected_updated_at": expected})


async def aoitalk_task_restore(
    operation_id: UUID,
    task_id: str,
    deletion_batch_id: str,
) -> dict[str, Any]:
    """Restore the exact deletion batch through the operation ledger; deletion_batch_id is required."""
    try:
        operation = _operation_id(operation_id)
        task = _task_id(task_id)
        batch = _uuid_text(deletion_batch_id, "deletion_batch_id")
    except ManageError as exc:
        return _error(exc, operation_id=operation_id, action="restore", task_id=task_id)
    return await _task_operation("restore", operation, {"task_id": task, "deletion_batch_id": batch})


async def aoitalk_task_operation_status(operation_id: UUID) -> dict[str, Any]:
    """Read the stored operation receipt by operation_id, including pending or unconfirmed side-effect state."""
    try:
        operation = _operation_id(operation_id)
        async def read(client: ManageClient):
            _, data, _ = await client.call("GET", f"/api/tasks/operations/{operation}", operation_endpoint=True)
            if not isinstance(data, dict):
                raise ManageError("invalid_response")
            # Keep the stored operation result and any current Task projection
            # separate as returned by AoiTalk; never relabel snapshots as current.
            return {**data, "ok": data.get("ok", True), "operation_id": operation}
        return await _session(read)
    except ManageError as exc:
        return _error(exc, operation_id=operation_id)


async def aoitalk_docs_search(
    query: str,
    limit: int = 20,
    project: str | None = None,
) -> dict[str, Any]:
    """Search Docs and return ID/title candidates only, with a maximum limit of 50 and no generated excerpts."""
    try:
        term = _text(query, "query", 500)
        count = _limit(limit, 1, 50, "limit")
        params: dict[str, Any] = {"q": term, "limit": count}
        if project:
            params["project"] = _text(project, "project", 120)
        async def read(client: ManageClient):
            _, data, headers = await client.call("GET", "/api/docs/search", params=params)
            raw_items = data if isinstance(data, list) else next(
                (data.get(key) for key in ("items", "results", "nodes", "candidates")
                 if isinstance(data.get(key), list)), None
            ) if isinstance(data, dict) else None
            if not isinstance(raw_items, list):
                raise ManageError("invalid_response")
            if len(raw_items) > count:
                raise ManageError("invalid_response")
            candidates: list[dict[str, Any]] = []
            for item in raw_items:
                if not isinstance(item, dict):
                    continue
                identity = item.get("id", item.get("node_id"))
                title = item.get("title")
                if isinstance(identity, str) and isinstance(title, str):
                    candidates.append({"id": identity, "title": aoitalk._redact_text(title, client.settings)})
            result = {"ok": True, "candidates": candidates, "limit": count}
            more = _has_more(headers)
            if more is not None:
                result["has_more"] = more
            return result
        return await _session(read)
    except ManageError as exc:
        return _error(exc)


async def aoitalk_docs_read(
    root_id: str,
    project_id: str | None = None,
    view: str = "document",
    cursor: str | None = None,
    depth: int = 8,
    page_chars: int = 12000,
) -> dict[str, Any]:
    """Read one bounded Docs projection page using the strict protocol DTO."""
    try:
        root = _uuid_text(root_id, "root_id")
        project = _uuid_text(project_id, "project_id") if project_id is not None else None
        view_name = _text(view, "view", 16)
        if view_name not in DOCS_READ_VIEWS:
            raise ManageError("invalid_input", detail="view must be document, record, or outline")
        page_cursor = _text(cursor or "", "cursor", 512, allow_empty=True)
        page_depth = _limit(depth, 0, 32, "depth")
        page_size = _limit(page_chars, MIN_DOC_PAGE_CHARS, MAX_DOC_PAGE_CHARS, "page_chars")
        body: dict[str, Any] = {
            "root_id": root, "project_id": project, "view": view_name,
            "cursor": page_cursor, "depth": page_depth, "page_chars": page_size,
        }
        async def read(client: ManageClient):
            _, data, headers = await client.call(
                "POST", "/api/docs/reads", mutation=False, json=body,
            )
            return {"ok": True, **_with_page_header(data, headers)}
        return await _session(read)
    except ManageError as exc:
        return _page_error(exc)


async def aoitalk_docs_prepare_edit(
    root_id: str,
    project_id: str | None = None,
    cursor: str | None = None,
    depth: int = 8,
    page_chars: int = 12000,
) -> dict[str, Any]:
    """Read one Docs edit page using the strict protocol DTO."""
    try:
        root = _uuid_text(root_id, "root_id")
        project = _uuid_text(project_id, "project_id") if project_id is not None else None
        page_cursor = _text(cursor or "", "cursor", 128, allow_empty=True)
        page_depth = _limit(depth, 0, 32, "depth")
        page_size = _limit(page_chars, MIN_DOC_PAGE_CHARS, MAX_DOC_PAGE_CHARS, "page_chars")
        body: dict[str, Any] = {
            "root_id": root, "project_id": project, "cursor": page_cursor,
            "depth": page_depth, "page_chars": page_size,
        }
        async def read(client: ManageClient):
            _, data, headers = await client.call("POST", "/api/docs/edit-reads", mutation=False, json=body)
            result = _with_page_header(data, headers)
            more = result.get("has_more")
            if more is None:
                more = bool(result.get("has_more_page", False))
            if more or result.get("coverage_complete") is not True or result.get("next_cursor") not in (None, ""):
                result.pop("write_token", None)
            result["ok"] = True
            return result
        return await _session(read)
    except ManageError as exc:
        return _page_error(exc)


async def aoitalk_docs_mutate(
    root_id: str,
    write_token: str,
    operation_id: str,
    changeset: DocsChangesetPayload,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Apply one typed Docs changeset; retries reuse all exact request fields."""
    attempted = False
    try:
        root = _uuid_text(root_id, "root_id")
        project = _uuid_text(project_id, "project_id") if project_id is not None else None
        token = _uuid_text(write_token, "write_token")
        operation = _uuid_text(operation_id, "operation_id")
        clean_changeset = _docs_changeset(changeset)
        async def mutate(client: ManageClient):
            nonlocal attempted
            attempted = True
            _, data, _ = await client.call("POST", "/api/docs/changesets", json={
                "root_id": root, "project_id": project, "write_token": token,
                "operation_id": operation, "changeset": clean_changeset,
            })
            if not isinstance(data, dict):
                raise ManageError("invalid_response", uncertain=True)
            return {**data, "ok": data.get("ok", True), "root_id": root,
                    "target": root, "write_token": token, "operation_id": operation}
        return await _session(mutate)
    except (ManageError, ValueError, TypeError) as caught:
        exc = caught if isinstance(caught, ManageError) else ManageError("invalid_input")
        result = _error(exc, root_id=root_id, target=root_id, operation_id=operation_id)
        if attempted and exc.uncertain:
            result["write_token"] = write_token
            result["submission_uncertain"] = True
            result["next_step"] = "Inspect status and retry only with the same write_token, operation_id and changeset."
        return result


async def aoitalk_knowledge_search(
    query: str = "",
    limit: int = 10,
    source_id: str | None = None,
    project_id: str | None = None,
    tags: list[str] | None = None,
    extension: str | None = None,
    path_prefix: str | None = None,
) -> dict[str, Any]:
    """Search Knowledge through the strict plugin search DTO without cursor fields."""
    try:
        term = _text(query, "query", 2000, allow_empty=True)
        count = _limit(limit, 1, 50, "limit")
        body: dict[str, Any] = {"query": term, "limit": count}
        if source_id is not None:
            body["source_id"] = _uuid_text(source_id, "source_id")
        if project_id is not None:
            body["project_id"] = _uuid_text(project_id, "project_id")
        if tags is not None:
            if not isinstance(tags, list) or len(tags) > 64:
                raise ManageError("invalid_input", detail="tags must contain at most 64 values")
            body["tags"] = [_text(tag, "tags", 200) for tag in tags]
        if extension is not None:
            body["extension"] = _text(extension, "extension", 32, allow_empty=True)
        if path_prefix is not None:
            body["path_prefix"] = _text(path_prefix, "path_prefix", 1000, allow_empty=True)
        async def search(client: ManageClient):
            _, data, headers = await client.call("POST", "/api/knowledge/plugin/search", mutation=False, json=body)
            result = _with_page_header(data, headers, "results")
            result.setdefault("ok", True)
            if isinstance(result.get("results"), list) and len(result["results"]) > count:
                raise ManageError("invalid_response")
            return result
        return await _session(search, knowledge=True)
    except ManageError as exc:
        return _page_error(exc)


async def aoitalk_knowledge_read(
    document_id: str,
    view: str = "body",
    max_chars: int = 16000,
    offset: int = 0,
    fingerprint: str | None = None,
) -> dict[str, Any]:
    """Read one bounded Knowledge document page through the strict plugin route."""
    try:
        document = _uuid_text(document_id, "document_id")
        view_name = _text(view, "view", 16)
        if view_name not in KNOWLEDGE_READ_VIEWS:
            raise ManageError("invalid_input", detail="view must be body or outline")
        page_size = _limit(max_chars, 1, MAX_DOC_PAGE_CHARS, "max_chars")
        page_offset = _nonnegative_int(offset, "offset")
        if page_offset > 0 and fingerprint is None:
            raise ManageError("invalid_input", detail="fingerprint is required when offset is nonzero")
        params: dict[str, Any] = {
            "view": view_name, "max_chars": page_size, "offset": page_offset,
        }
        if fingerprint is not None:
            value = _text(fingerprint, "fingerprint", 64)
            if not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ManageError("invalid_input", detail="fingerprint must be a lowercase SHA-256 value")
            params["fingerprint"] = value
        async def read(client: ManageClient):
            _, data, headers = await client.call(
                "GET", f"/api/knowledge/plugin/documents/{document}", params=params,
            )
            result = _with_page_header(data, headers)
            result["ok"] = True
            return result
        return await _session(read, knowledge=True)
    except ManageError as exc:
        return _page_error(exc)


TOOLS = [
    aoitalk_catalog,
    aoitalk_tasks_list,
    aoitalk_task_read,
    aoitalk_task_create,
    aoitalk_task_update,
    aoitalk_task_delete,
    aoitalk_task_restore,
    aoitalk_task_operation_status,
    aoitalk_docs_search,
    aoitalk_docs_read,
    aoitalk_docs_prepare_edit,
    aoitalk_docs_mutate,
    aoitalk_knowledge_search,
    aoitalk_knowledge_read,
]
READ_ONLY_TOOLS = {
    aoitalk_catalog, aoitalk_tasks_list, aoitalk_task_read,
    aoitalk_task_operation_status, aoitalk_docs_search, aoitalk_docs_read,
    aoitalk_knowledge_search, aoitalk_knowledge_read,
}
