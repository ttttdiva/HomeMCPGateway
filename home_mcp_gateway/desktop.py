"""Physical-pixel Windows desktop/window inspection and native MCP PNG responses."""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
from datetime import datetime, timezone
import json
import threading
from typing import Literal
import uuid
import ctypes
from ctypes import wintypes as w
import os
from typing import Any

from mcp.types import CallToolResult, TextContent
from PIL import ImageGrab
import psutil

from .qa_common import image_result
from . import desktop_input, desktop_uia


def _user32():
    if os.name != "nt":
        raise RuntimeError("Window/monitor inspection requires Windows and an interactive desktop session")
    user = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsWindowVisible", "IsIconic", "IsZoomed"):
        fn = getattr(user, name)
        fn.argtypes, fn.restype = [w.HWND], w.BOOL
    user.GetForegroundWindow.restype = w.HWND
    user.GetWindowTextLengthW.argtypes = [w.HWND]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.ClientToScreen.argtypes = [w.HWND, ctypes.POINTER(w.POINT)]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    return user


@contextmanager
def _physical_pixels(user):
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    try:
        yield
    finally:
        if previous:
            user.SetThreadDpiAwarenessContext(previous)


def _bounds(rect) -> dict:
    return {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
            "width": rect.right - rect.left, "height": rect.bottom - rect.top}


def list_monitors() -> dict[str, Any]:
    """List physical-pixel monitor bounds. Pass a returned 1-based monitor index to screenshot_image; 0 captures all."""
    user = _user32()
    monitors = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    user.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
    @callback_type
    def collect(handle, dc, rect, param):
        monitors.append({"monitor": len(monitors) + 1, **_bounds(rect.contents)})
        return True
    with _physical_pixels(user):
        if not user.EnumDisplayMonitors(None, None, collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    return {"monitors": monitors, "coordinate_space": "physical_pixels"}


def screenshot_image(monitor: int = 0) -> CallToolResult:
    """Return desktop PNG as MCP ImageContent with width/height. monitor=0 captures the entire virtual desktop, 1+ selects list_monitors index."""
    user = _user32()
    monitors = list_monitors()["monitors"]
    if monitor < 0 or monitor > len(monitors) or not monitors:
        raise ValueError(f"Invalid monitor {monitor}; available monitors: {monitors}")
    selected = monitors[monitor - 1] if monitor else {
        "left": min(m["left"] for m in monitors), "top": min(m["top"] for m in monitors),
        "right": max(m["right"] for m in monitors), "bottom": max(m["bottom"] for m in monitors)}
    bbox = tuple(selected[k] for k in ("left", "top", "right", "bottom"))
    with _physical_pixels(user):
        picture = ImageGrab.grab(bbox=bbox, all_screens=True, include_layered_windows=True)
    return image_result(picture, monitor=monitor, bounds=selected, coordinate_space="physical_pixels")


def _window_info(user, hwnd: int) -> dict:
    if not user.IsWindow(hwnd):
        raise ValueError(f"Window HWND {hwnd} no longer exists")
    text = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
    user.GetWindowTextW(hwnd, text, len(text))
    pid, rect = w.DWORD(), w.RECT()
    user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError(ctypes.get_last_error())
    name, create_time = None, None
    try:
        process = psutil.Process(pid.value)
        name, create_time = process.name(), process.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return {"hwnd": hwnd, "hwnd_hex": hex(hwnd), "title": text.value, "pid": pid.value,
            "process_name": name, "process_create_time": create_time, "bounds": _bounds(rect),
            "foreground": user.GetForegroundWindow() == hwnd, "visible": bool(user.IsWindowVisible(hwnd)),
            "minimized": bool(user.IsIconic(hwnd)), "maximized": bool(user.IsZoomed(hwnd))}


def list_windows(include_hidden: bool = False, title_contains: str = "", pid: int | None = None) -> dict[str, Any]:
    """List top-level Windows HWND/title/PID/process/bounds/foreground/minimized/maximized state in physical pixels."""
    user = _user32()
    windows, errors = [], []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    @callback_type
    def collect(hwnd, param):
        try:
            if include_hidden or user.IsWindowVisible(hwnd):
                info = _window_info(user, hwnd)
                if title_contains.lower() in info["title"].lower() and (pid is None or info["pid"] == pid):
                    windows.append(info)
        except (OSError, ValueError) as exc:
            errors.append({"hwnd": hwnd, "error": str(exc)})
        return True
    with _physical_pixels(user):
        if not user.EnumWindows(collect, 0):
            raise ctypes.WinError(ctypes.get_last_error())
    return {"windows": windows, "errors": errors, "coordinate_space": "physical_pixels"}


def _client_bounds(user, hwnd: int) -> dict:
    rect, origin = w.RECT(), w.POINT()
    if not user.GetClientRect(hwnd, ctypes.byref(rect)) or not user.ClientToScreen(hwnd, ctypes.byref(origin)):
        raise ctypes.WinError(ctypes.get_last_error())
    return _bounds(w.RECT(origin.x, origin.y, origin.x + rect.right, origin.y + rect.bottom))


def capture_window(hwnd: int, expected_pid: int | None = None) -> CallToolResult:
    """Capture one HWND using Pillow's Windows window capture as MCP PNG. Minimized/hidden/invalid windows report errors. Optional PID verifies ownership."""
    user = _user32()
    with _physical_pixels(user):
        info = _window_info(user, hwnd)
        if expected_pid is not None and info["pid"] != expected_pid:
            raise ValueError(f"HWND ownership changed: expected PID {expected_pid}, found {info['pid']}")
        if info["minimized"] or not info["visible"]:
            raise ValueError(f"Cannot capture minimized or hidden window: {info}")
        if info["bounds"]["width"] <= 0 or info["bounds"]["height"] <= 0:
            raise ValueError(f"Window has empty bounds: {info}")
        client = _client_bounds(user, hwnd)
        picture = ImageGrab.grab(window=hwnd)
        # HWND capture may exclude the frame; image coordinates must use the
        # actual captured client origin, not the outer title-bar/border origin.
        if picture.size == (client["width"], client["height"]):
            capture_bounds, capture_area = client, "client"
        elif picture.size == (info["bounds"]["width"], info["bounds"]["height"]):
            capture_bounds, capture_area = info["bounds"], "window"
        else:
            raise ValueError("capture_geometry_mismatch: PNG dimensions do not match client/window bounds; use desktop capture")
        after = _window_info(user, hwnd)
        if (after["pid"] != info["pid"] or after["minimized"] or after["bounds"] != info["bounds"]
                or _client_bounds(user, hwnd) != client):
            raise ValueError("stale_observation: window changed ownership/state/geometry during capture; observe again")
    return image_result(picture, window=info, bounds=capture_bounds, capture_area=capture_area,
                        capture_method="Pillow HWND/PrintWindow",
                        note="GPU/protected content may render blank; inspect the image or use desktop capture.")




# Only these two functions are MCP tools. Capture helpers above remain internal.
_LOCK = threading.RLock()
_OBSERVATIONS: OrderedDict[str, dict] = OrderedDict()
_OBSERVATION_LIMIT = 64
IMPLEMENTATION = "windows-computer-use-3-visual-first"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identity(user, hwnd: int, expected_pid: int | None = None, previous: dict | None = None) -> dict:
    info = _window_info(user, hwnd)
    if expected_pid is not None and info["pid"] != expected_pid:
        raise ValueError(f"stale_observation: HWND ownership changed; expected PID {expected_pid}, found {info['pid']}")
    if previous:
        if info["pid"] != previous["pid"] or (previous.get("process_create_time") is not None
                and info.get("process_create_time") != previous["process_create_time"]):
            raise ValueError("stale_observation: HWND/PID process identity changed; observe again")
    return info


def _remember(identifier: str, snapshot: dict) -> None:
    _OBSERVATIONS[identifier] = snapshot
    while len(_OBSERVATIONS) > _OBSERVATION_LIMIT:
        _OBSERVATIONS.popitem(last=False)


def desktop_observe(
    mode: Literal["windows", "desktop", "monitor", "window", "uia"] = "windows",
    hwnd: int | None = None,
    monitor: int = 1,
    expected_pid: int | None = None,
    title_contains: str = "",
    include_hidden: bool = False,
    with_uia: bool = False,
    query: str = "",
    role: str = "",
    max_elements: int = 80,
    max_depth: int = 16,
    max_nodes: int = 2000,
) -> CallToolResult:
    """Observe the real logged-in Windows desktop; no isolated browser.

    For a user request about something currently visible on screen, start here:
    list windows, then observe the intended window (normally with_uia=True),
    inspect the returned image/UIA, and interact with that visible target
    directly. Prefer this visual/direct path over indirect global controls such
    as media keys, browser-wide media panels, or shell automation; use those
    only as fallbacks when the requested visible target cannot be operated.

    mode=windows lists HWND/title/PID/process/bounds/state and monitors.
    desktop/monitor/window return native PNG ImageContent (physical pixels,
    no resizing). Window image bounds describe the captured area, separately
    from window.bounds (outer frame). monitor is 1-based; window/uia require
    hwnd. with_uia adds
    a bounded UI Automation ControlView to a window PNG. query searches Name
    and AutomationId; role is e.g. Button/Edit/CheckBox/ListItem. Limits bound
    results AND traversal. UIA refs are scoped to the returned observation_id.
    Use bounds.left/top + image pixel x/y for desktop_act coordinates, including
    negative coordinates. Pass observation_id and hwnd back to detect stale
    process ownership/geometry. Observations expire after 64 newer observations
    or a Gateway restart. Images may contain blank GPU/protected surfaces.
    """
    if mode not in ("windows", "desktop", "monitor", "window", "uia"):
        raise ValueError(f"Unknown observe mode: {mode}")
    if mode in ("window", "uia") or with_uia:
        if hwnd is None:
            raise ValueError("hwnd is required for window/UI Automation observation")
    if not 1 <= max_elements <= 500 or not 1 <= max_nodes <= 20000 or not 0 <= max_depth <= 64:
        raise ValueError("Use max_elements=1..500, max_nodes=1..20000 and max_depth=0..64")
    with _LOCK:
        user = _user32()
        with _physical_pixels(user):
            identifier = uuid.uuid4().hex
            monitors = list_monitors()["monitors"]
            info = _identity(user, hwnd, expected_pid) if hwnd is not None else None
            foreground_hwnd = user.GetForegroundWindow()
            foreground = None
            if foreground_hwnd:
                try:
                    foreground = _window_info(user, foreground_hwnd)
                except (OSError, ValueError):
                    pass
            metadata = dict(observation_id=identifier, mode=mode, timestamp=_now(),
                            coordinate_space="physical_pixels", monitors=monitors,
                            gateway_pid=os.getpid(), implementation=IMPLEMENTATION)
            if info:
                metadata["window"] = info
            if foreground:
                metadata["foreground_window"] = foreground
            if mode == "windows":
                metadata.update(list_windows(include_hidden, title_contains, expected_pid))
            uia = None
            if mode == "uia" or with_uia:
                uia = desktop_uia.request(dict(mode="observe", hwnd=hwnd, expected_pid=info["pid"],
                    query=query, role=role, max_elements=max_elements, max_depth=max_depth, max_nodes=max_nodes))
                for element in uia["elements"]:
                    element["ref"] = f"{identifier}:{element['ref']}"
                metadata["uia"] = uia
            capture = None
            if mode == "window":
                capture = capture_window(hwnd, info["pid"])
            elif mode in ("desktop", "monitor"):
                capture = screenshot_image(0 if mode == "desktop" else monitor)
            if capture:
                metadata.update(capture.structured_content)
                metadata["frame_id"] = identifier
                metadata["image_origin"] = {k: metadata["bounds"][k] for k in ("left", "top")}
                metadata["image_scale"] = 1
            if info:
                after = _identity(user, hwnd, info["pid"], info)
                if after["bounds"] != info["bounds"]:
                    raise ValueError("stale_observation: window moved during observation; retry")
            metadata["timestamp"] = _now()
            _remember(identifier, dict(window=info, windows=metadata.get("windows", []),
                foreground=foreground, monitors=monitors,
                elements={e["ref"]: e for e in (uia or {}).get("elements", [])},
                max_depth=max_depth, max_nodes=max_nodes, timestamp=metadata["timestamp"]))
            images = [block for block in capture.content if block.type == "image"] if capture else []
            return CallToolResult(content=[TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)), *images],
                                  structuredContent=metadata)


def _action_response(result: dict, *, observe_after: bool, with_uia: bool) -> dict | CallToolResult:
    """Input acceptance and the next observation are not goal verification."""
    result = {**result, "execution_status": "accepted",
              "verification": {"status": "unknown", "kind": "application_effect"},
              "retry_policy": "observe_before_retry"}
    if not observe_after:
        return result
    try:
        captured = desktop_observe(
            mode="window" if result["hwnd"] is not None else "desktop",
            hwnd=result["hwnd"], expected_pid=result.get("target_pid"),
            with_uia=with_uia and result["hwnd"] is not None,
        )
    except Exception as exc:
        # Input already ran. Never turn a failed screenshot into 'not executed'.
        result["observation_error"] = type(exc).__name__
        return result
    result["observation_after"] = captured.structured_content
    images = [block for block in captured.content if block.type == "image"]
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False)), *images],
        structuredContent=result,
    )


def desktop_act(
    action: Literal["move", "click", "double_click", "drag", "scroll", "text", "key", "activate", "restore", "uia"],
    hwnd: int | None = None,
    expected_pid: int | None = None,
    observation_id: str | None = None,
    x: int | None = None,
    y: int | None = None,
    end_x: int | None = None,
    end_y: int | None = None,
    button: Literal["left", "right", "middle"] = "left",
    delta_x: int = 0,
    delta_y: int = 0,
    text: str = "",
    keys: list[str] | None = None,
    duration_ms: int = 350,
    element_ref: str | None = None,
    uia_action: Literal["invoke", "set_value", "toggle", "select", "add_to_selection", "remove_from_selection", "expand", "collapse", "scroll_into_view", "focus"] = "invoke",
    target_guard_ref: str | None = None,
    observe_after: bool = False,
    observe_with_uia: bool = False,
) -> dict[str, Any] | CallToolResult:
    """Act on the real Windows desktop. Mouse/keyboard always use SendInput.

    Prefer an element or coordinate obtained from the target application's most
    recent desktop_observe result. For user-visible state changes, re-observe
    the target afterwards and verify the requested state actually changed;
    successful input delivery alone is not task completion.

    Coordinates are absolute physical pixels (negative coordinates supported).
    click + button=right is right-click; double_click uses button too. drag uses
    x/y -> end_x/end_y with duration_ms. scroll uses wheel units (120=one notch),
    delta_y positive=up, delta_x positive=right; optional x/y moves the cursor.
    text inserts literal Unicode/Japanese, independent of keyboard layout/IME.
    key uses keys=['Ctrl','Shift','a'], ['Enter'], ['F24'], etc.; character keys
    use the target keyboard layout. Use explicit Shift for uppercase shortcuts.
    hwnd restores/activates the target before SendInput. Omit hwnd to operate
    the current desktop/foreground. activate/restore require a target HWND.
    uia requires observation_id + element_ref from desktop_observe; uia_action
    selects the pattern; set_value uses text. UIA never silently substitutes
    for human-like SendInput. Pass observation_id for process/geometry checks;
    stale/missing UIA elements fail, never retarget by index or silently retry.
    Success means the OS accepted input, not that the app finished processing:
    observe again to verify the GUI outcome. UIPI/activation failures are errors.

    target_guard_ref optionally binds a pointer action to an observed UIA ref;
    identity, bounds and hit-test must still match immediately before SendInput.
    It never changes the input to a UIA Invoke. Unbound canvas coordinates remain
    supported. observe_after adds a fresh PNG/metadata to this response;
    observe_with_uia also requests bounded UIA data. Neither verifies the goal.
    """
    if action not in ("move", "click", "double_click", "drag", "scroll", "text", "key", "activate", "restore", "uia"):
        raise ValueError(f"Unknown desktop action: {action}")
    with _LOCK:
        user = desktop_input.configure(_user32())
        with _physical_pixels(user):
            snapshot = None
            if observation_id is not None:
                snapshot = _OBSERVATIONS.get(observation_id)
                if snapshot is None:
                    raise ValueError("stale_observation: unknown/evicted observation_id or Gateway restarted; observe again")
                if snapshot["window"]:
                    observed_hwnd = snapshot["window"]["hwnd"]
                    if hwnd is not None and hwnd != observed_hwnd:
                        raise ValueError("stale_observation: HWND does not match the observation")
                    hwnd = observed_hwnd
            previous = None
            if snapshot and hwnd is not None:
                previous = snapshot["window"] or next((v for v in snapshot["windows"] if v["hwnd"] == hwnd), None)
                if previous is None:
                    raise ValueError("stale_observation: target HWND was not in that observation")
            info = _identity(user, hwnd, expected_pid, previous) if hwnd is not None else None
            pixel_action = action in ("move", "click", "double_click", "drag", "scroll")
            guard_element = None
            if target_guard_ref is not None:
                if not pixel_action or not snapshot or hwnd is None:
                    raise ValueError("target_guard requires a pointer action and a window UIA observation")
                guard_element = snapshot["elements"].get(target_guard_ref)
                if not guard_element or not guard_element.get("actionable"):
                    raise ValueError("stale_observation: target_guard_ref is missing or has no stable identity")
            if snapshot and pixel_action:
                if snapshot["monitors"] != list_monitors()["monitors"]:
                    raise ValueError("stale_observation: monitor layout changed; observe again")
                if previous and previous["bounds"] != info["bounds"]:
                    raise ValueError("stale_observation: window moved/resized; observe again before coordinate input")
            if snapshot and hwnd is None and action in ("text", "key"):
                foreground = snapshot.get("foreground")
                if foreground and user.GetForegroundWindow() != foreground["hwnd"]:
                    raise ValueError("stale_observation: foreground window changed; specify/observe the intended HWND")
                if foreground:
                    _identity(user, foreground["hwnd"], foreground["pid"], foreground)
            if action in ("activate", "restore"):
                if hwnd is None:
                    raise ValueError(f"{action} requires hwnd")
                desktop_input.activate(user, hwnd, restore_only=action == "restore")
                result = dict(backend="Win32", window=_identity(user, hwnd, info["pid"], info))
            elif action == "uia":
                if not snapshot or not element_ref or hwnd is None:
                    raise ValueError("UIA action requires observation_id and element_ref from a window UIA observation")
                element = snapshot["elements"].get(element_ref)
                if element is None:
                    raise ValueError("stale_observation: unknown element_ref; observe the window again")
                if not element.get("actionable", False):
                    raise ValueError("UIA element has no stable identity; use its observed bounds with SendInput")
                # A UIA provider may accept SetFocus for a background window
                # without actually acquiring foreground keyboard focus. Focus
                # explicitly targets the real window, unlike read-only observe.
                if uia_action == "focus":
                    desktop_input.activate(user, hwnd)
                    _identity(user, hwnd, info["pid"], info)
                result = desktop_uia.request(dict(mode="act", hwnd=hwnd, expected_pid=info["pid"],
                    runtime_id=element["runtime_id"], role=element["role"], automation_id=element["automation_id"],
                    name=element["name"], process_id=element["process_id"],
                    native_hwnd=element["native_hwnd"], action=uia_action, text=text,
                    max_depth=snapshot["max_depth"], max_nodes=snapshot["max_nodes"]))
            else:
                if hwnd is not None:
                    desktop_input.activate(user, hwnd)
                    after = _identity(user, hwnd, info["pid"], info)
                    if snapshot and pixel_action and previous and after["bounds"] != previous["bounds"]:
                        raise ValueError("stale_observation: activation changed window bounds; observe again")
                if guard_element is not None:
                    desktop_uia.request(dict(
                        mode="guard", hwnd=hwnd, expected_pid=info["pid"],
                        **{key: guard_element[key] for key in (
                            "runtime_id", "role", "automation_id", "name", "process_id", "native_hwnd", "bounds")},
                        x=x, y=y, max_depth=snapshot["max_depth"], max_nodes=snapshot["max_nodes"],
                    ))
                    after = _identity(user, hwnd, info["pid"], info)
                    if after["bounds"] != previous["bounds"] or user.GetForegroundWindow() != hwnd:
                        raise ValueError("stale_observation: guarded target window changed during hit-test")
                result = desktop_input.perform(user, action, hwnd=hwnd, x=x, y=y, end_x=end_x, end_y=end_y,
                    button=button, delta_x=delta_x, delta_y=delta_y, text=text, keys=keys, duration_ms=duration_ms)
            return _action_response(
                {**result, "ok": True, "action": action, "timestamp": _now(), "observation_id": observation_id,
                 "hwnd": hwnd, "target_pid": info["pid"] if info else None,
                 "target_guard": "verified" if guard_element is not None else "not_requested",
                 "foreground_hwnd": user.GetForegroundWindow()},
                observe_after=observe_after, with_uia=observe_with_uia,
            )


TOOLS = [desktop_observe, desktop_act]
