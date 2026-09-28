"""Opt-in read-only Android runtime evidence; never installs or inputs on the phone."""
import base64
import io
import os
from pathlib import Path
import sys
import unittest

import anyio
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image


@unittest.skipUnless(os.environ.get("HOME_MCP_ADB_SERIAL"), "Set HOME_MCP_ADB_SERIAL to opt into real-device read-only QA")
class NativeAndroidTests(unittest.TestCase):
    def test_device_shell_logs_package_and_png_over_mcp(self):
        async def check():
            launcher = Path(__file__).resolve().parents[1] / "scripts" / "gateway_stdio.py"
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)])
            serial = os.environ["HOME_MCP_ADB_SERIAL"]
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    devices = await session.call_tool("adb_devices", {})
                    self.assertTrue(devices.structured_content["ok"], devices)
                    self.assertIn(serial, [d["serial"] for d in devices.structured_content["devices"]])
                    for name, args in (("adb_shell", {"command": "getprop ro.build.version.release"}),
                                       ("adb_logcat", {"lines": 10}), ("adb_package_info", {"package": "android"})):
                        result = await session.call_tool(name, {"serial": serial, **args})
                        self.assertFalse(result.is_error, result)
                        self.assertTrue(result.structured_content["ok"], result)
                    screenshot = await session.call_tool("adb_screenshot", {"serial": serial})
                    self.assertFalse(screenshot.is_error, screenshot)
                    block = next(c for c in screenshot.content if c.type == "image")
                    with Image.open(io.BytesIO(base64.b64decode(block.data))) as image:
                        image.load()
                        self.assertGreater(image.width, 100)
                        self.assertGreater(image.height, 100)
        anyio.run(check)
