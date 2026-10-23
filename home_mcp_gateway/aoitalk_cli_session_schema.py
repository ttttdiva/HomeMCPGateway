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
from typing import Annotated, Any, Callable, Literal, get_type_hints

from mcp.server.mcpserver.tools.base import Tool as _McpTool
from mcp.types import CallToolResult, TextContent
from pydantic import Field
from pydantic_core import PydanticUndefined
from typing_extensions import Required, TypedDict


class BusinessReply(TypedDict, total=False):
    ok: Required[bool]
    success: bool
    error: str
    message: str
    http_status: int
    retry_after_seconds: int
    local_cooldown_seconds: int
    detail: str
    output_truncated: bool
    submission_uncertain: bool
    next_step: str
    execution_target: str | None
    local_filesystem: bool | None
    sandbox: str | None
    turn_status: str | None
    delivery: dict[str, Any] | None
    role_context_mode: Literal["raw", "agent_role"] | None
    role_context_sha256: str | None


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
    execution_target: str | None
    local_filesystem: bool | None
    sandbox: str | None
    turn_status: str | None
    delivery: dict[str, Any] | None
    role_context_mode: Literal["raw", "agent_role"] | None
    role_context_sha256: str | None
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
    execution_target: str | None
    local_filesystem: bool | None
    sandbox: str | None
    turn_status: str | None
    delivery: dict[str, Any] | None
    role_context_mode: Literal["raw", "agent_role"] | None
    role_context_sha256: str | None
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
    execution_target: str | None
    local_filesystem: bool | None
    sandbox: str | None
    turn_status: str | None
    delivery: dict[str, Any] | None
    role_context_mode: Literal["raw", "agent_role"] | None
    role_context_sha256: str | None


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
    execution_target: str | None
    local_filesystem: bool | None
    sandbox: str | None
    turn_status: str | None
    delivery: dict[str, Any] | None
    role_context_mode: Literal["raw", "agent_role"] | None
    role_context_sha256: str | None


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


_LOCAL_CREATE_PROVIDERS = ("codex-cli", "claude-cli", "grok-cli", "antigravity-cli")
_REMOTE_CREATE_PROVIDER = "muse-remote"
_REMOTE_CREATE_ALLOWED_FIELDS = (
    "agent_id",
    "provider",
    "idempotency_key",
    "agent_revision_id",
    "title",
    "execution_profile_id",
    "role_context_mode",
)
_CREATE_SCHEMA_MARKER = "__aoitalk_provider_discriminated_create__"


def _provider_discriminated_create_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Add provider-specific required/forbidden fields to MCP's generated schema."""

    properties = dict(schema.get("properties", {}))
    # Keep the established local-provider schema defaults visible even though
    # the callable uses sentinels to distinguish remote-field omission/null.
    for name, default in (
        ("effort", None),
        ("permission_mode", "native_default"),
        ("allowed_capabilities", None),
    ):
        properties[name] = {**dict(properties[name]), "default": default}
    local_properties = dict(properties)
    for name in ("model", "working_directory", "permission_mode"):
        field_schema = dict(local_properties[name])
        alternatives = field_schema.get("anyOf")
        if isinstance(alternatives, list):
            non_null = [item for item in alternatives if item != {"type": "null"}]
            if len(non_null) == 1:
                local_properties[name] = {
                    **non_null[0],
                    **{key: value for key, value in field_schema.items() if key != "anyOf"},
                }
    local_properties["provider"] = {
        **dict(properties["provider"]),
        "enum": list(_LOCAL_CREATE_PROVIDERS),
    }
    remote_properties = {
        name: properties[name]
        for name in _REMOTE_CREATE_ALLOWED_FIELDS
        if name in properties
    }
    remote_properties["provider"] = {
        **{
            key: value
            for key, value in dict(properties["provider"]).items()
            if key != "enum"
        },
        "const": _REMOTE_CREATE_PROVIDER,
    }
    return {
        **schema,
        "oneOf": [
            {
                "type": "object",
                "properties": local_properties,
                "required": [
                    "agent_id",
                    "provider",
                    "model",
                    "working_directory",
                    "idempotency_key",
                ],
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": remote_properties,
                "required": ["agent_id", "provider", "idempotency_key"],
                "additionalProperties": False,
            },
        ],
    }


def _install_create_schema_hook() -> None:
    """Amend only the generated Tool schema for the create wrapper."""

    if getattr(_McpTool, _CREATE_SCHEMA_MARKER, False):
        return
    original = _McpTool.from_function.__func__

    def from_function(cls, fn, *args, **kwargs):
        tool = original(cls, fn, *args, **kwargs)
        if getattr(fn, _CREATE_SCHEMA_MARKER, False):
            tool.parameters = _provider_discriminated_create_schema(tool.parameters)
        return tool

    _McpTool.from_function = classmethod(from_function)
    setattr(_McpTool, _CREATE_SCHEMA_MARKER, True)


def _cli_session_create_signature(target: Callable[..., Any]) -> inspect.Signature:
    """Keep the common idempotency key required while remote route fields stay optional.

    The implementation uses private sentinels for omitted route fields so it can
    distinguish omission from an explicit JSON null.  Those sentinels are
    implementation defaults; the generated MCP schema still requires the key
    shared by every create request.
    """

    signature = inspect.signature(target, eval_str=True)
    if target.__name__ != "aoitalk_cli_session_create":
        return signature
    parameters = list(signature.parameters.values())
    missing_fields = {
        "model",
        "working_directory",
        "effort",
        "permission_mode",
        "allowed_capabilities",
    }
    parameters = [
        parameter.replace(
            default=PydanticUndefined,
            annotation=Annotated[(
                parameter.annotation,
                Field(default_factory=lambda value=parameter.default: value),
            )],
        )
        if parameter.name in missing_fields
        else parameter
        for parameter in parameters
    ]
    key_parameter = next(parameter for parameter in parameters if parameter.name == "idempotency_key")
    leading = [
        parameter for parameter in parameters
        if parameter.name in {"agent_id", "provider"}
    ]
    trailing = [
        parameter for parameter in parameters
        if parameter.name not in {"agent_id", "provider", "idempotency_key"}
    ]
    key_parameter = key_parameter.replace(default=inspect.Parameter.empty)
    parameters = [*leading, key_parameter, *trailing]
    return signature.replace(parameters=parameters)


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
    invoke.__signature__ = _cli_session_create_signature(target).replace(
        return_annotation=annotation
    )
    invoke.__annotations__ = {
        **get_type_hints(target, include_extras=True),
        "return": annotation,
    }
    if target.__name__ == "aoitalk_cli_session_create":
        setattr(invoke, _CREATE_SCHEMA_MARKER, True)
        _install_create_schema_hook()
    return invoke
