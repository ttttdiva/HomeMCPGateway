"""Authenticated HTTP bridge to AoiTalk's generic Agent task API.

The Gateway only validates, forwards and bounds requests and responses. Task
execution and provider routing remain owned by AoiTalk.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import time
import unicodedata
import uuid
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field, StrictBool, StrictInt

from . import aoitalk, aoitalk_auth
from .aoitalk_task_diagnostics import project_diagnostics

TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PROVIDER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
AGENT_ID_RE = re.compile(r"^[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")
EFFORT_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
STATUS_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
MAX_REQUIRED_OUTPUTS = 20
MAX_REQUIRED_OUTPUT_LENGTH = 512
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_RESULT_PAGE_CHARS = 8000  # Even JSON-escaped controls fit the output byte cap.
DIGEST_RE = re.compile(r"^[a-f0-9]{64}$")
MAX_WAIT_SECONDS = 20.0
POLL_INTERVAL = 1.0
TERMINAL_STATES = {"completed", "succeeded", "failed", "cancelled", "stopped", "interrupted"}
SERVER_CODES = {
    "invalid_input", "invalid_workspace", "workspace_not_allowed", "idempotency_conflict",
    "not_found", "agent_not_configured", "agent_not_found", "agent_inactive",
    "agent_revision_missing", "agent_route_mismatch", "permission_denied",
    "required_capability_missing", "capability_not_granted",
    "cli_route_not_supported", "agent_route_unavailable", "tool_capability_unknown",
    "tool_calling_unsupported", "command_tool_unavailable", "agent_work_unavailable",
    "unknown_capability", "command_permission_required", "invalid_idempotency_key",
    "lease_lost", "timeout", "cancelled", "execution_binding_mismatch",
    "agent_binding_mismatch", "command_runtime_unavailable", "audit_write_failed",
}
MESSAGES = {
    "configuration_error": "AoiTalkの接続設定・認証情報をローカルの.envで確認してください。",
    "invalid_input": "引数を確認してください（goal / workspace / agent_id / task_id / 数値範囲）。",
    "invalid_workspace": "workspaceは存在する絶対パスのディレクトリを指定してください。",
    "workspace_not_allowed": "workspaceがAoiTalk側で許可されていません。",
    "idempotency_conflict": "同じidempotency_keyのタスクと内容が異なります。既存タスクを確認してください。",
    "authentication_failed": ".envの認証情報でAoiTalkにログインできません。",
    "password_reset_required": "AoiTalk側で初回パスワード変更を完了してください。",
    "permission_denied": "AoiTalk側の認証済み依頼者と対象Agentの権限を確認してください。権限の回避は行いません。",
    "not_found": "タスクまたはAPIが見つかりません。task_idとAoiTalkのバージョンを確認してください。",
    "conflict": "AoiTalkが競合を返しました。",
    "rate_limited": "AoiTalkがリクエストを制限しています。時間を置いて再試行してください。",
    "server_error": "AoiTalkのAPIがエラーを返しました。AoiTalk側のログを確認してください。",
    "connection_error": "AoiTalkへ接続できません。AoiTalkの起動状態と接続先を確認してください。",
    "invalid_response": "AoiTalkの応答を検証できません。",
    "result_not_ready": "taskは正常完了していません。statusを確認してください。",
    "result_unavailable": "taskに取得可能な最終回答がありません。",
    "result_changed": "最終回答が変更されました。先頭ページから取得し直してください。",
    "agent_not_configured": "AoiTalk側で対象Agentを確認してください。",
    "agent_not_found": "指定したAgentが見つかりません。",
    "agent_inactive": "指定したAgentは無効です。",
    "agent_revision_missing": "指定したAgentの公開revisionが見つかりません。",
    "agent_route_mismatch": "provider/model/effort expectationがAgentの公開revisionと一致しません。",
    "required_capability_missing": "要求したcapabilityがAgentの公開revisionで許可されていません。",
    "capability_not_granted": "要求したcapabilityが依頼者に許可されていません。",
    "cli_route_not_supported": "CLI providerのAgentはこのAPIから実行できません。API providerのAgentを選択してください。",
    "agent_route_unavailable": "Agentの公開revisionに利用可能なprovider/model routeがありません。公開設定を確認してください。",
    "tool_capability_unknown": "対象modelのtool calling対応を確認できません。AoiTalkのモデル登録を確認してください。",
    "tool_calling_unsupported": "対象modelはtool callingに対応していません。対応modelへ変更してください。",
    "command_tool_unavailable": "command_execute用toolがAoiTalkに登録されていません。tool登録を確認してください。",
    "agent_work_unavailable": "AoiTalkのAgentWork runtimeを利用できません。runtimeの稼働状態を確認してください。",
    "unknown_capability": "要求されたcapabilityはAoiTalkに登録されていません。capability名を確認してください。",
    "command_permission_required": "このtaskにはcommand実行権限が必要です。mutate modeとallow_commandsを指定してください。",
    "invalid_idempotency_key": "idempotency_keyの形式が不正です。使用可能な文字と長さを確認してください。",
    "lease_lost": "taskの実行状態を確認できません。最新statusを確認してから再依頼してください。",
    "timeout": "taskが実行時間制限に達しました。goalを分割して再依頼してください。",
    "cancelled": "taskはキャンセルされました。必要なら改めて依頼してください。",
    "execution_binding_mismatch": "Agent実行情報がtaskと一致しません。Agentの公開状態を確認してください。",
    "agent_binding_mismatch": "Agentの実行bindingが変更されました。Agent設定を確認して再依頼してください。",
    "command_runtime_unavailable": "隔離されたcommand runtimeを利用できません。AoiTalkのruntime状態を確認してください。",
    "audit_write_failed": "Agent実行の監査記録を保存できませんでした。AoiTalkのdatabase状態を確認してください。",
}
SECRET_KEY = re.compile(
    r"(?i)(?:password|passwd|cookie|authorization|api[_-]?key|private[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|base64|credential)"
)
SECRET_VALUE = re.compile(
    r"(?i)\b(?:session|token|password|api[_-]?key|private[_-]?key|secret)\s*[=:]\s*[^\s,;]+"
)


SERVER_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SERVER_DETAIL_LIMIT = 300


class AgentTaskError(Exception):
    def __init__(self, code: str, http_status: int | None = None, *,
                 server_code: str | None = None, server_detail: str | None = None,
                 retry_after: int | None = None, local_hold: int | None = None):
        super().__init__(MESSAGES.get(code, MESSAGES["server_error"]))
        self.retry_after = retry_after
        self.local_hold = local_hold
        self.code = code if code in MESSAGES else "server_error"
        self.http_status = http_status
        self.server_code = server_code
        self.server_detail = server_detail


def _error(exc: AgentTaskError, **state: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": exc.code, "message": MESSAGES[exc.code], **state}
    if exc.http_status is not None:
        result["http_status"] = exc.http_status
        # Distinguish a refusal the server named from one inferred from the HTTP status alone.
        result["error_source"] = "server_error_code" if exc.server_code else "http_status_only"
    if exc.retry_after is not None:
        result["retry_after_seconds"] = exc.retry_after
    if exc.local_hold is not None:
        result["local_cooldown_seconds"] = exc.local_hold
    if exc.server_code:
        result["server_error_code"] = exc.server_code
    if exc.server_detail:
        result["server_detail"] = exc.server_detail
    return result


class AgentTaskClient(aoitalk.Client):
    """Use the shared authenticated transport with bounded, sanitized errors."""

    async def call(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                status = response.status_code
                response_headers = response.headers
                raw = bytearray()
                async for part in response.aiter_bytes():
                    raw.extend(part)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise AgentTaskError("invalid_response", status)
        except AgentTaskError:
            raise
        except (httpx.HTTPError, OSError):
            raise AgentTaskError("connection_error") from None

        if 300 <= status < 400:
            raise AgentTaskError("configuration_error", status)
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            data = None
        if status in {200, 202}:
            if not isinstance(data, dict):
                raise AgentTaskError("invalid_response", status)
            return status, data

        code = {400: "invalid_input", 401: "authentication_failed", 403: "permission_denied",
                404: "not_found", 409: "conflict", 422: "invalid_input", 429: "rate_limited"}.get(
                    status, "server_error")
        server_code = server_detail = None
        if isinstance(data, dict):
            raw_code = data.get("error")
            if isinstance(raw_code, str) and SERVER_CODE_RE.fullmatch(raw_code):
                server_code = raw_code
                if raw_code in SERVER_CODES:
                    code = raw_code
            raw_detail = data.get("detail")
            # Only authorization refusals carry fixed server-authored text; other
            # statuses may wrap provider exceptions, so their detail stays hidden.
            if status == 403 and isinstance(raw_detail, str):
                clean = SECRET_VALUE.sub("[redacted]", raw_detail)
                server_detail = aoitalk._safe_text(clean, self.settings, SERVER_DETAIL_LIMIT) or None
        retry_after = aoitalk.parse_retry_after(response_headers.get("retry-after")) if status == 429 else None
        raise AgentTaskError(code, status, server_code=server_code, server_detail=server_detail,
                             retry_after=retry_after)

    async def login_task(self) -> None:
        try:
            await self.login()
        except aoitalk.ClipError as exc:
            raise _task_error(exc) from None


def _task_error(exc: aoitalk.ClipError) -> AgentTaskError:
    return AgentTaskError(exc.code if exc.code in MESSAGES else "server_error", exc.http_status,
                          retry_after=exc.retry_after, local_hold=exc.local_hold)


async def _session(fn, *, sanitize: bool = True):
    try:
        settings = aoitalk.load_settings()
    except aoitalk.ClipError:
        raise AgentTaskError("configuration_error") from None
    async with aoitalk_auth.client_for(AgentTaskClient, settings, lambda c: c.login_task(), _task_error,
                                       lambda e: isinstance(e, AgentTaskError) and e.http_status == 401) as client:
        result = await fn(client)
        return _sanitize(result, settings) if sanitize else result


def _number(value: Any, low: float, high: float, name: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not low <= value <= high):
        raise AgentTaskError("invalid_input")
    return float(value)


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        raise AgentTaskError("invalid_input")
    return value


def _key(value: Any) -> str:
    if not isinstance(value, str) or not KEY_RE.fullmatch(value):
        raise AgentTaskError("invalid_input")
    return value


def _credential_overlap(value: str, settings: aoitalk.Settings) -> bool:
    return any(secret and secret in value for secret in (settings.username, settings.password))


def _agent_id(value: Any) -> str:
    if not isinstance(value, str) or not AGENT_ID_RE.fullmatch(value):
        raise AgentTaskError("invalid_input")
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise AgentTaskError("invalid_input") from None


def _text(value: Any, name: str, limit: int, *, allow_line_controls: bool = False) -> str:
    allowed_controls = {"\r", "\n", "\t"} if allow_line_controls else set()
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(unicodedata.category(c) == "Cc" and c not in allowed_controls for c in value)):
        raise AgentTaskError("invalid_input")
    return value


def _text_list(value: Any, name: str, *, max_items: int, max_length: int,
               pattern: re.Pattern[str] | None = None,
               allow_line_controls: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > max_items:
        raise AgentTaskError("invalid_input")
    result = [_text(item, name, max_length, allow_line_controls=allow_line_controls) for item in value]
    if pattern and any(not pattern.fullmatch(item) for item in result):
        raise AgentTaskError("invalid_input")
    return result


def _required_outputs(value: Any, name: str = "required_outputs") -> list[str]:
    """Validate workspace-relative paths; omitted and empty both mean none."""
    outputs = _text_list(value, name, max_items=MAX_REQUIRED_OUTPUTS,
                         max_length=MAX_REQUIRED_OUTPUT_LENGTH)
    for item in outputs:
        parts = item.replace("\\", "/").split("/")
        if (item != item.strip() or item.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", item)
                or any(part in {"", ".", ".."} for part in parts)):
            raise AgentTaskError("invalid_input")
    if len(set(outputs)) != len(outputs):
        raise AgentTaskError("invalid_input")
    return outputs


def _status(task: Any, *, expected_id: str | None = None) -> dict[str, Any]:
    if not isinstance(task, dict):
        raise AgentTaskError("invalid_response")
    task_id = task.get("task_id")
    status = task.get("status")
    if not isinstance(task_id, str) or not TASK_ID_RE.fullmatch(task_id):
        raise AgentTaskError("invalid_response")
    if expected_id is not None and task_id != expected_id:
        raise AgentTaskError("invalid_response")
    if not isinstance(status, str) or not STATUS_RE.fullmatch(status):
        raise AgentTaskError("invalid_response")
    result = {"task_id": task_id, "status": status}
    # Put bounded diagnostics ahead of potentially large progress/result fields
    # so they survive the shared output budget.
    result.update(project_diagnostics(task))
    for key in ("work_item_id", "agent_run_id", "agent_id", "agent_revision_id"):
        value = task.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > 128 or any(ord(c) < 32 for c in value):
                raise AgentTaskError("invalid_response")
            result[key] = value
    for key in ("cancellation_requested", "is_terminal"):
        value = task.get(key)
        if value is not None:
            if not isinstance(value, bool):
                raise AgentTaskError("invalid_response")
            result[key] = value
    for key in ("created_at", "updated_at", "started_at", "finished_at", "current_step",
                "summary_text", "failure_reason", "failure_detail", "progress", "result"):
        if key in task:
            if key in {"created_at", "updated_at", "started_at", "finished_at", "current_step",
                       "summary_text", "failure_reason", "failure_detail"} and not isinstance(task[key], str):
                raise AgentTaskError("invalid_response")
            result[key] = task[key]
    return result


def _redact_result_text(value: str, settings: aoitalk.Settings) -> str:
    # Apply the existing secret patterns to the WHOLE string, never a slice.
    clean = aoitalk._redact_text(SECRET_VALUE.sub("[redacted]", value), settings)
    return "".join(c for c in clean if c >= " " or c in "\r\n\t")


def _sanitize(value: Any, settings: aoitalk.Settings) -> dict[str, Any]:
    # Reserve space for the top-level ``output_truncated`` marker and JSON
    # separators; each value/key below is charged by its encoded JSON bytes.
    remaining = MAX_OUTPUT_BYTES - 256
    truncated = False
    omitted = object()

    def encoded_size(item: Any) -> int:
        return len(json.dumps(item, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

    def visit(item: Any, depth: int = 0, *, result_text: bool = False) -> Any:
        nonlocal remaining, truncated
        if remaining <= 0:
            truncated = True
            return omitted
        if depth > 8:
            truncated = True
            return "[truncated]"
        if isinstance(item, str):
            clean = _redact_result_text(item, settings)
            if not result_text:
                # Preserve short UI/error display behavior, including control removal.
                clean = "".join(c for c in clean if c >= " ")
            if len(clean) > 4000:
                clean = clean[:4000]
                truncated = True
            if encoded_size(clean) > remaining:
                low, high = 0, len(clean)
                while low < high:
                    middle = (low + high + 1) // 2
                    if encoded_size(clean[:middle]) <= remaining:
                        low = middle
                    else:
                        high = middle - 1
                clean = clean[:low]
                truncated = True
            remaining -= encoded_size(clean)
            return clean
        if item is None or isinstance(item, (bool, int, float)):
            safe_item = item if not isinstance(item, float) or math.isfinite(item) else None
            cost = encoded_size(safe_item)
            if cost > remaining:
                truncated = True
                return omitted
            remaining -= cost
            return safe_item
        if isinstance(item, list):
            remaining -= 2  # []
            output = []
            for child in item[:200]:
                if output:
                    remaining -= 1  # comma
                if remaining <= 0:
                    truncated = True
                    break
                safe = visit(child, depth + 1, result_text=result_text)
                if safe is not omitted:
                    output.append(safe)
            if len(item) > len(output):
                truncated = True
            return output
        if isinstance(item, dict):
            remaining -= 2  # {}
            output = {}
            for key, child in item.items():
                if not isinstance(key, str) or SECRET_KEY.search(key):
                    truncated = True
                    continue
                if key == "idempotency_key" and isinstance(child, str) and _credential_overlap(child, settings):
                    truncated = True
                    continue
                if output:
                    remaining -= 1  # comma
                clean_key = key[:100]
                if clean_key != key:
                    truncated = True
                remaining -= encoded_size(clean_key) + 1  # key plus colon
                if remaining <= 0:
                    truncated = True
                    break
                safe = visit(child, depth + 1, result_text=result_text or (depth == 0 and key == "result"))
                if safe is not omitted:
                    output[clean_key] = safe
            return output
        truncated = True
        return omitted

    safe = visit(value)
    if not isinstance(safe, dict):
        raise AgentTaskError("invalid_response")
    if truncated:
        safe["output_truncated"] = True
    if encoded_size(safe) > MAX_OUTPUT_BYTES:
        raise AgentTaskError("invalid_response")
    return safe


def _response_task(data: dict[str, Any], *, expected_id: str | None = None) -> dict[str, Any]:
    return _status(data.get("task"), expected_id=expected_id)


async def aoitalk_agent_task_start(
    goal: Annotated[str, Field(min_length=1, max_length=8000)],
    workspace: Annotated[str, Field(min_length=1, max_length=1024)],
    agent_id: Annotated[str, Field(pattern=r"^[A-Fa-f0-9]{8}(?:-[A-Fa-f0-9]{4}){3}-[A-Fa-f0-9]{12}$")],
    idempotency_key: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")],
    constraints: Annotated[list[Annotated[str, Field(max_length=1000)]] | None, Field(max_length=20)] = None,
    completion_criteria: Annotated[list[Annotated[str, Field(max_length=1000)]] | None, Field(max_length=20)] = None,
    mode: Literal["read_only", "mutate"] = "read_only",
    allow_commands: bool = False,
    allow_git_commit: StrictBool = False,
    allow_git_push: StrictBool = False,
    provider: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")] = None,
    model: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")] = None,
    effort: Annotated[str | None, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,39}$")] = None,
    required_capabilities: Annotated[list[Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")]] | None,
                                    Field(max_length=20)] = None,
    required_outputs: Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]] | None,
                                Field(max_length=20)] = None,
    edit_targets: Annotated[list[Annotated[str, Field(min_length=1, max_length=512)]] | None,
                            Field(max_length=20)] = None,
    requires_command_verification: StrictBool | None = None,
    execution_profile_id: Annotated[str | None, Field(min_length=1, max_length=64)] = None,
    tool_generation_max_tokens: Annotated[StrictInt | None, Field(ge=1, le=4096)] = None,
    timeout_sec: Annotated[StrictInt | None, Field(ge=1, le=3600)] = None,
    command_timeout_sec: Annotated[StrictInt | None, Field(ge=1, le=900)] = None,
) -> dict[str, Any]:
    """Submit a generic AoiTalk Agent task and return its task_id immediately.

    AoiTalk owns task execution and the provider route. provider/model/effort,
    when supplied, are expectations checked by AoiTalk against the published
    Agent revision; they are never endpoint, credential or route overrides.
    Git commit and push each require their explicit flag together with
    mode="mutate" and allow_commands=true; they do not grant command authority.
    Reuse the same idempotency_key after an uncertain response. A different key
    is a new submission. Poll aoitalk_agent_task_status for progress and for
    confirmation that a cancellation has stopped the task.
    required_outputs optionally lists up to 20 workspace-relative paths the
    task must produce; omitted or empty sends nothing. AoiTalk must support
    the field before it is used. edit_targets optionally lists up to 20
    workspace-relative FILES the task may write: omitted keeps AoiTalk's
    inference, an explicit empty list means no file writes, and a non-empty
    list is the caller's request that AoiTalk still narrows by grants and
    protected paths. It never widens authority; AoiTalk validates the paths.
    requires_command_verification optionally states the caller's structured
    intent: omitted/None sends nothing and AoiTalk infers it from the goal and
    completion criteria; an explicit true or false is forwarded exactly and
    overrides that inference. true means a successful command run must follow
    the last file edit, so true with allow_commands=false is contradictory and
    AoiTalk rejects it (422). false declares a genuinely static/file-based
    verification task; it is not a claim that tests were run.
    execution_profile_id (1-64 chars) selects an execution profile only; it
    does not change the Agent role or permissions, and provider/model/effort
    remain route assertions. tool_generation_max_tokens (1-4096),
    timeout_sec (1-3600) and command_timeout_sec (1-900) are strict integers;
    omitted/None sends nothing and explicit values are forwarded exactly.
    """
    key = ""
    state: dict[str, Any] = {}
    post_attempted = False
    key_echo_safe = True
    try:
        key = _key(idempotency_key)
        goal = _text(goal, "goal", 8000, allow_line_controls=True)
        workspace = _text(workspace, "workspace", 1024)
        agent_id = _agent_id(agent_id)
        if (mode not in {"read_only", "mutate"} or not isinstance(allow_commands, bool)
                or not isinstance(allow_git_commit, bool) or not isinstance(allow_git_push, bool)
                or (mode == "read_only" and allow_commands)):
            raise AgentTaskError("invalid_input")
        if (allow_git_commit or allow_git_push) and (mode != "mutate" or not allow_commands):
            raise AgentTaskError("invalid_input")
        constraints = _text_list(constraints, "constraints", max_items=20, max_length=1000,
                                 allow_line_controls=True)
        completion_criteria = _text_list(completion_criteria, "completion_criteria", max_items=20,
                                         max_length=1000, allow_line_controls=True)
        required_capabilities_supplied = required_capabilities is not None
        required_capabilities = _text_list(required_capabilities, "required_capabilities", max_items=20,
                                           max_length=64, pattern=CAPABILITY_RE)
        if len(set(required_capabilities)) != len(required_capabilities):
            raise AgentTaskError("invalid_input")
        required_outputs = _required_outputs(required_outputs)
        if requires_command_verification is not None and not isinstance(requires_command_verification, bool):
            raise AgentTaskError("invalid_input")
        if edit_targets is not None:
            edit_targets = _required_outputs(edit_targets, "edit_targets")
        if execution_profile_id is not None:
            if (not isinstance(execution_profile_id, str) or not 1 <= len(execution_profile_id) <= 64
                    or execution_profile_id != _text(execution_profile_id, "execution_profile_id", 64)):
                raise AgentTaskError("invalid_input")
        for value, upper in ((tool_generation_max_tokens, 4096), (timeout_sec, 3600),
                             (command_timeout_sec, 900)):
            if value is not None and (type(value) is not int or not 1 <= value <= upper):
                raise AgentTaskError("invalid_input")
        body: dict[str, Any] = {
            "goal": goal, "workspace": workspace, "constraints": constraints,
            "completion_criteria": completion_criteria, "mode": mode,
            "allow_commands": allow_commands, "agent_id": agent_id,
        }
        for name, value in (("execution_profile_id", execution_profile_id),
                            ("tool_generation_max_tokens", tool_generation_max_tokens),
                            ("timeout_sec", timeout_sec), ("command_timeout_sec", command_timeout_sec)):
            if value is not None:
                body[name] = value
        if allow_git_commit:
            body["allow_git_commit"] = True
        if allow_git_push:
            body["allow_git_push"] = True
        for name, value, pattern, max_length in (
            ("provider", provider, PROVIDER_RE, 80),
            ("model", model, MODEL_RE, 200),
            ("effort", effort, EFFORT_RE, 40),
        ):
            if value is not None:
                value = _text(value, name, max_length)
                if not pattern.fullmatch(value):
                    raise AgentTaskError("invalid_input")
                body[name] = value
        if required_capabilities_supplied:
            body["required_capabilities"] = required_capabilities
        if required_outputs:
            body["required_outputs"] = required_outputs
        if edit_targets is not None:
            # An explicit [] is meaningful (no file writes) and must be sent.
            body["edit_targets"] = edit_targets
        if requires_command_verification is not None:
            # An explicit false is meaningful (static task) and must be sent.
            body["requires_command_verification"] = requires_command_verification

        async def submit(client: AgentTaskClient) -> dict[str, Any]:
            nonlocal key_echo_safe, post_attempted
            key_echo_safe = not _credential_overlap(key, client.settings)
            if key_echo_safe:
                state["idempotency_key"] = key
            else:
                state.pop("idempotency_key", None)
            post_attempted = True
            http_status, data = await client.call(
                "POST", "/api/agent-tasks", json=body, headers={"Idempotency-Key": key}
            )
            task = _response_task(data)
            created = data.get("created")
            if not isinstance(created, bool):
                raise AgentTaskError("invalid_response", http_status)
            next_step = "poll aoitalk_agent_task_status(task_id); do not start a duplicate task"
            if not key_echo_safe:
                next_step += "; if retrying, reuse the exact idempotency_key input from this call"
            result = {"ok": True, **task, "created": created, "next_step": next_step}
            if key_echo_safe:
                result["idempotency_key"] = key
            return result

        return await _session(submit)
    except AgentTaskError as exc:
        uncertain = post_attempted and exc.code in {"connection_error", "invalid_response", "server_error"}
        if uncertain:
            retry_step = ("retry aoitalk_agent_task_start with the same idempotency_key returned in this reply; do not change it"
                          if key_echo_safe else
                          "retry aoitalk_agent_task_start with exactly the idempotency_key value supplied to the original call; do not change or omit it")
        else:
            retry_step = "resolve_error"
        return _error(exc, **state, submission_uncertain=uncertain,
                      next_step=retry_step)


async def aoitalk_agent_task_status(
    task_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
    wait_seconds: Annotated[float, Field(ge=0, le=20)] = 0,
) -> dict[str, Any]:
    """Read task status with an optional wait (0-20 seconds).

    Result previews preserve formatting but strings are capped at 4,000 chars.
    Use aoitalk_agent_task_result for a complete successful final answer when
    output_truncated is true.
    """
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id
        wait_seconds = _number(wait_seconds, 0, MAX_WAIT_SECONDS, "wait_seconds")

        async def read(client: AgentTaskClient) -> dict[str, Any]:
            deadline = time.monotonic() + wait_seconds
            while True:
                _, data = await client.call("GET", f"/api/agent-tasks/{task_id}")
                task = _response_task(data, expected_id=task_id)
                if (task["status"] in TERMINAL_STATES or task.get("is_terminal") is True
                        or time.monotonic() >= deadline):
                    return {"ok": True, **task}
                await asyncio.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.monotonic())))

        return await _session(read)
    except AgentTaskError as exc:
        return _error(exc, **state)


async def aoitalk_agent_task_tail(
    task_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
    max_bytes: Annotated[int, Field(ge=1024, le=64000)] = 12000,
) -> dict[str, Any]:
    """Read a bounded tail of one Agent task (1-200 events and 1-64000 bytes)."""
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id
        limit = int(_number(limit, 1, 200, "limit"))
        max_bytes = int(_number(max_bytes, 1024, 64000, "max_bytes"))

        async def read(client: AgentTaskClient) -> dict[str, Any]:
            _, data = await client.call(
                "GET", f"/api/agent-tasks/{task_id}/tail",
                params={"limit": limit, "max_bytes": max_bytes},
            )
            status = data.get("status")
            events = data.get("events")
            if (data.get("task_id") != task_id or not isinstance(status, str)
                    or not STATUS_RE.fullmatch(status) or not isinstance(events, list)):
                raise AgentTaskError("invalid_response")
            event_count = data.get("event_count")
            if event_count is not None and (isinstance(event_count, bool) or not isinstance(event_count, int)
                                            or event_count < 0):
                raise AgentTaskError("invalid_response")
            return {"ok": True, "task_id": task_id, "status": status,
                    "events": [event for event in events[:limit] if isinstance(event, dict)],
                    "truncated": bool(data.get("truncated")), "event_count": event_count}

        return await _session(read)
    except AgentTaskError as exc:
        return _error(exc, **state)


async def aoitalk_agent_task_result(
    task_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
    offset: Annotated[int, Field(strict=True, ge=0, le=MAX_RESPONSE_BYTES)] = 0,
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_RESULT_PAGE_CHARS)] = 4000,
    expected_digest: Annotated[str | None, Field(pattern=r"^[a-f0-9]{64}$")] = None,
) -> dict[str, Any]:
    """Read only a successful task's final answer, preserving CRLF, tabs and Unicode.

    Offsets/limits count Python Unicode characters in the fully redacted text.
    Start at offset=0, then pass next_offset and content_sha256 as expected_digest
    until next_offset is null. Verify the reconstructed UTF-8 SHA-256 digest.
    Continuations require the digest; a changed result must be read from zero.
    Only result.response (or legacy result.final_response) is eligible. No logs,
    progress, failure answers, tool output or reasoning fields are returned.
    Each call re-reads the authenticated task; no local task/result cache is used.
    """
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id
        if (isinstance(offset, bool) or not isinstance(offset, int)
                or not 0 <= offset <= MAX_RESPONSE_BYTES
                or isinstance(limit, bool) or not isinstance(limit, int)
                or not 1 <= limit <= MAX_RESULT_PAGE_CHARS
                or (expected_digest is not None and (
                    not isinstance(expected_digest, str) or not DIGEST_RE.fullmatch(expected_digest)))
                or (offset > 0 and expected_digest is None)):
            raise AgentTaskError("invalid_input")

        async def read(client: AgentTaskClient) -> dict[str, Any]:
            _, data = await client.call("GET", f"/api/agent-tasks/{task_id}")
            task = _response_task(data, expected_id=task_id)
            if task["status"] not in {"completed", "succeeded"}:
                raise AgentTaskError("result_not_ready")
            if task.get("is_terminal") is False:
                raise AgentTaskError("invalid_response")
            result = task.get("result")
            if result is None:
                raise AgentTaskError("result_unavailable")
            if not isinstance(result, dict):
                raise AgentTaskError("invalid_response")
            field = "response" if "response" in result else "final_response"
            if field not in result:
                raise AgentTaskError("result_unavailable")
            if not isinstance(result[field], str):
                raise AgentTaskError("invalid_response")
            clean = _redact_result_text(result[field], client.settings)
            try:
                content_bytes = clean.encode("utf-8")
            except UnicodeError:
                raise AgentTaskError("invalid_response") from None
            if len(content_bytes) > MAX_RESPONSE_BYTES:
                raise AgentTaskError("invalid_response")
            digest = hashlib.sha256(content_bytes).hexdigest()
            if expected_digest is not None and expected_digest != digest:
                raise AgentTaskError("result_changed")
            if offset > len(clean):
                raise AgentTaskError("invalid_input")
            end = min(offset + limit, len(clean))
            page = {
                "ok": True, "task_id": task_id, "status": task["status"],
                "content": clean[offset:end], "content_sha256": digest,
                "total_chars": len(clean), "total_bytes": len(content_bytes),
                "offset": offset, "next_offset": end if end < len(clean) else None,
                "output_truncated": offset > 0 or end < len(clean),
            }
            if len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > MAX_OUTPUT_BYTES:
                raise AgentTaskError("invalid_response")
            return page

        # The allowlisted envelope is bounded above and its one content string
        # is already fully redacted. Re-sanitizing individual pages could change
        # their text/digest or miss a secret spanning page boundaries.
        return await _session(read, sanitize=False)
    except AgentTaskError as exc:
        return _error(exc, **state)


async def aoitalk_agent_task_cancel(
    task_id: Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")],
) -> dict[str, Any]:
    """Request cancellation; poll status to learn when the task has actually stopped."""
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id

        async def cancel(client: AgentTaskClient) -> dict[str, Any]:
            _, data = await client.call("POST", f"/api/agent-tasks/{task_id}/cancel")
            task = _response_task(data, expected_id=task_id)
            requested = data.get("cancellation_requested")
            if not isinstance(requested, bool):
                raise AgentTaskError("invalid_response")
            return {"ok": True, **task, "cancellation_requested": requested,
                    "next_step": "poll aoitalk_agent_task_status; cancellation is only a request until status is terminal"}

        return await _session(cancel)
    except AgentTaskError as exc:
        return _error(exc, **state)


TOOLS = [aoitalk_agent_task_start, aoitalk_agent_task_status,
         aoitalk_agent_task_tail, aoitalk_agent_task_result, aoitalk_agent_task_cancel]
READ_ONLY_TOOLS = {aoitalk_agent_task_status, aoitalk_agent_task_tail, aoitalk_agent_task_result}
