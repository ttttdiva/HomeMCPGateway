"""Launch only the dedicated AoiTalk MCP catalog and scrub runtime credentials."""
from __future__ import annotations

import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if os.environ.get("HOME_MCP_PROFILE"):
    raise SystemExit("HOME_MCP_PROFILE is not valid for the AoiTalk plugin entry")

# The tunnel runtime key belongs to tunnel-client only. Never forward it,
# control-plane credentials, or organization admin credentials into MCP tools.
for _name in (
    "CONTROL_PLANE_API_KEY",
    "OPENAI_ADMIN_KEY",
    "HOME_MCP_AOITALK_RUNTIME_API_KEY",
):
    os.environ.pop(_name, None)

root = Path(__file__).resolve().parents[1]
os.environ["HOME_MCP_TOOL_LOG_DIR"] = str(root / ".runtime" / "aoitalk" / "tool_logs")

from home_mcp_gateway.aoitalk_plugin_server import main


if __name__ == "__main__":
    main()
