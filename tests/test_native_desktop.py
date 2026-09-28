"""Opt-in, real Windows GUI / SendInput / UIA through the actual MCP transport."""
import base64
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest

import anyio
from mcp.client import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from PIL import Image

from home_mcp_gateway import jobs


@unittest.skipUnless(os.name == "nt" and os.environ.get("HOME_MCP_DESKTOP_TESTS") == "1",
                     "Set HOME_MCP_DESKTOP_TESTS=1 in an interactive Windows session")
class NativeDesktopTests(unittest.TestCase):
    def test_native_desktop_window_and_input(self):
        async def check(directory):
            launcher = Path(__file__).resolve().parents[1] / "scripts/gateway_stdio.py"
            fixture = Path(__file__).with_name("native_window_fixture.py").resolve()
            params = StdioServerParameters(command=sys.executable, args=[str(launcher)],
                                           env={"HOME_MCP_AOITALK_ENABLED": "false"})
            job_id = None
            evidence = os.environ.get("HOME_MCP_DESKTOP_EVIDENCE_DIR")
            report = {"transport": "MCP stdio", "steps": []}

            def state():
                try:
                    return json.loads((Path(directory) / "state.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    return {}

            async def wait(predicate, description):
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    current = state()
                    if current and predicate(current):
                        return current
                    await anyio.sleep(.1)
                diagnostic = {"description": description, "state": state()}
                try:
                    observed = await session.call_tool("desktop_observe", {})
                    foreground = observed.structured_content.get("foreground_window", {})
                    diagnostic["foreground"] = {k: foreground.get(k) for k in ("hwnd", "pid", "process_name")}
                except Exception as exc:
                    diagnostic["observation_error"] = str(exc)
                if evidence:
                    Path(evidence).mkdir(parents=True, exist_ok=True)
                    (Path(evidence) / "failure-state.json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
                self.fail(f"GUI did not report {description}; diagnostic={diagnostic}")

            def png(result, filename=None, require_content=True):
                block = next(c for c in result.content if c.type == "image")
                self.assertEqual(block.mime_type, "image/png")
                raw = base64.b64decode(block.data)
                with Image.open(io.BytesIO(raw)) as image:
                    image.load()
                    self.assertGreater(image.width, 100)
                    self.assertGreater(image.height, 100)
                    self.assertEqual(result.structured_content["width"], image.width)
                    has_content = max(high - low for low, high in image.convert("RGB").getextrema()) > 30
                    if require_content:
                        self.assertTrue(has_content, "PNG has no visible content")
                if evidence and filename:
                    Path(evidence).mkdir(parents=True, exist_ok=True)
                    (Path(evidence) / filename).write_bytes(raw)
                return has_content

            def element(observation, name=None, role=None):
                entries = observation.structured_content["uia"]["elements"]
                matches = [e for e in entries if (name is None or e["name"] == name) and (role is None or e["role"] == role)]
                self.assertTrue(matches, json.dumps(observation.structured_content["uia"], ensure_ascii=True))
                return matches[0]

            def center(entry):
                b = entry["bounds"]
                return {"x": round((b["left"] + b["right"]) / 2), "y": round((b["top"] + b["bottom"]) / 2)}

            try:
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()

                        async def call(name, arguments):
                            result = await session.call_tool(name, arguments)
                            self.assertFalse(result.is_error, " | ".join(c.text for c in result.content if c.type == "text")[:4000])
                            return result

                        listing = await session.list_tools()
                        names = {t.name for t in listing.tools}
                        self.assertEqual(len(names), 76)
                        self.assertTrue({"desktop_observe", "desktop_act"}.issubset(names))
                        self.assertFalse({"list_monitors", "list_windows", "capture_window", "screenshot_image"} & names)
                        report["tool_count"] = len(names)
                        started = await call("job_start", {"command": [sys.executable, str(fixture), directory], "runtime_dir": directory})
                        self.assertEqual(started.structured_content["status"], "running", started.structured_content)
                        job_id = started.structured_content["job_id"]
                        try:
                            initial = await wait(lambda s: s.get("hwnd"), "fixture startup")
                        except AssertionError:
                            logs = await call("job_tail", {"job_id": job_id, "runtime_dir": directory})
                            self.fail(f"Fixture startup failed: {logs.structured_content}")
                        hwnd, pid = initial["hwnd"], initial["pid"]
                        target = {"hwnd": hwnd, "expected_pid": pid}
                        windows = await call("desktop_observe", {"title_contains": "Home MCP native QA fixture"})
                        self.assertTrue(any(w["hwnd"] == hwnd for w in windows.structured_content["windows"]))
                        report["monitors"] = windows.structured_content["monitors"]
                        report["fixture_pid"] = pid
                        report["gateway_pid"] = windows.structured_content["gateway_pid"]
                        for args in ({"mode": "desktop"}, *({"mode": "monitor", "monitor": m["monitor"]} for m in report["monitors"])):
                            png(await call("desktop_observe", args))
                        await call("desktop_act", {"action": "activate", **target})
                        await wait(lambda s: s["form_focused"], "fixture GUI activation")
                        observed = await call("desktop_observe", {"mode": "window", "with_uia": True, **target})
                        png(observed, "fixture-before.png")
                        if evidence:
                            (Path(evidence) / "initial-uia.json").write_text(json.dumps(observed.structured_content, ensure_ascii=False, indent=2), encoding="utf-8")
                        self.assertEqual(observed.structured_content["frame_id"], observed.structured_content["observation_id"])
                        self.assertLessEqual(len(observed.structured_content["uia"]["elements"]), 80)
                        report["steps"].append("windows, virtual/each-monitor/window PNG, bounded UIA, client-image origin")
                        print("Native desktop: capture and UIA discovery passed", flush=True)

                        async def act(action, **args):
                            return await call("desktop_act", {"action": action, **target, **args})

                        oid = observed.structured_content["observation_id"]
                        # Use PNG pixels, not UIA bounds, to detect frame/client
                        # origin mismatches in human-like screenshot clicking.
                        image_bounds = observed.structured_content["bounds"]
                        self.assertEqual((image_bounds["width"], image_bounds["height"]),
                                         (observed.structured_content["width"], observed.structured_content["height"]))
                        self.assertEqual(observed.structured_content["capture_area"], "client")
                        await act("click", observation_id=oid,
                                  x=image_bounds["left"] + 85, y=image_bounds["top"] + 39)
                        await wait(lambda s: s["clicks"] == 1, "SendInput button click")
                        # Re-observe between interaction phases; a snapshot taken
                        # before activation/layout/input is not a durable locator.
                        entry_view = await call("desktop_observe", {"mode": "uia", **target, "query": "Literal text", "role": "Edit"})
                        await act("click", observation_id=entry_view.structured_content["observation_id"],
                                  **center(element(entry_view, "Literal text", "Edit")))
                        literal = "literal + & ^ % {braces} "
                        japanese = "日本語テスト 😀"
                        await act("text", text=literal)
                        await wait(lambda s: s["text"] == literal, "literal text")
                        await act("text", text=japanese)
                        await wait(lambda s: s["text"] == literal + japanese, "Japanese and supplementary Unicode")
                        await act("key", keys=["Ctrl", "a"])
                        await wait(lambda s: s["selection_length"] == len((literal + japanese).encode("utf-16-le")) // 2, "Ctrl+A selection")
                        await act("text", text="置換した日本語")
                        await wait(lambda s: s["text"] == "置換した日本語", "replacement after Ctrl+A")
                        await act("key", keys=["Ctrl", "Shift", "F12"])
                        await wait(lambda s: s["shortcuts"] == 1, "Ctrl+Shift+F12")
                        await act("key", keys=["Alt", "F11"])
                        await wait(lambda s: s["alt_shortcuts"] == 1, "Alt+F11")
                        await act("key", keys=["End"])
                        await act("key", keys=["Enter"])
                        await act("text", text="second line")
                        await wait(lambda s: "\nsecond line" in s["text"], "Enter and second line")
                        await act("key", keys=["Backspace"])
                        await wait(lambda s: s["text"].endswith("second lin"), "Backspace")
                        checkbox_view = await call("desktop_observe", {"mode": "uia", **target, "query": "Option", "role": "CheckBox"})
                        await act("click", observation_id=checkbox_view.structured_content["observation_id"],
                                  **center(element(checkbox_view, "Option", "CheckBox")))
                        await wait(lambda s: s["checked"], "SendInput checkbox")
                        report["steps"].append("SendInput click, literal/Japanese/surrogate text, Ctrl+A, Ctrl+Shift+F12, Alt+F11, Enter, Backspace, checkbox")
                        print("Native desktop: mouse, Japanese and shortcuts passed", flush=True)

                        # Pattern actions are separate from human-like SendInput and checked by GUI state.
                        async def pattern(name, role, operation, **kwargs):
                            current = await call("desktop_observe", {"mode": "uia", **target, "query": name, "role": role})
                            item = element(current, name, role)
                            self.assertIn(operation, item["actions"], item)
                            return await act("uia", observation_id=current.structured_content["observation_id"],
                                             element_ref=item["ref"], uia_action=operation, **kwargs)

                        await pattern("Apply", "Button", "invoke")
                        await wait(lambda s: s["clicks"] == 2, "UIA Invoke")
                        await pattern("Literal text", "Edit", "set_value", text="UIA 日本語")
                        await wait(lambda s: s["text"] == "UIA 日本語", "UIA Value")
                        await pattern("Literal text", "Edit", "focus")
                        await wait(lambda s: s["entry_focused"], "UIA SetFocus GUI focus")
                        focused = await call("desktop_observe", {"mode": "uia", **target, "role": "Edit", "query": "Literal text"})
                        self.assertTrue(element(focused, "Literal text", "Edit")["keyboard_focused"])
                        self.assertEqual(element(focused, "Literal text", "Edit")["state"]["value"], "UIA 日本語")
                        await pattern("Option", "CheckBox", "toggle")
                        await wait(lambda s: not s["checked"], "UIA Toggle")
                        await pattern("Item 02", "ListItem", "select")
                        await wait(lambda s: s["list_selected"] == "Item 02", "UIA SelectionItem")
                        await pattern("Item 35", "ListItem", "scroll_into_view")
                        visible_item = await call("desktop_observe", {"mode": "uia", **target, "query": "Item 35"})
                        self.assertFalse(element(visible_item, "Item 35", "ListItem")["offscreen"])
                        await pattern("Choice", "ComboBox", "expand")
                        expanded = await call("desktop_observe", {"mode": "uia", **target, "role": "ComboBox"})
                        self.assertEqual(element(expanded, "Choice", "ComboBox")["state"]["expand_collapse_state"], "Expanded")
                        await pattern("Choice", "ComboBox", "collapse")
                        collapsed = await call("desktop_observe", {"mode": "uia", **target, "role": "ComboBox"})
                        self.assertEqual(element(collapsed, "Choice", "ComboBox")["state"]["expand_collapse_state"], "Collapsed")
                        report["steps"].append("UIA Invoke, Value, SetFocus, Toggle, SelectionItem, ScrollItem, ExpandCollapse")
                        print("Native desktop: UIA patterns passed", flush=True)

                        current = await call("desktop_observe", {"mode": "window", "with_uia": True, **target})
                        surface = center(element(current, "Scroll region"))
                        await act("click", **surface)
                        await act("scroll", **surface, delta_y=-360)
                        await wait(lambda s: s["vertical_events"] > 0 and s["vertical_value"] > 0, "vertical wheel scrolling")
                        await act("scroll", **surface, delta_x=240)
                        await wait(lambda s: s["horizontal_events"] > 0 and s["horizontal_value"] > 0, "horizontal wheel scrolling")
                        drag = element(current, "Drag region")["bounds"]
                        point = {"x": int(drag["left"] + 70), "y": int(drag["top"] + 65)}
                        await act("move", **point)
                        await act("click", button="right", **point)
                        await wait(lambda s: s["right_clicks"] > 0, "right click")
                        await act("double_click", **point)
                        await wait(lambda s: s["double_clicks"] > 0, "double click")
                        await anyio.sleep(.6)
                        await act("drag", **point, end_x=int(drag["left"] + 560), end_y=int(drag["top"] + 85), duration_ms=500)
                        await wait(lambda s: s["drags"] > 0 and s["drag_end"]["x"] > 500, "drag target movement")
                        report["steps"].append("SendInput move/right/double click, real vertical/horizontal scrolling, drag target")
                        print("Native desktop: scroll, drag, right/double click passed", flush=True)

                        (Path(directory) / "minimize").touch()
                        await wait(lambda s: s["minimized"], "minimize fixture")
                        failed = await session.call_tool("desktop_observe", {"mode": "window", **target})
                        self.assertTrue(failed.is_error)
                        self.assertIn("minimized", failed.content[0].text)
                        await act("restore")
                        await wait(lambda s: not s["minimized"], "restore fixture")
                        await act("activate")
                        restored = await call("desktop_observe", {"mode": "window", "with_uia": True, **target})
                        self.assertTrue(restored.structured_content["window"]["foreground"])
                        report["steps"].append("minimized capture error, restore and foreground activation")

                        negative = next((m for m in report["monitors"] if m["left"] < 0), None)
                        if negative:
                            old_id = restored.structured_content["observation_id"]
                            (Path(directory) / "position.json").write_text(json.dumps({"x": negative["left"] + 100, "y": negative["top"] + 100}), encoding="utf-8")
                            await wait(lambda s: s["controls"]["button"]["x"] < 0, "fixture moved to negative monitor")
                            stale = await session.call_tool("desktop_act", {"action": "click", **target,
                                "observation_id": old_id, **center(element(restored, "Apply", "Button"))})
                            self.assertTrue(stale.is_error)
                            self.assertIn("stale_observation", stale.content[0].text)
                            moved = await call("desktop_observe", {"mode": "window", "with_uia": True, **target})
                            left_button = center(element(moved, "Apply", "Button"))
                            self.assertLess(left_button["x"], 0)
                            sent = await act("click", observation_id=moved.structured_content["observation_id"], **left_button)
                            await wait(lambda s: s["clicks"] == 3, "negative-coordinate button click")
                            for key in ("x", "y"):
                                self.assertLessEqual(abs(sent.structured_content["cursor"][key] - left_button[key]), 1)
                            await act("click", **center(element(moved, "Literal text", "Edit")))
                            await act("key", keys=["Ctrl", "a"])
                            await act("text", text="負座標で日本語入力")
                            await wait(lambda s: s["text"] == "負座標で日本語入力", "Japanese input on negative monitor")
                            report["negative_coordinates_tested"] = True
                            report["steps"].append("moved-window stale rejection, negative-coordinate click/Japanese on second monitor")
                        else:
                            report["negative_coordinates_tested"] = False
                            report["negative_coordinates_note"] = "No negative-coordinate monitor present; numeric mapping is covered by unit tests"

                        wrong_pid = await session.call_tool("desktop_act", {"action": "click", "hwnd": hwnd, "expected_pid": pid + 1, "x": 0, "y": 0})
                        self.assertTrue(wrong_pid.is_error)
                        self.assertIn("ownership changed", wrong_pid.content[0].text)
                        limited = await call("desktop_observe", {"mode": "uia", **target, "max_elements": 2})
                        self.assertEqual(len(limited.structured_content["uia"]["elements"]), 2)
                        self.assertTrue(limited.structured_content["uia"]["truncated"])
                        bad_ref = await session.call_tool("desktop_act", {"action": "uia", **target,
                            "observation_id": limited.structured_content["observation_id"], "element_ref": "e999"})
                        self.assertTrue(bad_ref.is_error)
                        final = await call("desktop_observe", {"mode": "window", "with_uia": True, **target})
                        png(final, "fixture-after.png")
                        report["final_state"] = state()
                        report["steps"].append("final PNG/UIA observation, PID mismatch and invalid reference diagnostics")
                        all_windows = await call("desktop_observe", {})
                        edge = next((w for w in all_windows.structured_content["windows"]
                                     if (w.get("process_name") or "").lower() == "msedge.exe" and not w["minimized"]
                                     and w["title"] and w["bounds"]["width"] > 300 and w["bounds"]["height"] > 200), None)
                        report["edge_read_only"] = "no visible Edge window"
                        if edge:
                            captured = await call("desktop_observe", {"mode": "window", "hwnd": edge["hwnd"], "expected_pid": edge["pid"]})
                            has_content = png(captured, require_content=False)
                            edge_report = {"hwnd": edge["hwnd"], "pid": edge["pid"], "png": True,
                                           "hwnd_png_has_content": has_content,
                                           "width": captured.structured_content["width"], "height": captured.structured_content["height"],
                                           "capture_area": captured.structured_content["capture_area"]}
                            # PrintWindow can return a valid but blank GPU-backed
                            # surface. Explicitly verify the documented desktop
                            # alternative; do not misreport blank HWND pixels as
                            # a successful visual observation or activate Edge.
                            if not has_content:
                                screen = await call("desktop_observe", {"mode": "desktop"})
                                png(screen)
                                block = next(c for c in screen.content if c.type == "image")
                                origin = screen.structured_content["image_origin"]
                                b = captured.structured_content["bounds"]
                                with Image.open(io.BytesIO(base64.b64decode(block.data))) as image:
                                    box = (max(0, b["left"] - origin["left"]), max(0, b["top"] - origin["top"]),
                                           min(image.width, b["right"] - origin["left"]), min(image.height, b["bottom"] - origin["top"]))
                                    self.assertGreater(max(hi - lo for lo, hi in image.crop(box).convert("RGB").getextrema()), 30)
                                edge_report["desktop_region_has_content"] = True
                            inspected = await call("desktop_observe", {"mode": "uia", "hwnd": edge["hwnd"], "expected_pid": edge["pid"],
                                                                       "max_elements": 8, "max_depth": 4})
                            self.assertTrue(inspected.structured_content["uia"]["elements"])
                            edge_report["uia_elements"] = len(inspected.structured_content["uia"]["elements"])
                            report["edge_read_only"] = edge_report
                        report["passed"] = True
                        if evidence:
                            (Path(evidence) / "native-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                        print("NATIVE_DESKTOP_REPORT=" + json.dumps(report, ensure_ascii=True), flush=True)
            finally:
                if job_id:
                    (Path(directory) / "close").touch()
                    await anyio.sleep(.3)
                    stopped = await anyio.to_thread.run_sync(lambda: jobs.job_stop(job_id, runtime_dir=directory))
                    end = time.monotonic() + 5
                    while jobs.identity(stopped.get("worker_pid"), stopped.get("worker_create_time")) == "alive" and time.monotonic() < end:
                        await anyio.sleep(.05)
        with tempfile.TemporaryDirectory() as directory:
            anyio.run(check, directory)
