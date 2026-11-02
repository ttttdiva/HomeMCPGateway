"""Safety guards and focused read-only browser-measurement fixture coverage."""
import asyncio
import base64
import io
import json
import os
from pathlib import Path
import statistics
from types import SimpleNamespace
import time
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from PIL import Image
from home_mcp_gateway import browser_qa, jev_browser_server, server


class BrowserMeasurementGuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session_id = str(uuid4())
        self.handle = "edge_0123456789abcdef01234567"
        self.other_handle = "edge_89abcdef0123456789abcdef"
        self.page = SimpleNamespace(
            url="http://127.0.0.1:3002/measurement-fixture",
            is_closed=lambda: False,
        )
        self.session = {
            "attached": True,
            "page": None,
            "lock": asyncio.Lock(),
            "owned_tab_handles": {self.handle},
            "owned_tab_pages": {self.handle: self.page},
            "closed_owned_tab_handles": set(),
        }
        browser_qa._SESSIONS[self.session_id] = self.session

    async def asyncTearDown(self):
        browser_qa._SESSIONS.pop(self.session_id, None)

    async def test_other_existing_or_isolated_tab_is_rejected(self):
        for handle in (self.other_handle, "edge_aaaaaaaaaaaaaaaaaaaaaaaa"):
            with self.subTest(handle=handle), self.assertRaisesRegex(ValueError, "browser_capture_frames_tab_not_owned"):
                await browser_qa.browser_capture_frames_start(
                    self.session_id, handle, expected_url="http://127.0.0.1:3002/measurement-fixture"
                )
        self.session["attached"] = False
        with self.assertRaisesRegex(ValueError, "browser_track_boxes_tab_not_owned"):
            await browser_qa.browser_track_boxes_start(
                self.session_id, self.handle, ["#tile"], expected_url="http://127.0.0.1:3002/measurement-fixture"
            )

    async def test_closed_owned_tab_and_url_mismatch_are_rejected(self):
        self.page.is_closed = lambda: True
        with self.assertRaisesRegex(ValueError, "browser_tab_closed"):
            await browser_qa.browser_track_boxes_start(
                self.session_id, self.handle, ["#tile"], expected_url="http://127.0.0.1:3002/measurement-fixture"
            )
        self.page.is_closed = lambda: False
        with self.assertRaisesRegex(ValueError, "browser_track_boxes_url_mismatch"):
            await browser_qa.browser_track_boxes_start(
                self.session_id, self.handle, ["#tile"], expected_url_prefix="http://127.0.0.1:3002/other"
            )

    async def test_prefix_guard_requires_http_origin_path_and_exactly_one_guard(self):
        for kwargs, code in (
            ({}, "browser_capture_frames_url_guard_required"),
            ({"expected_url": "http://127.0.0.1:3002/a", "expected_url_prefix": "http://127.0.0.1:3002/a"}, "browser_capture_frames_url_guard_invalid"),
            ({"expected_url_prefix": "http://127.0.0.1:3002/a?token=x"}, "browser_capture_frames_url_guard_invalid"),
            ({"expected_url_prefix": "data:text/html,fixture"}, "browser_capture_frames_url_guard_invalid"),
        ):
            with self.subTest(code=code), self.assertRaisesRegex(ValueError, code):
                await browser_qa.browser_capture_frames_start(self.session_id, self.handle, **kwargs)

    async def test_output_paths_and_caps_are_validated_before_sampling(self):
        expected = "http://127.0.0.1:3002/measurement-fixture"
        for path in ("../escape", r"safe\..\escape", "D:/outside", "/outside"):
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "browser_measure_output_path_invalid"):
                await browser_qa.browser_track_boxes_start(
                    self.session_id, self.handle, ["#tile"], expected_url=expected, out_subdir=path
                )
        with self.assertRaisesRegex(ValueError, "browser_capture_frames_sample_limit_invalid"):
            await browser_qa.browser_capture_frames_start(
                self.session_id, self.handle, expected_url=expected, max_frames=121
            )
        with self.assertRaisesRegex(ValueError, "browser_track_boxes_interval_invalid"):
            await browser_qa.browser_track_boxes_start(
                self.session_id, self.handle, ["#tile"], expected_url=expected, interval_ms=15
            )

    async def test_tools_are_registered_on_both_surfaces(self):
        expected = {
            "browser_capture_frames_start", "browser_capture_frames_stop",
            "browser_track_boxes_start", "browser_track_boxes_stop",
            "browser_set_viewport", "browser_reset_viewport",
        }
        gateway = {tool.name for tool in await server.mcp.list_tools()}
        jev = {tool.name for tool in await jev_browser_server.mcp.list_tools()}
        self.assertTrue(expected <= gateway)
        self.assertTrue(expected <= jev)


class FakeViewportPage:
    def __init__(self, target_id, width=1024, height=768):
        self.target_id = target_id
        self.url = "http://127.0.0.1:3002/viewport-fixture"
        self.closed = False
        self.context = FakeViewportContext()
        self.context.page = self
        self.metrics = {
            "cssVisualViewport": {"clientWidth": width, "clientHeight": height},
            "cssLayoutViewport": {"clientWidth": width, "clientHeight": height},
        }
        self.original_metrics = json.loads(json.dumps(self.metrics))
        self.original_dsf = 1.0
        self.device_scale_factor = self.original_dsf
        self.viewport_size = {"width": width, "height": height}  # deliberately stale after CDP override
        self.close_calls = 0

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True
        self.close_calls += 1


class FakeViewportContext:
    def __init__(self):
        self.page = None
        self.sessions = []

    async def new_cdp_session(self, page):
        session = FakeViewportCDP(page)
        self.sessions.append(session)
        return session


class FakeViewportCDP:
    def __init__(self, page):
        self.page = page
        self.commands = []
        self.detach_calls = 0

    async def send(self, method, params=None):
        params = params or {}
        self.commands.append((method, params))
        if method == "Page.getLayoutMetrics":
            return self.page.metrics
        if method == "Emulation.setDeviceMetricsOverride":
            self.page.metrics = {
                "cssVisualViewport": {"clientWidth": params["width"], "clientHeight": params["height"]},
                "cssLayoutViewport": {"clientWidth": params["width"], "clientHeight": params["height"]},
            }
            self.page.device_scale_factor = params["deviceScaleFactor"]
            return {}
        if method == "Emulation.clearDeviceMetricsOverride":
            self.page.metrics = json.loads(json.dumps(self.page.original_metrics))
            self.page.device_scale_factor = self.page.original_dsf
            return {}
        raise AssertionError(f"unexpected CDP method: {method}")

    async def detach(self):
        self.detach_calls += 1


class FakeMouse:
    def __init__(self):
        self.moves = []
        self.downs = 0
        self.ups = 0

    async def move(self, *args, **kwargs):
        self.moves.append((args, kwargs))

    async def down(self, *args, **kwargs):
        self.downs += 1

    async def up(self, *args, **kwargs):
        self.ups += 1


class BrowserViewportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session_id = str(uuid4())
        self.handle = "edge_0123456789abcdef01234567"
        self.other_handle = "edge_89abcdef0123456789abcdef"
        self.page = FakeViewportPage("owned-one")
        self.other_page = FakeViewportPage("owned-two", 1280, 720)
        self.connection = SimpleNamespace(session_ids={self.session_id})
        self.session = {
            "attached": True,
            "shared_edge": True,
            "edge_connection_state": self.connection,
            "page": self.page,
            "lock": asyncio.Lock(),
            "owned_tab_handles": {self.handle, self.other_handle},
            "owned_tab_pages": {self.handle: self.page, self.other_handle: self.other_page},
            "closed_owned_tab_handles": set(),
            "viewport_overrides": {},
        }
        browser_qa._SESSIONS[self.session_id] = self.session

    async def asyncTearDown(self):
        browser_qa._SESSIONS.pop(self.session_id, None)

    async def test_viewport_is_owned_guarded_and_reset_restores_original(self):
        result = await browser_qa.browser_set_viewport(
            self.session_id, self.handle, 1600, 1000, device_scale_factor=1.5,
            expected_url="http://127.0.0.1:3002/viewport-fixture",
        )
        self.assertEqual(result["effective_viewport"], {"width": 1600.0, "height": 1000.0, "device_scale_factor": 1.5})
        self.assertEqual(result["original_viewport"], {"width": 1024.0, "height": 768.0, "device_scale_factor": None})
        self.assertEqual(await browser_qa._drag_viewport_size(self.page), (1600.0, 1000.0))
        self.assertEqual(self.other_page.metrics["cssVisualViewport"]["clientWidth"], 1280)

        reset = await browser_qa.browser_reset_viewport(
            self.session_id, self.handle, expected_url_prefix="http://127.0.0.1:3002/viewport-fixture"
        )
        self.assertTrue(reset["cleared"])
        self.assertTrue(reset["had_tracked_override"])
        self.assertEqual(reset["effective_viewport"]["width"], 1024.0)
        self.assertEqual(await browser_qa._drag_viewport_size(self.page), (1024.0, 768.0))
        self.assertEqual(self.other_page.metrics["cssVisualViewport"]["clientWidth"], 1280)

    async def test_viewport_guard_caps_and_isolated_tabs_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "browser_set_viewport_tab_not_owned"):
            await browser_qa.browser_set_viewport(
                self.session_id, "edge_aaaaaaaaaaaaaaaaaaaaaaaa", 1600, 1000,
                expected_url="http://127.0.0.1:3002/viewport-fixture",
            )
        with self.assertRaisesRegex(ValueError, "browser_set_viewport_width_invalid"):
            await browser_qa.browser_set_viewport(
                self.session_id, self.handle, 319, 1000,
                expected_url="http://127.0.0.1:3002/viewport-fixture",
            )
        with self.assertRaisesRegex(ValueError, "browser_set_viewport_url_guard_required"):
            await browser_qa.browser_set_viewport(self.session_id, self.handle, 1600, 1000)
        self.session["attached"] = False
        with self.assertRaisesRegex(ValueError, "browser_reset_viewport_tab_not_owned"):
            await browser_qa.browser_reset_viewport(
                self.session_id, self.handle, expected_url="http://127.0.0.1:3002/viewport-fixture"
            )

    async def test_detach_clears_override_while_owned_tab_remains_open(self):
        await browser_qa.browser_set_viewport(
            self.session_id, self.handle, 1600, 1000,
            expected_url="http://127.0.0.1:3002/viewport-fixture",
        )
        result = await browser_qa.browser_detach(self.session_id)
        self.assertTrue(result["detached"])
        self.assertFalse(self.page.is_closed())
        self.assertEqual(self.page.metrics["cssVisualViewport"]["clientWidth"], 1024)
        self.assertNotIn(self.handle, self.session["viewport_overrides"])
        self.assertIn("Emulation.clearDeviceMetricsOverride", [method for method, _ in self.page.context.sessions[-1].commands])

    async def test_drag_center_checks_use_the_active_viewport_override(self):
        self.page.mouse = FakeMouse()
        self.page.keyboard = SimpleNamespace(down=AsyncMock(), up=AsyncMock())
        await browser_qa.browser_set_viewport(
            self.session_id, self.handle, 1600, 1000,
            expected_url="http://127.0.0.1:3002/viewport-fixture",
        )
        source = SimpleNamespace(name="source")
        target = SimpleNamespace(name="target")
        with patch.object(browser_qa, "_drag_locator", new_callable=AsyncMock, side_effect=[source, target]), \
             patch.object(browser_qa, "_drag_box", new_callable=AsyncMock, side_effect=[
                 {"x": 1700, "y": 200, "width": 40, "height": 40},
                 {"x": 100, "y": 200, "width": 40, "height": 40},
             ]):
            with self.assertRaisesRegex(ValueError, "browser_drag_source_outside_viewport"):
                await browser_qa.browser_drag(
                    self.session_id, self.handle, "#source", "#target",
                    expected_url="http://127.0.0.1:3002/viewport-fixture", hold_ms=0,
                )
        self.assertEqual(self.page.mouse.downs, 0)

        with patch.object(browser_qa, "_drag_locator", new_callable=AsyncMock, side_effect=[source, target]), \
             patch.object(browser_qa, "_drag_box", new_callable=AsyncMock, side_effect=[
                 {"x": 1500, "y": 200, "width": 40, "height": 40},
                 {"x": 100, "y": 200, "width": 40, "height": 40},
             ]):
            result = await browser_qa.browser_drag(
                self.session_id, self.handle, "#source", "#target",
                expected_url="http://127.0.0.1:3002/viewport-fixture", hold_ms=0,
            )
        self.assertTrue(result["url_guard_matched_after_settle"])
        self.assertGreater(self.page.mouse.downs, 0)


class FakeCaptureCDP:
    def __init__(self, image_data, *, delay=0.0, responses=None):
        self.image_data = image_data
        self.delay = delay
        self.responses = list(responses or [])
        self.starts = []
        self.detach_calls = 0

    async def send(self, method, params=None):
        if method != "Page.captureScreenshot":
            raise AssertionError(f"unexpected CDP method: {method}")
        self.starts.append(time.monotonic())
        await asyncio.sleep(self.delay)
        if self.responses:
            response = self.responses.pop(0)
            if isinstance(response, Exception):
                raise response
            return {"data": base64.b64encode(response).decode("ascii")}
        return {"data": base64.b64encode(self.image_data).decode("ascii")}

    async def detach(self):
        self.detach_calls += 1


class FakeCaptureContext:
    def __init__(self, cdp):
        self.cdp = cdp

    async def new_cdp_session(self, _page):
        return self.cdp


class FakeCapturePage:
    def __init__(self, cdp):
        self.url = "http://127.0.0.1:3002/frame-schedule"
        self.context = FakeCaptureContext(cdp)
        self.viewport_size = None

    def is_closed(self):
        return False


class BrowserFrameScheduleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session_id = str(uuid4())
        self.handle = "edge_0123456789abcdef01234567"
        output = io.BytesIO()
        Image.new("RGB", (24, 16), (80, 120, 160)).save(output, format="JPEG", quality=70)
        self.image_data = output.getvalue()

    async def _run_fake_capture(self, cdp, *, interval_ms, max_frames, duration_ms):
        page = FakeCapturePage(cdp)
        session = {
            "attached": True,
            "page": page,
            "lock": asyncio.Lock(),
            "owned_tab_handles": {self.handle},
            "owned_tab_pages": {self.handle: page},
            "closed_owned_tab_handles": set(),
        }
        browser_qa._SESSIONS[self.session_id] = session
        started = await browser_qa.browser_capture_frames_start(
            self.session_id, self.handle,
            expected_url=page.url,
            interval_ms=interval_ms,
            max_frames=max_frames,
            duration_ms=duration_ms,
            quality=30,
            out_subdir="dispatch37/frame-schedule-tests",
        )
        measurement = browser_qa._MEASUREMENTS[started["measurement_id"]]
        await asyncio.wait_for(measurement["task"], timeout=5)
        result = await browser_qa.browser_capture_frames_stop(self.session_id, started["measurement_id"])
        return result, measurement, session

    async def asyncTearDown(self):
        browser_qa._SESSIONS.pop(self.session_id, None)

    async def test_slow_capture_uses_absolute_slots_and_reports_dropped_frames(self):
        cdp = FakeCaptureCDP(self.image_data, delay=0.09)
        result, measurement, _ = await self._run_fake_capture(
            cdp, interval_ms=50, max_frames=8, duration_ms=900
        )
        self.assertGreaterEqual(result["dropped_frames"], 3)
        self.assertGreaterEqual(result["sample_count"], 3)
        starts_ms = [(value - measurement["started_monotonic"]) * 1_000 for value in cdp.starts]
        distances_to_schedule = [abs(value - round(value / 50) * 50) for value in starts_ms]
        self.assertLess(max(distances_to_schedule), 16, starts_ms)
        times = [row["t_ms"] for row in result["frames"] if row.get("file")]
        intervals = [end - start for start, end in zip(times, times[1:])]
        self.assertAlmostEqual(result["actual_interval_ms"]["median"], statistics.median(intervals), delta=0.05)
        self.assertAlmostEqual(result["capture_duration_ms"]["median"], 90.0, delta=30.0)
        self.assertIn("interval_not_achievable", result["warnings"])
        self.assertTrue(result["window_comparisons"])
        for pair in result["comparisons"] + result["window_comparisons"]:
            self.assertAlmostEqual(pair["actual_interval_ms"], pair["to_t_ms"] - pair["from_t_ms"], delta=0.02)

    async def test_one_capture_failure_is_recorded_and_later_slots_continue(self):
        cdp = FakeCaptureCDP(self.image_data, delay=0.005, responses=[RuntimeError("safe fixture failure")])
        result, _, _ = await self._run_fake_capture(
            cdp, interval_ms=40, max_frames=4, duration_ms=500
        )
        self.assertIsNone(result["error_code"])
        self.assertEqual(result["failed_frames"], 1)
        self.assertEqual(result["frames"][0]["error_code"], "browser_capture_frames_capture_failed")
        self.assertGreaterEqual(result["sample_count"], 2, result)
        self.assertIn("capture_failures", result["warnings"])
        self.assertIsNotNone(result["actual_interval_ms"]["median"])


class LiveEdgeBrowserMeasurementFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_blink_jitter_stable_and_drag_overlap_on_owned_tab(self):
        if os.environ.get("HOME_MCP_RUN_LIVE_EDGE_FIXTURES") != "1":
            self.skipTest("Set HOME_MCP_RUN_LIVE_EDGE_FIXTURES=1 to use the existing Edge broker")
        from qa_test_support import local_server

        fixture = Path(__file__).parent / "fixtures" / "browser_measurement_fixture.html"
        html = fixture.read_text(encoding="utf-8")
        attached = await browser_qa.browser_attach_edge(timeout_sec=5)
        self.assertEqual(attached.get("mode"), "attached")
        session_id = attached["session_id"]
        before = await browser_qa.browser_list_tabs(session_id)
        before_handles = sorted(row["tab_handle"] for row in before["tabs"])
        owned_handle = ""
        frame_measurement = None
        box_measurement = None
        after_count = None
        try:
            opened = await browser_qa.browser_open_tab(session_id)
            owned_handle = opened["tab_handle"]
            with local_server() as (origin, _port):
                url = f"{origin}/browser-measurement-fixture"
                navigation = await browser_qa.browser_navigate(session_id, url)
                self.assertEqual(navigation["status"], 200)
                async with browser_qa._session(session_id) as caller:
                    await caller["page"].set_content(html, wait_until="domcontentloaded")
                    await caller["page"].bring_to_front()
                await asyncio.sleep(0.15)
                frame_measurement = await browser_qa.browser_capture_frames_start(
                    session_id=session_id,
                    tab_handle=owned_handle,
                    expected_url=url,
                    interval_ms=50,
                    max_frames=60,
                    duration_ms=9_000,
                    clip_rect={"x": 0, "y": 0, "width": 380, "height": 150},
                    quality=38,
                    flicker_diff_threshold=0.002,
                    vertical_shift_threshold_px=3,
                    out_subdir="dispatch37/browser-measurement-fixture",
                )
                box_measurement = await browser_qa.browser_track_boxes_start(
                    session_id=session_id,
                    tab_handle=owned_handle,
                    selectors=["#blink", "#jitter", "#stable"],
                    expected_url=url,
                    interval_ms=50,
                    max_samples=60,
                    duration_ms=9_000,
                    vertical_jitter_threshold_px=3,
                    out_subdir="dispatch37/browser-measurement-fixture",
                )
                frame_started = browser_qa._MEASUREMENTS[frame_measurement["measurement_id"]]["started_monotonic"]
                boxes_started = browser_qa._MEASUREMENTS[box_measurement["measurement_id"]]["started_monotonic"]
                drag_started = time.monotonic()
                drag = await browser_qa.browser_drag(
                    session_id=session_id,
                    tab_handle=owned_handle,
                    source_selector="#drag-source",
                    target_selector="#drop-target",
                    expected_url=url,
                    steps=16,
                    hold_ms=750,
                )
                drag_finished = time.monotonic()
                self.assertEqual(drag["action"], "drag")
                async with browser_qa._session(session_id) as caller:
                    self.assertEqual(await caller["page"].locator("#drag-result").inner_text(), "drop-ok")
                # The actual attached Edge samples are remotely transported over CDP;
                # let several animation phases pass while the two background samplers remain active.
                await asyncio.sleep(5.5)
                frames = await browser_qa.browser_capture_frames_stop(session_id, frame_measurement["measurement_id"])
                frame_measurement = None
                boxes = await browser_qa.browser_track_boxes_stop(session_id, box_measurement["measurement_id"])
                box_measurement = None
                self.assertGreaterEqual(frames["sample_count"], 4)
                self.assertGreaterEqual(boxes["sample_count"], 4)
                self.assertEqual(frames["kind"], "frames")
                self.assertTrue(Path(frames["manifest_path"]).is_file())
                self.assertTrue(Path(boxes["manifest_path"]).is_file())
                summaries = {row["selector_index"]: row for row in boxes["elements"]}
                self.assertTrue(summaries[0]["flicker_detected"], summaries[0])
                self.assertTrue(summaries[1]["vertical_jitter_detected"], summaries[1])
                self.assertFalse(summaries[2]["flicker_detected"])
                self.assertFalse(summaries[2]["vertical_jitter_detected"])
                self.assertTrue(frames["analysis"]["flicker_detected"], frames["analysis"])
                self.assertTrue(frames["analysis"]["vertical_jitter_detected"], frames["analysis"])
                self.assertGreaterEqual(frames["analysis"]["flicker_evidence"]["repeated_changed_region_pairs"], 2)
                self.assertGreaterEqual(frames["analysis"]["vertical_jitter_evidence"]["qualifying_shift_pairs"], 2)
                self.assertLess(frames["analysis"]["vertical_jitter_evidence"]["max_absolute_estimated_shift_px"], 40)
                frame_drag_start_ms = (drag_started - frame_started) * 1_000
                frame_drag_end_ms = (drag_finished - frame_started) * 1_000
                boxes_drag_start_ms = (drag_started - boxes_started) * 1_000
                boxes_drag_end_ms = (drag_finished - boxes_started) * 1_000
                self.assertTrue(any(frame_drag_start_ms <= row["t_ms"] <= frame_drag_end_ms for row in frames["frames"]))
                self.assertTrue(any(boxes_drag_start_ms <= sample["t_ms"] <= boxes_drag_end_ms for element in boxes["elements"] for sample in element["samples"]))
                for manifest_path in (frames["manifest_path"], boxes["manifest_path"]):
                    manifest_text = Path(manifest_path).read_text(encoding="utf-8")
                    self.assertNotIn("#blink", manifest_text)
                    self.assertNotIn("#jitter", manifest_text)
                    self.assertNotIn("waiting", manifest_text)
                for summary in summaries.values():
                    self.assertTrue(any(sample["t_ms"] < 9_000 for sample in summary["samples"]))
                self.assertIn("analysis", frames)
                self.assertGreaterEqual(len(frames["comparisons"]), 2)
                self.assertTrue(Path(frames["output_directory"]).resolve().is_relative_to((Path(__file__).resolve().parents[1] / ".runtime").resolve()))
            closed = await browser_qa.browser_close_tab(session_id, owned_handle)
            self.assertTrue(closed["closed"])
            owned_handle = ""
        finally:
            for measurement in (frame_measurement, box_measurement):
                if measurement:
                    try:
                        await browser_qa._stop_owned_measurement(session_id, measurement["measurement_id"], measurement["kind"])
                    except Exception:
                        pass
            if owned_handle:
                try:
                    await browser_qa.browser_close_tab(session_id, owned_handle)
                except Exception:
                    pass
            after_handles = None
            try:
                after = await browser_qa.browser_list_tabs(session_id)
                after_handles = sorted(row["tab_handle"] for row in after["tabs"])
                after_count = len(after_handles)
            finally:
                await browser_qa.browser_detach(session_id)
                # This releases only the test process's local Playwright client.
                await browser_qa.close_all()
            self.assertEqual(after_handles, before_handles)
            print("LIVE_EDGE_MEASUREMENT_RESULT=" + json.dumps({
                "mode": attached.get("mode"),
                "shared_connection_reused": attached.get("shared_connection_reused"),
                "before_tab_count": len(before_handles),
                "after_tab_count": after_count,
                "tab_identity_equal": after_handles == before_handles,
                "drag": drag["action"],
                "drag_overlap_confirmed": True,
                "frames": {
                    "started_utc": frames["started_utc"],
                    "sample_count": frames["sample_count"],
                    "flicker_detected": frames["analysis"]["flicker_detected"],
                    "vertical_jitter_detected": frames["analysis"]["vertical_jitter_detected"],
                    "manifest_path": frames["manifest_path"],
                },
                "boxes": {
                    "started_utc": boxes["started_utc"],
                    "sample_count": boxes["sample_count"],
                    "flicker_detected": summaries[0]["flicker_detected"],
                    "vertical_jitter_detected": summaries[1]["vertical_jitter_detected"],
                    "stable_element_unchanged": not summaries[2]["flicker_detected"] and not summaries[2]["vertical_jitter_detected"],
                    "manifest_path": boxes["manifest_path"],
                },
            }, separators=(",", ":")))


class LiveEdgeViewportFixtureTests(unittest.IsolatedAsyncioTestCase):
    async def test_viewport_is_per_owned_tab_resets_and_reports_live_frame_intervals(self):
        if os.environ.get("HOME_MCP_RUN_LIVE_EDGE_FIXTURES") != "1":
            self.skipTest("Set HOME_MCP_RUN_LIVE_EDGE_FIXTURES=1 to use the existing Edge broker")
        from qa_test_support import local_server

        fixture = Path(__file__).parent / "fixtures" / "browser_viewport_fixture.html"
        html = fixture.read_text(encoding="utf-8")
        attached = await browser_qa.browser_attach_edge(timeout_sec=5)
        session_id = attached["session_id"]
        before = await browser_qa.browser_list_tabs(session_id)
        before_handles = sorted(row["tab_handle"] for row in before["tabs"])
        owned = []
        pages = []
        session_attached = True
        live_frame_results = []

        async def viewport_snapshot(page, cdp=None):
            owns_cdp = cdp is None
            if cdp is None:
                cdp = await page.context.new_cdp_session(page)
            try:
                metrics = await cdp.send("Page.getLayoutMetrics")
                width, height = browser_qa._viewport_dimensions(metrics)
                screenshot = await cdp.send("Page.captureScreenshot", {"format": "jpeg", "quality": 25})
                image = Image.open(io.BytesIO(base64.b64decode(screenshot["data"])))
                pixel_size = image.size
                image.close()
                return {"width": width, "height": height, "pixel_size": pixel_size}
            finally:
                if owns_cdp:
                    await cdp.detach()

        async def probe_width(tab_handle, url):
            started = await browser_qa.browser_track_boxes_start(
                session_id=session_id,
                tab_handle=tab_handle,
                selectors=["#viewport-probe"],
                expected_url=url,
                interval_ms=50,
                max_samples=1,
                duration_ms=250,
                out_subdir="dispatch37/viewport-fixture",
            )
            result = await browser_qa.browser_track_boxes_stop(session_id, started["measurement_id"])
            return result["elements"][0]["samples"][0]["width"]

        async def probe_dsf(tab_handle, url):
            started = await browser_qa.browser_track_boxes_start(
                session_id=session_id,
                tab_handle=tab_handle,
                selectors=["#dpr-probe"],
                expected_url=url,
                interval_ms=50,
                max_samples=1,
                duration_ms=250,
                out_subdir="dispatch37/viewport-fixture",
            )
            result = await browser_qa.browser_track_boxes_stop(session_id, started["measurement_id"])
            return result["elements"][0]["samples"][0]["width"]

        try:
            with local_server() as (origin, _port):
                urls = [f"{origin}/viewport-fixture/one", f"{origin}/viewport-fixture/two"]
                for url in urls:
                    opened = await browser_qa.browser_open_tab(session_id)
                    owned.append(opened["tab_handle"])
                    self.assertEqual((await browser_qa.browser_navigate(session_id, url))["status"], 200)
                    async with browser_qa._session(session_id) as caller:
                        page = caller["owned_tab_pages"][owned[-1]]
                        await page.set_content(html, wait_until="domcontentloaded")
                    pages.append(page)

                original_first = await viewport_snapshot(pages[0])
                original_second = await viewport_snapshot(pages[1])
                original_dsf_probe = await probe_dsf(owned[0], urls[0])
                original_second_dsf = await probe_dsf(owned[1], urls[1])
                self.assertAlmostEqual(await probe_width(owned[0], urls[0]), original_first["width"], delta=1)
                self.assertAlmostEqual(await probe_width(owned[1], urls[1]), original_second["width"], delta=1)

                applied = await browser_qa.browser_set_viewport(
                    session_id=session_id,
                    tab_handle=owned[0],
                    width=1600,
                    height=1000,
                    device_scale_factor=1.5,
                    expected_url=urls[0],
                )
                self.assertAlmostEqual(applied["effective_viewport"]["width"], 1600, delta=1)
                self.assertAlmostEqual(applied["effective_viewport"]["height"], 1000, delta=1)
                self.assertEqual(applied["effective_viewport"]["device_scale_factor"], 1.5)
                self.assertAlmostEqual(applied["original_viewport"]["width"], original_first["width"], delta=1)
                # Test whether the override is visible to a fresh fixture document too.
                async with browser_qa._session(session_id) as caller:
                    await pages[0].set_content(html, wait_until="domcontentloaded")
                self.assertAlmostEqual(await probe_width(owned[0], urls[0]), applied["effective_viewport"]["width"], delta=1)
                override_cdp = browser_qa._SESSIONS[session_id]["viewport_overrides"][owned[0]]["cdp_session"]
                overridden = await viewport_snapshot(pages[0], override_cdp)
                actual_dsf_probe = await probe_dsf(owned[0], urls[0])
                self.assertEqual(actual_dsf_probe, 150, {
                    "effective_viewport": applied["effective_viewport"],
                    "original_screenshot": original_first["pixel_size"],
                    "override_screenshot": overridden["pixel_size"],
                    "original_dsf_probe": original_dsf_probe,
                })
                self.assertAlmostEqual(overridden["pixel_size"][0] / overridden["width"], 1.5, delta=0.01)
                self.assertAlmostEqual(overridden["pixel_size"][1] / overridden["height"], 1.5, delta=0.01)
                drag_viewport = await browser_qa._drag_viewport_size(pages[0])
                self.assertAlmostEqual(drag_viewport[0], applied["effective_viewport"]["width"], delta=1)
                self.assertAlmostEqual(drag_viewport[1], applied["effective_viewport"]["height"], delta=1)
                unchanged_second = await viewport_snapshot(pages[1])
                unchanged_second_dsf = await probe_dsf(owned[1], urls[1])
                self.assertAlmostEqual(unchanged_second["width"], original_second["width"], delta=1)
                self.assertAlmostEqual(unchanged_second["height"], original_second["height"], delta=1)
                self.assertEqual(unchanged_second["pixel_size"], original_second["pixel_size"])
                self.assertEqual(unchanged_second_dsf, original_second_dsf)
                await pages[0].bring_to_front()
                drag = await browser_qa.browser_drag(
                    session_id=session_id,
                    tab_handle=owned[0],
                    source_selector="#far-source",
                    target_selector="#far-target",
                    expected_url=urls[0],
                    steps=12,
                    timeout_sec=3,
                    hold_ms=50,
                    settle_ms=180,
                )
                self.assertTrue(drag["url_guard_matched_after_settle"])
                self.assertEqual(drag["settle_ms"], 180)
                self.assertGreaterEqual(drag["release_to_return_ms"], 175)

                reset = await browser_qa.browser_reset_viewport(
                    session_id, owned[0], expected_url_prefix=f"{origin}/viewport-fixture/one"
                )
                self.assertAlmostEqual(reset["restored_viewport"]["width"], original_first["width"], delta=1)
                self.assertAlmostEqual(await probe_width(owned[0], urls[0]), original_first["width"], delta=1)
                self.assertEqual(await probe_dsf(owned[0], urls[0]), original_dsf_probe)
                restored_snapshot = await viewport_snapshot(pages[0])
                self.assertAlmostEqual(restored_snapshot["width"], original_first["width"], delta=1)
                self.assertAlmostEqual(restored_snapshot["height"], original_first["height"], delta=5)

                for requested_interval in (50, 100):
                    measurement = await browser_qa.browser_capture_frames_start(
                        session_id=session_id,
                        tab_handle=owned[0],
                        expected_url=urls[0],
                        interval_ms=requested_interval,
                        max_frames=20,
                        duration_ms=2_000,
                        clip_rect={"x": 0, "y": 0, "width": 320, "height": 80},
                        quality=30,
                        out_subdir="dispatch37/viewport-fixture",
                    )
                    await asyncio.wait_for(
                        browser_qa._MEASUREMENTS[measurement["measurement_id"]]["task"], timeout=5
                    )
                    result = await browser_qa.browser_capture_frames_stop(
                        session_id, measurement["measurement_id"]
                    )
                    live_frame_results.append(result)
                    self.assertEqual(result["requested_interval_ms"], requested_interval)
                    self.assertGreaterEqual(result["sample_count"], 2)
                    self.assertIn("actual_interval_ms", result)
                    self.assertIn("capture_duration_ms", result)

                # Close one viewport-overridden owned tab through the normal cleanup path.
                await browser_qa.browser_set_viewport(
                    session_id, owned[1], 1400, 900, device_scale_factor=1.25, expected_url=urls[1]
                )
                closed = await browser_qa.browser_close_tab(session_id, owned[1])
                self.assertTrue(closed["closed"])
                owned.pop(1)
                pages.pop(1)

                # Leave the remaining owned tab overridden so detach must clear it while the page persists.
                await browser_qa.browser_set_viewport(
                    session_id, owned[0], 1500, 900, device_scale_factor=1.25, expected_url=urls[0]
                )
                await browser_qa.browser_detach(session_id)
                session_attached = False
                self.assertFalse(pages[0].is_closed())
                detached_viewport = await viewport_snapshot(pages[0])
                self.assertAlmostEqual(detached_viewport["width"], original_first["width"], delta=1)
                self.assertAlmostEqual(detached_viewport["height"], original_first["height"], delta=5)
                await pages[0].close()
                owned.clear()
                pages.clear()

            verify_session = (await browser_qa.browser_attach_edge(timeout_sec=5))["session_id"]
            after = await browser_qa.browser_list_tabs(verify_session)
            after_handles = sorted(row["tab_handle"] for row in after["tabs"])
            self.assertEqual(after_handles, before_handles)
            await browser_qa.browser_detach(verify_session)
            await browser_qa.close_all()
            print("LIVE_EDGE_VIEWPORT_RESULT=" + json.dumps({
                "mode": attached.get("mode"),
                "before_tab_count": len(before_handles),
                "after_tab_count": len(after_handles),
                "tab_identity_equal": after_handles == before_handles,
                "original_first": original_first,
                "applied": applied["effective_viewport"],
                "overridden_pixels": overridden["pixel_size"],
                "original_dsf_probe_width": original_dsf_probe,
                "applied_dsf_probe_width": 150,
                "other_tab_unchanged": unchanged_second == original_second,
                "reset_width": reset["effective_viewport"]["width"],
                "detach_restored_width": detached_viewport["width"],
                "drag_settle_ms": drag["settle_ms"],
                "drag_release_to_return_ms": drag["release_to_return_ms"],
                "frame_intervals": [{
                    "requested_interval_ms": result["requested_interval_ms"],
                    "actual_interval_ms": result["actual_interval_ms"],
                    "capture_duration_ms": result["capture_duration_ms"],
                    "dropped_frames": result["dropped_frames"],
                    "failed_frames": result["failed_frames"],
                    "warning": result["warning"],
                    "manifest_path": result["manifest_path"],
                } for result in live_frame_results],
            }, separators=(",", ":")))
        finally:
            if session_attached:
                for handle in list(owned):
                    try:
                        await browser_qa.browser_close_tab(session_id, handle)
                    except Exception:
                        page = browser_qa._SESSIONS.get(session_id, {}).get("owned_tab_pages", {}).get(handle)
                        if page is not None:
                            try:
                                await page.close()
                            except Exception:
                                pass
                try:
                    await browser_qa.browser_detach(session_id)
                except Exception:
                    pass
            else:
                for page in list(pages):
                    try:
                        if not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
            await browser_qa.close_all()


if __name__ == "__main__":
    unittest.main()
