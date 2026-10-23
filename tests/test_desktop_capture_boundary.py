"""CPU-only capture/response/registry contracts; no native capture or input."""
import base64
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image

from home_mcp_gateway import desktop as d
from home_mcp_gateway.qa_common import register_tools


def bounds(left, top, width, height):
    return dict(left=left, top=top, right=left + width, bottom=top + height,
                width=width, height=height)


class DesktopCaptureBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(d.os.environ, {"HOME_MCP_TOOL_LOG": "0"}))
        self.stack.enter_context(patch.object(d, "_OBSERVATIONS", d.OrderedDict()))
        self.user = MagicMock()
        self.user.GetForegroundWindow.return_value = 0
        self.info = dict(hwnd=123, pid=456, process_create_time=1,
                         title="CPU fixture", visible=True, minimized=False,
                         bounds=bounds(-200, 100, 120, 140))
        self.client = bounds(-192, 132, 104, 100)
        self.stack.enter_context(patch.object(d, "_user32", return_value=self.user))
        self.stack.enter_context(patch.object(d, "_physical_pixels", side_effect=lambda _: nullcontext()))
        self.window_info = self.stack.enter_context(patch.object(d, "_window_info", return_value=self.info))
        self.stack.enter_context(patch.object(d, "_client_bounds", return_value=self.client))
        self.stack.enter_context(patch.object(d, "list_monitors", return_value={
            "monitors": [dict(monitor=1, **bounds(-3840, 0, 7280, 2160))]}))
        self.stack.enter_context(patch.object(d, "_window_geometry", return_value={"client_bounds": self.client}))
        self.stack.enter_context(patch.object(d, "_dwm_frame", return_value=None))
        frame = Image.new("RGB", (120, 140), "navy")
        frame.putpixel((10, 40), (255, 255, 255))
        self.wgc = self.stack.enter_context(patch.object(d, "_wgc_frame", return_value=frame))
        self.printwindow = self.stack.enter_context(patch.object(
            d.ImageGrab, "grab", return_value=Image.new("RGB", (104, 100), "green")))
        self.stack.enter_context(patch.object(d.desktop_input, "configure", side_effect=lambda user: user))
        self.activate = self.stack.enter_context(patch.object(
            d.desktop_input, "activate", side_effect=AssertionError("native input forbidden in CPU test")))
        self.perform = self.stack.enter_context(patch.object(
            d.desktop_input, "perform", side_effect=AssertionError("native input forbidden in CPU test")))
        self.server = MCPServer("CPU desktop capture boundary")
        register_tools(self.server, d.TOOLS)

    async def observe(self, **overrides):
        return await self.server.call_tool("desktop_observe", {
            "mode": "window", "hwnd": 123, "expected_pid": 456, **overrides})

    async def test_wgc_png_and_json_round_trip_publish_one_observation(self):
        result = await self.observe()
        self.assertFalse(result.is_error)
        wire = json.loads(result.model_dump_json(by_alias=True))
        metadata = wire["structuredContent"]
        self.assertEqual(json.loads(wire["content"][0]["text"]), metadata)
        self.assertEqual(metadata["capture_backend"], "wgc")
        self.assertEqual(metadata["gateway_pid"], d.os.getpid())
        self.assertEqual(metadata["image_origin"], {"left": -192, "top": 132})
        image = next(block for block in wire["content"] if block["type"] == "image")
        with Image.open(io.BytesIO(base64.b64decode(image["data"]))) as picture:
            picture.load()
            self.assertEqual(picture.size, (104, 100))
            self.assertEqual(picture.getpixel((2, 8)), (255, 255, 255))
        self.assertEqual(list(d._OBSERVATIONS), [metadata["observation_id"]])
        self.printwindow.assert_not_called()
        self.activate.assert_not_called()
        self.perform.assert_not_called()

    async def test_auto_fallback_preserves_failed_backend_diagnostic(self):
        self.wgc.side_effect = RuntimeError("CPU WGC backend failure")
        result = await self.observe()
        data = result.structured_content
        self.assertEqual(data["capture_backend"], "printwindow")
        self.assertEqual(data["capture_attempts"][0], {
            "backend": "wgc", "ok": False, "error": "RuntimeError: CPU WGC backend failure"})
        self.assertEqual(len(d._OBSERVATIONS), 1)

    async def test_all_backend_errors_propagate_without_publishing(self):
        self.wgc.side_effect = RuntimeError("CPU WGC backend failure")
        self.printwindow.side_effect = OSError("CPU PrintWindow failure")
        with self.assertRaises(ToolError) as caught:
            await self.observe()
        self.assertIn("capture_failed", str(caught.exception))
        self.assertIn("wgc: RuntimeError: CPU WGC backend failure", str(caught.exception))
        self.assertIn("printwindow: OSError: CPU PrintWindow failure", str(caught.exception))
        self.assertEqual(d._OBSERVATIONS, {})

    async def test_wrong_pid_fails_before_capture(self):
        with self.assertRaisesRegex(ToolError, "expected PID 999, found 456"):
            await self.observe(expected_pid=999)
        self.wgc.assert_not_called()
        self.printwindow.assert_not_called()
        self.assertEqual(d._OBSERVATIONS, {})

    async def test_process_reuse_during_capture_does_not_publish(self):
        changed = {**self.info, "process_create_time": 2}
        self.window_info.side_effect = [self.info, self.info, self.info, changed]
        with self.assertRaisesRegex(ToolError, "process identity changed"):
            await self.observe()
        self.assertEqual(d._OBSERVATIONS, {})

    async def test_json_failure_does_not_publish_or_evict_previous_observation(self):
        with patch.object(d, "_OBSERVATION_LIMIT", 1):
            result = await self.observe()
            before = deepcopy(d._OBSERVATIONS)
            # Capture succeeds; only the final observation metadata is invalid.
            with patch.object(d, "_window_geometry", return_value={"invalid": object()}):
                with self.assertRaisesRegex(ToolError, "TypeError: .*not JSON serializable") as caught:
                    await self.observe()
            self.assertIsInstance(caught.exception.__cause__.__cause__, TypeError)
            self.assertEqual(list(d._OBSERVATIONS), [result.structured_content["observation_id"]])
            self.assertEqual(d._OBSERVATIONS, before)

    async def test_response_construction_failure_does_not_publish(self):
        with patch.object(d, "CallToolResult", side_effect=RuntimeError("CPU response validation failure")):
            with self.assertRaisesRegex(ToolError, "CPU response validation failure"):
                await self.observe()
        self.assertEqual(d._OBSERVATIONS, {})

    async def test_fresh_process_registry_rejects_old_id_before_input(self):
        result = await self.observe()
        identifier = result.structured_content["observation_id"]
        # A different Gateway process starts with its own empty registry.
        with patch.object(d, "_OBSERVATIONS", d.OrderedDict()):
            with self.assertRaisesRegex(ToolError, "unknown/evicted observation_id or Gateway restarted"):
                await self.server.call_tool("desktop_act", {
                    "action": "click", "observation_id": identifier, "x": 0, "y": 0})
        self.activate.assert_not_called()
        self.perform.assert_not_called()

    def test_failed_post_action_observation_preserves_acceptance_without_retry(self):
        # Test only the response builder; no action is invoked, even on mocks.
        with patch.object(d, "desktop_observe", side_effect=RuntimeError("CPU capture failure")) as observe:
            result = d._action_response({"ok": True, "hwnd": 123, "target_pid": 456},
                                        observe_after=True, with_uia=False)
        self.assertEqual(result["execution_status"], "accepted")
        self.assertEqual(result["verification"]["status"], "unknown")
        self.assertEqual(result["observation_error"], "RuntimeError")
        self.assertEqual(result["retry_policy"], "observe_before_retry")
        observe.assert_called_once()
        self.activate.assert_not_called()
        self.perform.assert_not_called()
