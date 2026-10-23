"""MCP surface for the AoiTalk plugin; deliberately independent of server.py."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from . import aoitalk, aoitalk_auth, aoitalk_agent_tasks, aoitalk_cli_sessions, aoitalk_manage, aoitalk_tasks
from .aoitalk_task_schema import structured_task_tool
from .aoitalk_agent_task_schema import structured_agent_task_tool
from .aoitalk_cli_session_schema import structured_cli_session_tool
from .telemetry import ToolTelemetryMiddleware, instrument_tool


if os.environ.get("HOME_MCP_PROFILE"):
    raise RuntimeError("HOME_MCP_PROFILE cannot be used with the AoiTalk plugin entry")


@asynccontextmanager
async def lifespan(server):
    await aoitalk_auth.activate()
    try:
        yield {}
    finally:
        await aoitalk_auth.deactivate()


mcp = MCPServer(
    "AoiTalk Plugin",
    title="AoiTalk management and Agent tools",
    description="Dedicated MCP tools for AoiTalk clips, tasks, Docs, Knowledge, API Agents and CLI sessions.",
    instructions=(
        "Use AoiTalk-specific tools for AoiTalk records. Task mutations require a caller-held operation_id. "
        "After an uncertain write, inspect its status and retry only with the same identifier and identical arguments."
    ),
    lifespan=lifespan,
    middleware=[ToolTelemetryMiddleware()],
)


def _register(function: Any, *, read_only: bool, destructive: bool = False,
              idempotent: bool = True, open_world: bool = True, wrapper=None) -> None:
    target = instrument_tool(function)
    if wrapper is not None:
        target = wrapper(target)
    mcp.tool(annotations=ToolAnnotations(
        read_only_hint=read_only,
        destructive_hint=destructive,
        idempotent_hint=idempotent,
        open_world_hint=open_world,
    ))(target)


# The existing 21 tools keep their native functions, wire schemas and wrappers.
for function in aoitalk.TOOLS:
    writes_docs = function is aoitalk.aoitalk_clip_ingest
    _register(function, read_only=not writes_docs, destructive=writes_docs)

for function in aoitalk_tasks.TOOLS:
    read_only = function in aoitalk_tasks.READ_ONLY_TOOLS
    _register(function, read_only=read_only,
              destructive=function is aoitalk_tasks.aoitalk_local_task_start,
              open_world=not read_only,
              wrapper=structured_task_tool)

for function in aoitalk_agent_tasks.TOOLS:
    read_only = function in aoitalk_agent_tasks.READ_ONLY_TOOLS
    _register(function, read_only=read_only, destructive=not read_only,
              open_world=not read_only,
              wrapper=structured_agent_task_tool)

for function in aoitalk_cli_sessions.TOOLS:
    read_only = function in aoitalk_cli_sessions.READ_ONLY_TOOLS
    # reconnect is a real read-only observation and retains its established annotation.
    _register(function, read_only=read_only, destructive=not read_only,
              open_world=not read_only,
              wrapper=structured_cli_session_tool)

for function in aoitalk_manage.TOOLS:
    read_only = function in aoitalk_manage.READ_ONLY_TOOLS
    is_prepare = function is aoitalk_manage.aoitalk_docs_prepare_edit
    _register(function, read_only=read_only and not is_prepare,
              destructive=function is aoitalk_manage.aoitalk_docs_mutate
              or function in {aoitalk_manage.aoitalk_task_create,
                              aoitalk_manage.aoitalk_task_update,
                              aoitalk_manage.aoitalk_task_delete,
                              aoitalk_manage.aoitalk_task_restore},
              idempotent=True)


def main() -> None:
    # Defense in depth for direct module entry as well as the stdio wrapper.
    for name in ("CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY", "HOME_MCP_AOITALK_RUNTIME_API_KEY"):
        os.environ.pop(name, None)
    root = Path(__file__).resolve().parents[1]
    os.environ["HOME_MCP_TOOL_LOG_DIR"] = str(root / ".runtime" / "aoitalk" / "tool_logs")
    mcp.run()


if __name__ == "__main__":
    main()
