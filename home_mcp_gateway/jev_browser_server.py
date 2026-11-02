"""Jev Browser MCP surface.

Existing Edge access is broker-only: tools use the authenticated broker IPC and
its loopback Playwright endpoint. This server must not read Edge's DevTools
endpoint file or open a direct CDP connection to Edge.
"""
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

# This server exposes the explicit existing-Edge attach lifecycle, bounded
# direct element tools, and the Jev-assisted runner. Attach failures never
# switch browsers; the runner keeps its legacy sessionless isolated mode. No
# filesystem, shell, desktop, or ADB tools are exposed here.
TOOLS = [
    browser_qa.browser_attach_edge,
    browser_qa.browser_open_tab,
    browser_qa.browser_list_tabs,
    browser_qa.browser_select_tab,
    browser_qa.browser_close_tab,
    browser_qa.browser_click,
    browser_qa.browser_fill,
    browser_qa.browser_keyboard,
    browser_qa.browser_find,
    browser_qa.browser_drag,
    browser_qa.browser_set_viewport,
    browser_qa.browser_reset_viewport,
    browser_qa.browser_capture_frames_start,
    browser_qa.browser_capture_frames_stop,
    browser_qa.browser_track_boxes_start,
    browser_qa.browser_track_boxes_stop,
    browser_qa.browser_detach,
    browser_qa.browser_run_plan,
]
register_tools(mcp, TOOLS)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
