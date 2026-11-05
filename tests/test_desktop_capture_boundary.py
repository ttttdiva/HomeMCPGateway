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

class WGCWorkerBoundaryTests(unittest.TestCase):
    """Only fixed synthetic Python children; no native desktop or network access."""
    def setUp(self):
        from home_mcp_gateway import desktop_capture as capture
        self.capture = capture
        self.children = []
        real_popen = capture.subprocess.Popen
        self.popen_type = real_popen
        def track(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            self.children.append(child)
            return child
        self.patcher = patch.object(capture.subprocess, "Popen", side_effect=track)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.addCleanup(self.assert_children_exited)

    def assert_children_exited(self):
        for child in self.children:
            self.assertIsNotNone(child.poll())

    def run_code(self, code, timeout=3):
        import sys
        return self.capture._run([sys.executable, "-B", "-c", code], 123, 456, timeout)

    def payload(self, image=None, **metadata):
        image = image or Image.new("RGB", (3, 2), "red")
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        return json.dumps(dict(hwnd=123, pid=456, png=base64.b64encode(stream.getvalue()).decode(), **metadata))

    def emit(self, payload):
        return self.run_code("import sys; sys.stdout.write(" + repr(payload) + ")")

    def test_success_and_untrusted_payloads(self):
        image = self.emit(self.payload())
        self.assertEqual((image.mode, image.size), ("RGB", (3, 2)))
        bad = ["{", json.dumps({"hwnd": 123, "pid": 456, "png": "!!!!"}),
               self.payload(Image.new("RGBA", (3, 2))),
               self.payload().replace('"hwnd": 123', '"hwnd": 124'),
               self.payload().replace('"pid": 456', '"pid": 457')]
        obj = json.loads(self.payload())
        raw = base64.b64decode(obj['png'])
        obj['png'] = base64.b64encode(raw[:30]).decode()
        bad.append(json.dumps(obj))
        for value in bad:
            with self.subTest(value=value[:50]), self.assertRaisesRegex(RuntimeError, "invalid payload"):
                self.emit(value)
        with patch.object(self.capture, "MAX_OUTPUT", 64):
            with self.assertRaisesRegex(RuntimeError, "failed"):
                self.run_code("import sys; sys.stdout.write('x' * 100000)")
        with patch.object(self.capture, "MAX_PIXELS", 2):
            with self.assertRaisesRegex(RuntimeError, "invalid payload"):
                self.emit(self.payload())
        with patch.object(self.capture, "MAX_PNG", 8):
            with self.assertRaisesRegex(RuntimeError, "invalid payload"):
                self.emit(self.payload())

    def test_nonzero_and_timeout_fall_back_to_nonuniform_printwindow(self):
        from contextlib import nullcontext
        info = dict(hwnd=123, pid=456, visible=True, minimized=False, bounds=bounds(0, 0, 3, 2))
        fallback = Image.new("RGB", (3, 2), "black")
        fallback.putpixel((0, 0), (255, 255, 255))
        for code, timeout in [("import os; os._exit(7)", 3), ("import time; time.sleep(10)", .15)]:
            with self.subTest(code=code), ExitStack() as stack:
                stack.enter_context(patch.object(d, "_user32", return_value=MagicMock()))
                stack.enter_context(patch.object(d, "_physical_pixels", side_effect=lambda _: nullcontext()))
                stack.enter_context(patch.object(d, "_window_info", return_value=info))
                stack.enter_context(patch.object(d, "_client_bounds", return_value=info['bounds']))
                stack.enter_context(patch.object(d, "_wgc_frame", side_effect=lambda *a: self.run_code(code, timeout)))
                stack.enter_context(patch.object(d.ImageGrab, "grab", return_value=fallback))
                result = d.capture_window(123, 456)
                data = result.structured_content
                self.assertEqual(data['capture_backend'], 'printwindow')
                self.assertFalse(data['uniform_image'])
                self.assertFalse(data['capture_attempts'][0]['ok'])
                with self.assertRaisesRegex(ValueError, 'capture_failed'):
                    d.capture_window(123, 456, 'wgc')

    def test_cancellation_kills_and_reaps_child(self):
        real_wait = self.popen_type.wait
        interrupted_once = [False]
        def interrupted(child, timeout=None):
            if timeout is not None and not interrupted_once[0]:
                interrupted_once[0] = True
                raise KeyboardInterrupt()
            return real_wait(child, timeout=timeout)
        with patch.object(self.popen_type, 'wait', new=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.run_code('import time; time.sleep(10)')

    def test_geometry_and_flat_reporting(self):
        info = dict(hwnd=123, pid=456, visible=True, minimized=False, bounds=bounds(0, 0, 3, 2))
        with ExitStack() as stack:
            stack.enter_context(patch.object(d, '_user32', return_value=MagicMock()))
            stack.enter_context(patch.object(d, '_physical_pixels', side_effect=lambda _: nullcontext()))
            window = stack.enter_context(patch.object(d, '_window_info', return_value=info))
            stack.enter_context(patch.object(d, '_client_bounds', return_value=info['bounds']))
            stack.enter_context(patch.object(d, '_dwm_frame', return_value=None))
            stack.enter_context(patch.object(d, '_wgc_frame', return_value=Image.new('RGB', (3, 2))))
            stack.enter_context(patch.object(d.ImageGrab, 'grab', return_value=Image.new('RGB', (3, 2))))
            data = d.capture_window(123, 456).structured_content
            self.assertTrue(data['uniform_image'])
            self.assertEqual([a['uniform_image'] for a in data['capture_attempts']], [True, True])
            window.side_effect = [info, {**info, 'bounds': bounds(0, 0, 4, 2)}]
            with self.assertRaisesRegex(ValueError, 'stale_observation'):
                d.capture_window(123, 456)
    def test_child_rejects_foreign_target_before_capture(self):
        import ctypes
        import sys
        from types import SimpleNamespace
        fake_user = MagicMock()
        fake_user.IsWindow.return_value = 1
        def foreign(hwnd, owner):
            owner._obj.value = 999
            return 1
        fake_user.GetWindowThreadProcessId.side_effect = foreign
        fake_capture = MagicMock()
        with patch.object(ctypes, 'WinDLL', return_value=fake_user), patch.dict(sys.modules, {
                'numpy': MagicMock(), 'windows_capture': SimpleNamespace(WindowsCapture=fake_capture)}):
            with self.assertRaisesRegex(RuntimeError, 'target changed'):
                self.capture._worker(123, 456)
        fake_capture.assert_not_called()

    def test_stdout_retaining_descendant_cleanup_on_all_exits(self):
        import os
        import psutil
        import sys
        import time
        original_read = os.read
        original_wait = self.popen_type.wait
        for scenario in ('exit', 'nonzero', 'timeout', 'oversize', 'cancel'):
            with self.subTest(scenario=scenario):
                seen = bytearray()
                cancelled = [False]
                def record(fd, count):
                    part = original_read(fd, count)
                    seen.extend(part)
                    return part
                def interrupt_after_identity(child, timeout=None):
                    if scenario == 'cancel' and b'\n' in seen and not cancelled[0]:
                        cancelled[0] = True
                        raise KeyboardInterrupt()
                    return original_wait(child, timeout=timeout)
                descendant = 'import time; time.sleep(30)'
                tail = {'exit': 'pass', 'nonzero': '__import__("os")._exit(7)', 'timeout': 'time.sleep(30)',
                        'oversize': "sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(30)",
                        'cancel': 'time.sleep(30)'}[scenario]
                code = ("import subprocess,sys,time,psutil; "
                        "p=subprocess.Popen([sys.executable,'-B','-c'," + repr(descendant) + "], "
                        "creationflags=subprocess.CREATE_NO_WINDOW); "
                        "print(str(p.pid)+','+repr(psutil.Process(p.pid).create_time()),flush=True); " + tail)
                start = time.monotonic()
                with patch.object(os, 'read', side_effect=record), \
                     patch.object(self.popen_type, 'wait', new=interrupt_after_identity), \
                     patch.object(self.capture, 'MAX_OUTPUT', 1024), \
                     patch.object(self.capture.threading, 'Thread', side_effect=AssertionError('reader thread forbidden')):
                    with self.assertRaises(KeyboardInterrupt if scenario == 'cancel' else RuntimeError):
                        self.run_code(code, 1.5 if scenario == 'timeout' else 3)
                self.assertLess(time.monotonic() - start, 5)
                self.assertIn(b'\n', seen, 'fixture must publish descendant identity before cleanup')
                pid_text, created_text = bytes(seen).split(b'\n', 1)[0].split(b',')
                pid, created = int(pid_text), float(created_text)
                end = time.monotonic() + 2
                while time.monotonic() < end:
                    try:
                        proc = psutil.Process(pid)
                        if proc.create_time() != created or not proc.is_running():
                            break
                    except psutil.NoSuchProcess:
                        break
                    time.sleep(.02)
                else:
                    self.fail('the owned descendant with the recorded creation time survived')
                if scenario == 'cancel':
                    self.assertTrue(cancelled[0])
