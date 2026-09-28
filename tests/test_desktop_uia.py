"""UIA worker contract, bounded traversal and identity tests without COM."""
import json
from types import SimpleNamespace
import subprocess
import unittest
from unittest.mock import MagicMock, patch

from home_mcp_gateway import desktop_uia as u


class Element:
    def __init__(self, rid, name, role=50000, children=()):
        self.rid, self.children = rid, list(children)
        self.CurrentName, self.CurrentControlType = name, role
        self.CurrentAutomationId, self.CurrentProcessId, self.CurrentNativeWindowHandle = name + 'Id', 123, 456
        self.CurrentBoundingRectangle = SimpleNamespace(left=-200, top=10, right=-100, bottom=40)
        self.CurrentIsKeyboardFocusable, self.CurrentIsEnabled = True, True
        self.CurrentHasKeyboardFocus, self.CurrentIsOffscreen = False, False
        self.GetCurrentPropertyValue = MagicMock(return_value=False)
        self.SetFocus = MagicMock()

    def GetRuntimeId(self):
        return self.rid


class Walker:
    def __init__(self, root):
        self.siblings = {}
        def visit(parent):
            for i, child in enumerate(parent.children):
                self.siblings[id(child)] = parent.children[i + 1] if i + 1 < len(parent.children) else None
                visit(child)
        visit(root)
        self.GetFirstChildElement = MagicMock(side_effect=lambda e: e.children[0] if e.children else None)
        self.GetNextSiblingElement = MagicMock(side_effect=lambda e: self.siblings.get(id(e)))


class DesktopUIATests(unittest.TestCase):
    def setUp(self):
        self.apply = Element([42, 1], 'Apply')
        self.edit = Element([42, 2], 'Japanese 日本語', 50004)
        self.root = Element([42, 99], 'Fixture', 50032, [self.apply, self.edit])
        self.walker = Walker(self.root)
        self.automation = SimpleNamespace(ElementFromHandle=lambda hwnd: self.root, ControlViewWalker=self.walker)
        self.types = SimpleNamespace(UIA_ButtonControlTypeId=50000, UIA_EditControlTypeId=50004,
                                     UIA_WindowControlTypeId=50032, UIA_IsInvokePatternAvailablePropertyId=30031)
        self.request = dict(mode='observe', hwnd=456, expected_pid=123, max_nodes=50, max_depth=4, max_elements=80)

    def handle(self, **kwargs):
        return u._handle({**self.request, **kwargs}, self.automation, self.types)

    def test_filters_role_name_and_automation_id(self):
        for query, role, expected in [('appLYid', 'button', 'Apply'), ('日本語', 'Edit', 'Japanese 日本語')]:
            with self.subTest(query=query):
                data = self.handle(query=query, role=role)
                self.assertEqual([e['name'] for e in data['elements']], [expected])
                self.assertEqual(data['visited'], 3)
                self.assertFalse(data['truncated'])
                self.assertEqual(data['backend'], 'UIAutomationCore.COM')
                self.assertLess(data['elements'][0]['bounds']['left'], 0)
        self.assertFalse(any(call.args[0] is self.root for call in self.walker.GetNextSiblingElement.call_args_list))

    def test_max_depth_nodes_and_result_limits(self):
        for limits, count in [(dict(max_elements=1), 1), (dict(max_depth=0), 1), (dict(max_nodes=2), 2)]:
            with self.subTest(limits=limits):
                result = self.handle(**limits)
                self.assertEqual(len(result['elements']), count)
                self.assertTrue(result['truncated'])
        for bad in [dict(max_nodes=0), dict(max_depth=65), dict(max_elements=501), dict(mode='invalid')]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.handle(**bad)

    def test_action_revalidates_identity_and_executes_exact_pattern(self):
        entry = self.handle(query='Apply')['elements'][0]
        with patch.object(u, '_operate') as operate:
            data = self.handle(**{k: entry[k] for k in ('runtime_id', 'role', 'name', 'automation_id', 'process_id', 'native_hwnd')},
                               mode='act', action='invoke')
            self.assertTrue(data['ok'])
            operate.assert_called_once_with(self.apply, self.types, 'invoke', '')

    def test_missing_reused_identity_and_wrong_owner_never_invoke(self):
        entry = self.handle(query='Apply')['elements'][0]
        base = {k: entry[k] for k in ('runtime_id', 'role', 'name', 'automation_id', 'process_id', 'native_hwnd')}
        with patch.object(u, '_operate') as operate:
            for changed in [dict(runtime_id=[]), dict(runtime_id=[999]), dict(name='Replaced'), dict(process_id=987),
                            dict(native_hwnd=999), dict(expected_pid=987), dict(max_nodes=1)]:
                with self.subTest(changed=changed), self.assertRaisesRegex(ValueError, 'stale_observation'):
                    self.handle(**{**base, 'mode': 'act', 'action': 'invoke', **changed})
            operate.assert_not_called()

    def test_legacy_empty_and_duplicate_runtime_ids_are_not_actionable(self):
        self.apply.rid = []
        result = self.handle(query='Apply')['elements'][0]
        self.assertFalse(result['actionable'])
        self.assertEqual(result['actions'], [])
        self.apply.rid = self.edit.rid
        entries = self.handle()['elements'][1:]
        self.assertTrue(all(not e['actionable'] for e in entries))
        self.assertTrue(all(not e['actions'] for e in entries))

    def test_transient_element_failure_preserves_sibling_traversal(self):
        self.apply.GetRuntimeId = MagicMock(side_effect=RuntimeError('provider unavailable'))
        result = self.handle()
        self.assertEqual([e['name'] for e in result['elements']], ['Fixture', 'Japanese 日本語'])
        self.assertIn('provider unavailable', result['errors'][0])

    def test_all_action_methods_and_readonly_value(self):
        pattern = MagicMock()
        pattern.CurrentIsReadOnly = False
        with patch.object(u, '_pattern', return_value=pattern) as get:
            for action, (name, method) in u.METHODS.items():
                with self.subTest(action=action):
                    u._operate(self.apply, self.types, action, '日本語')
                    get.assert_called_with(self.apply, self.types, name)
                    getattr(pattern, method).assert_called_with(*(['日本語'] if action == 'set_value' else []))
            u._operate(self.apply, self.types, 'focus', '')
            self.apply.SetFocus.assert_called_once()
            pattern.CurrentIsReadOnly = True
            with self.assertRaisesRegex(ValueError, 'read-only'):
                u._operate(self.apply, self.types, 'set_value', 'x')
        with patch.object(u, '_pattern', return_value=None):
            with self.assertRaisesRegex(ValueError, 'not supported'):
                u._operate(self.apply, self.types, 'invoke', '')
        with self.assertRaisesRegex(ValueError, 'Unknown'):
            u._operate(self.apply, self.types, 'invalid', '')

    def test_pattern_state_readonly_and_focus_information(self):
        types = SimpleNamespace(UIA_IsValuePatternAvailablePropertyId=1,
                                UIA_IsTogglePatternAvailablePropertyId=2,
                                UIA_IsSelectionItemPatternAvailablePropertyId=3,
                                UIA_IsExpandCollapsePatternAvailablePropertyId=4,
                                UIA_IsSelectionPatternAvailablePropertyId=5)
        self.apply.GetCurrentPropertyValue.return_value = True
        pattern = SimpleNamespace(CurrentValue='日本語' * 1000, CurrentIsReadOnly=True, CurrentToggleState=1,
                                  CurrentIsSelected=True, CurrentExpandCollapseState=1, CurrentCanSelectMultiple=True)
        identity = u._identity(self.apply, {50000: 'Button'})
        with patch.object(u, '_pattern', return_value=pattern):
            data = u._inspect(self.apply, types, identity, 1, 0)
        self.assertEqual(len(data['state']['value']), 2048)
        self.assertNotIn('set_value', data['actions'])
        self.assertIn('focus', data['actions'])
        self.assertEqual(data['state']['toggle_state'], 'On')
        self.assertEqual(data['state']['expand_collapse_state'], 'Expanded')
        self.assertTrue(data['state']['selected'])
        self.assertTrue(data['state']['can_select_multiple'])

    def test_worker_launch_utf8_timeout_and_invalid_responses(self):
        with patch.object(u, 'os', SimpleNamespace(name='nt')), patch.object(u.subprocess, 'CREATE_NO_WINDOW', 0, create=True), \
             patch.object(u.subprocess, 'run') as run:
            run.return_value = SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr='')
            self.assertTrue(u.request({'text': '日本語'})['ok'])
            self.assertEqual(json.loads(run.call_args.kwargs['input']), {'text': '日本語'})
            self.assertEqual(run.call_args.kwargs['timeout'], 20)
            self.assertIn('desktop_uia.py', run.call_args.args[0][-1])
            for stdout, code, expected in [('[]', 0, 'invalid JSON'), ('not json', 0, 'invalid JSON'),
                                           ('{"ok":false,"error":"broken"}', 1, 'broken')]:
                run.return_value = SimpleNamespace(returncode=code, stdout=stdout, stderr='worker error')
                with self.assertRaisesRegex(RuntimeError, expected):
                    u.request({})
            run.side_effect = subprocess.TimeoutExpired('worker', 0.1)
            with self.assertRaisesRegex(RuntimeError, 'action may already have run'):
                u.request({}, timeout_sec=0.1)
            run.side_effect = OSError('cannot launch')
            with self.assertRaisesRegex(RuntimeError, 'Could not launch'):
                u.request({})

    def test_pointer_guard_accepts_only_target_or_child_and_does_not_invoke(self):
        entry = self.handle(query='Apply')['elements'][0]
        payload = {k: entry[k] for k in (
            'runtime_id', 'role', 'name', 'automation_id', 'process_id', 'native_hwnd', 'bounds')}
        self.types.tagPOINT = lambda x, y: (x, y)
        self.automation.ElementFromPoint = MagicMock(return_value=self.apply)
        self.automation.CompareElements = lambda left, right: left is right
        self.automation.RawViewWalker = SimpleNamespace(GetParentElement=lambda e: self.apply if e is self.edit else None)
        with patch.object(u, '_operate') as operate:
            for hit in (self.apply, self.edit):
                self.automation.ElementFromPoint.return_value = hit
                result = self.handle(**payload, mode='guard', x=-150, y=20)
                self.assertEqual(result['target_guard'], 'verified')
            self.automation.ElementFromPoint.return_value = self.root
            with self.assertRaisesRegex(ValueError, 'covered'):
                self.handle(**payload, mode='guard', x=-150, y=20)
            self.automation.ElementFromPoint.return_value = self.apply
            for change in ({'x': 10}, {'bounds': {}}, {'name': 'Changed'}, {'runtime_id': [999]}):
                with self.subTest(change=change), self.assertRaises(ValueError):
                    self.handle(**{**payload, 'mode': 'guard', 'x': -150, 'y': 20, **change})
            self.apply.CurrentIsEnabled = False
            with self.assertRaisesRegex(ValueError, 'unavailable'):
                self.handle(**payload, mode='guard', x=-150, y=20)
            operate.assert_not_called()
