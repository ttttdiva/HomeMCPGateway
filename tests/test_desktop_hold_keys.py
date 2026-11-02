"""Held-key safety and lifecycle tests using the production action with mocked Win32."""
import asyncio
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import MagicMock, patch

from mcp.server import MCPServer
from home_mcp_gateway import desktop as d
from home_mcp_gateway.qa_common import register_tools


class HeldKeyActionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.state = set()
        self.events = []
        self.key_details = []
        self.sent_hwnds = []
        self.user = MagicMock()
        self.user.GetForegroundWindow.return_value = 123
        self.user.GetAsyncKeyState.side_effect = lambda vk: 0x8000 if vk in self.state else 0
        self.user.MapVirtualKeyExW.side_effect = lambda vk, _mode, _layout: 0x1D if vk == 0xA2 else vk
        self.info = {
            "hwnd": 123, "pid": 456, "process_create_time": 1.0,
            "visible": True, "minimized": False,
            "bounds": {"left": 0, "top": 0, "right": 640, "bottom": 480,
                       "width": 640, "height": 480},
        }
        self.observation = {
            "window": deepcopy(self.info), "windows": [], "timestamp": d._now(), "mode": "window",
        }
        self.stack.enter_context(patch.object(d, "_OBSERVATIONS", d.OrderedDict({"obs-test": self.observation})))
        self.stack.enter_context(patch.object(d, "_user32", return_value=self.user))
        self.stack.enter_context(patch.object(d, "_physical_pixels", return_value=nullcontext()))
        self.stack.enter_context(patch.object(d, "_identity", side_effect=self.identity))
        self.stack.enter_context(patch.object(d, "_client_bounds", return_value=self.info["bounds"]))
        self.stack.enter_context(patch.object(d, "_is_cloaked", return_value=False))
        self.approved_process = d._approved_unreal_project_process
        self.stack.enter_context(patch.object(d, "_approved_unreal_project_process", return_value=True))
        self.stack.enter_context(patch.object(d.desktop_input, "configure", side_effect=lambda user: user))
        self.stack.enter_context(patch.object(d.desktop_input, "keyboard_layout", return_value=0x409))
        self.send = self.stack.enter_context(patch.object(d.desktop_input, "send", side_effect=self.fake_send))

    def identity(self, user, hwnd, expected_pid=None, previous=None):
        if hwnd != 123:
            raise ValueError("Window HWND no longer exists")
        current = deepcopy(self.info)
        if previous and current["process_create_time"] != previous.get("process_create_time"):
            raise ValueError("stale_observation: process identity changed")
        return current

    def fake_send(self, user, events, hwnd, **_kwargs):
        self.sent_hwnds.append(hwnd)
        self.events.extend((event.ki.wVk, bool(event.ki.dwFlags & d.desktop_input.KEYUP)) for event in events)
        for event in events:
            vk = event.ki.wVk
            self.key_details.append((vk, event.ki.wScan, event.ki.dwFlags))
            if event.ki.dwFlags & d.desktop_input.KEYUP:
                self.state.discard(vk)
            else:
                self.state.add(vk)
        return len(events)

    async def hold(self, **overrides):
        args = {
            "observation_id": "obs-test", "hwnd": 123, "expected_pid": 456,
            "expected_process_create_time": 1.0, "keys": ["w"], "duration_ms": 100,
        }
        args.update(overrides)
        return await d.desktop_hold_keys(**args)

    async def test_successful_single_and_chord_holds_elapsed_and_release_reverse(self):
        single = await self.hold()
        self.assertTrue(single["ok"])
        self.assertEqual(single["status"], "completed")
        self.assertGreaterEqual(single["held_duration_ms"], 95)
        self.assertTrue(single["release"]["verified"])
        self.assertEqual(self.events, [(0x57, False), (0x57, True)])

        self.events.clear()
        chord = await self.hold(keys=["Shift", "w", "space"], duration_ms=120)
        self.assertTrue(chord["ok"])
        self.assertGreaterEqual(chord["held_duration_ms"], 110)
        self.assertEqual(self.events, [
            (0xA0, False), (0x57, False), (0x20, False),
            (0x20, True), (0x57, True), (0xA0, True),
        ])

    async def test_left_ctrl_single_and_movement_chord_use_left_vk_and_reverse_release(self):
        self.assertIn("LeftCtrl/LCtrl; VK_LCONTROL", d.desktop_hold_keys.__doc__)
        self.assertIn("Right Ctrl and generic Ctrl aliases are refused", d.desktop_hold_keys.__doc__)
        single = await self.hold(keys=["LeftCtrl"])
        self.assertTrue(single["ok"])
        self.assertEqual(single["keys"], ["leftctrl"])
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])
        self.assertEqual(self.key_details, [(0xA2, 0x1D, 0), (0xA2, 0x1D, d.desktop_input.KEYUP)])
        self.assertEqual(self.sent_hwnds, [123, 123])

        self.events.clear()
        self.key_details.clear()
        self.sent_hwnds.clear()
        chord = await self.hold(keys=["lctrl", "w"], duration_ms=110)
        self.assertTrue(chord["ok"])
        self.assertEqual(chord["keys"], ["leftctrl", "w"])
        self.assertEqual(self.events, [(0xA2, False), (0x57, False), (0x57, True), (0xA2, True)])
        self.assertEqual([vk for vk, _scan, _flags in self.key_details], [0xA2, 0x57, 0x57, 0xA2])
        self.assertEqual(self.key_details[0], (0xA2, 0x1D, 0))
        self.assertEqual(self.key_details[-1], (0xA2, 0x1D, d.desktop_input.KEYUP))
        self.assertTrue(all(hwnd == 123 for hwnd in self.sent_hwnds))

    def test_registered_tool_schema_documents_left_ctrl_aliases_and_refusals(self):
        server = MCPServer("held-key-schema-test")
        register_tools(server, d.TOOLS)
        tool = next(tool for tool in server._tool_manager.list_tools() if tool.name == "desktop_hold_keys")
        key_description = tool.parameters["properties"]["keys"]["description"]
        self.assertIn("LeftCtrl/LCtrl", key_description)
        self.assertIn("right Ctrl", key_description)
        self.assertIn("Ctrl/Control without a side", key_description)

    async def test_rejects_stale_identity_unapproved_process_and_missing_foreground_before_input(self):
        stale = deepcopy(self.observation)
        stale["timestamp"] = (datetime.now(timezone.utc) - timedelta(seconds=16)).isoformat()
        with patch.dict(d._OBSERVATIONS, {"old": stale}):
            result = await self.hold(observation_id="old")
            self.assertEqual((result["status"], result["stop_reason"]), ("rejected", "stale_observation"))
        result = await self.hold(expected_process_create_time=2.0)
        self.assertEqual(result["stop_reason"], "identity_changed")
        result = await self.hold(expected_pid=999)
        self.assertEqual(result["stop_reason"], "identity_changed")
        result = await self.hold(hwnd=999)
        self.assertEqual(result["stop_reason"], "stale_observation")
        with patch.object(d, "_approved_unreal_project_process", return_value=False):
            result = await self.hold()
        self.assertEqual(result["stop_reason"], "unapproved_target")
        self.user.GetForegroundWindow.return_value = 0
        result = await self.hold()
        self.assertEqual(result["stop_reason"], "foreground_lost")
        self.assertEqual(self.send.call_count, 0)
        self.assertEqual(self.sent_hwnds, [])

    async def test_process_gate_binds_editor_project_and_packaged_executable_path(self):
        process = MagicMock()
        process.create_time.return_value = 1.0
        process.name.return_value = "UnrealEditor.exe"
        process.exe.return_value = r"C:\Program Files\Epic Games\UE_5.8\Engine\Binaries\Win64\UnrealEditor.exe"
        process.cmdline.return_value = [process.exe.return_value,
                                        r"D:\Dev\95_Unferat_Game\UnferatGame.uproject"]
        with patch.object(d, "_approved_unreal_project_process", wraps=self.approved_process), \
             patch.object(d.psutil, "Process", return_value=process):
            self.assertTrue(d._approved_unreal_project_process(456, 1.0))
            process.cmdline.return_value = [process.exe.return_value, r"D:\Dev\OtherGame\Other.uproject"]
            self.assertFalse(d._approved_unreal_project_process(456, 1.0))
            process.name.return_value = "UnferatGame.exe"
            process.exe.return_value = r"D:\Dev\95_Unferat_Game\Binaries\Win64\UnferatGame.exe"
            process.cmdline.return_value = [process.exe.return_value]
            self.assertTrue(d._approved_unreal_project_process(456, 1.0))
            process.exe.return_value = r"C:\Games\UnferatGame.exe"
            self.assertFalse(d._approved_unreal_project_process(456, 1.0))
            process.name.return_value = "OtherUnrealGame.exe"
            self.assertFalse(d._approved_unreal_project_process(456, 1.0))
            process.name.return_value = "UnrealEditor.exe"
            process.create_time.return_value = 2.0
            self.assertFalse(d._approved_unreal_project_process(456, 1.0))

    async def test_foreground_loss_after_down_stops_and_releases_owned_key(self):
        self.user.GetForegroundWindow.side_effect = [123, 123, 123, 0]
        result = await self.hold(keys=["leftctrl"])
        self.assertEqual((result["status"], result["stop_reason"]), ("aborted", "foreground_lost"))
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])
        self.assertTrue(result["release"]["verified"])

    async def test_pid_reuse_and_window_close_after_down_release_only_owned_keys(self):
        changed = deepcopy(self.info)
        changed["process_create_time"] = 2.0
        with patch.object(d, "_identity", side_effect=[
            deepcopy(self.info), deepcopy(self.info), deepcopy(self.info), changed,
        ]):
            result = await self.hold(keys=["leftctrl"])
        self.assertEqual((result["status"], result["stop_reason"]), ("aborted", "identity_changed"))
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])

        self.events.clear()
        self.state.clear()
        with patch.object(d, "_identity", side_effect=[
            deepcopy(self.info), deepcopy(self.info), deepcopy(self.info),
            ValueError("Window HWND 123 no longer exists"),
        ]):
            result = await self.hold(keys=["leftctrl"])
        self.assertEqual((result["status"], result["stop_reason"]), ("aborted", "window_closed"))
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])

    async def test_partial_chord_failure_releases_only_accepted_downs_in_reverse(self):
        calls = 0
        def partial(user, events, hwnd, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("mocked second down failure")
            return self.fake_send(user, events, hwnd)
        self.send.side_effect = partial
        result = await self.hold(keys=["leftctrl", "w"])
        self.assertEqual((result["status"], result["stop_reason"]), ("aborted", "input_error"))
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])
        self.assertEqual([item["key"] for item in result["release"]["keys"]], ["leftctrl"])

    async def test_preheld_user_key_and_invalid_chords_are_refused_without_input(self):
        self.state.add(0xA2)
        result = await self.hold(keys=["leftctrl"])
        self.assertEqual(result["stop_reason"], "preheld_user_key")
        self.assertEqual(self.send.call_count, 0)
        self.state.clear()
        for arguments in (
            {"duration_ms": 99}, {"duration_ms": 8001}, {"duration_ms": 4.5},
            {"keys": []}, {"keys": ["w", "a", "s", "d", "space"]},
            {"keys": ["Ctrl", "w"]}, {"keys": ["RightCtrl"]}, {"keys": ["RCtrl"]},
            {"keys": ["w", "W"]}, {"keys": ["Shift", "lshift"]},
            {"keys": ["leftctrl", "lctrl"]},
        ):
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                await self.hold(**arguments)
        self.assertEqual(self.send.call_count, 0)

    async def test_cancellation_and_framework_timeout_return_release_receipt(self):
        entered = asyncio.Event()
        blocker = asyncio.Event()
        async def blocked_sleep(_seconds):
            entered.set()
            await blocker.wait()
        with patch.object(d, "_held_key_sleep", side_effect=blocked_sleep):
            task = asyncio.create_task(self.hold(keys=["leftctrl"], duration_ms=100))
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            result = await task
        self.assertEqual((result["status"], result["stop_reason"]), ("cancelled", "cancelled"))
        self.assertTrue(result["release"]["verified"])
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])

        self.events.clear()
        self.state.clear()
        entered.clear()
        with patch.object(d, "_held_key_sleep", side_effect=blocked_sleep):
            result = await asyncio.wait_for(self.hold(keys=["leftctrl"], duration_ms=5000), timeout=0.02)
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.events, [(0xA2, False), (0xA2, True)])

    async def test_send_exception_and_unverified_keyup_never_claim_success(self):
        self.send.side_effect = RuntimeError("mocked SendInput failure")
        failed = await self.hold(keys=["leftctrl"])
        self.assertEqual(failed["status"], "failed")
        self.assertFalse(failed["input_started"])
        self.assertIsNone(failed["release"]["verified"])

        self.send.side_effect = None
        self.events.clear()
        self.state.clear()
        def stuck_keyup(user, events, hwnd, **_kwargs):
            if events[0].ki.dwFlags & d.desktop_input.KEYUP:
                self.events.append((events[0].ki.wVk, True))
                return 1  # Accepted by SendInput, but state deliberately remains down.
            return self.fake_send(user, events, hwnd)
        self.send.side_effect = stuck_keyup
        result = await self.hold(keys=["leftctrl"])
        self.assertFalse(result["ok"])
        self.assertEqual((result["status"], result["stop_reason"]), ("failed", "release_unverified"))
        self.assertFalse(result["release"]["verified"])

    async def test_concurrent_holds_and_other_input_are_blocked(self):
        entered = asyncio.Event()
        blocker = asyncio.Event()
        async def blocked_sleep(_seconds):
            entered.set()
            await blocker.wait()
        with patch.object(d, "_held_key_sleep", side_effect=blocked_sleep):
            task = asyncio.create_task(self.hold(duration_ms=100))
            await entered.wait()
            with self.assertRaisesRegex(RuntimeError, "another held-key operation"):
                await self.hold(keys=["a"])
            with self.assertRaisesRegex(RuntimeError, "desktop_busy"):
                d.desktop_act("key", keys=["a"])
            blocker.set()
            result = await task
        self.assertTrue(result["release"]["verified"])
        self.assertEqual(self.events, [(0x57, False), (0x57, True)])


if __name__ == "__main__":
    unittest.main()
