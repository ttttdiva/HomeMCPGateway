"""MCP output schemas for the generic AoiTalk Agent task bridge."""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any, Callable

from mcp.types import CallToolResult, TextContent
from typing_extensions import Required, TypedDict


class AgentTaskFields(TypedDict, total=False):
    task_id: str
    status: str
    work_item_id: str
    agent_run_id: str
    agent_id: str
    agent_revision_id: str
    cancellation_requested: bool
    is_terminal: bool
    created_at: str
    updated_at: str
    started_at: str
    finished_at: str
    current_step: str
    summary_text: str
    failure_reason: str
    failure_detail: str
    progress: Any
    result: Any
    failure: dict[str, Any] | None
    tool_failures: dict[str, Any] | None


class BusinessReply(TypedDict, total=False):
    ok: Required[bool]
    error: str
    message: str
    http_status: int
    retry_after_seconds: int
    local_cooldown_seconds: int
    error_source: str
    server_error_code: str
    server_detail: str
    output_truncated: bool
    next_step: str


class TaskReply(AgentTaskFields, BusinessReply, total=False):
    pass


class StartReply(TaskReply, total=False):
    created: bool
    idempotency_key: str
    submission_uncertain: bool


class TailReply(BusinessReply, total=False):
    task_id: str
    status: str
    events: list[dict[str, Any]]
    truncated: bool
    event_count: int | None


class ResultReply(BusinessReply, total=False):
    task_id: str
    status: str
    content: str
    content_sha256: str
    total_chars: int
    total_bytes: int
    offset: int
    next_offset: int | None


REPLIES = {
    "aoitalk_agent_task_start": StartReply,
    "aoitalk_agent_task_status": TaskReply,
    "aoitalk_agent_task_tail": TailReply,
    "aoitalk_agent_task_result": ResultReply,
    "aoitalk_agent_task_cancel": TaskReply,
}


def structured_agent_task_tool(target: Callable[..., Any]) -> Callable[..., Any]:
    """Publish a typed output schema while preserving the returned JSON values."""

    @functools.wraps(target)
    async def invoke(**kwargs):
        result = await target(**kwargs)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))],
            structuredContent=result,
        )

    annotation = Annotated[CallToolResult, REPLIES[target.__name__]]
    invoke.__signature__ = inspect.signature(target, eval_str=True).replace(return_annotation=annotation)
    invoke.__annotations__ = {**inspect.get_annotations(target, eval_str=True), "return": annotation}
    return invoke
