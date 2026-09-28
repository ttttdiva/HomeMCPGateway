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
from typing import Any

import httpx

from . import aoitalk

TASK_ID_RE = re.compile(r"^[a-f0-9]{32}$")
KEY_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
STATES = {"queued", "running", "completed", "failed", "cancelled"}
TERMINAL = {"completed", "failed", "cancelled"}
POLL_INTERVAL = 1.0
MAX_WAIT_SECONDS = 20.0
# AoiTalk-side codes that are safe (fixed, non-sensitive) to surface verbatim.
SERVER_CODES = {"invalid_input", "invalid_workspace", "workspace_not_allowed", "idempotency_conflict",
                "not_found", "corrupt_record"}
MESSAGES = {
    "configuration_error": "AoiTalkの接続設定・認証情報をローカルの.envで確認してください。",
    "invalid_input": "引数を確認してください（goal / workspace / task_id / 数値範囲）。",
    "invalid_workspace": "workspaceは存在する絶対パスのディレクトリを指定してください。",
    "workspace_not_allowed": "workspaceがAoiTalkのlocal_agent_tasks.allowed_rootsの外です。",
    "idempotency_conflict": "同じidempotency_keyで別内容のタスクが登録済みです。新しいキーを使ってください。",
    "authentication_failed": ".envの認証情報でAoiTalkにログインできません。",
    "password_reset_required": "AoiTalk側で初回パスワード変更を完了してください。",
    "permission_denied": "AoiTalkの管理者権限が必要です。権限の回避は行いません。",
    "not_found": "タスクまたはAPIが見つかりません。task_idとAoiTalkのバージョンを確認してください。",
    "disabled": "AoiTalk側でlocal agent tasksが無効です（Enterprise または local_agent_tasks.enabled=false）。",
    "conflict": "AoiTalkが競合を返しました。",
    "rate_limited": "AoiTalkがリクエストを制限しています。時間を置いて再試行してください。",
    "server_error": "AoiTalkのAPIがエラーを返しました。AoiTalk側のログを確認してください。",
    "connection_error": "AoiTalkへ接続できません。AoiTalkの起動状態と接続先を確認してください。",
    "invalid_response": "AoiTalkの応答を検証できません。",
    "corrupt_record": "AoiTalk側のタスク記録を読めません。",
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
            if server_code in SERVER_CODES:
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
        return await fn(client)
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


def _task(data: dict[str, Any]) -> dict[str, Any]:
    task = data.get("task")
    if not isinstance(task, dict) or task.get("status") not in STATES:
        raise TaskError("invalid_response")
    try:
        _task_id(task.get("task_id"))
    except TaskError:
        raise TaskError("invalid_response") from None
    return task


def _next_step(task: dict[str, Any]) -> str:
    if task.get("status") in TERMINAL:
        return "done: read summary_text/result; call aoitalk_local_task_tail only if details are needed"
    if task.get("stalled"):
        return "possibly stalled: inspect aoitalk_local_task_tail, then cancel if no progress"
    return "poll aoitalk_local_task_status(task_id, wait_seconds=20) every few minutes; do not start a duplicate task"


async def aoitalk_local_task_start(
    goal: str,
    workspace: str,
    constraints: list[str] | None = None,
    completion_criteria: list[str] | None = None,
    mode: str = "read_only",
    allow_commands: bool = True,
    allow_adb: bool = False,
    allow_git_commit: bool = False,
    max_tool_rounds: int | None = None,
    timeout_sec: int | None = None,
    command_timeout_sec: int | None = None,
    expected_branch: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Delegate a multi-step local goal to AoiTalk's local LLM agent; returns task_id at once.

    Use for adaptive work (build -> analyze error -> search -> fix -> rebuild ...).
    For fixed command sequences use job_start/run_command instead (deterministic).
    Give a goal, an absolute workspace directory, constraints and completion
    criteria, not low-level commands. mode="read_only" (default) exposes only
    read tools + guarded commands; mode="mutate" allows edits inside the workspace.
    Git reset/clean/checkout/switch/stash/push, .env/secret reads and writes
    outside the workspace are blocked by AoiTalk. adb and git commit are opt-in.
    mode is the permission authority; goal wording does not widen or narrow
    tools. Never blocks; then poll
    aoitalk_local_task_status. Retrying with the same idempotency_key returns
    the same task instead of a duplicate.
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
            "allow_commands": allow_commands, "allow_adb": allow_adb, "allow_git_commit": allow_git_commit,
        }
        for name, value in (("max_tool_rounds", max_tool_rounds), ("timeout_sec", timeout_sec),
                            ("command_timeout_sec", command_timeout_sec), ("expected_branch", expected_branch)):
            if value is not None:
                body[name] = value

        async def submit(client: TaskClient) -> dict[str, Any]:
            nonlocal post_attempted
            post_attempted = True
            status, data = await client.call("POST", "/api/local-agent/tasks", json=body,
                                             headers={"Idempotency-Key": key})
            task = _task(data)
            return {"ok": True, "task_id": task["task_id"], "status": task["status"],
                    "created": bool(data.get("created", status == 202)), "idempotency_key": key,
                    "executor": task.get("executor", "aoitalk_local"), "workspace": task.get("workspace"),
                    "mode": task.get("mode"), "next_step": _next_step(task)}

        return await _session(submit)
    except TaskError as exc:
        return _error(exc, **state, submission_uncertain=post_attempted and exc.code in {
            "connection_error", "invalid_response", "server_error"},
            next_step=("retry aoitalk_local_task_start with the same idempotency_key (no duplicate is created)"
                       if post_attempted else "resolve_error"))


def _compact(task: dict[str, Any]) -> dict[str, Any]:
    keep = ("task_id", "status", "executor", "goal", "workspace", "mode", "created_at", "started_at",
            "finished_at", "last_progress_at", "seconds_since_progress", "current_step", "latest_activity",
            "counters", "changed_files", "cancel_requested", "failure_reason", "failure_detail",
            "summary_text", "completion_summary", "stalled", "worker_alive", "stall_threshold_sec",
            "baseline", "llm", "is_terminal")
    result = {key: task[key] for key in keep if key in task}
    detail = task.get("result")
    if isinstance(detail, dict):
        result["result"] = {key: detail.get(key) for key in (
            "stopped_reason", "rounds", "report", "report_parsed", "checks", "git", "commands") if key in detail}
    return result


async def aoitalk_local_task_status(task_id: str, wait_seconds: float = 0) -> dict[str, Any]:
    """Read one AoiTalk local task: state, progress counters, stalled flag and final summary.

    wait_seconds (0-20) waits briefly for completion; the call never blocks
    longer. Terminal states: completed / failed / cancelled. While running,
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


async def aoitalk_local_task_tail(task_id: str, limit: int = 30, max_bytes: int = 12000) -> dict[str, Any]:
    """Return a bounded tail of an AoiTalk local task's event log (commands, edits, notes).

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
            if data.get("task_id") != task_id or data.get("status") not in STATES or not isinstance(events, list):
                raise TaskError("invalid_response")
            return {"ok": True, "task_id": task_id, "status": data["status"],
                    "events": [event for event in events if isinstance(event, dict)],
                    "truncated": bool(data.get("truncated")), "event_count": data.get("event_count")}

        return await _session(read)
    except TaskError as exc:
        return _error(exc, **state)


async def aoitalk_local_task_cancel(task_id: str) -> dict[str, Any]:
    """Request cancellation of an AoiTalk local task (idempotent; running commands are stopped).

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
            return {"ok": True, **_compact(task), "next_step": _next_step(task)}

        return await _session(cancel)
    except TaskError as exc:
        return _error(exc, **state)


async def aoitalk_local_task_list(limit: int = 10, status: str | None = None) -> dict[str, Any]:
    """List recent AoiTalk local tasks (rediscover task_ids after restarts) and check the connection.

    Also returns the AoiTalk worker configuration: model, allowed workspace
    roots and limits. status filters by queued/running/completed/failed/cancelled.
    """
    try:
        limit = int(_number(limit, 1, 100, "limit"))
        if status is not None and status not in STATES:
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
                "enabled", "running", "model", "allowed_roots", "max_concurrency", "limits", "queued")}
            return {"ok": True, "authenticated": True, "base_url": client.settings.base_url,
                    "service": service_view,
                    "tasks": [_compact(task) for task in tasks if isinstance(task, dict)]}

        return await _session(read)
    except TaskError as exc:
        return _error(exc)


TOOLS = [aoitalk_local_task_start, aoitalk_local_task_status, aoitalk_local_task_tail,
         aoitalk_local_task_cancel, aoitalk_local_task_list]
READ_ONLY_TOOLS = {aoitalk_local_task_status, aoitalk_local_task_tail, aoitalk_local_task_list}
