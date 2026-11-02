import asyncio
from contextlib import asynccontextmanager
import json
import os
import time
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import AsyncMock, patch

from home_mcp_gateway import browser_agent, browser_qa, jev_browser_server, server
from qa_test_support import local_server


class FakePage:
    def __init__(self, target_id, url="https://user:secret@example.test/start?token=secret#fragment"):
        self.target_id = target_id
        self.url = url
        self.closed = False
        self.handlers = {}
        self.close_calls = 0
        self.goto_calls = []
        self.goto_error = None
        self.visibility_state = "visible"
        self.bring_to_front_calls = 0
        self.bring_to_front_sets_visible = True

    def is_closed(self):
        return self.closed

    def on(self, name, callback):
        self.handlers.setdefault(name, []).append(callback)

    def remove_listener(self, name, callback):
        callbacks = self.handlers.get(name, [])
        if callback in callbacks:
            callbacks.remove(callback)

    async def close(self):
        self.close_calls += 1
        self.closed = True

    async def goto(self, url, wait_until="domcontentloaded", timeout=30_000):
        self.goto_calls.append((url, wait_until, timeout))
        if self.goto_error is not None:
            raise self.goto_error
        self.url = url
        return SimpleNamespace(status=200)

    async def title(self):
        return "QA fixture"

    async def evaluate(self, script):
        if script != "document.visibilityState":
            raise AssertionError("only the fixed visibility probe is allowed")
        return self.visibility_state

    async def bring_to_front(self):
        self.bring_to_front_calls += 1
        if self.bring_to_front_sets_visible:
            self.visibility_state = "visible"

    def locator(self, selector):
        return FakeLocator(selector)


class FakeLocator:
    def __init__(self, selector):
        self.selector = selector

    async def inner_text(self, timeout=None):
        return "synthetic fixture"

    async def count(self):
        return 1

    async def evaluate_all(self, script, options):
        self.script = script
        self.options = options
        return [
            {"tag": "INPUT", "text": "csrf-canary", "attributes": {
                "id": "csrf", "value": "csrf-canary", "data-token": "csrf-canary",
            }},
            {"tag": "A", "text": "Fixture link", "attributes": {
                "href": "https://user:pass@example.test/path?access_token=href-canary#fragment",
                "data-token": "href-canary",
            }},
        ]


class FakeTargetSession:
    def __init__(self, page=None, *, version=False):
        self.page = page
        self.version = version
        self.detach_calls = 0

    async def send(self, method):
        if method == "Browser.getVersion" and self.version:
            return {"product": "Microsoft Edge/156.0.fixture"}
        if method == "Target.getTargetInfo" and self.page:
            return {"targetInfo": {"targetId": self.page.target_id}}
        raise AssertionError("unexpected CDP method")

    async def detach(self):
        self.detach_calls += 1


class FakeContext:
    def __init__(self, pages):
        self.pages = pages
        self.close_calls = 0
        self.new_page_calls = 0
        self.new_context_calls = 0

    async def new_cdp_session(self, page):
        return FakeTargetSession(page)

    async def close(self):
        self.close_calls += 1

    def set_default_timeout(self, timeout):
        self.default_timeout = timeout

    def on(self, event, callback):
        return None

    async def new_page(self):
        self.new_page_calls += 1
        page = FakePage(f"owned-target-{self.new_page_calls}", "about:blank")
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, context):
        self.contexts = [context]
        self.context = context
        self.mode = "attached"
        self.close_calls = 0
        self.new_context_calls = 0
        self.version = "156.0.fixture"
        self.connected = True

    def is_connected(self):
        return self.connected

    async def new_context(self, **kwargs):
        self.new_context_calls += 1
        return self.context

    async def new_browser_cdp_session(self):
        return FakeTargetSession(version=True)

    async def close(self):
        self.close_calls += 1
        self.connected = False
        if self.mode == "owned":
            for context in self.contexts:
                for page in list(context.pages):
                    await page.close()


class FakeChromium:
    def __init__(self, browser=None, error=None):
        self.browser = browser
        self.error = error
        self.connect_args = []
        self.protocol_connect_args = []
        self.launch_calls = 0
        self.launch_browser = None

    async def connect_over_cdp(self, endpoint, **kwargs):
        self.connect_args.append((endpoint, kwargs))
        if self.error:
            raise self.error
        return self.browser

    async def connect(self, endpoint, **kwargs):
        self.protocol_connect_args.append((endpoint, kwargs))
        if self.error:
            raise self.error
        self.browser.connected = True
        return self.browser

    async def launch(self, **kwargs):
        self.launch_calls += 1
        if self.launch_browser is None:
            raise AssertionError("attach must never launch a browser")
        self.launch_browser.mode = "owned"
        return self.launch_browser


class FakeDriver:
    def __init__(self, chromium):
        self.chromium = chromium
        self.stop_calls = 0

    async def stop(self):
        self.stop_calls += 1


class FakeStarter:
    def __init__(self, driver_factory):
        self.driver_factory = driver_factory
        self.drivers = []

    async def start(self):
        driver = self.driver_factory()
        self.drivers.append(driver)
        return driver


class ExistingEdgeAttachTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.page = FakePage("target-one")
        self.context = FakeContext([self.page])
        self.browser = FakeBrowser(self.context)
        self.chromium = FakeChromium(self.browser)
        self.playwright_starter = FakeStarter(lambda: FakeDriver(self.chromium))
        self.broker_endpoint = "ws://127.0.0.1:54123/123e4567-e89b-12d3-a456-426614174000"
        self.playwright_patch = patch(
            "playwright.async_api.async_playwright",
            return_value=self.playwright_starter,
        )
        self.playwright_patch.start()
        self._set_broker_mock(
            AsyncMock(return_value=(self.broker_endpoint, "Microsoft Edge/156.0.fixture"))
        )
        self.addCleanup(self.playwright_patch.stop)
        self.addCleanup(lambda: self.broker_patch.stop())

    def _set_broker_mock(self, mock):
        current = getattr(self, "broker_patch", None)
        if current is not None:
            current.stop()
        self.broker_patch = patch(
            "home_mcp_gateway.edge_cdp_broker.get_browser_endpoint",
            new=mock,
        )
        self.broker_patch.start()

    async def asyncTearDown(self):
        await browser_qa.close_all()

    @property
    def driver(self):
        return self.playwright_starter.drivers[-1]

    async def test_attach_lists_stable_handles_and_requires_explicit_selection(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        self.assertEqual(attached["mode"], "attached")
        self.assertEqual(attached["browser_version"], "Microsoft Edge/156.0.fixture")
        self.assertEqual(attached["selected_tab_handle"], "")
        self.assertEqual(attached["tab_count"], 1)
        handle = attached["tabs"][0]["tab_handle"]
        self.assertEqual(attached["tabs"][0]["url"], "https://example.test/start")
        endpoint, kwargs = self.chromium.protocol_connect_args[0]
        self.assertEqual(endpoint, self.broker_endpoint)
        self.assertEqual(kwargs, {"timeout": 5000})
        self.assertEqual(self.chromium.connect_args, [])
        self.assertEqual(self.context.new_page_calls, 0)
        self.assertEqual(self.context.new_context_calls, 0)
        self.assertEqual(self.chromium.launch_calls, 0)

        self.page.url = "https://example.test/next?auth=canary#private"
        listed = await browser_qa.browser_list_tabs(sid)
        self.assertEqual(listed["tabs"][0]["tab_handle"], handle)
        self.assertEqual(listed["tabs"][0]["url"], "https://example.test/next")
        self.assertNotIn("canary", str(listed))
        self.assertNotIn("private", str(listed))

        selected = await browser_qa.browser_select_tab(sid, handle)
        self.assertEqual(selected["selected_tab_handle"], handle)
        self.assertEqual(selected["url"], "https://example.test/next")
        self.assertEqual(len(self.page.handlers.get("console", [])), 1)
        first_plan = await browser_qa.browser_run_plan(steps=[{"action": "done"}], session_id=sid)
        second_plan = await browser_qa.browser_run_plan(steps=[{"action": "done"}], session_id=sid)
        self.assertEqual(first_plan["mode"], "attached")
        self.assertEqual(second_plan["mode"], "attached")
        self.assertEqual(first_plan["observation"]["text"], "")
        self.assertEqual(first_plan["observation"]["title"], "")
        self.assertEqual(browser_qa._SESSIONS[sid]["page"], self.page)
        read_plan = await browser_qa.browser_run_plan(steps=[{"action": "read"}], session_id=sid)
        self.assertEqual(read_plan["observation"]["text"], "synthetic fixture")

        console_canary = "console-token-canary"
        pageerror_canary = "page-password-canary"
        message = type("Message", (), {
            "type": "error",
            "text": f"console contains {console_canary}",
            "location": {"url": "https://user:pass@example.test/path?token=secret#fragment", "lineNumber": 3},
        })()
        for callback in self.page.handlers["console"]:
            callback(message)
        for callback in self.page.handlers["pageerror"]:
            callback(RuntimeError(pageerror_canary))
        errors = await browser_qa.browser_errors(sid)
        self.assertNotIn(console_canary, json.dumps(errors))
        self.assertNotIn(pageerror_canary, json.dumps(errors))
        self.assertNotIn("token=secret", json.dumps(errors))
        self.assertEqual(errors["console"][0]["text"], "[redacted]")

        popup = FakePage("target-two", "https://example.test/popup?secret=x")
        self.context.pages.append(popup)
        from_popup = await browser_agent._adopt_popup(browser_qa._SESSIONS[sid], [self.page])
        self.assertIs(from_popup, self.page)
        self.assertIs(browser_qa._SESSIONS[sid]["page"], self.page)
        popup_handles = await browser_qa.browser_list_tabs(sid)
        self.assertEqual(len(popup_handles["tabs"]), 2)
        popup_handle = next(row["tab_handle"] for row in popup_handles["tabs"] if row["url"].endswith("/popup"))
        await browser_qa.browser_select_tab(sid, popup_handle)
        self.assertIs(browser_qa._SESSIONS[sid]["page"], popup)
        self.assertEqual(self.page.handlers["console"], [])
        self.assertEqual(len(popup.handlers["console"]), 1)

        await browser_qa.browser_select_tab(sid, handle)
        self.page.closed = True
        with self.assertRaisesRegex(ValueError, "browser_tab_closed"):
            await browser_qa.browser_select_tab(sid, handle)
        with self.assertRaisesRegex(ValueError, "browser_tab_closed"):
            await browser_qa.browser_page_text(sid)
        await browser_qa.browser_detach(sid)

    async def test_detach_releases_only_the_client_connection(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        await browser_qa.browser_select_tab(sid, attached["tabs"][0]["tab_handle"])
        result = await browser_qa.browser_detach(sid)
        self.assertFalse(result["external_browser_closed"])
        self.assertTrue(result["shared_connection_retained"])
        self.assertEqual(self.browser.close_calls, 0)
        self.assertEqual(self.context.close_calls, 0)
        self.assertEqual(self.page.close_calls, 0)
        self.assertEqual(self.page.handlers["console"], [])
        self.assertEqual(self.driver.stop_calls, 0)
        self.assertTrue((await browser_qa.browser_detach(sid))["already_detached"])

        reattached = await browser_qa.browser_attach_edge()
        self.assertEqual(len(self.chromium.protocol_connect_args), 1)
        self.assertEqual(reattached["tabs"][0]["tab_handle"], attached["tabs"][0]["tab_handle"])
        self.assertTrue(reattached["shared_connection_reused"])

    async def test_concurrent_and_repeated_callers_share_one_connection_but_keep_ownership(self):
        endpoint_gate = asyncio.Event()
        calls = 0

        async def delayed_endpoint(timeout_sec):
            nonlocal calls
            calls += 1
            await endpoint_gate.wait()
            return self.broker_endpoint, "Microsoft Edge/156.0.fixture"

        self._set_broker_mock(AsyncMock(side_effect=delayed_endpoint))
        first = asyncio.create_task(browser_qa.browser_attach_edge())
        await asyncio.sleep(0)
        second = asyncio.create_task(browser_qa.browser_attach_edge())
        await asyncio.sleep(0)
        endpoint_gate.set()
        a, b = await asyncio.gather(first, second)
        self.assertEqual(calls, 1)
        self.assertEqual(len(self.chromium.protocol_connect_args), 1)
        self.assertNotEqual(a["session_id"], b["session_id"])
        left = await browser_qa.browser_open_tab(a["session_id"])
        with self.assertRaisesRegex(ValueError, "browser_tab_not_owned"):
            await browser_qa.browser_close_tab(b["session_id"], left["tab_handle"])
        self.assertFalse(self.context.pages[-1].closed)
        await browser_qa.browser_close_tab(a["session_id"], left["tab_handle"])
        await browser_qa.browser_detach(a["session_id"])
        again = await browser_qa.browser_attach_edge()
        self.assertEqual(len(self.chromium.protocol_connect_args), 1)
        self.assertTrue(again["shared_connection_reused"])
        self.assertEqual(self.chromium.connect_args, [])

    async def test_open_tab_selects_new_owned_page_for_navigation_and_only_owned_tab_closes(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        existing_handle = attached["tabs"][0]["tab_handle"]
        existing_page = self.page
        await browser_qa.browser_select_tab(sid, existing_handle)

        opened = await browser_qa.browser_open_tab(sid)
        owned_handle = opened["tab_handle"]
        owned_page = self.context.pages[-1]
        self.assertEqual(opened["mode"], "attached")
        self.assertEqual(opened["url"], "about:blank")
        self.assertTrue(opened["selected"])
        self.assertTrue(opened["owned_by_session"])
        self.assertNotEqual(owned_handle, existing_handle)
        self.assertIs(browser_qa._SESSIONS[sid]["page"], owned_page)
        self.assertEqual(browser_qa._SESSIONS[sid]["selected_tab_handle"], owned_handle)
        self.assertEqual(self.context.new_page_calls, 1)
        self.assertEqual(self.context.new_context_calls, 0)

        navigated = await browser_qa.browser_navigate(sid, "http://127.0.0.1:3002/at/login")
        self.assertEqual(navigated["status"], 200)
        self.assertEqual(owned_page.goto_calls, [("http://127.0.0.1:3002/at/login", "domcontentloaded", 30_000)])
        self.assertEqual(existing_page.goto_calls, [])
        owned_page.url = "https://example.test/fixture/after?secret=value#fragment"
        listed = await browser_qa.browser_list_tabs(sid)
        owned_listing = next(row for row in listed["tabs"] if row["tab_handle"] == owned_handle)
        self.assertEqual(owned_listing["url"], "https://example.test/fixture/after")
        self.assertEqual(owned_listing["tab_handle"], owned_handle)
        self.assertNotIn("secret", json.dumps(listed))
        self.assertNotIn("fragment", json.dumps(listed))
        plan = await browser_qa.browser_run_plan(steps=[{"action": "done"}], session_id=sid)
        self.assertEqual(plan["mode"], "attached")
        self.assertIs(browser_qa._SESSIONS[sid]["page"], owned_page)

        with self.assertRaisesRegex(ValueError, "browser_tab_not_owned"):
            await browser_qa.browser_close_tab(sid, existing_handle)
        self.assertFalse(existing_page.closed)
        self.assertEqual(existing_page.close_calls, 0)

        closed = await browser_qa.browser_close_tab(sid, owned_handle)
        self.assertTrue(closed["closed"])
        self.assertFalse(closed["already_closed"])
        self.assertTrue(owned_page.closed)
        self.assertEqual(owned_page.close_calls, 1)
        self.assertIsNone(browser_qa._SESSIONS[sid]["page"])
        self.assertEqual(browser_qa._SESSIONS[sid]["selected_tab_handle"], owned_handle)
        listed_after_close = await browser_qa.browser_list_tabs(sid)
        self.assertTrue(listed_after_close["selected_tab_closed"])
        self.assertEqual([row["tab_handle"] for row in listed_after_close["tabs"]], [existing_handle])
        with self.assertRaisesRegex(ValueError, "browser_tab_closed"):
            await browser_qa.browser_page_text(sid)
        repeated = await browser_qa.browser_close_tab(sid, owned_handle)
        self.assertTrue(repeated["already_closed"])
        self.assertFalse(existing_page.closed)
        await browser_qa.browser_detach(sid)

    async def test_open_tab_is_attached_only_and_both_surfaces_register_owned_tab_tools(self):
        self.assertIn(browser_qa.browser_open_tab, browser_qa.TOOLS)
        self.assertIn(browser_qa.browser_close_tab, browser_qa.TOOLS)
        self.assertIn(browser_qa.browser_drag, browser_qa.TOOLS)
        self.assertIn(browser_qa.browser_open_tab, jev_browser_server.TOOLS)
        self.assertIn(browser_qa.browser_close_tab, jev_browser_server.TOOLS)
        self.assertIn(browser_qa.browser_drag, jev_browser_server.TOOLS)

        self.chromium.launch_browser = FakeBrowser(FakeContext([]))
        started = await browser_qa.browser_start(headless=True)
        with self.assertRaisesRegex(ValueError, "browser_session_not_attached"):
            await browser_qa.browser_open_tab(started["session_id"])
        await browser_qa.browser_close(started["session_id"])

    async def test_browser_drag_is_registered_on_gateway_and_jev_mcp_surfaces(self):
        gateway_tools = await server.mcp.list_tools()
        jev_tools = await jev_browser_server.mcp.list_tools()
        expected = {
            "browser_drag", "browser_set_viewport", "browser_reset_viewport",
            "browser_capture_frames_start", "browser_capture_frames_stop",
            "browser_track_boxes_start", "browser_track_boxes_stop",
        }
        self.assertTrue(expected <= {tool.name for tool in gateway_tools})
        self.assertTrue(expected <= {tool.name for tool in jev_tools})

    async def test_jev_direct_element_tools_guard_owned_selection_and_url(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        existing_handle = attached["tabs"][0]["tab_handle"]
        await browser_qa.browser_select_tab(sid, existing_handle)
        target = SimpleNamespace(click=AsyncMock(), fill=AsyncMock())
        page = browser_qa._SESSIONS[sid]["page"]
        page.keyboard = SimpleNamespace(press=AsyncMock(), insert_text=AsyncMock())
        with patch.object(browser_qa, "_filtered_element_locator", return_value=target) as locator:
            calls = (
                (browser_qa.browser_click, {"selector": "button", "guard_url_prefix": "https://example.test"}, "browser_click_tab_not_owned"),
                (browser_qa.browser_fill, {"selector": "textarea", "value": "fill-canary", "guard_url_prefix": "https://example.test"}, "browser_fill_tab_not_owned"),
                (browser_qa.browser_keyboard, {"key": "Enter", "guard_url_prefix": "https://example.test"}, "browser_keyboard_tab_not_owned"),
                (browser_qa.browser_find, {"selector": "button", "guard_url_prefix": "https://example.test"}, "browser_find_tab_not_owned"),
            )
            for tool, arguments, reason in calls:
                with self.subTest(tool=tool.__name__), self.assertRaisesRegex(ValueError, reason):
                    await tool(session_id=sid, **arguments)
            locator.assert_not_called()
            target.click.assert_not_awaited()
            target.fill.assert_not_awaited()
            page.keyboard.press.assert_not_awaited()

            opened = await browser_qa.browser_open_tab(sid)
            owned_handle = opened["tab_handle"]
            owned_page = browser_qa._SESSIONS[sid]["page"]
            owned_page.keyboard = SimpleNamespace(press=AsyncMock(), insert_text=AsyncMock())
            fixture_url = "http://127.0.0.1:3002/jev-owned-fixture"
            await browser_qa.browser_navigate(sid, fixture_url)

            wrong_guards = (
                (browser_qa.browser_click, {"selector": "button", "guard_url_prefix": "http://127.0.0.1:3002/other"}, "browser_click_url_mismatch"),
                (browser_qa.browser_fill, {"selector": "textarea", "value": "value-canary", "guard_url_prefix": "http://127.0.0.1:3002/other"}, "browser_fill_url_mismatch"),
                (browser_qa.browser_keyboard, {"key": "Enter", "guard_url_prefix": "http://127.0.0.1:3002/other"}, "browser_keyboard_url_mismatch"),
                (browser_qa.browser_find, {"selector": "button", "guard_url_prefix": "http://127.0.0.1:3002/other"}, "browser_find_url_mismatch"),
            )
            for tool, arguments, reason in wrong_guards:
                with self.subTest(tool=tool.__name__), self.assertRaisesRegex(ValueError, reason):
                    await tool(session_id=sid, **arguments)
            locator.assert_not_called()
            target.click.assert_not_awaited()
            target.fill.assert_not_awaited()
            owned_page.keyboard.press.assert_not_awaited()

            with self.assertRaisesRegex(ValueError, "browser_click_url_guard_required"):
                await browser_qa.browser_click(sid, "button")
            locator.assert_not_called()

            click = await browser_qa.browser_click(
                sid, "button", within="Fixture region", test_id="send", nth=0,
                guard_url_prefix=fixture_url,
            )
            self.assertTrue(click["executed"])
            target.click.assert_awaited_once()

            fill_value = "fill-canary-秘密"
            fill = await browser_qa.browser_fill(
                sid, "textarea", fill_value, verify_value=False,
                within="css:#fixture", test_id="composer", nth=0,
                guard_url_prefix=fixture_url,
            )
            self.assertTrue(fill["executed"])
            self.assertNotIn(fill_value, json.dumps(fill, ensure_ascii=False))
            target.fill.assert_awaited_once_with(fill_value, timeout=30_000)

            keyboard = await browser_qa.browser_keyboard(
                sid, key="Enter", guard_url_prefix=fixture_url
            )
            self.assertTrue(keyboard["sent"])
            owned_page.keyboard.press.assert_awaited_once_with("Enter")

            found = await browser_qa.browser_find(
                sid, "input", guard_url_prefix=fixture_url
            )
            self.assertEqual(found["count"], 1)
            self.assertNotIn("csrf-canary", json.dumps(found))
            await browser_qa.browser_close_tab(sid, owned_handle)
            self.assertFalse(self.page.closed)
            self.assertEqual(self.page.close_calls, 0)
            self.assertEqual(self.page.goto_calls, [])
        await browser_qa.browser_detach(sid)

    async def _owned_click_fixture(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        opened = await browser_qa.browser_open_tab(sid)
        page = browser_qa._SESSIONS[sid]["page"]
        page.url = "http://127.0.0.1:3002/fixture"
        locator = SimpleNamespace(click=AsyncMock(return_value=None))
        patcher = patch.object(browser_qa, "_filtered_element_locator", return_value=locator)
        patcher.start()
        self.addCleanup(patcher.stop)
        return sid, opened["tab_handle"], page, locator

    async def test_hidden_owned_tab_is_foregrounded_before_click(self):
        sid, _handle, page, locator = await self._owned_click_fixture()
        page.visibility_state = "hidden"
        result = await browser_qa.browser_click(
            sid, "button", timeout_sec=2,
            guard_url_prefix="http://127.0.0.1:3002/fixture",
        )
        locator.click.assert_awaited_once()
        self.assertEqual(page.bring_to_front_calls, 1)
        self.assertEqual(page.visibility_state, "visible")
        self.assertEqual(result["tab_visibility"]["initial_state"], "hidden")
        self.assertEqual(result["tab_visibility"]["final_state"], "visible")

    async def test_hidden_owned_tab_retries_only_before_click_execution(self):
        sid, _handle, page, locator = await self._owned_click_fixture()

        async def click(*, button, timeout):
            if locator.click.await_count == 1:
                page.visibility_state = "hidden"
                raise TimeoutError(
                    "Timeout 3000ms exceeded\nCall log:\n"
                    "  - waiting for element to be visible, enabled and stable"
                )

        locator.click.side_effect = click
        result = await browser_qa.browser_click(
            sid, "button", timeout_sec=5,
            guard_url_prefix="http://127.0.0.1:3002/fixture",
        )
        self.assertEqual(locator.click.await_count, 2)
        self.assertEqual(page.bring_to_front_calls, 1)
        self.assertEqual(page.visibility_state, "visible")
        self.assertTrue(result["executed"])

    async def test_visible_slice_timeout_before_input_keeps_waiting_until_deadline(self):
        sid, _handle, page, locator = await self._owned_click_fixture()

        async def click(*, button, timeout):
            if locator.click.await_count == 1:
                raise TimeoutError(
                    "Timeout 3000ms exceeded\nCall log:\n"
                    "  - waiting for element to be visible, enabled and stable"
                )

        locator.click.side_effect = click
        result = await browser_qa.browser_click(
            sid, "button", timeout_sec=5,
            guard_url_prefix="http://127.0.0.1:3002/fixture",
        )
        self.assertEqual(locator.click.await_count, 2)
        self.assertEqual(page.bring_to_front_calls, 0)
        self.assertTrue(result["executed"])

    async def test_visible_timeout_without_input_stops_at_the_overall_deadline(self):
        sid, _handle, _page, locator = await self._owned_click_fixture()
        locator.click.side_effect = TimeoutError(
            "Timeout 200ms exceeded\nCall log:\n"
            "  - waiting for element to be visible, enabled and stable"
        )
        started = browser_qa.time.monotonic()
        with self.assertRaisesRegex(
                RuntimeError,
                "browser_action_failed: TimeoutError: Timed out waiting for the element to be visible, enabled and stable",
        ):
            await browser_qa.browser_click(
                sid, "button", timeout_sec=0.2,
                guard_url_prefix="http://127.0.0.1:3002/fixture",
            )
        self.assertLess(browser_qa.time.monotonic() - started, 1.0)
        self.assertGreaterEqual(locator.click.await_count, 1)

    async def test_click_execution_evidence_prevents_retry(self):
        sid, _handle, page, locator = await self._owned_click_fixture()

        async def click(*, button, timeout):
            page.visibility_state = "hidden"
            raise TimeoutError(
                "Timeout 3000ms exceeded\nCall log:\n"
                "  - performing click action\n"
                "  - waiting for element to be visible, enabled and stable"
            )

        locator.click.side_effect = click
        with self.assertRaisesRegex(ValueError, "browser_click_tab_hidden"):
            await browser_qa.browser_click(
                sid, "button", timeout_sec=5,
                guard_url_prefix="http://127.0.0.1:3002/fixture",
            )
        locator.click.assert_awaited_once()

    async def test_hidden_click_failure_respects_the_overall_timeout(self):
        sid, _handle, page, locator = await self._owned_click_fixture()
        page.visibility_state = "hidden"
        page.bring_to_front_sets_visible = False
        started = browser_qa.time.monotonic()
        with self.assertRaisesRegex(ValueError, "browser_click_tab_hidden"):
            await browser_qa.browser_click(
                sid, "button", timeout_sec=0.2,
                guard_url_prefix="http://127.0.0.1:3002/fixture",
            )
        elapsed = browser_qa.time.monotonic() - started
        self.assertLess(elapsed, 0.6)
        locator.click.assert_not_awaited()

    async def test_navigation_exception_keeps_safe_type_and_reason(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        await browser_qa.browser_select_tab(sid, attached["tabs"][0]["tab_handle"])
        self.page.goto_error = RuntimeError(
            "net::ERR_CONNECTION_RESET https://user:pass@example.test/path?token=canary#fragment"
        )
        with self.assertRaisesRegex(RuntimeError, "browser_navigation_failed") as caught:
            await browser_qa.browser_navigate(sid, "http://127.0.0.1:3002/fixture")
        safe = str(caught.exception)
        self.assertIn("RuntimeError", safe)
        self.assertIn("net::ERR_CONNECTION_RESET", safe)
        for private in ("user", "pass", "token", "canary", "fragment"):
            self.assertNotIn(private, safe)
        await browser_qa.browser_detach(sid)

    async def test_both_mcp_lifespans_release_borrowed_clients_only(self):
        for lifespan, mcp in ((server.lifespan, server.mcp), (jev_browser_server.lifespan, jev_browser_server.mcp)):
            attached = await browser_qa.browser_attach_edge()
            sid = attached["session_id"]
            await browser_qa.browser_select_tab(sid, attached["tabs"][0]["tab_handle"])
            browser_closes = self.browser.close_calls
            context_closes = self.context.close_calls
            page_closes = self.page.close_calls
            driver_stops = self.driver.stop_calls
            async with lifespan(mcp):
                pass
            self.assertNotIn(sid, browser_qa._SESSIONS)
            self.assertEqual(self.browser.close_calls, browser_closes + 1)
            self.assertEqual(self.context.close_calls, context_closes)
            self.assertEqual(self.page.close_calls, page_closes)
            self.assertEqual(self.driver.stop_calls, driver_stops + 1)

    async def test_failed_broker_attempt_is_requeried_without_direct_cdp_retry(self):
        failed = AsyncMock(side_effect=RuntimeError("edge_broker_attach_failed"))
        self._set_broker_mock(failed)
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "edge_broker_attach_failed"):
                await browser_qa.browser_attach_edge()
        self.assertEqual(failed.await_count, 2)
        failed.assert_awaited_with(5.0)
        self.assertEqual(self.chromium.connect_args, [])
        self.assertEqual(self.chromium.protocol_connect_args, [])
        self.assertEqual(self.chromium.launch_calls, 0)
        self.assertEqual(self.browser.close_calls, 0)
        self.assertEqual(self.playwright_starter.drivers, [])

    async def test_broker_unavailable_is_retryable_without_cdp(self):
        endpoint_call = AsyncMock(side_effect=[
            RuntimeError("edge_broker_unavailable"),
            (self.broker_endpoint, "Microsoft Edge/156.0.fixture"),
        ])
        self._set_broker_mock(endpoint_call)
        with self.assertRaisesRegex(RuntimeError, "edge_broker_unavailable"):
            await browser_qa.browser_attach_edge()
        attached = await browser_qa.browser_attach_edge()
        self.assertEqual(attached["mode"], "attached")
        self.assertEqual(endpoint_call.await_count, 2)
        self.assertEqual(self.chromium.connect_args, [])

    async def test_latched_broker_failure_can_be_requeried_after_explicit_reset(self):
        endpoint_call = AsyncMock(side_effect=[
            RuntimeError("edge_broker_connect_failed"),
            (self.broker_endpoint, "Microsoft Edge/156.0.fixture"),
        ])
        self._set_broker_mock(endpoint_call)
        with self.assertRaisesRegex(RuntimeError, "edge_broker_connect_failed"):
            await browser_qa.browser_attach_edge()

        attached = await browser_qa.browser_attach_edge()

        self.assertEqual(attached["mode"], "attached")
        self.assertEqual(endpoint_call.await_count, 2)
        self.assertEqual(self.chromium.connect_args, [])
        self.assertEqual(len(self.chromium.protocol_connect_args), 1)

    async def test_attached_tool_errors_hide_endpoint_details(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        await browser_qa.browser_select_tab(sid, attached["tabs"][0]["tab_handle"])
        with self.assertRaisesRegex(RuntimeError, "browser_action_failed") as error:
            async with browser_qa._session(sid):
                raise RuntimeError("ws://127.0.0.1:9222/devtools/browser/connection-secret")
        self.assertNotIn("connection-secret", str(error.exception))
        await browser_qa.browser_detach(sid)

    async def test_attached_plan_errors_keep_their_fixed_code(self):
        attached = await browser_qa.browser_attach_edge()
        sid = attached["session_id"]
        await browser_qa.browser_select_tab(sid, attached["tabs"][0]["tab_handle"])
        for code in ("browser_plan_invalid", "jev_low_confidence"):
            with self.assertRaisesRegex(browser_agent.BrowserAgentError, code):
                async with browser_qa._session(sid):
                    raise browser_agent.BrowserAgentError(code)
        await browser_qa.browser_detach(sid)

    async def test_attach_timeout_supports_a_bounded_consent_wait(self):
        attached = await browser_qa.browser_attach_edge(timeout_sec=60)
        sid = attached["session_id"]
        _endpoint, kwargs = self.chromium.protocol_connect_args[-1]
        self.assertEqual(kwargs["timeout"], 60_000)
        self.broker_patch.new.assert_awaited_once_with(60.0)
        await browser_qa.browser_detach(sid)
        with self.assertRaisesRegex(ValueError, "between 1 and 60"):
            await browser_qa.browser_attach_edge(timeout_sec=61)

    async def test_explicit_isolated_start_and_close_still_own_the_browser(self):
        isolated_context = FakeContext([])
        self.chromium.launch_browser = FakeBrowser(isolated_context)
        started = await browser_qa.browser_start(headless=True)
        sid = started["session_id"]
        self.assertEqual(self.chromium.launch_calls, 1)
        self.assertEqual(self.chromium.launch_browser.new_context_calls, 1)
        self.assertEqual(isolated_context.new_page_calls, 1)
        result = await browser_qa.browser_close(sid)
        self.assertTrue(result["closed"])
        self.assertNotIn("external_browser_closed", result)
        self.assertEqual(self.chromium.launch_browser.close_calls, 1)
        self.assertEqual(isolated_context.pages[0].close_calls, 1)

    async def test_browser_find_omits_value_attributes(self):
        locator = FakeLocator("input[type=hidden]")
        handle = "edge_0123456789abcdef01234567"
        fake_page = type("Page", (), {
            "url": "https://example.test/path?token=secret#fragment",
            "locator": lambda _self, _selector: locator,
            "is_closed": lambda _self: False,
        })()

        @asynccontextmanager
        async def fake_session(session_id):
            yield {
                "page": fake_page,
                "attached": True,
                "selected_tab_handle": handle,
                "owned_tab_handles": {handle},
                "owned_tab_pages": {handle: fake_page},
            }

        with patch.object(browser_qa, "_session", side_effect=fake_session):
            result = await browser_qa.browser_find(
                "attached-session", "input[type=hidden]",
                guard_url_prefix="https://example.test/path",
            )
        filter_at = locator.script.index(".filter(a => !options.redactSecrets || a.name.toLowerCase() !== 'value')")
        value_projection_at = locator.script.index(".map(a => [a.name, a.value])")
        self.assertLess(filter_at, value_projection_at)
        self.assertTrue(locator.options["redactSecrets"])
        self.assertEqual(result["elements"][0]["text"], "")
        self.assertEqual(result["elements"][0]["attributes"], {"id": "csrf"})
        self.assertEqual(result["elements"][1]["attributes"]["href"], "https://example.test/path")
        self.assertNotIn("canary", json.dumps(result))

    async def test_safe_url_strips_userinfo_query_and_fragment(self):
        self.assertEqual(
            browser_agent._jev_safe_url("https://name:password@example.test/a?token=secret#fragment"),
            "https://example.test/a",
        )
        self.assertEqual(browser_qa._safe_browser_url("data:text/html,private"), "data:")


@unittest.skipUnless(
    os.environ.get("HOME_MCP_RUN_LIVE_EDGE_FIXTURES") == "1",
    "Set HOME_MCP_RUN_LIVE_EDGE_FIXTURES=1 to use the existing Edge broker",
)
class LiveEdgeClickFixtureTests(unittest.IsolatedAsyncioTestCase):
    last_measurement: dict[str, Any] = {}

    async def test_background_owned_local_fixture_clicks_once_and_preserves_other_tabs(self):
        session_id = ""
        owned_handles: list[str] = []
        original_handles: set[str] = set()
        elapsed = None
        marker = None
        visibility = {}
        with local_server() as (origin, _port):
            try:
                attached = await browser_qa.browser_attach_edge(timeout_sec=10)
                session_id = attached["session_id"]
                original_handles = {
                    tab["tab_handle"] for tab in attached["tabs"]
                }

                first = await browser_qa.browser_open_tab(session_id)
                owned_handles.append(first["tab_handle"])
                first_url = origin + "/edge-background-click-first"
                await browser_qa.browser_navigate(session_id, first_url)
                first_page = browser_qa._SESSIONS[session_id]["page"]
                await first_page.set_content("""<!doctype html><title>Owned fixture A</title>
                  <button id="target" onclick="window.__fixtureMarker = (window.__fixtureMarker || 0) + 1">Click</button>""")

                second = await browser_qa.browser_open_tab(session_id)
                owned_handles.append(second["tab_handle"])
                await browser_qa.browser_navigate(session_id, origin + "/edge-background-click-second")
                second_page = browser_qa._SESSIONS[session_id]["page"]
                await second_page.set_content("""<!doctype html><title>Owned fixture B</title>
                  <p>This tab belongs to this fixture.</p>""")

                listed = await browser_qa.browser_list_tabs(session_id)
                current_handles = {tab["tab_handle"] for tab in listed["tabs"]}
                self.assertEqual(current_handles, original_handles | set(owned_handles))
                await browser_qa.browser_select_tab(session_id, first["tab_handle"])
                self.assertEqual(await first_page.evaluate("document.visibilityState"), "hidden")

                started = time.monotonic()
                click = await browser_qa.browser_click(
                    session_id,
                    "#target",
                    timeout_sec=3,
                    guard_url_prefix=first_url,
                )
                elapsed = time.monotonic() - started
                marker = await first_page.evaluate("window.__fixtureMarker")
                self.assertLess(elapsed, 3.0)
                self.assertEqual(marker, 1)
                self.assertEqual(click["tab_visibility"]["initial_state"], "hidden")
                self.assertEqual(click["tab_visibility"]["final_state"], "visible")
                visibility = click["tab_visibility"]
            finally:
                if session_id and session_id in browser_qa._SESSIONS:
                    for tab_handle in reversed(owned_handles):
                        try:
                            await browser_qa.browser_close_tab(session_id, tab_handle)
                        except Exception:
                            pass
                    after = await browser_qa.browser_list_tabs(session_id)
                    final_handles = {tab["tab_handle"] for tab in after["tabs"]}
                    self.assertEqual(final_handles, original_handles)
                    type(self).last_measurement = {
                        "click_elapsed_sec": round(elapsed, 3) if elapsed is not None else None,
                        "click_marker_count": marker,
                        "visibility": visibility,
                        "initial_tab_count": len(original_handles),
                        "fixture_owned_tab_count": len(owned_handles),
                        "tab_count_with_fixtures": len(original_handles) + len(owned_handles),
                        "final_tab_count": len(final_handles),
                        "identities_restored": final_handles == original_handles,
                    }
                    await browser_qa.browser_detach(session_id)
                    await browser_qa.close_all()


if __name__ == "__main__":
    unittest.main()
