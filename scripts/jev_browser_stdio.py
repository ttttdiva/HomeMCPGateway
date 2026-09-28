"""Launch the Jev-only browser MCP for local Codex over stdio."""
import os
from pathlib import Path
import sys

# Bind discovery to this checkout, not an unrelated editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# The Jev API key is resolved inside the Gateway from its own .env. Do not
# inherit tunnel administration credentials into the local Codex MCP process.
for name in ("CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY"):
    os.environ.pop(name, None)

from home_mcp_gateway.jev_browser_server import main

if __name__ == "__main__":
    main()
