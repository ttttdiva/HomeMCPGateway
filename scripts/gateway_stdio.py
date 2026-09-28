"""Launch the gateway without exposing tunnel authentication in tool environments."""
import os
from pathlib import Path
import sys

# Bind discovery to this checkout, not an unrelated editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

for name in ("CONTROL_PLANE_API_KEY", "OPENAI_ADMIN_KEY"):
    os.environ.pop(name, None)

from home_mcp_gateway.server import main

if __name__ == "__main__":
    main()
