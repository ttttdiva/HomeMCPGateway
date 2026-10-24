"""One-shot command-line entry point for the local resource queue."""
from __future__ import annotations

from pathlib import Path
import sys

# Bind to this checkout rather than an unrelated editable installation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from home_mcp_gateway.resource_queue_cli import main


if __name__ == "__main__":
    raise SystemExit(main())
