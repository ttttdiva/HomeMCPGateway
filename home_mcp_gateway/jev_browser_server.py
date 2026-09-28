from __future__ import annotations

from contextlib import asynccontextmanager

from mcp.server import MCPServer

from . import browser_qa, telemetry
from .qa_common import register_tools


@asynccontextmanager
async def lifespan(server):
    try:
        yield {}
    finally:
        await browser_qa.close_all()


mcp = MCPServer("Home MCP Jev Browser", lifespan=lifespan, middleware=[telemetry.ToolTelemetryMiddleware()])

# Codex gets only the high-level Jev-assisted browser runner. The implementation
# is shared with the Full MCP server; no filesystem/shell/desktop/ADB tools are
# exposed by this server.
register_tools(mcp, [browser_qa.browser_run_plan])


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
