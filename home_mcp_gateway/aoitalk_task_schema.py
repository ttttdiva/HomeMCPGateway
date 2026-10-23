"""Discovery schemas for task replies; keep the existing JSON wire values intact.

CallToolResult lets the SDK validate the documented structure without replacing
optional fields with defaults or discarding future bounded business fields.
The native task functions continue to return ordinary dictionaries.
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any, Callable, get_type_hints

from mcp.types import CallToolResult, TextContent
from typing_extensions import Required, TypedDict

from .aoitalk_tasks import TaskState


class Route(TypedDict, total=False):
    provider: str | None
    model: str | None
    effort: str | None


class TaskResult(TypedDict, total=False):
    stopped_reason: str | None
    rounds: int | None
    report: Any
    report_parsed: Any
    checks: Any
    git: dict[str, Any] | None
    commands: list[dict[str, Any]] | None


class TaskFields(TypedDict, total=False):
    task_id: str
    status: TaskState
    agent_id: str | None
    agent_revision_id: str | None
    agent_run_id: str | None
    work_id: str | None
    session_id: str | None
    project_id: str | None
    requested_route: Route | None
    resolved_route: Route | None
    execution_state: str | None
    executor: str
    goal: str
    workspace: str | None
    mode: str | None
    created_at: str | None
    started_at: str | None
    finished_at: str | None
    last_progress_at: str | None
    seconds_since_progress: float | None
    current_step: str | None
    latest_activity: Any
    counters: dict[str, int] | None
    changed_files: list[str] | None
    cancel_requested: bool | None
    failure_reason: str | None
    failure_detail: str | None
    summary_text: str | None
    completion_summary: str | None
    stalled: bool | None
    worker_alive: bool | None
    stall_threshold_sec: float | None
    baseline: dict[str, Any] | None
    llm: dict[str, Any] | None
    is_terminal: bool | None
    failure: dict[str, Any] | None
    tool_failures: dict[str, Any] | None
    result: TaskResult | None


class BusinessReply(TypedDict, total=False):
    ok: Required[bool]
    error: str
    message: str
    http_status: int
    retry_after_seconds: int
    local_cooldown_seconds: int
    detail: str
    output_truncated: bool


class TaskReply(TaskFields, BusinessReply, total=False):
    next_step: str


class StartReply(TaskReply, total=False):
    created: bool
    idempotency_key: str
    submission_uncertain: bool


class TailReply(BusinessReply, total=False):
    task_id: str
    status: TaskState
    events: list[dict[str, Any]]
    truncated: bool
    event_count: int | None


class Service(TypedDict, total=False):
    enabled: bool | None
    running: bool | None
    model: str | None
    allowed_roots: list[str] | None
    max_concurrency: int | None
    limits: dict[str, Any] | None
    queued: int | None
    agent_id: str | None
    default_agent_id: str | None
    integration: Any


class ListReply(BusinessReply, total=False):
    authenticated: bool
    base_url: str
    service: Service
    tasks: list[TaskFields]


REPLIES = {
    "aoitalk_local_task_start": StartReply,
    "aoitalk_local_task_status": TaskReply,
    "aoitalk_local_task_tail": TailReply,
    "aoitalk_local_task_cancel": TaskReply,
    "aoitalk_local_task_list": ListReply,
}


def structured_task_tool(target: Callable[..., Any]) -> Callable[..., Any]:
    """Publish a typed output schema without changing success/business-error JSON."""
    @functools.wraps(target)
    async def invoke(**kwargs):
        result = await target(**kwargs)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result,
        )

    annotation = Annotated[CallToolResult, REPLIES[target.__name__]]
    invoke.__signature__ = inspect.signature(target, eval_str=True).replace(return_annotation=annotation)
    invoke.__annotations__ = {**get_type_hints(target, include_extras=True), "return": annotation}
    return invoke
