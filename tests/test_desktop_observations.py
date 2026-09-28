"""Observation integrity and exact PNG-origin regression tests."""
from contextlib import ExitStack, nullcontext
from copy import deepcopy
import unittest
from unittest.mock import MagicMock, patch

from PIL import Image
from home_mcp_gateway import desktop as d


def bounds(left, top, width, height):
    return dict(left=left, top=top, right=left + width, bottom=top + height, width=width, height=height)


class DesktopObservationTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(d, '_OBSERVATIONS', d.OrderedDict()))
        self.user = MagicMock()
        self.user.GetForegroundWindow.return_value = 123
        self.info = dict(hwnd=123, pid=456, process_create_time=1, minimized=False, visible=True,
                         bounds=bounds(-200, 100, 120, 140))
        self.client = bounds(-192, 132, 104, 100)
        self.monitors = {'monitors': [dict(monitor=1, **bounds(-3840, 0, 7280, 2160))]}
        for name, value in [('_user32', self.user), ('_physical_pixels', nullcontext()),
                            ('_window_info', self.info), ('_client_bounds', self.client), ('list_monitors', self.monitors)]:
            self.stack.enter_context(patch.object(d, name, return_value=value))
        self.stack.enter_context(patch.object(d.desktop_input, 'configure', side_effect=lambda u: u))
        self.activate = self.stack.enter_context(patch.object(d.desktop_input, 'activate'))
        self.perform = self.stack.enter_context(patch.object(d.desktop_input, 'perform', return_value={'backend': 'SendInput'}))
        self.grab = self.stack.enter_context(patch.object(d.ImageGrab, 'grab', return_value=Image.new('RGB', (104, 100))))

    def observe(self):
        return d.desktop_observe(mode='window', hwnd=123).structured_content

    def test_client_image_origin_is_not_outer_window_origin(self):
        data = self.observe()
        self.assertEqual(data['bounds'], self.client)
        self.assertEqual(data['image_origin'], {'left': -192, 'top': 132})
        self.assertEqual(data['window']['bounds'], self.info['bounds'])
        self.assertEqual(data['capture_area'], 'client')
        self.assertEqual((data['width'], data['height']), (104, 100))
        result = d.desktop_act('click', observation_id=data['observation_id'], x=-172, y=152)
        self.assertTrue(result['ok'])
        self.assertEqual(self.perform.call_args.kwargs['x'], -172)

    def test_outer_capture_and_unrecognized_capture_dimensions(self):
        self.grab.return_value = Image.new('RGB', (120, 140))
        data = self.observe()
        self.assertEqual(data['capture_area'], 'window')
        self.assertEqual(data['bounds'], self.info['bounds'])
        self.grab.return_value = Image.new('RGB', (99, 99))
        with self.assertRaisesRegex(ValueError, 'capture_geometry_mismatch'):
            self.observe()

    def test_capture_geometry_race_and_minimized_window(self):
        changed = deepcopy(self.info)
        changed['bounds'] = bounds(0, 0, 120, 140)
        with patch.object(d, '_window_info', side_effect=[self.info, changed]):
            with self.assertRaisesRegex(ValueError, 'stale_observation'):
                d.capture_window(123)
        self.info['minimized'] = True
        with self.assertRaisesRegex(ValueError, 'minimized'):
            self.observe()

    def test_moved_window_and_monitor_layout_reject_coordinate_input(self):
        data = self.observe()
        changed = deepcopy(self.info)
        changed['bounds'] = bounds(100, 200, 120, 140)
        with patch.object(d, '_window_info', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'window moved/resized'):
                d.desktop_act('click', observation_id=data['observation_id'], x=10, y=10)
        with patch.object(d, 'list_monitors', return_value={'monitors': []}):
            with self.assertRaisesRegex(ValueError, 'monitor layout changed'):
                d.desktop_act('scroll', observation_id=data['observation_id'], delta_y=120)
        self.perform.assert_not_called()

    def test_wrong_hwnd_process_reuse_pid_and_unknown_observation(self):
        data = self.observe()
        oid = data['observation_id']
        for arguments in [dict(observation_id=oid, hwnd=999), dict(observation_id=oid, expected_pid=999),
                          dict(observation_id='not-present')]:
            with self.subTest(arguments=arguments), self.assertRaisesRegex(ValueError, 'stale_observation'):
                d.desktop_act('text', text='never sent', **arguments)
        changed = {**self.info, 'process_create_time': 2}
        with patch.object(d, '_window_info', return_value=changed):
            with self.assertRaisesRegex(ValueError, 'process identity changed'):
                d.desktop_act('text', observation_id=oid, text='never sent')
        self.perform.assert_not_called()

    def test_observation_eviction(self):
        with patch.object(d, '_OBSERVATION_LIMIT', 2):
            first = self.observe()
            self.observe()
            self.observe()
            self.assertEqual(len(d._OBSERVATIONS), 2)
            with self.assertRaisesRegex(ValueError, 'unknown/evicted'):
                d.desktop_act('key', observation_id=first['observation_id'], keys=['Enter'])

    def test_uia_refs_cannot_be_reused_across_observations(self):
        entry = dict(ref='e0', runtime_id=[42, 1], role='Button', automation_id='apply', name='Apply',
                     process_id=456, native_hwnd=123, actionable=True)
        with patch.object(d.desktop_uia, 'request', side_effect=lambda payload: {'elements': [deepcopy(entry)]}) as worker:
            first = d.desktop_observe(mode='uia', hwnd=123).structured_content
            second = d.desktop_observe(mode='uia', hwnd=123).structured_content
            ref = first['uia']['elements'][0]['ref']
            self.assertNotEqual(ref, second['uia']['elements'][0]['ref'])
            with self.assertRaisesRegex(ValueError, 'unknown element_ref'):
                d.desktop_act('uia', observation_id=second['observation_id'], element_ref=ref)
            self.assertEqual(worker.call_count, 2)
            d.desktop_act('uia', observation_id=first['observation_id'], element_ref=ref, uia_action='invoke')
            self.assertEqual(worker.call_args.args[0]['runtime_id'], [42, 1])
            self.assertEqual(worker.call_args.args[0]['process_id'], 456)
            self.activate.assert_not_called()
            d.desktop_act('uia', observation_id=first['observation_id'], element_ref=ref, uia_action='focus')
            self.activate.assert_called_once_with(self.user, 123)

    def test_observe_validates_limits_and_required_hwnd(self):
        for arguments in [dict(mode='window'), dict(mode='uia'), dict(with_uia=True), dict(mode='invalid'),
                          dict(max_elements=0), dict(max_depth=65), dict(max_nodes=20001)]:
            with self.subTest(arguments=arguments), self.assertRaises(ValueError):
                d.desktop_observe(**arguments)

    def test_observe_after_returns_one_png_without_claiming_completion(self):
        data = self.observe()
        result = d.desktop_act('click', observation_id=data['observation_id'],
                               x=-172, y=152, observe_after=True)
        self.assertTrue(result.structured_content['ok'])
        self.assertEqual(result.structured_content['execution_status'], 'accepted')
        self.assertEqual(result.structured_content['verification']['status'], 'unknown')
        self.assertEqual(len([c for c in result.content if c.type == 'image']), 1)
        after = result.structured_content['observation_after']
        self.assertNotEqual(after['observation_id'], data['observation_id'])
        self.perform.assert_called_once()

    def test_observe_after_failure_preserves_input_acceptance(self):
        data = self.observe()
        with patch.object(d, 'desktop_observe', side_effect=RuntimeError('fixture')):
            result = d.desktop_act('text', observation_id=data['observation_id'],
                                   text='日本語😀', observe_after=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['verification']['status'], 'unknown')
        self.assertEqual(result['observation_error'], 'RuntimeError')
        self.assertNotIn('日本語', str(result))
        self.perform.assert_called_once()

    def test_pointer_guard_keeps_sendinput_and_checks_after_activation(self):
        entry = dict(ref='e0', runtime_id=[42, 1], role='Button', automation_id='apply', name='Apply',
                     process_id=456, native_hwnd=123, actionable=True,
                     bounds=bounds(-190, 140, 60, 20))
        with patch.object(d.desktop_uia, 'request',
                          return_value={'elements': [deepcopy(entry)]}) as worker:
            data = d.desktop_observe(mode='uia', hwnd=123).structured_content
            ref = data['uia']['elements'][0]['ref']
            worker.return_value = {'ok': True, 'target_guard': 'verified'}
            result = d.desktop_act('click', observation_id=data['observation_id'],
                                   target_guard_ref=ref, x=-170, y=150)
            self.assertEqual(result['target_guard'], 'verified')
            payload = worker.call_args.args[0]
            self.assertEqual(payload['mode'], 'guard')
            self.assertNotIn('action', payload)
            self.assertEqual(payload['bounds'], entry['bounds'])
            self.perform.assert_called_once()
            self.perform.reset_mock()
            worker.side_effect = RuntimeError('target_guard: covered')
            with self.assertRaisesRegex(RuntimeError, 'covered'):
                d.desktop_act('click', observation_id=data['observation_id'],
                              target_guard_ref=ref, x=-170, y=150)
            self.perform.assert_not_called()

    def test_guard_requires_explicit_current_ref_but_canvas_still_works(self):
        data = self.observe()
        with self.assertRaisesRegex(ValueError, 'target_guard_ref'):
            d.desktop_act('click', observation_id=data['observation_id'],
                          target_guard_ref='missing', x=-170, y=150)
        with self.assertRaisesRegex(ValueError, 'pointer action'):
            d.desktop_act('text', observation_id=data['observation_id'],
                          target_guard_ref='missing', text='not sent')
        self.perform.assert_not_called()
        result = d.desktop_act('click', observation_id=data['observation_id'], x=-170, y=150)
        self.assertEqual(result['target_guard'], 'not_requested')
        self.perform.assert_called_once()
