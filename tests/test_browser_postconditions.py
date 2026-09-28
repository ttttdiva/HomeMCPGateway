"""Post-dispatch readback on real isolated Edge; no new agent/provider."""
import json
from pathlib import Path
import sys
import unittest
from urllib.parse import quote

from home_mcp_gateway import browser_qa as b
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


class BrowserPostconditionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sid = (await b.browser_start(channel='msedge'))['session_id']
        self.page = b._SESSIONS[self.sid]['page']
        await self.page.set_content("""<input id="value"><button id="save" onclick="window.count++; document.querySelector('#result').textContent='Saved'">Save</button><div id="result">Ready</div><input id="check" type="checkbox"><div id="editable" contenteditable="true"></div>""")
        await self.page.evaluate('window.count=0')

    async def asyncTearDown(self):
        await b.browser_close(self.sid)

    async def test_local_readback_and_no_implicit_submit(self):
        result = await b.browser_fill(self.sid, '#value', 'secret fixture 日本語😀')
        self.assertEqual(result['verification']['status'], 'verified')
        self.assertNotIn('secret fixture', json.dumps(result))
        self.assertEqual(await self.page.evaluate('window.count'), 0)
        result = await b.browser_fill(self.sid, '#editable', '日本語')
        self.assertEqual(result['verification']['status'], 'verified')

    async def test_controlled_input_revert_reports_failure_without_values(self):
        await self.page.locator('#value').evaluate("e => e.addEventListener('input', () => e.value = 'previous')")
        result = await b.browser_fill(self.sid, '#value', 'private-new-value', verify_timeout_sec=.15)
        self.assertTrue(result['executed'])
        self.assertEqual(result['verification']['status'], 'failed')
        self.assertNotIn('private-new-value', json.dumps(result))
        self.assertNotIn('previous', json.dumps(result))

    async def test_click_verifies_effect_and_does_not_repeat_failed_assertion(self):
        result = await b.browser_click(self.sid, '#save', expected_text='Saved', result_selector='#result')
        self.assertEqual(result['verification']['status'], 'verified')
        self.assertEqual(await self.page.evaluate('window.count'), 1)
        result = await b.browser_click(self.sid, '#save', expected_text='Missing', verify_timeout_sec=.15)
        self.assertEqual(result['verification']['status'], 'failed')
        self.assertEqual(await self.page.evaluate('window.count'), 2)
        checked = await b.browser_click(self.sid, '#check', expected_checked=True)
        self.assertEqual(checked['verification']['status'], 'verified')

    async def test_fill_transport_error_never_echoes_entered_value(self):
        with self.assertRaisesRegex(RuntimeError, 'browser_fill_outcome_unknown') as error:
            await b.browser_fill(self.sid, '#missing', 'private-input-must-not-echo', timeout_sec=.03)
        self.assertNotIn('private-input-must-not-echo', str(error.exception))

    async def test_verification_budget_includes_evaluation(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        async def stalled(*args):
            await asyncio.sleep(5)
        locator = SimpleNamespace(evaluate=stalled)
        with patch('playwright.async_api.expect', return_value=SimpleNamespace(to_have_js_property=AsyncMock())):
            result = await asyncio.wait_for(b._verify_effect(self.page, locator=locator, value='private', timeout=.01), timeout=.5)
        self.assertEqual(result['status'], 'failed')

    async def test_invalid_wait_budget_is_rejected_before_input(self):
        for budget in [0, 31, float('nan'), True]:
            with self.assertRaises(ValueError):
                await b.browser_click(self.sid, '#save', verify_timeout_sec=budget)
        self.assertEqual(await self.page.evaluate('window.count'), 0)


class BrowserPostconditionStdioTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_and_real_wire_roundtrip(self):
        root = Path(__file__).resolve().parents[1]
        params = StdioServerParameters(command=sys.executable, args=[str(root/'scripts/gateway_stdio.py')])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                tools = {tool.name: tool for tool in listing.tools}
                self.assertIn('expected_text', tools['browser_click'].input_schema['properties'])
                self.assertIn('verify_value', tools['browser_fill'].input_schema['properties'])
                self.assertIn('browser_run_plan', tools)
                self.assertIn('steps', tools['browser_run_plan'].input_schema['required'])
                started = await session.call_tool('browser_start', {'channel': 'msedge'})
                self.assertFalse(started.is_error)
                sid = started.structured_content['session_id']
                try:
                    await session.call_tool('browser_navigate', {'session_id': sid, 'url': 'data:text/html,'+quote('<input id="entry">')})
                    result = await session.call_tool('browser_fill', {'session_id': sid, 'selector': '#entry', 'value': '日本語'})
                    self.assertFalse(result.is_error)
                    self.assertEqual(result.structured_content['verification']['status'], 'verified')
                finally:
                    await session.call_tool('browser_close', {'session_id': sid})
