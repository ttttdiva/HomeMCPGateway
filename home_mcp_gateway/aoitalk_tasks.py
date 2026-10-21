"""AoiTalk local-agent task bridge: one MCP call submits, later calls only poll.

ChatGPT previously drove long local work (git status -> search -> read -> build
-> fix -> rebuild -> adb ...) as dozens of individual MCP calls.  These tools
hand the whole goal to AoiTalk's local LLM agent instead.  Every call returns
within seconds; the work runs in AoiTalk's durable worker and survives MCP,
tunnel and Gateway restarts.

Authentication, endpoint validation and credential handling reuse the
ClipIngest adapter (:mod:`home_mcp_gateway.aoitalk`): credentials are read from
operator-owned ``.env`` files and never cross the MCP boundary.
"""
from __future__ import annotations

import asyncio
import json
import math
import re
import time
import uuid
from typing import Annotated, Any, Literal

import httpx
from pydantic import Field

from . import aoitalk

TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
STATES = {"queued", "waiting", "running", "waiting_approval",
          "completed", "failed", "cancelled", "interrupted"}
TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
POLL_INTERVAL = 1.0
MAX_WAIT_SECONDS = 20.0
TaskState = Literal["queued", "waiting", "running", "waiting_approval",
                    "completed", "failed", "cancelled", "interrupted"]
TaskId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
# AoiTalk-side codes that are safe (fixed, non-sensitive) to surface verbatim.
SERVER_CODES = {"invalid_input", "invalid_workspace", "workspace_not_allowed", "idempotency_conflict",
                "not_found", "corrupt_record", "agent_not_configured", "agent_not_found",
                "agent_inactive", "agent_route_mismatch", "agent_revision_missing",
                "canonical_runtime_unavailable", "command_runtime_unavailable", "permission_denied"}
MESSAGES = {
    "configuration_error": "AoiTalkの接続設定・認証情報をローカルの.envで確認してください。",
    "invalid_input": "引数を確認してください（goal / workspace / task_id / 数値範囲）。",
    "invalid_workspace": "workspaceは存在する絶対パスのディレクトリを指定してください。",
    "workspace_not_allowed": "workspaceがAoiTalkのlocal_agent_tasks.allowed_rootsの外です。",
    "idempotency_conflict": "同じidempotency_keyのタスクと内容が異なります。既存タスクを確認してください。",
    "authentication_failed": ".envの認証情報でAoiTalkにログインできません。",
    "password_reset_required": "AoiTalk側で初回パスワード変更を完了してください。",
    "permission_denied": "AoiTalk側の認証済み依頼者と対象Agentの権限を確認してください。権限の回避は行いません。",
    "not_found": "タスクまたはAPIが見つかりません。task_idとAoiTalkのバージョンを確認してください。",
    "disabled": "AoiTalk側でlocal agent tasksが無効です（Enterprise または local_agent_tasks.enabled=false）。",
    "conflict": "AoiTalkが競合を返しました。",
    "rate_limited": "AoiTalkがリクエストを制限しています。時間を置いて再試行してください。",
    "server_error": "AoiTalkのAPIがエラーを返しました。AoiTalk側のログを確認してください。",
    "connection_error": "AoiTalkへ接続できません。AoiTalkの起動状態と接続先を確認してください。",
    "invalid_response": "AoiTalkの応答を検証できません。",
    "corrupt_record": "AoiTalk側のタスク記録を読めません。",
    "agent_not_configured": "委譲先Agentを明示指定するか、AoiTalk側で既定の委譲先を設定してください。",
    "agent_not_found": "指定したAgentが見つかりません。",
    "agent_inactive": "指定したAgentは無効です。",
    "agent_revision_missing": "指定したAgentの公開revisionが見つかりません。",
    "agent_route_mismatch": "指定したprovider/model/effortがAgentの正準routeと一致しません。",
    "canonical_runtime_unavailable": "共通Agentの実行経路を利用できません。別の実行系へは切り替えません。",
    "command_runtime_unavailable": "隔離コマンド実行環境の準備ができていません。ホストshellや別の実行系へは切り替えません。",
}


class TaskError(Exception):
    def __init__(self, code: str, http_status: int | None = None, detail: str | None = None):
        super().__init__(MESSAGES.get(code, MESSAGES["server_error"]))
        self.code = code if code in MESSAGES else "server_error"
        self.http_status = http_status
        self.detail = detail


def _error(exc: TaskError, **state: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": exc.code, "message": MESSAGES[exc.code], **state}
    if exc.http_status is not None:
        result["http_status"] = exc.http_status
    if exc.detail:
        result["detail"] = exc.detail
    return result


class TaskClient(aoitalk.Client):
    """ClipIngest client plus bounded, code-only error bodies for task routes."""

    async def call(self, method: str, path: str, **kwargs: Any) -> tuple[int, dict[str, Any]]:
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                status = response.status_code
                raw = b""
                async for part in response.aiter_bytes():
                    raw += part
                    if len(raw) > aoitalk.MAX_RESPONSE_BYTES:
                        raise TaskError("invalid_response")
        except TaskError:
            raise
        except (httpx.HTTPError, OSError):
            raise TaskError("connection_error") from None
        if 300 <= status < 400:
            raise TaskError("configuration_error", status)
        try:
            data = json.loads(raw) if raw else {}
        except (ValueError, UnicodeError):
            data = None
        if status in {200, 202}:
            if not isinstance(data, dict):
                raise TaskError("invalid_response", status)
            return status, data
        code = {401: "authentication_failed", 403: "permission_denied", 404: "not_found",
                409: "conflict", 422: "invalid_input", 400: "invalid_input", 429: "rate_limited"}.get(status, "server_error")
        detail = None
        if isinstance(data, dict):
            server_code = data.get("error")
            if isinstance(server_code, str) and server_code in SERVER_CODES:
                code = server_code
            raw_detail = data.get("detail")
            if isinstance(raw_detail, str):
                if status == 409 and "disabled" in raw_detail:
                    code = "disabled"
                detail = aoitalk._safe_text(raw_detail, self.settings, 300)
            elif status == 422 and isinstance(raw_detail, list):
                # FastAPI validation: expose only field locations, never input values.
                fields = sorted({".".join(str(p) for p in item.get("loc", [])[1:])
                                 for item in raw_detail if isinstance(item, dict)})
                detail = "invalid fields: " + ", ".join(fields[:10])
        raise TaskError(code, status, detail)

    async def login_task(self) -> None:
        try:
            await self.login()
        except aoitalk.ClipError as exc:
            raise TaskError(exc.code if exc.code in MESSAGES else "server_error", exc.http_status) from None


async def _session(fn):
    try:
        settings = aoitalk.load_settings()
    except aoitalk.ClipError:
        raise TaskError("configuration_error") from None
    client = TaskClient(settings)
    try:
        await client.login_task()
        return _sanitize(await fn(client), settings)
    finally:
        await client.http.aclose()


def _task_id(value: Any) -> str:
    if not isinstance(value, str) or not TASK_ID_RE.fullmatch(value):
        raise TaskError("invalid_input", detail="task_id must be the 32-hex id returned by start")
    return value


def _number(value: Any, low: float, high: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise TaskError("invalid_input", detail=f"{name} must be between {low:g} and {high:g}")
    return float(value)



def _optional_text(value: Any, name: str, limit: int = 256) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise TaskError("invalid_input", detail=f"{name} must be a nonempty string of at most {limit} chars")
    return value


def _identity(task: dict[str, Any]) -> dict[str, Any]:
    """Only public, bounded identity and route fields; no provider config blobs."""
    result: dict[str, Any] = {}
    for key in ("agent_id", "agent_revision_id", "agent_run_id", "work_id", "session_id", "project_id"):
        value = task.get(key)
        if value is not None:
            try:
                result[key] = _optional_text(value, key)
            except TaskError:
                raise TaskError("invalid_response") from None
    if "execution_state" in task:
        state = task["execution_state"]
        if state is not None and (not isinstance(state, str) or not re.fullmatch(r"[a-z][a-z_]{0,63}", state)):
            raise TaskError("invalid_response")
        result["execution_state"] = state
    for key in ("requested_route", "resolved_route"):
        if key not in task:
            continue
        route = task[key]
        if route is None:
            result[key] = None
            continue
        if not isinstance(route, dict):
            raise TaskError("invalid_response")
        result[key] = {}
        for field in ("provider", "model", "effort"):
            if field in route:
                try:
                    result[key][field] = _optional_text(route[field], field)
                except TaskError:
                    raise TaskError("invalid_response") from None
    return result


def _sanitize(value: Any, settings: aoitalk.Settings) -> Any:
    """Bound selected task output recursively and redact credential-shaped data."""
    remaining = 64000
    truncated = False
    omitted = object()
    atomic_text_keys = {"task_id", "status", "execution_state", "agent_id", "agent_revision_id",
                        "agent_run_id", "work_id", "session_id", "project_id", "provider", "model", "effort",
                        "idempotency_key", "error"}
    wire_keys = atomic_text_keys | {
        "ok", "error", "message", "http_status", "detail", "output_truncated", "tasks", "service",
        "events", "truncated", "event_count", "authenticated", "base_url", "created", "idempotency_key",
        "submission_uncertain", "next_step", "requested_route", "resolved_route", "executor", "goal",
        "workspace", "mode", "created_at", "started_at", "finished_at", "last_progress_at",
        "seconds_since_progress", "current_step", "latest_activity", "counters", "changed_files",
        "cancel_requested", "failure_reason", "failure_detail", "summary_text", "completion_summary",
        "stalled", "worker_alive", "stall_threshold_sec", "baseline", "llm", "is_terminal", "result",
        "stopped_reason", "rounds", "report", "report_parsed", "checks", "git", "commands",
        "enabled", "running", "allowed_roots", "max_concurrency", "limits", "queued", "default_agent_id",
        "integration", "llm_calls", "tool_calls", "tool_requests", "tool_failures", "blocked_calls",
        "commands_failed", "files_written",
    }
    secret_key = re.compile(r"(?i)(?:password|passwd|cookie|authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|secret|base64)")

    def public_value(key: str, item: Any) -> bool:
        if not isinstance(item, str):
            return False
        if settings.password and settings.password in item:
            return False
        if key == "status":
            return item in STATES
        if key == "error":
            return item in MESSAGES
        if key == "task_id":
            return bool(TASK_ID_RE.fullmatch(item))
        if key in {"agent_id", "default_agent_id", "agent_revision_id", "agent_run_id", "work_id", "session_id", "project_id"}:
            return bool(re.fullmatch(r"[a-fA-F0-9]{8}(?:-[a-fA-F0-9]{4}){3}-[a-fA-F0-9]{12}", item))
        if key == "execution_state":
            return bool(re.fullmatch(r"[a-z][a-z_]{0,63}", item))
        if key == "provider":
            return item in {"openai_compatible_local", "ollama", "codex-cli"}
        if key == "model":
            return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", item))
        if key == "effort":
            return bool(re.fullmatch(r"[a-z][a-z0-9_]{0,39}", item))
        if key == "idempotency_key":
            return bool(KEY_RE.fullmatch(item)) and item not in {settings.username, settings.password}
        return False

    def visit(item: Any, depth: int = 0, *, atomic_text: bool = False, public_text: bool = False,
              public_container: bool = False, task_array: bool = False) -> Any:
        nonlocal remaining, truncated
        if remaining <= 0:
            truncated = True
            return omitted
        if depth > 8:
            truncated = True
            return "[truncated]"
        if isinstance(item, str):
            original = item
            if not public_text:
                item = re.sub(r"(?i)\b(?:session|token|password|api[_-]?key)\s*[=:]\s*[^\s,;]+", "[redacted]", item)
                for secret in (settings.password, settings.username):
                    if secret:
                        item = item.replace(secret, "[redacted]")
                item = re.sub(r"(?i)\b(?:bearer\s+|aoitpat_|sk-)[^\s]+", "[redacted]", item)
            if atomic_text and item != original:
                # A redacted ID/enum/key must not masquerade as an actionable
                # public value or violate its typed discovery schema.
                truncated = True
                return omitted
            clean = "".join(c for c in item if c >= " " or c in "\n\t")
            if atomic_text and len(clean) > min(12000, remaining):
                truncated = True
                return omitted
            text = clean[:min(12000, remaining)]
            truncated |= len(text) < len(clean)
            remaining -= len(text)
            return text
        if item is None or isinstance(item, (bool, int)):
            if remaining < 16:
                truncated = True
                return omitted
            remaining -= 16
            return item
        if isinstance(item, float):
            if remaining < 16:
                truncated = True
                return omitted
            remaining -= 16
            return item if math.isfinite(item) else None
        if isinstance(item, list):
            result = []
            for child in item[:200]:
                value = visit(child, depth + 1, public_container=task_array)
                if value is omitted:
                    break
                # A partial task identity is unusable for status/cancel/replay.
                if (isinstance(child, dict) and "task_id" in child and "status" in child
                        and (not isinstance(value, dict) or "task_id" not in value or "status" not in value)):
                    truncated = True
                    break
                result.append(value)
            truncated |= len(result) < len(item)
            return result
        if isinstance(item, dict):
            result = {}
            truncated |= len(item) > 100
            for key, child in list(item.items())[:100]:
                if not isinstance(key, str) or secret_key.search(key):
                    continue
                # Public protocol names are not credentials merely because a
                # short configured username happens to occur in their spelling.
                key = key if key in wire_keys else aoitalk._safe_text(key, settings, 128)
                if remaining <= len(key):
                    truncated = True
                    break
                remaining -= len(key)
                value = visit(child, depth + 1, atomic_text=key in atomic_text_keys,
                              public_text=public_container and public_value(key, child),
                              public_container=public_container and key in {"requested_route", "resolved_route", "llm", "service"},
                              task_array=public_container and key == "tasks")
                if value is omitted:
                    break
                result[key] = value
            return result
        return None

    result = visit(value, public_container=True)
    if truncated and isinstance(result, dict):
        result["output_truncated"] = True
    return result

def _task(data: dict[str, Any]) -> dict[str, Any]:
    task = data.get("task")
    if (not isinstance(task, dict) or not isinstance(task.get("status"), str)
            or task["status"] not in STATES):
        raise TaskError("invalid_response")
    try:
        _task_id(task.get("task_id"))
    except TaskError:
        raise TaskError("invalid_response") from None
    _identity(task)
    return task


def _next_step(task: dict[str, Any]) -> str:
    if task.get("status") in TERMINAL:
        return "done: read summary_text/result; call aoitalk_local_task_tail only if details are needed"
    if task.get("status") == "waiting_approval":
        return "approval required in AoiTalk; do not resubmit or bypass the approval"
    if task.get("execution_state") == "loading_model":
        return "waiting for the selected model; poll status; do not switch models or start another worker"
    if task.get("status") == "waiting":
        return "waiting for canonical Agent work; inspect status/tail; do not start a duplicate task"
    if task.get("stalled"):
        return "possibly stalled: inspect aoitalk_local_task_tail, then cancel if no progress"
    return "poll aoitalk_local_task_status(task_id, wait_seconds=20) every few minutes; do not start a duplicate task"


async def aoitalk_local_task_start(
    goal: Annotated[str, Field(min_length=1, max_length=8000)],
    workspace: Annotated[str, Field(min_length=1, max_length=1024)],
    constraints: Annotated[list[Annotated[str, Field(max_length=1000)]] | None, Field(max_length=20)] = None,
    completion_criteria: Annotated[list[Annotated[str, Field(max_length=1000)]] | None, Field(max_length=20)] = None,
    mode: Literal["read_only", "mutate"] = "read_only",
    allow_commands: bool | None = None,
    allow_adb: bool = False,
    allow_git_commit: bool = False,
    max_tool_rounds: Annotated[int | None, Field(ge=1, le=1000)] = None,
    timeout_sec: Annotated[int | None, Field(ge=1, le=86400)] = None,
    command_timeout_sec: Annotated[int | None, Field(ge=1, le=86400)] = None,
    expected_branch: Annotated[str | None, Field(max_length=200)] = None,
    idempotency_key: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9._:-]{1,128}$")] = None,
    agent_id: Annotated[str | None, Field(min_length=36, max_length=36)] = None,
    provider: Literal["openai_compatible_local", "ollama", "codex-cli"] | None = None,
    model: Annotated[str | None, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}$")] = None,
    effort: Annotated[str | None, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")] = None,
) -> dict[str, Any]:
    """Delegate a multi-step workspace goal to AoiTalk's LLM agent; returns task_id at once.

    Use for adaptive work (build -> analyze error -> search -> fix -> rebuild ...).
    For fixed command sequences use job_start/run_command instead (deterministic).
    Give a goal, an absolute workspace directory, constraints and completion
    criteria, not low-level commands. mode="read_only" (default) exposes only
    read tools in the canonical Agent path. Explicit mode="mutate" plus
    allow_commands=true requires command_execute authority and the existing
    isolated command runtime to pass readiness before model use. No host-shell
    fallback is permitted. Requested permissions are intersected with Agent
    authority; goal wording cannot widen them. The canonical path currently
    rejects adb, git commit, and expected_branch options; legacy arguments are
    retained for API compatibility. Submission is bounded; then poll
    aoitalk_local_task_status. Retrying with the same idempotency_key returns
    the same task instead of a duplicate. agent_id selects a canonical AoiTalk
    Agent; omission requires an explicitly configured delegation default.
    provider/model/effort are expectations checked against its pinned revision,
    never silent route overrides. When agent_id is supplied and provider is
    omitted, the generic canonical Agent task API is used; poll it with
    aoitalk_agent_task_status. Calls without agent_id or with an explicit
    legacy provider keep the local-task API. For initial read-only checks explicitly pass
    allow_commands=false: host commands are not an OS sandbox. Omission is
    passed through so canonical execution defaults to false while an explicitly
    selected legacy execution path retains its existing server-side default.
    """
    key = idempotency_key if idempotency_key is not None else f"gw-{uuid.uuid4().hex}"
    state: dict[str, Any] = {"idempotency_key": key}
    post_attempted = False
    try:
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise TaskError("invalid_input", detail="idempotency_key must match [A-Za-z0-9._:-]{1,128}")
        if not isinstance(goal, str) or not goal.strip() or len(goal) > 8000:
            raise TaskError("invalid_input", detail="goal must be 1-8000 chars")
        if not isinstance(workspace, str) or not workspace.strip():
            raise TaskError("invalid_workspace")
        body: dict[str, Any] = {
            "goal": goal, "workspace": workspace, "constraints": list(constraints or []),
            "completion_criteria": list(completion_criteria or []), "mode": mode,
            "allow_adb": allow_adb, "allow_git_commit": allow_git_commit,
        }
        if allow_commands is not None:
            if not isinstance(allow_commands, bool):
                raise TaskError("invalid_input", detail="allow_commands must be boolean or null")
            body["allow_commands"] = allow_commands
        for name, value in (("max_tool_rounds", max_tool_rounds), ("timeout_sec", timeout_sec),
                            ("command_timeout_sec", command_timeout_sec), ("expected_branch", expected_branch)):
            if value is not None:
                body[name] = value

        for name, value in (("agent_id", agent_id), ("provider", provider), ("model", model), ("effort", effort)):
            if value is not None:
                body[name] = _optional_text(value, name)

        if agent_id is not None and provider is None:
            legacy_only_options = (
                allow_adb,
                allow_git_commit,
                max_tool_rounds,
                timeout_sec,
                command_timeout_sec,
                expected_branch,
            )
            if any(value not in (None, False) for value in legacy_only_options):
                raise TaskError(
                    "invalid_input",
                    detail=(
                        "agent_id with omitted provider uses the canonical Agent API; "
                        "ADB, Git commit, timeout, max_tool_rounds and expected_branch "
                        "options are not supported on that route"
                    ),
                )

            from . import aoitalk_agent_tasks

            routed = await aoitalk_agent_tasks.aoitalk_agent_task_start(
                goal=goal,
                workspace=workspace,
                agent_id=agent_id,
                idempotency_key=key,
                constraints=constraints,
                completion_criteria=completion_criteria,
                mode=mode,
                allow_commands=allow_commands is True,
                provider=None,
                model=model,
                effort=effort,
            )
            if not routed.get("ok"):
                return routed
            result = {
                "ok": True,
                "task_id": routed["task_id"],
                "status": routed["status"],
                "created": routed.get("created", False),
                "executor": "aoitalk_agent_task",
                "workspace": workspace,
                "mode": mode,
                "agent_id": routed.get("agent_id", agent_id),
                "next_step": (
                    "poll aoitalk_agent_task_status(task_id); do not start a duplicate task"
                ),
            }
            for name in ("agent_revision_id", "agent_run_id"):
                if routed.get(name) is not None:
                    result[name] = routed[name]
            if routed.get("work_item_id") is not None:
                result["work_id"] = routed["work_item_id"]
            if routed.get("idempotency_key") is not None:
                result["idempotency_key"] = routed["idempotency_key"]
            return result

        async def submit(client: TaskClient) -> dict[str, Any]:
            nonlocal post_attempted
            post_attempted = True
            status, data = await client.call("POST", "/api/local-agent/tasks", json=body,
                                             headers={"Idempotency-Key": key})
            task = _task(data)
            return {"ok": True, "task_id": task["task_id"], "status": task["status"],
                    "created": bool(data.get("created", status == 202)), "idempotency_key": key,
                    "executor": task.get("executor", "aoitalk_local"), "workspace": task.get("workspace"),
                    "mode": task.get("mode"), **_identity(task), "next_step": _next_step(task)}

        return await _session(submit)
    except TaskError as exc:
        uncertain = post_attempted and exc.code in {"connection_error", "invalid_response", "server_error"}
        return _error(exc, **state, submission_uncertain=uncertain,
            next_step=("retry aoitalk_local_task_start with the same idempotency_key (no duplicate is created)"
                       if uncertain else "inspect the existing task; do not silently change the idempotency_key"
                       if exc.code == "idempotency_conflict" else "resolve_error"))


def _compact(task: dict[str, Any]) -> dict[str, Any]:
    keep = ("task_id", "status", "executor", "goal", "workspace", "mode", "created_at", "started_at",
            "finished_at", "last_progress_at", "seconds_since_progress", "current_step", "latest_activity",
            "counters", "changed_files", "cancel_requested", "failure_reason", "failure_detail",
            "summary_text", "completion_summary", "stalled", "worker_alive", "stall_threshold_sec",
            "baseline", "llm", "is_terminal")
    result = {**_identity(task), **{key: task[key] for key in keep if key in task}}
    detail = task.get("result")
    if isinstance(detail, dict):
        result["result"] = {key: detail.get(key) for key in (
            "stopped_reason", "rounds", "report", "report_parsed", "checks", "git", "commands") if key in detail}
    return result


async def aoitalk_local_task_status(task_id: TaskId, wait_seconds: Annotated[float, Field(ge=0, le=20)] = 0) -> dict[str, Any]:
    """Read one AoiTalk workspace task: state, progress counters, stalled flag and final summary.

    wait_seconds (0-20) waits briefly for completion; the call never blocks
    longer. Terminal states: completed / failed / cancelled / interrupted. While running,
    stalled=true means no progress within the threshold or no worker heartbeat.
    """
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id
        wait_seconds = _number(wait_seconds, 0, MAX_WAIT_SECONDS, "wait_seconds")

        async def read(client: TaskClient) -> dict[str, Any]:
            deadline = time.monotonic() + wait_seconds
            while True:
                _, data = await client.call("GET", f"/api/local-agent/tasks/{task_id}")
                task = _task(data)
                if task["task_id"] != task_id:
                    raise TaskError("invalid_response")
                if task["status"] in TERMINAL or time.monotonic() >= deadline:
                    return {"ok": True, **_compact(task), "next_step": _next_step(task)}
                await asyncio.sleep(min(POLL_INTERVAL, max(0.0, deadline - time.monotonic())))

        return await _session(read)
    except TaskError as exc:
        return _error(exc, **state)


async def aoitalk_local_task_tail(task_id: TaskId, limit: Annotated[int, Field(ge=1, le=200)] = 30,
                                  max_bytes: Annotated[int, Field(ge=1024, le=64000)] = 12000) -> dict[str, Any]:
    """Return a bounded tail of an AoiTalk workspace task's event log (commands, edits, notes).

    limit 1-200 events, max_bytes 1024-64000. Secrets are redacted by AoiTalk;
    full raw logs are never returned.
    """
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id
        limit = int(_number(limit, 1, 200, "limit"))
        max_bytes = int(_number(max_bytes, 1024, 64000, "max_bytes"))

        async def read(client: TaskClient) -> dict[str, Any]:
            _, data = await client.call("GET", f"/api/local-agent/tasks/{task_id}/tail",
                                        params={"limit": limit, "max_bytes": max_bytes})
            events = data.get("events")
            if data.get("task_id") != task_id or not isinstance(data.get("status"), str) or data.get("status") not in STATES or not isinstance(events, list):
                raise TaskError("invalid_response")
            return {"ok": True, "task_id": task_id, "status": data["status"],
                    "events": [event for event in events[:limit] if isinstance(event, dict)],
                    "truncated": bool(data.get("truncated")), "event_count": data.get("event_count")}

        return await _session(read)
    except TaskError as exc:
        return _error(exc, **state)


async def aoitalk_local_task_cancel(task_id: TaskId) -> dict[str, Any]:
    """Request cancellation of an AoiTalk workspace task (idempotent; running commands are stopped).

    A queued task is cancelled immediately; a running task stops at the next
    safe point. Poll aoitalk_local_task_status for the final cancelled state.
    """
    state: dict[str, Any] = {}
    try:
        task_id = _task_id(task_id)
        state["task_id"] = task_id

        async def cancel(client: TaskClient) -> dict[str, Any]:
            _, data = await client.call("POST", f"/api/local-agent/tasks/{task_id}/cancel")
            task = _task(data)
            if task["task_id"] != task_id:
                raise TaskError("invalid_response")
            return {"ok": True, **_compact(task), "next_step": _next_step(task)}

        return await _session(cancel)
    except TaskError as exc:
        return _error(exc, **state)


async def aoitalk_local_task_list(limit: Annotated[int, Field(ge=1, le=100)] = 10,
                                  status: TaskState | None = None) -> dict[str, Any]:
    """List recent AoiTalk workspace tasks (rediscover task_ids after restarts) and check the connection.

    Also returns the AoiTalk worker configuration: model, allowed workspace
    roots and limits. Status also distinguishes model/approval waits and interruption.
    """
    try:
        limit = int(_number(limit, 1, 100, "limit"))
        if status is not None and (not isinstance(status, str) or status not in STATES):
            raise TaskError("invalid_input", detail="status must be one of " + ", ".join(sorted(STATES)))

        async def read(client: TaskClient) -> dict[str, Any]:
            _, service = await client.call("GET", "/api/local-agent/status")
            params: dict[str, Any] = {"limit": limit}
            if status:
                params["status"] = status
            _, data = await client.call("GET", "/api/local-agent/tasks", params=params)
            tasks = data.get("tasks")
            if not isinstance(tasks, list):
                raise TaskError("invalid_response")
            service_view = {key: service.get(key) for key in (
                "enabled", "running", "model", "allowed_roots", "max_concurrency", "limits", "queued",
                "agent_id", "default_agent_id", "integration") }
            return {"ok": True, "authenticated": True, "base_url": client.settings.base_url,
                    "service": service_view,
                    "tasks": [_compact(_task({"task": task})) for task in tasks[:limit]]}

        return await _session(read)
    except TaskError as exc:
        return _error(exc)


TOOLS = [aoitalk_local_task_start, aoitalk_local_task_status, aoitalk_local_task_tail,
         aoitalk_local_task_cancel, aoitalk_local_task_list]
READ_ONLY_TOOLS = {aoitalk_local_task_status, aoitalk_local_task_tail, aoitalk_local_task_list}
