import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from home_mcp_gateway import browser_qa, jev_browser_server


class BrowserDragGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session_id = str(uuid4())
        self.owned_handle = "edge_0123456789abcdef01234567"
        self.other_handle = "edge_89abcdef0123456789abcdef"
        self.page = SimpleNamespace(
            url="http://127.0.0.1:3002/drag-fixture?token=canary#private",
            is_closed=lambda: False,
        )
        browser_qa._SESSIONS[self.session_id] = {
            "attached": True,
            "page": None,
            "lock": asyncio.Lock(),
            "owned_tab_handles": {self.owned_handle},
            "owned_tab_pages": {self.owned_handle: self.page},
            "closed_owned_tab_handles": set(),
        }

    async def asyncTearDown(self):
        browser_qa._SESSIONS.pop(self.session_id, None)

    async def test_drag_rejects_tabs_not_opened_by_this_caller(self):
        performer = AsyncMock()
        with patch.object(browser_qa, "_perform_browser_drag", performer):
            with self.assertRaisesRegex(ValueError, "browser_drag_tab_not_owned"):
                await browser_qa.browser_drag(
                    session_id=self.session_id,
                    tab_handle=self.other_handle,
                    source_selector="#source",
                    target_selector="#target",
                    expected_url="http://127.0.0.1:3002/drag-fixture",
                )
        performer.assert_not_awaited()

    async def test_drag_rejects_url_mismatch_before_resolving_elements(self):
        performer = AsyncMock()
        with patch.object(browser_qa, "_perform_browser_drag", performer):
            with self.assertRaisesRegex(ValueError, "browser_drag_url_mismatch"):
                await browser_qa.browser_drag(
                    session_id=self.session_id,
                    tab_handle=self.owned_handle,
                    source_selector="#source",
                    target_selector="#target",
                    expected_url_prefix="http://127.0.0.1:3002/another-fixture",
                )
        performer.assert_not_awaited()

    async def test_drag_requires_exactly_one_http_url_guard(self):
        for values, expected in (
            ({}, "browser_drag_url_guard_required"),
            ({"expected_url": "http://127.0.0.1/a", "expected_url_prefix": "http://127.0.0.1"}, "browser_drag_url_guard_invalid"),
            ({"expected_url_prefix": "data:text/html,fixture"}, "browser_drag_url_guard_invalid"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaisesRegex(ValueError, expected):
                    await browser_qa.browser_drag(
                        session_id=self.session_id,
                        tab_handle=self.owned_handle,
                        source_selector="#source",
                        target_selector="#target",
                        **values,
                    )

    async def test_drag_settle_ms_is_bounded_and_url_is_checked_after_release(self):
        for value in (-1, 5_001):
            with self.subTest(settle_ms=value), self.assertRaisesRegex(ValueError, "browser_drag_settle_invalid"):
                await browser_qa.browser_drag(
                    session_id=self.session_id,
                    tab_handle=self.owned_handle,
                    source_selector="#source",
                    target_selector="#target",
                    expected_url="http://127.0.0.1:3002/drag-fixture",
                    settle_ms=value,
                )
        for value in (-1, 5_001):
            with self.subTest(release_delay_ms=value), self.assertRaisesRegex(ValueError, "browser_drag_release_delay_invalid"):
                await browser_qa.browser_drag(
                    session_id=self.session_id,
                    tab_handle=self.owned_handle,
                    source_selector="#source",
                    target_selector="#target",
                    expected_url="http://127.0.0.1:3002/drag-fixture",
                    release_delay_ms=value,
                )
        summary = {
            "action": "drag",
            "source": {"resolved": True, "center": {"x": 10.0, "y": 20.0}},
            "target": {"resolved": True, "center": {"x": 30.0, "y": 40.0}},
            "steps": 12,
            "hold_ms": 100,
            "settle_ms": 25,
            "release_to_return_ms": 25.1,
            "modifiers": [],
            "final_url": {"host": "127.0.0.1:3002", "path": "/after"},
        }

        async def change_url(*_args, **_kwargs):
            self.page.url = "http://127.0.0.1:3002/after"
            return summary

        with patch.object(browser_qa, "_perform_browser_drag", side_effect=change_url) as performer:
            result = await browser_qa.browser_drag(
                session_id=self.session_id,
                tab_handle=self.owned_handle,
                source_selector="#source",
                target_selector="#target",
                expected_url_prefix="http://127.0.0.1:3002/drag-fixture",
                settle_ms=25,
            )
        self.assertEqual(result["settle_ms"], 25)
        self.assertEqual(result["final_url"]["path"], "/after")
        self.assertFalse(result["url_guard_matched_after_settle"])
        performer.assert_awaited_once()

    async def test_prefix_guard_uses_path_boundary_and_result_omits_sensitive_url_parts(self):
        summary = {
            "action": "drag",
            "source": {"resolved": True, "center": {"x": 10.0, "y": 20.0}},
            "target": {"resolved": True, "center": {"x": 30.0, "y": 40.0}},
            "steps": 8,
            "hold_ms": 100,
            "modifiers": [],
            "final_url": {"host": "127.0.0.1:3002", "path": "/drag-fixture"},
        }
        performer = AsyncMock(return_value=summary)
        with patch.object(browser_qa, "_perform_browser_drag", performer):
            result = await browser_qa.browser_drag(
                session_id=self.session_id,
                tab_handle=self.owned_handle,
                source_selector="#source-secret",
                target_selector="#target-secret",
                expected_url_prefix="http://127.0.0.1:3002/drag-fixture",
                steps=8,
            )
        self.assertEqual(result["final_url"], {"host": "127.0.0.1:3002", "path": "/drag-fixture"})
        serialized = json.dumps(result)
        for secret in ("canary", "private", "source-secret", "target-secret"):
            self.assertNotIn(secret, serialized)
        performer.assert_awaited_once()

    async def test_offsets_and_release_delay_are_forwarded_only_after_guard(self):
        performer = AsyncMock(return_value={"action": "drag", "final_url": {"host": "127.0.0.1:3002", "path": "/drag-fixture"}})
        with patch.object(browser_qa, "_perform_browser_drag", performer):
            await browser_qa.browser_drag(
                session_id=self.session_id,
                tab_handle=self.owned_handle,
                source_selector="#source",
                target_selector="#target",
                expected_url_prefix="http://127.0.0.1:3002/drag-fixture",
                target_offset_x=0.4,
                target_offset_y=72,
                release_delay_ms=125,
            )
        self.assertEqual(performer.await_args.kwargs["target_offset_x"], 0.4)
        self.assertEqual(performer.await_args.kwargs["target_offset_y"], 72)
        self.assertEqual(performer.await_args.kwargs["release_delay_ms"], 125)

    async def test_drag_rejects_invalid_offset_values_before_page_work(self):
        performer = AsyncMock()
        for axis, value in (("x", -0.01), ("y", float("nan")), ("x", float("inf")), ("y", "0.5")):
            with self.subTest(axis=axis, value=value), patch.object(browser_qa, "_perform_browser_drag", performer):
                with self.assertRaisesRegex(ValueError, f"browser_drag_target_offset_{axis}_invalid"):
                    await browser_qa.browser_drag(
                        session_id=self.session_id,
                        tab_handle=self.owned_handle,
                        source_selector="#source",
                        target_selector="#target",
                        expected_url="http://127.0.0.1:3002/drag-fixture",
                        **{f"target_offset_{axis}": value},
                    )
        performer.assert_not_awaited()


class BrowserDragOffsetTests(unittest.TestCase):
    def test_ratio_pixel_and_default_center_offsets(self):
        self.assertEqual(browser_qa._drag_offset_pixels(0.25, 200, "x"), 50)
        self.assertEqual(browser_qa._drag_offset_pixels(1, 100, "y"), 99.99)
        self.assertEqual(browser_qa._drag_offset_pixels(75, 100, "y"), 75)
        self.assertEqual(browser_qa._drag_offset_pixels(None, 100, "x"), 50)

    def test_offset_must_fit_target_box(self):
        for value in (-1, 101, float("nan"), float("inf"), "20"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "browser_drag_target_offset_x_invalid"):
                browser_qa._drag_offset_pixels(value, 100, "x")


class BrowserDragInteractionTests(unittest.IsolatedAsyncioTestCase):
    async def test_html5_and_pointer_event_drags_with_scrolling(self):
        try:
            started = await browser_qa.browser_start(
                headless=True, browser_type="chromium", channel="msedge", width=1280, height=800
            )
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install a Playwright browser to run the drag fixture")
            raise
        session_id = started["session_id"]
        html = """<!doctype html><meta charset=utf-8><style>
          body { margin: 0; font: 16px sans-serif; }
          .item { display:inline-flex; align-items:center; justify-content:center; width:140px; height:72px; border:1px solid #333; }
          #native-target, #pointer-target { margin-left:180px; }
          #pointer-row { margin-top:1100px; }
        </style>
        <div id=native-source class=item draggable=true>Native source</div>
        <div id=native-target class=item>Native target</div>
        <output id=native-result>waiting</output>
        <div id=pointer-row>
          <div id=pointer-source class=item>Pointer source</div>
          <div id=pointer-target class=item>Pointer target</div>
          <output id=pointer-result>waiting</output>
        </div>
        <script>
          const nativeSource=document.querySelector('#native-source');
          const nativeTarget=document.querySelector('#native-target');
          nativeSource.addEventListener('dragstart',e=>e.dataTransfer.setData('text/plain','fixture'));
          nativeTarget.addEventListener('dragover',e=>e.preventDefault());
          nativeTarget.addEventListener('drop',e=>{e.preventDefault();const r=nativeTarget.getBoundingClientRect();const payload=e.dataTransfer.getData('text/plain');document.querySelector('#native-result').textContent=`${payload==='fixture'?'native-drop-ok':'native-drop-invalid'}:${(e.clientY-r.top).toFixed(2)}:${r.height.toFixed(2)}`});
          const pointerSource=document.querySelector('#pointer-source');
          const pointerTarget=document.querySelector('#pointer-target');
          let active=false, over=false;
          pointerSource.addEventListener('pointerdown',e=>{if(e.button===0){active=true;pointerSource.setPointerCapture(e.pointerId)}});
          document.addEventListener('pointermove',e=>{if(!active)return;const r=pointerTarget.getBoundingClientRect();over=e.clientX>=r.left&&e.clientX<=r.right&&e.clientY>=r.top&&e.clientY<=r.bottom});
          document.addEventListener('pointerup',()=>{if(!active)return;if(over)document.querySelector('#pointer-result').textContent='pointer-drop-ok';active=false;over=false});
        </script>"""
        try:
            async with browser_qa._session(session_id) as session:
                page = session["page"]
                await page.set_content(html)
                native = await browser_qa._perform_browser_drag(
                    page,
                    source_selector="#native-source",
                    target_selector="#native-target",
                    steps=12,
                    timeout_sec=10,
                    hold_ms=40,
                    modifiers=[],
                    target_offset_x=0.4,
                    target_offset_y=0.75,
                )
                self.assertEqual(native["action"], "drag")
                self.assertTrue(native["source"]["resolved"])
                self.assertTrue(native["target"]["resolved"])
                self.assertEqual(native["steps"], 12)
                ratio_result = await page.locator("#native-result").inner_text()
                self.assertTrue(ratio_result.startswith("native-drop-ok:"), ratio_result)
                _, ratio_drop_y, target_height = ratio_result.split(":")
                self.assertGreaterEqual(float(ratio_drop_y) / float(target_height), 0.5)
                self.assertAlmostEqual(float(ratio_drop_y) / float(target_height), 0.75, delta=0.03)
                self.assertAlmostEqual(native["target_offset"]["resolved_y_px"], float(target_height) * 0.75, delta=0.02)
                self.assertAlmostEqual(native["target"]["drop_point"]["x"], native["target"]["center"]["x"] - 0.1 * 142, delta=0.1)

                # A >1 offset is interpreted as pixels. The native drag/drop event
                # must reach that point through the browser's DataTransfer path.
                pixel = await browser_qa._perform_browser_drag(
                    page,
                    source_selector="#native-source",
                    target_selector="#native-target",
                    steps=12,
                    timeout_sec=10,
                    hold_ms=40,
                    modifiers=[],
                    target_offset_x=80,
                    target_offset_y=58,
                    release_delay_ms=20,
                )
                pixel_result = await page.locator("#native-result").inner_text()
                self.assertTrue(pixel_result.startswith("native-drop-ok:"), pixel_result)
                _, pixel_drop_y, _ = pixel_result.split(":")
                self.assertAlmostEqual(float(pixel_drop_y), 58, delta=1)
                self.assertEqual(pixel["target_offset"]["resolved_x_px"], 80)
                self.assertEqual(pixel["target_offset"]["resolved_y_px"], 58)
                self.assertEqual(pixel["release_delay_ms"], 20)

                pointer = await browser_qa._perform_browser_drag(
                    page,
                    source_selector="#pointer-source",
                    target_selector="#pointer-target",
                    steps=14,
                    timeout_sec=10,
                    hold_ms=40,
                    modifiers=[],
                )
                self.assertEqual(pointer["action"], "drag")
                self.assertEqual(pointer["steps"], 14)
                self.assertEqual(await page.locator("#pointer-result").inner_text(), "pointer-drop-ok")
        finally:
            await browser_qa.browser_close(session_id)


if __name__ == "__main__":
    unittest.main()
