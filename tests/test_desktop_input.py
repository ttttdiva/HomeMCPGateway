"""Deterministic Win32 input construction/failure tests; no real desktop input."""
import ctypes
from contextlib import ExitStack
import unittest
from unittest.mock import MagicMock, patch

from home_mcp_gateway import desktop_input as d


class DesktopInputTests(unittest.TestCase):
    def test_input_abi_uses_fixed_width_win32_fields(self):
        self.assertEqual(ctypes.sizeof(d.INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertEqual(ctypes.sizeof(d.U32), 4)
        self.assertEqual(ctypes.sizeof(d.U16), 2)
        self.assertEqual(d.mouse(0x800).mi.dwFlags, 0x800)
        self.assertEqual(d.mouse(0x800, data=-120).mi.mouseData, 0xFFFFFF88)

    def test_negative_virtual_desktop_normalization(self):
        bounds = dict(left=-3840, top=-200, width=7280, height=2360)
        self.assertEqual(d.normalize(-3840, -200, bounds), (0, 0))
        self.assertEqual(d.normalize(3439, 2159, bounds), (65535, 65535))
        self.assertEqual(d.normalize(-99999, 99999, bounds), (0, 65535))
        nx, ny = d.normalize(-3644, 250, bounds)
        self.assertGreater(nx, 0)
        self.assertLess(nx, 65535)
        self.assertGreater(ny, 0)
        self.assertEqual(d.normalize(0, 0, dict(left=0, top=0, width=1, height=1)), (0, 0))
        with self.assertRaisesRegex(ValueError, 'Empty'):
            d.normalize(0, 0, dict(left=0, top=0, width=0, height=1))

    def test_unicode_literals_surrogate_pairs_and_releases(self):
        text = '日本語 +^%{x} 😀'
        events = d.unicode_inputs(text)
        raw = text.encode('utf-16-le')
        units = [int.from_bytes(raw[i:i + 2], 'little') for i in range(0, len(raw), 2)]
        self.assertEqual([e.ki.wScan for e in events[::2]], units)
        self.assertEqual(len(events), len(units) * 2)
        for down, up in zip(events[::2], events[1::2]):
            self.assertEqual(down.ki.wVk, 0)
            self.assertEqual(down.ki.dwFlags, d.UNICODE)
            self.assertEqual(up.ki.dwFlags, d.UNICODE | d.KEYUP)
            self.assertEqual(up.ki.wScan, down.ki.wScan)
        self.assertEqual(d.unicode_inputs(''), [])

    def user(self):
        user = MagicMock()
        user.VkKeyScanExW.side_effect = lambda char, layout: ord(char.upper())
        user.MapVirtualKeyExW.side_effect = lambda vk, mode, layout: vk
        user.GetForegroundWindow.return_value = 999
        user.GetWindowThreadProcessId.return_value = 77
        user.GetKeyboardLayout.return_value = 0x411
        return user

    def test_chords_named_keys_letters_numbers_extended_and_reverse_release(self):
        user = self.user()
        names = ['Enter', 'Tab', 'Escape', 'Backspace', 'Delete', 'Left', 'Right', 'Up', 'Down',
                 'Home', 'End', 'PageUp', 'PageDown', *[f'F{i}' for i in range(1, 25)], *'az019']
        for name in names:
            with self.subTest(key=name):
                events = d.chord_inputs(user, ['Ctrl', 'Shift', 'Alt', 'Win', name], 0x411)
                downs, ups = events[:5], events[5:]
                self.assertEqual([e.ki.wVk for e in downs[:4]], [0xA2, 0xA0, 0xA4, 0x5B])
                self.assertEqual([e.ki.wVk for e in ups], [e.ki.wVk for e in reversed(downs)])
                self.assertTrue(all(e.ki.dwFlags & d.KEYUP for e in ups))
                self.assertTrue(downs[3].ki.dwFlags & d.EXTENDED)
        self.assertEqual(d.keyboard_layout(user, 123), 0x411)
        user.GetWindowThreadProcessId.assert_called_with(123, None)
        user.GetKeyboardLayout.assert_called_with(77)
        self.assertTrue(all(call.args[2] == 0x411 for call in user.MapVirtualKeyExW.call_args_list))

    def test_keyboard_layout_modifiers_are_deduplicated(self):
        user = self.user()
        user.VkKeyScanExW.side_effect = None
        user.VkKeyScanExW.return_value = (6 << 8) | 0x51  # AltGr-style Ctrl+Alt+Q
        events = d.chord_inputs(user, ['Ctrl', '@'], 1234)
        self.assertEqual([e.ki.wVk for e in events[:3]], [0xA2, 0xA4, 0x51])
        user.VkKeyScanExW.assert_called_once_with('@', 1234)

    def test_invalid_and_unrepresentable_shortcuts_fail_before_input(self):
        user = self.user()
        for keys in ([], [''], ['F25'], ['😀'], [None]):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                d.chord_inputs(user, keys, 1)
        user.VkKeyScanExW.side_effect = None
        user.VkKeyScanExW.return_value = -1
        with self.assertRaisesRegex(ValueError, 'keyboard layout'):
            d.chord_inputs(user, ['日'], 1)
        user.SendInput.assert_not_called()

    def test_partial_insertion_releases_only_accepted_key_and_reports_identity(self):
        user = self.user()
        user.GetWindowThreadProcessId.side_effect = lambda hwnd, ptr: setattr(ptr._obj, 'value', 987)
        user.SendInput.side_effect = [1, 1]
        with ExitStack() as stack:
            stack.enter_context(patch.object(ctypes, 'set_last_error', create=True))
            stack.enter_context(patch.object(ctypes, 'get_last_error', return_value=5, create=True))
            stack.enter_context(patch.object(d, '_integrity', side_effect=[8192, 12288]))
            with self.assertRaises(RuntimeError) as raised:
                d.send(user, [d.key(0x11), d.key(0x41), d.key(0x41, flags=d.KEYUP), d.key(0x11, flags=d.KEYUP)], 123)
        message = str(raised.exception)
        for text in ('inserted 1/4', 'win32_error=5', 'target_pid=987', 'foreground_hwnd=999',
                     'Possible UIPI', 'does not establish', 'No action was retried'):
            self.assertIn(text, message)
        self.assertEqual(user.SendInput.call_count, 2)
        count, events, size = user.SendInput.call_args.args
        self.assertEqual(count, 1)
        self.assertEqual(events[0].ki.wVk, 0x11)
        self.assertEqual(events[0].ki.dwFlags, d.KEYUP)

    def test_drag_releases_button_even_when_movement_fails(self):
        user = self.user()
        sent = []
        def send(fake_user, events, hwnd):
            sent.extend(events)
            if len(sent) == 3:
                raise RuntimeError('movement failure')
            return len(events)
        with patch.object(d, 'configure'), patch.object(d, 'virtual_bounds', return_value=dict(left=0, top=0, width=500, height=500)), \
             patch.object(d, 'send', side_effect=send), patch.object(d.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'movement failure'):
                d.perform(user, 'drag', x=10, y=10, end_x=100, end_y=100, duration_ms=1)
        self.assertEqual(sent[-1].mi.dwFlags, d.BUTTONS['left'][1])

    def test_empty_send_and_successful_event_count(self):
        user = self.user()
        self.assertEqual(d.send(user, []), 0)
        user.SendInput.assert_not_called()
        user.SendInput.return_value = 2
        with patch.object(ctypes, 'set_last_error', create=True), \
             patch.object(ctypes, 'get_last_error', return_value=0, create=True):
            self.assertEqual(d.send(user, d.unicode_inputs('a')), 2)

    def test_click_and_double_click_are_atomic_input_batches(self):
        user = self.user()
        for action, count in [('click', 3), ('double_click', 5)]:
            with self.subTest(action=action), patch.object(d, 'configure'), \
                 patch.object(d, 'virtual_bounds', return_value=dict(left=-500, top=0, width=1000, height=500)), \
                 patch.object(d, 'send', return_value=count) as send:
                result = d.perform(user, action, x=-100, y=200)
                send.assert_called_once()
                events = send.call_args.args[1]
                self.assertEqual(len(events), count)
                self.assertEqual(events[0].mi.dwFlags, d.MOVE | d.ABSOLUTE | d.VIRTUALDESK)
                self.assertEqual([e.mi.dwFlags for e in events[1:]], [2, 4] * ((count - 1) // 2))
                self.assertEqual(result['events_inserted'], count)


class ComputerUseInputTests(unittest.TestCase):
    """Modifier-held pointer actions and key sequences used by DCC tools such as Blender."""

    def run_perform(self, action, **kwargs):
        user = DesktopInputTests.user(self)
        sent = []
        def send(fake_user, events, hwnd):
            sent.append(list(events))
            return len(events)
        with patch.object(d, 'configure'), patch.object(d, 'virtual_bounds', return_value=dict(left=0, top=0, width=500, height=500)), \
             patch.object(d, 'send', side_effect=send), patch.object(d.time, 'sleep'):
            result = d.perform(user, action, **kwargs)
        return sent, result

    def test_middle_drag_with_modifiers_holds_and_releases_in_order(self):
        sent, result = self.run_perform('drag', x=10, y=10, end_x=50, end_y=50, button='middle',
                                        duration_ms=32, modifiers=['Shift', 'Alt'])
        flat = [e for batch in sent for e in batch]
        keys = [(e.ki.wVk, bool(e.ki.dwFlags & d.KEYUP)) for e in flat if e.type == 1]
        self.assertEqual(keys, [(0xA0, False), (0xA4, False), (0xA4, True), (0xA0, True)])
        buttons = [e.mi.dwFlags for e in flat if e.type == 0 and e.mi.dwFlags & (0x0020 | 0x0040)]
        self.assertEqual(buttons, [0x0020, 0x0040])
        self.assertEqual(flat[0].type, 1)       # modifiers go down before any pointer event
        self.assertEqual(flat[-1].type, 1)      # and come up after the button release
        self.assertEqual(result['modifiers'], ['Shift', 'Alt'])

    def test_modifiers_are_released_when_the_action_fails(self):
        user = DesktopInputTests.user(self)
        sent = []
        def send(fake_user, events, hwnd):
            sent.extend(events)
            if any(e.type == 0 for e in events):
                raise RuntimeError('pointer failure')
            return len(events)
        with patch.object(d, 'configure'), patch.object(d, 'virtual_bounds', return_value=dict(left=0, top=0, width=500, height=500)), \
             patch.object(d, 'send', side_effect=send), patch.object(d.time, 'sleep'):
            with self.assertRaisesRegex(RuntimeError, 'pointer failure'):
                d.perform(user, 'click', x=1, y=1, modifiers=['Ctrl'])
        self.assertTrue(sent[-1].ki.dwFlags & d.KEYUP)
        self.assertEqual(sent[-1].ki.wVk, 0xA2)

    def test_modifiers_validated_before_any_input(self):
        user = DesktopInputTests.user(self)
        with patch.object(d, 'configure'), patch.object(d, 'virtual_bounds', return_value=dict(left=0, top=0, width=5, height=5)), \
             patch.object(d, 'send') as send:
            for bad in (['Hyper'], 'Ctrl', ['a']):
                with self.subTest(modifiers=bad), self.assertRaises(ValueError):
                    d.perform(user, 'click', x=1, y=1, modifiers=bad)
            with self.assertRaisesRegex(ValueError, 'pointer actions only'):
                d.perform(user, 'key', keys=['a'], modifiers=['Ctrl'])
        send.assert_not_called()

    def test_wheel_with_ctrl_and_numpad_keys(self):
        sent, _ = self.run_perform('scroll', x=5, y=5, delta_y=-240, modifiers=['Ctrl'])
        wheel = [e for batch in sent for e in batch if e.type == 0 and e.mi.dwFlags == 0x0800]
        self.assertEqual(len(wheel), 1)
        self.assertEqual(wheel[0].mi.mouseData, (-240) & 0xFFFFFFFF)
        user = DesktopInputTests.user(self)
        events = d.chord_inputs(user, ['NumPad1'], 0x409)
        self.assertEqual([e.ki.wVk for e in events], [0x61, 0x61])

    def test_press_key_sequence_presses_each_key_separately(self):
        sent, _ = self.run_perform('press_key', keys=['Down', 'Down', 'Enter'])
        self.assertEqual(len(sent), 3)
        self.assertEqual([batch[0].ki.wVk for batch in sent], [0x28, 0x28, 0x0D])
