"""Launch the selected stdio profile without exposing unrelated credentials."""
import os
from pathlib import Path
import sys

# Bind discovery to this checkout, not an unrelated editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

profile = os.environ.get("HOME_MCP_PROFILE", "")
if profile:
    from home_mcp_gateway.local_agent_profile import PROFILE, clean_environment, main
    if profile != PROFILE:
        raise SystemExit("invalid HOME_MCP_PROFILE")
    environment = clean_environment(os.environ)
    os.environ.clear()
    os.environ.update(environment)
else:
    for name in ("CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY"):
        os.environ.pop(name, None)
    from home_mcp_gateway.server import main

if __name__ == "__main__":
    main()
