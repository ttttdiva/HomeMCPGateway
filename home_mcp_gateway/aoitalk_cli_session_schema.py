"""MCP discovery schemas for the AoiTalk CLI-session bridge.

The HTTP API owns the CLI lifecycle and the provider-specific continuation
handle.  The Gateway publishes only bounded, public session/event projections;
the wrapper keeps the original JSON result available in ``structuredContent``
without adding defaults to it.
"""
from __future__ import annotations

import functools
import inspect
import json
from typing import Annotated, Any, Callable, get_type_hints

from mcp.types import CallToolResult, TextContent
from pydantic import Field
from typing_extensions import Required, TypedDict


class BusinessReply(TypedDict, total=False):
    ok: Required[bool]
    success: bool
    error: str
    message: str
    http_status: int
    detail: str
    output_truncated: bool
    submission_uncertain: bool
    next_step: str


class Session(TypedDict, total=False):
    session_id: str | None
    owner_id: str | None
    agent_id: str | None
    agent_revision_id: str | None
    binding_id: str | None
    provider: str | None
    model: str | None
    effort: str | None
    working_directory: str | None
    title: str | None
    permission_mode: str | None
    policy: dict[str, Any] | None
    native_state: str | None
    run_id: str | None
    work_item_id: str | None
    run: dict[str, Any] | None
    work: dict[str, Any] | None
    status: str | None
    created_at: str | None
    updated_at: str | None
    terminal: bool
    cancel_requested: bool
    reconnect_required: bool
    last_cursor: str | None
    next_cursor: str | None


class SessionReply(BusinessReply, total=False):
    session_id: str | None
    session: Session | None
    owner_id: str | None
    agent_id: str | None
    agent_revision_id: str | None
    binding_id: str | None
    binding_snapshot_hash: str | None
    provider: str | None
    model: str | None
    effort: str | None
    title: str | None
    permission_mode: str | None
    working_directory: str | None
    policy: dict[str, Any] | None
    native_state: str | None
    safe_to_send: bool | None
    reconciled: bool | None
    run_id: str | None
    work_item_id: str | None
    run: dict[str, Any] | None
    work: dict[str, Any] | None
    created: bool | None
    accepted: bool | None
    reconnected: bool | None
    cancelled: bool | None
    message_id: str | None
    idempotency_key: str | None
    status: str | None


class Event(TypedDict, total=False):
    event_id: str
    source: str
    run_id: str | None
    work_item_id: str | None
    sequence: int
    event_type: str
    status: str | None
    message: str | None
    payload: dict[str, Any]
    created_at: str | None


class SessionListReply(BusinessReply, total=False):
    sessions: Annotated[list[Session], Field(max_length=200)]
    limit: int


class SessionEventsReply(BusinessReply, total=False):
    session_id: str | None
    status: str
    events: Annotated[list[Event], Field(max_length=500)]
    cursor: str | None
    next_cursor: str | None
    event_count: int
    truncated: bool
    cursor_advance_blocked: bool
    has_more: bool


class SessionEventPage(TypedDict, total=False):
    """Nested collect page; ``ok`` is only required on the outer reply."""

    session_id: str | None
    status: str | None
    events: Annotated[list[Event], Field(max_length=500)]
    cursor: str | None
    next_cursor: str | None
    event_count: int | None
    truncated: bool | None
    cursor_advance_blocked: bool | None
    has_more: bool | None


class SessionCollectReply(BusinessReply, total=False):
    sessions: Annotated[list[SessionEventPage], Field(max_length=200)]
    items: Annotated[list[SessionEventPage], Field(max_length=200)]
    results: Annotated[list[SessionEventPage], Field(max_length=200)]


REPLIES = {
    "aoitalk_cli_session_create": SessionReply,
    "aoitalk_cli_session_send": SessionReply,
    "aoitalk_cli_session_list": SessionListReply,
    "aoitalk_cli_session_status": SessionReply,
    "aoitalk_cli_session_collect": SessionCollectReply,
    "aoitalk_cli_session_tail": SessionEventsReply,
    "aoitalk_cli_session_cancel": SessionReply,
    "aoitalk_cli_session_reconnect": SessionReply,
}


def structured_cli_session_tool(target: Callable[..., Any]) -> Callable[..., Any]:
    """Publish a typed output schema while preserving the wire JSON exactly."""

    @functools.wraps(target)
    async def invoke(**kwargs):
        result = await target(**kwargs)
        return CallToolResult(
            content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False, separators=(",", ":")))],
            structuredContent=result,
        )

    annotation = Annotated[CallToolResult, REPLIES[target.__name__]]
    invoke.__signature__ = inspect.signature(target, eval_str=True).replace(
        return_annotation=annotation
    )
    invoke.__annotations__ = {
        **get_type_hints(target, include_extras=True),
        "return": annotation,
    }
    return invoke
