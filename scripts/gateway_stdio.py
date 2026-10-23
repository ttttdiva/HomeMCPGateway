"""Launch the selected stdio profile without exposing unrelated credentials."""
import faulthandler
import os
from pathlib import Path
import sys

# Native capture faults can terminate Python before tool telemetry completes.
# Keep fatal thread stacks on the existing stderr sink, never on MCP stdout.
try:
    faulthandler.enable(file=sys.stderr, all_threads=True)
except (OSError, RuntimeError, ValueError):
    pass  # Diagnostics must not prevent startup when stderr is unavailable.

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
    for name in ("CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY", "HOME_MCP_AOITALK_RUNTIME_API_KEY"):
        os.environ.pop(name, None)
    from home_mcp_gateway.server import main

if __name__ == "__main__":
    main()
