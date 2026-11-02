"""Claude-specific MCP URL must not affect Muse and must preserve tool coverage."""
from pathlib import Path
import sys

import pytest
pytest.importorskip("httpx")
from starlette.testclient import TestClient
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gateway_muse_http as server  # noqa: E402

PATH = "c" * 64
TOKEN = "d" * 64


def test_muse_default_and_claude_are_distinct(tmp_path):
    claude = server.build_app(PATH, TOKEN, audit_path=tmp_path / "claude.jsonl", prefix="/claude-mcp")
    muse = server.build_app(PATH, TOKEN, audit_path=tmp_path / "muse.jsonl")

    def check(app, correct, wrong):
        with TestClient(app) as client:
            headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
            msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "0.1"}}}
            response = client.post(correct, headers=headers, json=msg)
            assert response.status_code == 200
            assert response.json()["result"]["serverInfo"]["name"] == "Home MCP Gateway"
            missing = client.post(correct, json=msg)
            assert missing.status_code == 401
            other_prefix = client.post(wrong, headers=headers, json=msg)
            assert other_prefix.status_code == 404
    check(claude, f"/claude-mcp/{PATH}/mcp", f"/muse-mcp/{PATH}/mcp")
    check(muse, f"/muse-mcp/{PATH}/mcp", f"/claude-mcp/{PATH}/mcp")


def test_unknown_prefix_rejected(tmp_path):
    with pytest.raises(ValueError, match="invalid_mcp_prefix"):
        server.build_app(PATH, TOKEN, audit_path=tmp_path / "audit.jsonl", prefix="/not-allowed")
