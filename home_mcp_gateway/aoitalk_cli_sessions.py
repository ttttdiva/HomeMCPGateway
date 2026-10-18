"""Thin authenticated bridge for AoiTalk's durable CLI sessions.

The session owner, native provider handle, generation worker, event ledger and
reconnect policy remain in AoiTalk.  This module only validates MCP arguments,
performs one authenticated HTTP request, and returns a bounded public
projection.  It deliberately does not start a CLI process or an LLM loop.
"""
from __future__ import annotations

import json
import math
import re
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from . import aoitalk


SESSION_ID_PATTERN = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
SESSION_ID_RE = re.compile(SESSION_ID_PATTERN)
CURSOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._~:/+=-]{0,511}$")
PROVIDERS = {"codex-cli", "claude-cli", "grok-cli", "antigravity-cli"}
PERMISSION_MODES = {"native_default", "standard", "accept_edits"}
PRIVATE_HANDLE_KEYS = frozenset({
    "native_handle",
    "native_session_id",
    "session_handle",
    "continuation_handle",
    "provider_session_id",
    "conversation_id",
})
SESSION_STATUSES = {
    "ready", "queued", "running", "completed", "failed", "waiting_input",
    "waiting_approval", "disconnected", "interrupted", "cancelling", "cancelled",
}
MAX_MESSAGE_CHARS = 100_000
MAX_OUTPUT_BYTES = 64_000
SessionId = Annotated[str, Field(pattern=SESSION_ID_PATTERN)]

SAFE_ERROR_CODES = {
    "agent_inactive",
    "agent_not_configured",
    "agent_not_found",
    "agent_revision_missing",
    "agent_route_mismatch",
    "agent_scope_invalid",
    "agent_selection_denied",
    "agent_authority_unavailable",
    "agent_authority_denied",
    "agent_binding_denied",
    "agent_binding_required",
    "agent_binding_proof_missing",
    "agent_route_backend_mismatch",
    "agent_route_unavailable",
    "authority_revoked",
    "canonical_runtime_unavailable",
    "authentication_failed",
    "configuration_error",
    "conflict",
    "cursor_invalid",
    "idempotency_conflict",
    "invalid_input",
    "native_session_unavailable",
    "native_session_disconnected",
    "not_found",
    "permission_denied",
    "permission_mode_denied",
    "permission_mode_invalid",
    "permission_mode_unsupported",
    "provider_unavailable",
    "rate_limited",
    "reconnect_not_allowed",
    "reconnect_required",
    "server_error",
    "session_busy",
    "session_not_active",
    "session_not_found",
    "cancelled",
    "connection_error",
    "command_runtime_unavailable",
    "cli_development_session_not_found",
    "cli_development_session_forbidden",
    "cli_development_session_conflict",
    "cli_development_session_error",
    "cli_development_session_invalid",
    "cli_development_session_unavailable",
    "cli_development_sessions_disabled",
    "cli_session_context_invalid",
    "http_error",
    "invalid_event_cursor",
    "invalid_identity",
    "invalid_workspace",
    "invalid_working_directory",
    "invalid_session_id",
    "message_required",
    "message_too_long",
    "provider_cache_root_invalid",
    "provider_cache_root_missing",
    "provider_cache_root_reparse_denied",
    "provider_state_root_changed",
    "provider_not_supported",
    "run_not_available",
    "service_error",
    "service_unavailable",
    "send_idempotency_conflict",
    "send_idempotency_unavailable",
    "session_cancelled",
    "session_completed",
    "session_disconnected",
    "session_failed",
    "session_idempotency_conflict",
    "session_interrupted",
    "session_owner_mismatch",
    "trusted_windows_agent_denied",
    "trusted_windows_disabled",
    "trusted_windows_owner_denied",
    "trusted_windows_owner_policy_missing",
    "trusted_windows_roots_unavailable",
    "trusted_windows_unavailable",
    "work_binding_missing",
    "work_coordinator_unavailable",
    "workspace_not_allowed",
    "working_directory_absolute_required",
    "working_directory_invalid",
    "working_directory_outside_allowed_root",
    "working_directory_reparse_denied",
    "working_directory_unavailable",
}

MESSAGES = {
    "agent_inactive": "指定したAgentは無効です。",
    "agent_not_configured": "委譲先Agentが設定されていません。",
    "agent_not_found": "指定したAgentが見つかりません。",
    "agent_revision_missing": "指定したAgentの公開revisionが見つかりません。",
    "agent_route_mismatch": "provider/model/effortがAgentの正準routeと一致しません。",
    "agent_scope_invalid": "Agentの実行scopeを検証できません。",
    "agent_selection_denied": "Agentの選択権限を確認してください。",
    "agent_authority_unavailable": "Agent権限を確認できません。",
    "agent_authority_denied": "Agent authorityが拒否しました。",
    "agent_binding_denied": "Agent bindingを作成できません。",
    "agent_binding_required": "Agent bindingがありません。",
    "agent_binding_proof_missing": "Agent bindingの証明が不足しています。",
    "agent_route_backend_mismatch": "Agent routeのbackendがCLI providerと一致しません。",
    "agent_route_unavailable": "Agent routeを利用できません。",
    "authority_revoked": "Agentまたは依頼者の権限が失効しています。",
    "canonical_runtime_unavailable": "共通Agent実行経路を利用できません。別の実行系へ切り替えません。",
    "authentication_failed": ".envの認証情報でAoiTalkにログインできません。",
    "configuration_error": "AoiTalkの接続設定・認証情報をローカルの.envで確認してください。",
    "conflict": "AoiTalkが競合を返しました。現在のセッション状態を確認してください。",
    "cursor_invalid": "イベントcursorが無効です。現在のcursorを再取得してください。",
    "idempotency_conflict": "同じidempotency_keyの依頼内容が一致しません。",
    "invalid_input": "引数を確認してください（session_id / provider / message / cursor）。",
    "native_session_unavailable": "CLI providerの継続セッションを利用できません。",
    "native_session_disconnected": "CLI providerの継続セッションが切断されています。",
    "not_found": "CLIセッションまたはAPIが見つかりません。",
    "permission_denied": "AoiTalk側の認証済み依頼者とセッション権限を確認してください。",
    "permission_mode_denied": "permission modeがdeployment allow-list外です。",
    "permission_mode_invalid": "permission modeが無効です。",
    "permission_mode_unsupported": "permission modeがproviderで対応していません。",
    "provider_unavailable": "選択したCLI providerを利用できません。",
    "rate_limited": "AoiTalkがリクエストを制限しています。時間を置いて再試行してください。",
    "reconnect_not_allowed": "現在のセッション状態ではreconnectできません。",
    "reconnect_required": "既存のCLI継続を再接続する必要があります。",
    "server_error": "AoiTalkのAPIがエラーを返しました。AoiTalk側のログを確認してください。",
    "session_busy": "CLIセッションは別の生成で使用中です。状態とeventsを確認してください。",
    "session_not_active": "CLIセッションはactiveではありません。statusを確認してください。",
    "session_not_found": "指定したCLIセッションが見つかりません。",
    "cancelled": "CLIセッションの取消要求を受け付けました。終端状態を確認してください。",
    "connection_error": "AoiTalkへ接続できません。AoiTalkの起動状態と接続先を確認してください。",
    "command_runtime_unavailable": "隔離実行環境の準備ができていません。別の実行系へ切り替えません。",
    "cli_development_session_not_found": "CLIセッションが見つかりません。",
    "cli_development_session_forbidden": "CLIセッションへの権限がありません。",
    "cli_development_session_conflict": "CLIセッションの状態またはbindingが競合しています。",
    "cli_development_session_error": "CLI development sessionがエラーを返しました。",
    "cli_development_session_invalid": "CLIセッションの入力が無効です。",
    "cli_development_session_unavailable": "CLIセッションを利用できません。",
    "cli_development_sessions_disabled": "CLI development sessionsが無効です。",
    "cli_session_context_invalid": "CLIセッションの永続contextが無効です。",
    "http_error": "認証済みAPIがリクエストを拒否しました。",
    "invalid_event_cursor": "イベントcursorが無効です。",
    "invalid_identity": "session/run identityが無効です。",
    "invalid_workspace": "working_directoryが許可されたworkspaceではありません。",
    "invalid_working_directory": "working_directoryが無効です。",
    "invalid_session_id": "session_idが無効です。",
    "message_required": "messageは必須です。",
    "message_too_long": "messageが長すぎます。",
    "provider_cache_root_invalid": "provider cache rootが信頼できません。",
    "provider_cache_root_missing": "provider cache rootが設定されていません。",
    "provider_cache_root_reparse_denied": "provider cache rootのreparse pathは許可されません。",
    "provider_state_root_changed": "provider state rootが開始時の値から変化しました。",
    "provider_not_supported": "CLI providerが対応していません。",
    "run_not_available": "取消対象のactive runがありません。",
    "service_error": "CLI session serviceがエラーを返しました。",
    "service_unavailable": "CLI session serviceを利用できません。",
    "send_idempotency_conflict": "同じsend keyのmessage内容が一致しません。",
    "send_idempotency_unavailable": "send keyの結果を照合できません。",
    "session_cancelled": "CLIセッションは取消済みです。",
    "session_completed": "CLIセッションは完了済みです。",
    "session_disconnected": "CLIセッションは切断されています。",
    "session_failed": "CLIセッションは失敗状態です。",
    "session_idempotency_conflict": "同じsession keyの依頼内容が一致しません。",
    "session_interrupted": "CLIセッションは中断されています。",
    "session_owner_mismatch": "CLIセッションのownerが一致しません。",
    "trusted_windows_agent_denied": "Agentはtrusted Windows laneで許可されていません。",
    "trusted_windows_disabled": "trusted Windows laneが無効です。",
    "trusted_windows_owner_denied": "ownerはtrusted Windows laneで許可されていません。",
    "trusted_windows_owner_policy_missing": "trusted Windows owner allow-listがありません。",
    "trusted_windows_roots_unavailable": "trusted Windows rootを利用できません。",
    "trusted_windows_unavailable": "trusted Windows laneを利用できません。",
    "work_binding_missing": "CLI Work bindingがありません。",
    "work_coordinator_unavailable": "共有Work coordinatorを利用できません。",
    "workspace_not_allowed": "working_directoryがAoiTalkの許可root外です。",
    "working_directory_absolute_required": "working_directoryは絶対パスが必要です。",
    "working_directory_invalid": "working_directoryを解決できません。",
    "working_directory_outside_allowed_root": "working_directoryが許可root外です。",
    "working_directory_reparse_denied": "reparse pathは許可されません。",
    "working_directory_unavailable": "working_directoryを利用できません。",
}


class CliSessionError(Exception):
    def __init__(self, code: str, http_status: int | None = None, detail: str | None = None,
                 *, uncertain: bool = False):
        self.code = code if code in MESSAGES else "server_error"
        self.http_status = http_status
        self.detail = detail
        self.uncertain = uncertain
        super().__init__(MESSAGES[self.code])


def _error(exc: CliSessionError, **state: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "error": exc.code,
        "message": MESSAGES[exc.code],
        **state,
    }
    if exc.http_status is not None:
        result["http_status"] = exc.http_status
    if exc.detail:
        result["detail"] = exc.detail
    return result


def _bounded_text(value: Any, name: str, limit: int, *, allow_newlines: bool = False) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise CliSessionError("invalid_input", detail=f"{name} must be a nonempty string of at most {limit} chars")
    for char in value:
        if ord(char) < 32 and (not allow_newlines or char not in "\r\n\t"):
            raise CliSessionError("invalid_input", detail=f"{name} contains a control character")
    return value


def _session_id(value: Any) -> str:
    if not isinstance(value, str) or not SESSION_ID_RE.fullmatch(value):
        raise CliSessionError("invalid_input", detail="session_id is an opaque AoiTalk session id")
    return value


def _cursor(value: Any, *, optional: bool = True) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not CURSOR_RE.fullmatch(value):
        raise CliSessionError("invalid_input", detail="cursor is an opaque bounded value")
    return value


def _key(value: Any) -> str:
    return _bounded_text(value, "idempotency_key", 128, allow_newlines=False)


def _limit(value: Any, low: int, high: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CliSessionError("invalid_input", detail=f"{name} must be between {low} and {high}")
    return value


def _absolute_directory(value: Any) -> str:
    text = _bounded_text(value, "working_directory", 1024)
    if not (text.startswith(("/", "\\\\")) or re.match(r"^[A-Za-z]:[\\/]", text)):
        raise CliSessionError("invalid_input", detail="working_directory must be absolute")
    return text


class CliSessionClient(aoitalk.Client):
    """Authenticated HTTP client with bounded, code-only error bodies."""

    async def call(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                status = response.status_code
                raw = b""
                async for part in response.aiter_bytes():
                    raw += part
                    if len(raw) > aoitalk.MAX_RESPONSE_BYTES:
                        raise CliSessionError("server_error", detail="response exceeded the bounded limit")
        except CliSessionError:
            raise
        except (httpx.HTTPError, OSError):
            raise CliSessionError("server_error", uncertain=True) from None
        if 300 <= status < 400:
            raise CliSessionError("configuration_error", status)
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            raise CliSessionError("server_error", status, "AoiTalk returned invalid JSON", uncertain=True) from None
        if status in {200, 201, 202}:
            if not isinstance(data, dict):
                raise CliSessionError("server_error", status, "AoiTalk returned a non-object response", uncertain=True)
            return status, data

        code = {
            400: "invalid_input",
            401: "authentication_failed",
            403: "permission_denied",
            404: "not_found",
            409: "conflict",
            422: "invalid_input",
            429: "rate_limited",
        }.get(status, "server_error")
        detail = None
        if isinstance(data, dict):
            raw_code = data.get("error") or data.get("code")
            if isinstance(raw_code, str) and raw_code in SAFE_ERROR_CODES:
                code = raw_code
            elif isinstance(raw_code, str) and re.fullmatch(r"agent_authority_[A-Za-z0-9_.:-]{1,80}", raw_code):
                code = "permission_denied"
            raw_detail = data.get("detail")
            if isinstance(raw_detail, str):
                detail = aoitalk._safe_text(raw_detail, self.settings, 300)
            elif status == 422 and isinstance(raw_detail, list):
                fields = sorted({".".join(str(p) for p in item.get("loc", [])[1:])
                                 for item in raw_detail if isinstance(item, dict)})
                detail = "invalid fields: " + ", ".join(fields[:10])
        raise CliSessionError(code, status, detail)

    async def login_cli_session(self) -> None:
        try:
            await self.login()
        except aoitalk.ClipError as exc:
            raise CliSessionError(exc.code, exc.http_status) from None


async def _session(fn):
    try:
        settings = aoitalk.load_settings()
    except aoitalk.ClipError:
        raise CliSessionError("configuration_error") from None
    client = CliSessionClient(settings)
    try:
        await client.login_cli_session()
        return _sanitize(await fn(client), settings)
    finally:
        await client.http.aclose()


def _safe_key(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        return None
    if value.casefold() in PRIVATE_HANDLE_KEYS:
        return None
    if re.search(r"(?i)(?:password|passwd|cookie|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|lease|native_session|provider_args|environment)", value):
        return None
    return value


def _sanitize(value: Any, settings: aoitalk.Settings) -> Any:
    """Bound response data and remove credentials/provider continuation handles."""
    # Account in UTF-8 bytes because MCP JSON is transmitted as UTF-8.  A
    # character budget would allow a Japanese/emoji-heavy event page to exceed
    # the advertised wire bound.
    remaining = MAX_OUTPUT_BYTES
    truncated = False
    omitted = object()

    def safe_text(item: str) -> str:
        # Keep multiline assistant output readable while applying the same
        # credential-shaped redaction used by the existing AoiTalk adapter.
        text = item
        for secret in (settings.password, settings.username):
            if secret:
                text = text.replace(secret, "[redacted]")
        text = re.sub(r"(?i)\b(?:bearer\s+|aoitpat_|sk-)[^\s]+", "[redacted]", text)
        return "".join(char for char in text if char >= " " or char in "\r\n\t")

    def fit_utf8(text: str, budget: int) -> tuple[str, bool]:
        encoded = text.encode("utf-8")
        if len(encoded) <= budget:
            return text, False
        if budget <= 0:
            return "", True
        return encoded[:budget].decode("utf-8", errors="ignore"), True

    def reserve(amount: int) -> bool:
        nonlocal remaining, truncated
        if amount > remaining:
            truncated = True
            return False
        remaining -= amount
        return True

    def visit(item: Any, depth: int = 0) -> Any:
        nonlocal remaining, truncated
        if remaining <= 0 or depth > 8:
            truncated = True
            return omitted
        if isinstance(item, str):
            text = safe_text(item)
            # Keep one field bounded even when the whole response has room.
            field_clipped = len(text) > 12_000
            text, byte_clipped = fit_utf8(text[:12_000], remaining)
            if field_clipped or byte_clipped:
                truncated = True
            remaining -= len(text.encode("utf-8"))
            return text
        if item is None or isinstance(item, (bool, int)):
            if not reserve(16):
                return omitted
            return item
        if isinstance(item, float):
            if not reserve(16):
                return omitted
            return item if math.isfinite(item) else None
        if isinstance(item, list):
            result = []
            for child in item[:500]:
                if not reserve(2):
                    break
                clean = visit(child, depth + 1)
                if clean is omitted:
                    break
                result.append(clean)
            if len(result) < len(item):
                truncated = True
            return result
        if isinstance(item, dict):
            result = {}
            for raw_key, child in list(item.items())[:100]:
                key = _safe_key(raw_key)
                if key is None:
                    # Private/unknown fields are redacted metadata, not lost
                    # event text.  Do not block a cursor for this omission.
                    continue
                if not reserve(len(key.encode("utf-8")) + 4):
                    break
                clean = visit(child, depth + 1)
                if clean is omitted:
                    break
                result[key] = clean
            if len(item) > 100:
                truncated = True
            return result
        truncated = True
        return omitted

    result = visit(value)
    if result is omitted or not isinstance(result, dict):
        raise CliSessionError("server_error", detail="AoiTalk returned an invalid response")
    if truncated:
        # An event cursor may advance only when every event before that cursor
        # was delivered.  If output budgeting dropped any event/text, remove
        # all nested next_cursor values so callers can retry the same cursor.
        def block_cursor_advance(item: Any) -> Any:
            if isinstance(item, dict):
                if isinstance(item.get("events"), list) and "next_cursor" in item:
                    item.pop("next_cursor", None)
                    item["cursor_advance_blocked"] = True
                for child in item.values():
                    block_cursor_advance(child)
            elif isinstance(item, list):
                for child in item:
                    block_cursor_advance(child)
            return item

        result = block_cursor_advance(result)
        result["output_truncated"] = True
    return result


def _public_success(data: dict[str, Any], **extra: Any) -> dict[str, Any]:
    result = {"ok": True, **data, **extra}
    nested = result.get("session")
    if isinstance(nested, dict) and "session_id" not in result:
        candidate = nested.get("session_id") or nested.get("id")
        if isinstance(candidate, str):
            result["session_id"] = candidate
    if "session_id" not in result and isinstance(result.get("id"), str):
        result["session_id"] = result["id"]
    return result


def _uncertain(exc: CliSessionError) -> bool:
    return exc.uncertain or (
        exc.http_status is not None and 500 <= exc.http_status <= 599
    ) or (exc.code == "server_error" and exc.http_status is None)


async def aoitalk_cli_session_create(
    agent_id: Annotated[str, Field(min_length=36, max_length=36)],
    provider: Literal["codex-cli", "claude-cli", "grok-cli", "antigravity-cli"],
    model: Annotated[str, Field(min_length=1, max_length=160)],
    working_directory: Annotated[str, Field(min_length=1, max_length=1024)],
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
    effort: Annotated[str | None, Field(max_length=40)] = None,
    title: Annotated[str | None, Field(max_length=200)] = None,
    permission_mode: Literal["native_default", "standard", "accept_edits"] = "native_default",
) -> dict[str, Any]:
    """Create a durable AoiTalk CLI continuation session; never starts a local CLI here."""
    state = {"idempotency_key": idempotency_key}
    attempted = False
    try:
        _bounded_text(agent_id, "agent_id", 36)
        if not isinstance(agent_id, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", agent_id):
            raise CliSessionError("invalid_input", detail="agent_id must be a UUID")
        if provider not in PROVIDERS:
            raise CliSessionError("invalid_input", detail="provider is not supported")
        _bounded_text(model, "model", 160)
        workspace = _absolute_directory(working_directory)
        key = _key(idempotency_key)
        if permission_mode not in PERMISSION_MODES:
            raise CliSessionError("invalid_input", detail="permission_mode is not supported")
        body: dict[str, Any] = {"agent_id": agent_id, "provider": provider, "model": model,
                                "working_directory": workspace, "idempotency_key": key,
                                "permission_mode": permission_mode}
        if effort is not None:
            body["effort"] = _bounded_text(effort, "effort", 40)
        if title is not None:
            body["title"] = _bounded_text(title, "title", 200, allow_newlines=False)

        async def create(client: CliSessionClient) -> dict[str, Any]:
            nonlocal attempted
            attempted = True
            _, data = await client.call("POST", "/api/cli-sessions", json=body,
                                        headers={"Idempotency-Key": key})
            return _public_success(data, idempotency_key=key)

        return await _session(create)
    except CliSessionError as exc:
        return _error(exc, **state, submission_uncertain=attempted and _uncertain(exc),
                      next_step=("retry with the same idempotency_key; do not create a duplicate"
                                 if attempted and _uncertain(exc) else "resolve_error"))


async def aoitalk_cli_session_send(
    session_id: SessionId,
    message: Annotated[str, Field(min_length=1, max_length=MAX_MESSAGE_CHARS)],
    idempotency_key: Annotated[str, Field(min_length=1, max_length=128)],
) -> dict[str, Any]:
    """Submit one raw user message to a durable CLI session using its idempotency key."""
    state = {"session_id": session_id, "idempotency_key": idempotency_key}
    attempted = False
    try:
        sid = _session_id(session_id)
        raw = _bounded_text(message, "message", MAX_MESSAGE_CHARS, allow_newlines=True)
        key = _key(idempotency_key)

        async def send(client: CliSessionClient) -> dict[str, Any]:
            nonlocal attempted
            attempted = True
            _, data = await client.call("POST", f"/api/cli-sessions/{sid}/messages",
                                        json={"message": raw, "idempotency_key": key},
                                        headers={"Idempotency-Key": key})
            return _public_success(data, session_id=sid, idempotency_key=key)

        return await _session(send)
    except CliSessionError as exc:
        return _error(exc, **state, submission_uncertain=attempted and _uncertain(exc),
                      next_step=("retry with the same idempotency_key; do not send a duplicate"
                                 if attempted and _uncertain(exc) else "resolve_error"))


async def aoitalk_cli_session_list(
    limit: Annotated[int, Field(ge=1, le=200)] = 10,
    status: Literal[
        "ready", "queued", "running", "completed", "failed", "waiting_input",
        "waiting_approval", "disconnected", "interrupted", "cancelling", "cancelled",
    ] | None = None,
) -> dict[str, Any]:
    """List durable CLI sessions without loading a provider or starting a turn."""
    try:
        count = _limit(limit, 1, 200, "limit")
        params: dict[str, Any] = {"limit": count}
        if status is not None:
            if status not in SESSION_STATUSES:
                raise CliSessionError("invalid_input", detail="status is not supported")
            params["status"] = status

        async def read(client: CliSessionClient) -> dict[str, Any]:
            _, data = await client.call("GET", "/api/cli-sessions", params=params)
            if not isinstance(data.get("sessions"), list):
                raise CliSessionError("server_error", detail="sessions must be an array")
            return _public_success(data, sessions=data["sessions"][:count], limit=count)

        return await _session(read)
    except CliSessionError as exc:
        return _error(exc)


async def aoitalk_cli_session_status(session_id: SessionId) -> dict[str, Any]:
    """Read one CLI session status; this never reconnects or retries a provider turn."""
    state = {"session_id": session_id}
    try:
        sid = _session_id(session_id)

        async def read(client: CliSessionClient) -> dict[str, Any]:
            _, data = await client.call("GET", f"/api/cli-sessions/{sid}")
            return _public_success(data, session_id=sid)

        return await _session(read)
    except CliSessionError as exc:
        return _error(exc, **state)


async def aoitalk_cli_session_collect(
    cursors: Annotated[dict[str, str], Field(max_length=200)],
    session_ids: Annotated[list[str] | None, Field(max_length=200)] = None,
    limit_per_session: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Collect bounded events for multiple sessions from per-session cursors."""
    try:
        count = _limit(limit_per_session, 1, 500, "limit_per_session")
        if not isinstance(cursors, dict) or len(cursors) > 200:
            raise CliSessionError("invalid_input", detail="cursors must contain at most 200 sessions")
        normalized_cursors: dict[str, str] = {}
        for raw_sid, raw_cursor in cursors.items():
            normalized_cursors[_session_id(raw_sid)] = _cursor(raw_cursor, optional=False) or ""
        normalized_ids = None
        if session_ids is not None:
            if not isinstance(session_ids, list) or len(session_ids) > 200:
                raise CliSessionError("invalid_input", detail="session_ids must contain at most 200 sessions")
            normalized_ids = [_session_id(value) for value in session_ids]
        body: dict[str, Any] = {"cursors": normalized_cursors,
                                "limit_per_session": count}
        if normalized_ids is not None:
            body["session_ids"] = normalized_ids

        async def collect(client: CliSessionClient) -> dict[str, Any]:
            _, data = await client.call("POST", "/api/cli-sessions/collect", json=body)
            return _public_success(data)

        return await _session(collect)
    except CliSessionError as exc:
        return _error(exc)


async def aoitalk_cli_session_tail(
    session_id: SessionId,
    after: Annotated[str | None, Field(max_length=512)] = None,
    limit: Annotated[int, Field(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Read a bounded event tail after an opaque per-session cursor."""
    state = {"session_id": session_id}
    try:
        sid = _session_id(session_id)
        cursor = _cursor(after)
        count = _limit(limit, 1, 500, "limit")
        params: dict[str, Any] = {"limit": count}
        if cursor is not None:
            params["after"] = cursor

        async def read(client: CliSessionClient) -> dict[str, Any]:
            _, data = await client.call("GET", f"/api/cli-sessions/{sid}/events", params=params)
            events = data.get("events")
            if not isinstance(events, list):
                raise CliSessionError("server_error", detail="events must be an array")
            return _public_success(data, session_id=sid, events=events[:count])

        return await _session(read)
    except CliSessionError as exc:
        return _error(exc, **state)


async def aoitalk_cli_session_cancel(session_id: SessionId) -> dict[str, Any]:
    """Request cancellation; a successful request is not terminal completion."""
    state = {"session_id": session_id}
    attempted = False
    try:
        sid = _session_id(session_id)

        async def cancel(client: CliSessionClient) -> dict[str, Any]:
            nonlocal attempted
            attempted = True
            _, data = await client.call("POST", f"/api/cli-sessions/{sid}/cancel")
            return _public_success(data, session_id=sid)

        return await _session(cancel)
    except CliSessionError as exc:
        return _error(exc, **state, submission_uncertain=attempted and _uncertain(exc),
                      next_step=("inspect status/events; do not blindly resend cancel"
                                 if attempted and _uncertain(exc) else "resolve_error"))


async def aoitalk_cli_session_reconnect(session_id: SessionId) -> dict[str, Any]:
    """Ask AoiTalk to reconcile a continuation; never auto-resends a message."""
    state = {"session_id": session_id}
    attempted = False
    try:
        sid = _session_id(session_id)

        async def reconnect(client: CliSessionClient) -> dict[str, Any]:
            nonlocal attempted
            attempted = True
            _, data = await client.call("POST", f"/api/cli-sessions/{sid}/reconnect")
            return _public_success(data, session_id=sid)

        return await _session(reconnect)
    except CliSessionError as exc:
        return _error(exc, **state, submission_uncertain=attempted and _uncertain(exc),
                      next_step=("inspect status/events; do not resend reconnect"
                                 if attempted and _uncertain(exc) else "resolve_error"))


TOOLS = [
    aoitalk_cli_session_create,
    aoitalk_cli_session_send,
    aoitalk_cli_session_list,
    aoitalk_cli_session_status,
    aoitalk_cli_session_collect,
    aoitalk_cli_session_tail,
    aoitalk_cli_session_cancel,
    aoitalk_cli_session_reconnect,
]
READ_ONLY_TOOLS = {
    aoitalk_cli_session_list,
    aoitalk_cli_session_status,
    aoitalk_cli_session_collect,
    aoitalk_cli_session_tail,
    aoitalk_cli_session_reconnect,
}
