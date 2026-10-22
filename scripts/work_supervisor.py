"""Entry point for the read-only work supervisor (run / once / status / ack / heartbeat)."""
from pathlib import Path
import sys

# Bind to this checkout, not an unrelated editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from home_mcp_gateway.work_supervisor import main

if __name__ == "__main__":
    raise SystemExit(main())
