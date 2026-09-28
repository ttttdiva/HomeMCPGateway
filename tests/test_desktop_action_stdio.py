"""Verify new desktop action contracts over real MCP, without restarting Tunnel."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import time
import unittest

import anyio
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image


class DesktopActionStdioTests(unittest.TestCase):
    def test_mixed_response_schema_and_png(self):
        async def check():
            fixture = Path(__file__).with_name("stdio_action_fixture.py")
            params = StdioServerParameters(command=sys.executable, args=[str(fixture)])
            with anyio.fail_after(30):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listing = await session.list_tools()
                        tool = next(t for t in listing.tools if t.name == "desktop_act")
                        for key in ("target_guard_ref", "observe_after", "observe_with_uia"):
                            self.assertIn(key, tool.input_schema["properties"])
                        for observe_after in (False, True):
                            result = await session.call_tool("desktop_act", {
                                "action": "text", "hwnd": 123, "text": "fixture 日本語😀",
                                "observe_after": observe_after,
                            })
                            self.assertFalse(result.is_error, result)
                            data = result.structured_content
                            self.assertTrue(data["ok"])
                            self.assertEqual(data["verification"]["status"], "unknown")
                            self.assertNotIn("fixture 日本語", json.dumps(data, ensure_ascii=False))
                            images = [c for c in result.content if c.type == "image"]
                            self.assertEqual(len(images), int(observe_after))
                            if observe_after:
                                self.assertIn("observation_after", data)
                                with Image.open(io.BytesIO(base64.b64decode(images[0].data))) as image:
                                    self.assertEqual(image.size, (64, 48))
        anyio.run(check)

    @unittest.skipUnless(os.environ.get("HOME_MCP_ACTION_FIXTURE_DIR"),
                         "Requires an explicitly started native QA fixture")
    def test_real_guarded_sendinput_and_local_readback(self):
        directory = Path(os.environ["HOME_MCP_ACTION_FIXTURE_DIR"])
        state_path = directory / "state.json"

        def state():
            return json.loads(state_path.read_text(encoding="utf-8"))

        async def check():
            initial = state()
            target = {"hwnd": initial["hwnd"], "expected_pid": initial["pid"]}
            launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)])
            report = {"transport": "new source, separate MCP stdio", "steps": []}
            with anyio.fail_after(60):
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, args):
                            result = await session.call_tool(name, args)
                            self.assertFalse(result.is_error, result)
                            return result

                        listing = await session.list_tools()
                        tool = next(t for t in listing.tools if t.name == "desktop_act")
                        self.assertIn("target_guard_ref", tool.input_schema["properties"])
                        await call("desktop_act", {"action": "activate", **target})
                        observed = await call("desktop_observe", {
                            "mode": "window", "with_uia": True, "role": "Button",
                            "query": "Apply", **target,
                        })
                        data = observed.structured_content
                        self.assertEqual(data["window"]["title"], "Home MCP native QA fixture")
                        item = next(e for e in data["uia"]["elements"] if e["name"] == "Apply")
                        b = item["bounds"]
                        before = state()["clicks"]
                        result = await call("desktop_act", {
                            "action": "click", **target, "observation_id": data["observation_id"],
                            "target_guard_ref": item["ref"], "observe_after": True,
                            "observe_with_uia": True,
                            "x": (b["left"] + b["right"]) // 2,
                            "y": (b["top"] + b["bottom"]) // 2,
                        })
                        self.assertEqual(result.structured_content["target_guard"], "verified")
                        self.assertEqual(result.structured_content["backend"], "SendInput")
                        self.assertEqual(result.structured_content["verification"]["status"], "unknown")
                        self.assertEqual(len([c for c in result.content if c.type == "image"]), 1)
                        deadline = time.monotonic() + 3
                        while state()["clicks"] == before and time.monotonic() < deadline:
                            await anyio.sleep(.05)
                        self.assertEqual(state()["clicks"], before + 1)
                        report["steps"].append("guarded pointer produced exactly one GUI click")

                        # Wrong point is blocked by the guard, not sent to the application.
                        failed = await session.call_tool("desktop_act", {
                            "action": "click", **target, "observation_id": data["observation_id"],
                            "target_guard_ref": item["ref"], "x": b["right"] + 4, "y": b["top"],
                        })
                        self.assertTrue(failed.is_error)
                        self.assertEqual(state()["clicks"], before + 1)
                        report["steps"].append("out-of-target point blocked; no second click")
                        fresh = await call("desktop_observe", {
                            "mode": "uia", "query": "Literal text", "role": "Edit", **target,
                        })
                        entry = fresh.structured_content["uia"]["elements"][0]
                        await call("desktop_act", {
                            "action": "uia", "uia_action": "focus", **target,
                            "observation_id": fresh.structured_content["observation_id"],
                            "element_ref": entry["ref"],
                        })
                        await call("desktop_act", {"action": "text", "text": "日本語😀", **target})
                        await call("desktop_act", {"action": "key", "keys": ["Ctrl", "a"], **target})
                        await call("desktop_act", {"action": "text", "text": "置換確認😀", **target})
                        deadline = time.monotonic() + 3
                        while state()["text"] != "置換確認😀" and time.monotonic() < deadline:
                            await anyio.sleep(.05)
                        self.assertEqual(state()["text"], "置換確認😀")
                        report["steps"].append("Unicode/surrogates and Ctrl+A replacement verified")
                        final = await call("desktop_observe", {"mode": "window", **target})
                        image = next(c for c in final.content if c.type == "image")
                        (directory / "verified.png").write_bytes(base64.b64decode(image.data))
                        (directory / "verification.json").write_text(
                            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        anyio.run(check)
