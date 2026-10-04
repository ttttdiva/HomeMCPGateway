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


class ComputerUseCoordinateTests(unittest.TestCase):
    """window=/coordinate_space/modifiers/alias additions to desktop_act."""
    setUp = DesktopObservationTests.setUp
    observe = DesktopObservationTests.observe

    def kwargs(self):
        return self.perform.call_args.kwargs

    def test_window_space_is_relative_to_observed_client_origin(self):
        data = self.observe()
        d.desktop_act('click', observation_id=data['observation_id'], x=10, y=20, coordinate_space='window')
        self.assertEqual((self.kwargs()['x'], self.kwargs()['y']), (-182, 152))

    def test_normalized_spans_first_to_last_pixel_of_observed_image(self):
        data = self.observe()
        for (nx, ny), expected in [((0, 0), (-192, 132)), ((1, 1), (-89, 231)), ((0.5, 0.5), (-140, 182))]:
            with self.subTest(point=(nx, ny)):
                d.desktop_act('move', observation_id=data['observation_id'], x=nx, y=ny, coordinate_space='normalized')
                self.assertEqual((self.kwargs()['x'], self.kwargs()['y']), expected)

    def test_normalized_drag_converts_end_point_and_rejects_out_of_range(self):
        data = self.observe()
        d.desktop_act('drag', observation_id=data['observation_id'], x=0.0, y=0.0, end_x=1.0, end_y=1.0,
                      coordinate_space='normalized', button='middle')
        kw = self.kwargs()
        self.assertEqual((kw['end_x'], kw['end_y'], kw['button']), (-89, 231, 'middle'))
        self.perform.reset_mock()
        with self.assertRaisesRegex(ValueError, '0..1'):
            d.desktop_act('click', observation_id=data['observation_id'], x=1.2, y=0.5, coordinate_space='normalized')
        self.perform.assert_not_called()

    def test_window_and_normalized_without_a_reference_fail_before_input(self):
        for space in ('window', 'normalized'):
            with self.subTest(space=space), self.assertRaisesRegex(ValueError, 'requires a target window'):
                d.desktop_act('click', x=0.5, y=0.5, coordinate_space=space)
        with self.assertRaisesRegex(ValueError, 'coordinate_space'):
            d.desktop_act('click', x=1, y=1, coordinate_space='percent')
        self.perform.assert_not_called()

    def test_hwnd_without_observation_uses_live_client_area(self):
        d.desktop_act('click', hwnd=123, x=0.0, y=1.0, coordinate_space='normalized')
        self.assertEqual((self.kwargs()['x'], self.kwargs()['y']), (-192, 231))

    def test_screen_space_is_unchanged_and_accepts_floats(self):
        d.desktop_act('click', x=-12.4, y=7.6)
        self.assertEqual((self.kwargs()['x'], self.kwargs()['y']), (-12, 8))

    def test_aliases_map_to_canonical_actions(self):
        for alias, action, extra in [('right_click', 'click', {'button': 'right'}),
                                     ('middle_click', 'click', {'button': 'middle'}),
                                     ('type_text', 'text', {}), ('hotkey', 'key', {}), ('press_key', 'press_key', {})]:
            with self.subTest(alias=alias):
                self.perform.reset_mock()
                d.desktop_act(alias, x=1, y=2, text='a', keys=['a']) if alias.endswith('click') else \
                    d.desktop_act(alias, text='a', keys=['Ctrl', 'a'])
                args, kw = self.perform.call_args
                self.assertEqual(args[1], action)
                for key, value in extra.items():
                    self.assertEqual(kw[key], value)

    def test_modifiers_reach_input_and_are_pointer_only(self):
        d.desktop_act('scroll', x=5, y=6, delta_y=120, modifiers=['Ctrl', 'Shift'])
        self.assertEqual(self.kwargs()['modifiers'], ['Ctrl', 'Shift'])
        self.perform.reset_mock()
        with self.assertRaisesRegex(ValueError, 'pointer actions only'):
            d.desktop_act('hotkey', keys=['a'], modifiers=['Ctrl'])
        self.perform.assert_not_called()

    def test_window_name_selects_target_and_records_resolution(self):
        entry = dict(self.info, title='Blender 4.2', process_name='blender.exe', foreground=True)
        with patch.object(d, 'list_windows', return_value={'windows': [entry]}), \
             patch.object(d, '_is_cloaked', return_value=False):
            result = d.desktop_act('click', window='Blender', x=0.5, y=0.5, coordinate_space='normalized')
            self.assertEqual(result['hwnd'], 123)
            self.assertEqual(result['window_resolution']['matched_by'], 'process_name')
            with self.assertRaisesRegex(ValueError, 'window_not_found'):
                d.desktop_act('click', window='NoSuchApp', x=1, y=1)

    def test_observe_window_name_returns_geometry_and_png(self):
        entry = dict(self.info, title='Blender 4.2', process_name='blender.exe', foreground=True)
        with patch.object(d, 'list_windows', return_value={'windows': [entry]}), \
             patch.object(d, '_is_cloaked', return_value=False):
            result = d.desktop_observe(window='blender')
        meta = result.structured_content
        self.assertEqual(meta['mode'], 'window')
        self.assertEqual(meta['capture_backend'], 'printwindow')
        self.assertEqual(meta['window_resolution']['selected_hwnd'], 123)
        self.assertEqual(meta['window_geometry']['client_size'], {'width': 104, 'height': 100})
        self.assertEqual(meta['image_origin'], {'left': -192, 'top': 132})
        self.assertEqual([c.type for c in result.content], ['text', 'image'])
