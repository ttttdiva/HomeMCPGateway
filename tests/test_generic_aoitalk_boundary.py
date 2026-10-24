"""Offline import/lifespan isolation and frozen pre-Phase-B catalog contracts."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "tests" / "fixtures" / "phase_b_catalog_contract.json"

# A fresh interpreter is required: other tests intentionally import shared AoiTalk
# clients, which would conceal a transitive dependency in the generic entry.
GENERIC_PROBE = r'''
import asyncio
import hashlib
import importlib.abc
import json
from pathlib import Path
import runpy
import sys
from unittest.mock import AsyncMock, patch

root = Path(sys.argv[1])
sys.path.insert(0, str(root))
contract = json.loads((root / "tests/fixtures/phase_b_catalog_contract.json").read_text(encoding="utf-8"))

class RejectAoiTalk(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("home_mcp_gateway.aoitalk"):
            raise AssertionError("generic imported AoiTalk: " + fullname)

sys.meta_path.insert(0, RejectAoiTalk())

async def check():
    from home_mcp_gateway import server
    tools = await server.mcp.list_tools()
    names = [tool.name for tool in tools]
    assert names == contract["generic_names"], names
    assert not any(name.startswith("aoitalk_") for name in names)
    assert {"job_start", "job_status", "job_tail", "job_wait", "job_stop",
            "job_list", "job_steer", "job_steer_status"} <= set(names)
    for tool in tools:
        data = tool.model_dump(mode="json")
        wire = {key: data[key] for key in ("input_schema", "output_schema", "annotations")}
        digest = hashlib.sha256(json.dumps(wire, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        assert digest == contract["generic_contract_sha256"][tool.name], tool.name

    with patch.object(server.browser_qa, "close_all", new_callable=AsyncMock) as close:
        async with server.lifespan(server.mcp) as state:
            assert state == {}
        close.assert_awaited_once_with()
        close.reset_mock()
        try:
            async with server.lifespan(server.mcp):
                raise RuntimeError("synthetic lifecycle failure")
        except RuntimeError as exc:
            assert str(exc) == "synthetic lifecycle failure"
        else:
            raise AssertionError("lifecycle swallowed failure")
        close.assert_awaited_once_with()
    assert not any(name.startswith("home_mcp_gateway.aoitalk") for name in sys.modules)
    print(json.dumps({"names": names, "aoi_modules": [], "cleanup_paths": 2}))

# Browser tooling may import dotenv for its own lazy configuration. Only reads
# during generic import/discovery/lifespan are forbidden by this probe.
with patch("dotenv.dotenv_values", side_effect=AssertionError("unexpected config read")), \
     patch("dotenv.load_dotenv", side_effect=AssertionError("unexpected config load")), \
     patch("httpx.AsyncClient.send", side_effect=AssertionError("unexpected HTTP")), \
     patch("httpx.Client.send", side_effect=AssertionError("unexpected HTTP")):
    runpy.run_path(str(root / "scripts/gateway_stdio.py"), run_name="offline_import_probe")
    asyncio.run(check())
'''


class GenericAoiTalkBoundaryTests(unittest.TestCase):
    def test_generic_catalog_import_and_lifespan_ignore_legacy_config(self):
        expected = json.loads(CONTRACT.read_text(encoding="utf-8"))
        for enabled in (None, "0", "1"):
            with self.subTest(legacy_enabled=enabled), tempfile.TemporaryDirectory() as cwd:
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith(("HOME_MCP_", "AOITALK_", "CONTROL_PLANE_", "OPENAI_"))}
                env.update(HOME_MCP_TOOL_LOG="0", HOME_MCP_AOITALK_URL="not-a-url",
                           HOME_MCP_AOITALK_ENV_FILE=str(Path(cwd) / "missing.env"))
                if enabled is not None:
                    env["HOME_MCP_AOITALK_ENABLED"] = enabled
                result = subprocess.run(
                    [sys.executable, "-c", GENERIC_PROBE, str(ROOT)], cwd=cwd,
                    env=env, capture_output=True, text=True, encoding="utf-8", timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                evidence = json.loads(result.stdout)
                self.assertEqual(evidence["names"], expected["generic_names"])
                self.assertEqual(evidence["aoi_modules"], [])
                self.assertEqual(evidence["cleanup_paths"], 2)


if __name__ == "__main__":
    unittest.main()
