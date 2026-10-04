"""Physical-pixel Windows desktop/window inspection and native MCP PNG responses."""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
from datetime import datetime, timezone
import json
import math
import re
import threading
import time
from typing import Literal
import uuid
import ctypes
from ctypes import wintypes as w
import os
from typing import Any

from mcp.types import CallToolResult, TextContent
from PIL import Image, ImageGrab
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


def _dwm_frame(hwnd: int) -> dict | None:
    """DWM extended frame bounds: the visible window rectangle (no invisible resize border)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        rect = w.RECT()
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(rect), ctypes.sizeof(rect)) == 0:  # DWMWA_EXTENDED_FRAME_BOUNDS
            return _bounds(rect)
    except (OSError, AttributeError):
        pass
    return None


def _is_cloaked(hwnd: int) -> bool:
    """True for windows DWM hides (other virtual desktops, suspended UWP shells)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
        dwm.DwmGetWindowAttribute.restype = ctypes.c_long
        value = w.DWORD(0)
        if dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value)) == 0:  # DWMWA_CLOAKED
            return bool(value.value)
    except (OSError, AttributeError):
        pass
    return False


def _monitor_dpi(user, handle) -> int | None:
    try:
        shcore = ctypes.WinDLL("shcore")
        dpi_x, dpi_y = w.UINT(), w.UINT()
        shcore.GetDpiForMonitor.argtypes = [w.HMONITOR, ctypes.c_int, ctypes.POINTER(w.UINT), ctypes.POINTER(w.UINT)]
        shcore.GetDpiForMonitor.restype = ctypes.c_long
        if shcore.GetDpiForMonitor(handle, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)) == 0:  # MDT_EFFECTIVE_DPI
            return int(dpi_x.value)
    except Exception:
        pass
    return None


def _window_geometry(user, hwnd: int, info: dict, client: dict) -> dict:
    """Everything a client needs to turn an image position into a screen position.

    All rectangles are physical screen pixels. scale_factor is the *monitor* DPI
    scale (1.5 = 150%). window_dpi differs for DPI-unaware applications, but
    this gateway always reads and injects physical pixels so it is informational.
    """
    monitor_dpi = window_dpi = awareness = None
    try:
        user.MonitorFromWindow.argtypes = [w.HWND, w.DWORD]
        user.MonitorFromWindow.restype = w.HMONITOR
        monitor_dpi = _monitor_dpi(user, user.MonitorFromWindow(hwnd, 2))  # MONITOR_DEFAULTTONEAREST
        user.GetDpiForWindow.argtypes = [w.HWND]
        user.GetDpiForWindow.restype = w.UINT
        window_dpi = int(user.GetDpiForWindow(hwnd)) or None
        user.GetWindowDpiAwarenessContext.argtypes = [w.HWND]
        user.GetWindowDpiAwarenessContext.restype = ctypes.c_void_p
        user.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
        awareness = {0: "unaware", 1: "system", 2: "per_monitor"}.get(
            user.GetAwarenessFromDpiAwarenessContext(user.GetWindowDpiAwarenessContext(hwnd)))
    except Exception:  # geometry is informational; never fail an observation over DPI probing
        pass
    dpi = monitor_dpi or window_dpi
    center_x = info["bounds"]["left"] + info["bounds"]["width"] / 2
    center_y = info["bounds"]["top"] + info["bounds"]["height"] / 2
    monitor = next((m.get("monitor") for m in list_monitors()["monitors"]
                    if m["left"] <= center_x < m["right"] and m["top"] <= center_y < m["bottom"]), None)
    return {
        "window_bounds": info["bounds"], "extended_frame_bounds": _dwm_frame(hwnd),
        "client_bounds": client, "client_size": {"width": client["width"], "height": client["height"]},
        "monitor": monitor, "monitor_dpi": monitor_dpi, "window_dpi": window_dpi,
        "dpi_awareness": awareness,
        "scale_factor": round(dpi / 96, 4) if dpi else None,
        "scale_percent": round(dpi / 96 * 100) if dpi else None,
    }


def _uniform(picture: Image.Image) -> bool:
    """True when the image is a single flat colour (typical failed GPU/protected capture)."""
    return all(low == high for low, high in picture.convert("RGB").getextrema())


def _wgc_frame(hwnd: int, timeout: float = 3.0) -> Image.Image:
    """One Windows Graphics Capture frame of the whole window, as RGB."""
    try:
        from windows_capture import WindowsCapture
        import numpy
    except Exception as exc:  # optional dependency
        raise RuntimeError(f"windows-capture is not installed ({type(exc).__name__}: {exc}); pip install windows-capture") from exc
    done, box = threading.Event(), {}
    capture = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)

    @capture.event
    def on_frame_arrived(frame, control):
        if "image" not in box:
            try:
                # BGRA -> RGB. The mapped GPU buffer is only valid inside this
                # callback, so copy before signalling.
                box["image"] = Image.fromarray(numpy.ascontiguousarray(frame.frame_buffer[:, :, 2::-1]), "RGB")
            except Exception as exc:
                box["error"] = f"{type(exc).__name__}: {exc}"
        done.set()
        control.stop()

    @capture.event
    def on_closed():
        done.set()

    control = capture.start_free_threaded()
    try:
        if not done.wait(timeout):
            raise TimeoutError(f"no frame within {timeout}s (window minimized, protected, or not capturable)")
    finally:
        try:
            control.stop()
            control.wait()
        except Exception:
            pass
    if "image" not in box:
        raise RuntimeError(box.get("error") or "Windows Graphics Capture closed without a frame")
    return box["image"]


def _capture_wgc(user, hwnd: int, info: dict, client: dict):
    """Windows Graphics Capture reads the DWM-composed surface, so GPU-drawn
    viewports (Blender, games, browsers, Electron) are captured, unlike PrintWindow."""
    frame = _wgc_frame(hwnd)
    window, extended = info["bounds"], _dwm_frame(hwnd)
    # The frame covers the visible frame; locate it on screen to crop the client area.
    if extended and (frame.width, frame.height) == (extended["width"], extended["height"]):
        origin = extended
    elif (frame.width, frame.height) == (window["width"], window["height"]):
        origin = window
    else:
        raise ValueError(f"capture_geometry_mismatch: WGC frame {frame.size} matches neither frame {extended} nor window {window}")
    box = (client["left"] - origin["left"], client["top"] - origin["top"],
           client["right"] - origin["left"], client["bottom"] - origin["top"])
    if box[0] < 0 or box[1] < 0 or box[2] > frame.width or box[3] > frame.height or box[2] <= box[0] or box[3] <= box[1]:
        raise ValueError(f"capture_geometry_mismatch: client area {box} lies outside WGC frame {frame.size}")
    return frame.crop(box), client, "client"


def _capture_printwindow(user, hwnd: int, info: dict, client: dict):
    picture = ImageGrab.grab(window=hwnd)
    # HWND capture may exclude the frame; image coordinates must use the
    # actual captured client origin, not the outer title-bar/border origin.
    if picture.size == (client["width"], client["height"]):
        return picture, client, "client"
    if picture.size == (info["bounds"]["width"], info["bounds"]["height"]):
        return picture, info["bounds"], "window"
    raise ValueError("capture_geometry_mismatch: PNG dimensions do not match client/window bounds; use desktop capture")


def _capture_screen(user, hwnd: int, info: dict, client: dict):
    # Copies whatever is on screen in that rectangle, including windows in front
    # of the target. Only used when requested explicitly.
    bbox = tuple(client[k] for k in ("left", "top", "right", "bottom"))
    return ImageGrab.grab(bbox=bbox, all_screens=True, include_layered_windows=True), client, "client"


_CAPTURE_BACKENDS = {
    "wgc": ("Windows Graphics Capture", _capture_wgc),
    "printwindow": ("Pillow HWND/PrintWindow", _capture_printwindow),
    "screen": ("Screen region copy (may include overlapping windows)", _capture_screen),
}
_BACKEND_ORDER = {"auto": ("wgc", "printwindow"), "wgc": ("wgc",), "printwindow": ("printwindow",), "screen": ("screen",)}


def capture_window(hwnd: int, expected_pid: int | None = None, backend: str = "auto") -> CallToolResult:
    """Capture one HWND's client area as MCP PNG. backend=auto tries Windows Graphics Capture, then PrintWindow.

    Minimized/hidden/invalid windows report errors. Optional PID verifies ownership. A flat single-colour
    result makes auto try the next backend; every attempt is reported in capture_attempts.
    """
    if backend not in _BACKEND_ORDER:
        raise ValueError(f"Unknown capture backend {backend!r}; use one of {sorted(_BACKEND_ORDER)}")
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
        attempts, chosen = [], None
        for name in _BACKEND_ORDER[backend]:
            try:
                picture, capture_bounds, capture_area = _CAPTURE_BACKENDS[name][1](user, hwnd, info, client)
            except Exception as exc:
                attempts.append({"backend": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
                continue
            flat = _uniform(picture)
            attempts.append({"backend": name, "ok": True, "uniform_image": flat})
            if chosen is None or (chosen[4] and not flat):
                chosen = (name, picture, capture_bounds, capture_area, flat)
            if not flat:
                break
        if chosen is None:
            raise ValueError("capture_failed: " + "; ".join(f"{a['backend']}: {a['error']}" for a in attempts))
        name, picture, capture_bounds, capture_area, flat = chosen
        after = _window_info(user, hwnd)
        if (after["pid"] != info["pid"] or after["minimized"] or after["bounds"] != info["bounds"]
                or _client_bounds(user, hwnd) != client):
            raise ValueError("stale_observation: window changed ownership/state/geometry during capture; observe again")
    notes = ["GPU/protected content may render blank; inspect the image or retry with capture_backend='wgc'/'screen'."]
    if flat:
        notes.append("The image is one flat colour; the window may be blank, or the backend could not read its surface.")
    return image_result(picture, window=info, bounds=capture_bounds, capture_area=capture_area,
                        capture_method=_CAPTURE_BACKENDS[name][0], capture_backend=name,
                        capture_attempts=attempts, uniform_image=flat, note=" ".join(notes))


def _resolve_window(query: str) -> tuple[dict, dict]:
    """Pick one top-level window from a name. Plain text matches the title or process name
    ('Blender' also matches blender.exe); 'pid:1234' and 'hwnd:0x1A2B' select exactly.

    Best candidate: not minimized, exact > process > prefix > substring match, then largest area.
    """
    if not isinstance(query, str) or not query.strip():
        raise ValueError("window must be a non-empty title/process name, 'pid:<n>' or 'hwnd:<n>'")
    needle = query.strip().lower()
    windows = [v for v in list_windows()["windows"] if not _is_cloaked(v["hwnd"])]
    selector = re.fullmatch(r"(pid|hwnd):\s*(0x[0-9a-f]+|\d+)", needle)
    scored = []
    for info in windows:
        title, process = info["title"].lower(), (info["process_name"] or "").lower()
        stem = process[:-4] if process.endswith(".exe") else process
        if selector:
            token = selector.group(2)
            number = int(token, 16 if token.startswith("0x") else 10)
            matched = {"pid": info["pid"], "hwnd": info["hwnd"]}[selector.group(1)] == number
            rank, how = (4, selector.group(1)) if matched else (0, None)
        elif title == needle:
            rank, how = 5, "title_exact"
        elif needle in (process, stem):
            rank, how = 4, "process_name"
        elif title.startswith(needle):
            rank, how = 3, "title_prefix"
        elif needle in title:
            rank, how = 2, "title_contains"
        elif needle in process:
            rank, how = 1, "process_contains"
        else:
            continue
        if rank and (info["title"] or selector or how.startswith("process")):
            area = info["bounds"]["width"] * info["bounds"]["height"]
            scored.append(((not info["minimized"], rank, area, info["foreground"]), how, info))
    if not scored:
        titles = [f"{v['title']!r} ({v['process_name']})" for v in windows if v["title"]][:25]
        raise ValueError(f"window_not_found: no visible window matches {query!r}. Visible windows: {titles}")
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0]
    resolution = {"query": query, "matched_by": best[1], "selected_hwnd": best[2]["hwnd"],
                  "candidate_count": len(scored),
                  "candidates": [{"hwnd": i["hwnd"], "pid": i["pid"], "title": i["title"],
                                  "process_name": i["process_name"], "minimized": i["minimized"],
                                  "matched_by": how} for _, how, i in scored[:5]]}
    if len(scored) > 1:
        resolution["note"] = "Several windows matched; the best one was selected. Pass hwnd/'hwnd:<n>' to choose another."
    return best[2], resolution




# Only these two functions are MCP tools. Capture helpers above remain internal.
_LOCK = threading.RLock()
_OBSERVATIONS: OrderedDict[str, dict] = OrderedDict()
_OBSERVATION_LIMIT = 64
IMPLEMENTATION = "windows-workspace-ui-4-computer-use"
COORDINATE_SPACES = {
    "screen": "Absolute physical pixels on the virtual desktop (negative values allowed). Default.",
    "window": "Pixels of the observed window image: (0,0) is its top-left pixel. For the default client capture "
              "this is the window's client area (the drawable region without title bar/borders).",
    "normalized": "0..1 over the same area as 'window': (0,0) top-left, (1,1) bottom-right. "
                  "Independent of DPI scaling and of window resizing.",
}


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



def _convert_coordinates(
    coordinate_space: str,
    x: float | None,
    y: float | None,
    ref_bounds: dict | None,
) -> tuple[int | None, int | None]:
    """Convert screen, window, or normalized coordinates to physical screen pixels."""
    if x is None and y is None:
        return None, None
    if x is None or y is None:
        raise ValueError("Both x and y must be provided for coordinate-based actions")

    if coordinate_space == "screen":
        return round(x), round(y)
    elif coordinate_space == "window":
        if not ref_bounds:
            raise ValueError("coordinate_space='window' requires a target window or observation")
        return round(ref_bounds["left"] + x), round(ref_bounds["top"] + y)
    elif coordinate_space == "normalized":
        if not ref_bounds:
            raise ValueError("coordinate_space='normalized' requires a target window or observation")
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise ValueError(f"normalized coordinates must be within 0..1, got ({x}, {y}); "
                             "use coordinate_space='window' or 'screen' to target outside the observed image")
        return (
            round(ref_bounds["left"] + x * max(1, ref_bounds["width"] - 1)),
            round(ref_bounds["top"] + y * max(1, ref_bounds["height"] - 1)),
        )
    else:
        raise ValueError(f"Unknown coordinate_space: {coordinate_space}; use 'screen', 'window', or 'normalized'")


def desktop_observe(
    mode: Literal["windows", "desktop", "monitor", "window", "uia"] | None = None,
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
    window: str | None = None,
    capture_backend: Literal["auto", "wgc", "printwindow", "screen"] = "auto",
) -> CallToolResult:
    """Observe application windows and displays in the connected Windows workspace.

    Pass window="Window Title or process" (e.g. window="Blender") to capture that
    window directly, returning native PNG ImageContent, DPI scaling, and geometry.

    mode=windows lists HWND/title/PID/process/bounds/state and monitors.
    desktop/monitor/window return native PNG ImageContent (physical pixels,
    no resizing). Window image bounds describe the captured area, separately
    from window.bounds (outer frame). monitor is 1-based; window/uia require
    hwnd or window parameter. with_uia adds a bounded UI Automation ControlView.
    """
    window_resolution = None
    if window is not None:
        win_info, window_resolution = _resolve_window(window)
        hwnd = win_info["hwnd"]
        if expected_pid is None:
            expected_pid = win_info["pid"]
        if mode is None:
            mode = "window"

    if mode is None:
        mode = "window" if hwnd is not None else "windows"

    if mode not in ("windows", "desktop", "monitor", "window", "uia"):
        raise ValueError(f"Unknown observe mode: {mode}")
    if mode in ("window", "uia") or with_uia:
        if hwnd is None:
            raise ValueError("hwnd or window parameter is required for window/UI Automation observation")
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
            if window_resolution:
                metadata["window_resolution"] = window_resolution
            if info:
                metadata["window"] = info
                client = _client_bounds(user, hwnd)
                geometry = _window_geometry(user, hwnd, info, client)
                metadata["window_geometry"] = geometry
                metadata["dpi"] = geometry.get("monitor_dpi") or geometry.get("window_dpi")
                metadata["scale_factor"] = geometry.get("scale_factor")
                metadata["scale_percent"] = geometry.get("scale_percent")
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
                capture = capture_window(hwnd, info["pid"], backend=capture_backend)
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
                bounds=metadata.get("bounds"),
                window_geometry=metadata.get("window_geometry"),
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


# Convenience names that map onto the canonical SendInput actions.
_ACTION_ALIASES = {
    "right_click": ("click", {"button": "right"}),
    "middle_click": ("click", {"button": "middle"}),
    "type_text": ("text", {}),
    "hotkey": ("key", {}),
}
_CANONICAL_ACTIONS = ("move", "click", "double_click", "drag", "scroll", "text", "key", "press_key",
                      "activate", "restore", "uia")


def desktop_act(
    action: Literal["move", "click", "double_click", "right_click", "middle_click", "drag", "scroll",
                    "text", "type_text", "key", "hotkey", "press_key", "activate", "restore", "uia"],
    hwnd: int | None = None,
    expected_pid: int | None = None,
    observation_id: str | None = None,
    x: float | None = None,
    y: float | None = None,
    end_x: float | None = None,
    end_y: float | None = None,
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
    window: str | None = None,
    coordinate_space: Literal["screen", "window", "normalized"] = "screen",
    modifiers: list[str] | None = None,
) -> dict[str, Any] | CallToolResult:
    """Interact with the connected Windows workspace GUI. Mouse/keyboard always use SendInput.

    Typical loop: desktop_observe(window="App") -> look at the PNG -> desktop_act(...)
    -> desktop_observe again to verify. Input acceptance is not task completion.

    Target: window="Title or process" (e.g. "Blender", "pid:1234", "hwnd:0x1A2B") or hwnd
    restores/activates that window before input. Pass observation_id from the observe you
    are reading coordinates from to reject stale geometry. Omit both for the current desktop.

    Coordinates x/y/end_x/end_y depend on coordinate_space:
    screen (default) = absolute physical pixels, negatives allowed;
    window = pixels of the observed image (0,0 = its top-left; the client area by default);
    normalized = 0..1 over that same image, (0,0) top-left, (1,1) bottom-right, DPI-independent.
    window/normalized need window/hwnd or an observation_id.

    Actions: move, click, double_click, right_click, middle_click, drag (x/y -> end_x/end_y
    over duration_ms, with button=left/right/middle), scroll (wheel units, 120 = one notch,
    delta_y positive = up, delta_x positive = right; optional x/y moves the cursor first).
    modifiers=['Ctrl','Shift','Alt','Win'] are held around any pointer action (Ctrl+click,
    Alt+middle-drag, Shift+wheel).
    text/type_text inserts literal Unicode text independent of keyboard layout/IME.
    key/hotkey presses keys=['Ctrl','Shift','a'] together (chord); press_key presses
    keys=['Down','Down','Enter'] one after another. Numpad keys: NumPad0..NumPad9.
    Use explicit Shift for uppercase shortcuts. activate/restore require a target window.

    uia requires observation_id + element_ref from desktop_observe; uia_action selects the
    pattern; set_value uses text. UIA never silently substitutes for SendInput; stale or
    missing elements fail instead of retargeting. target_guard_ref optionally binds a pointer
    action to an observed UIA ref (identity, bounds and hit-test re-checked before input).
    observe_after adds a fresh PNG + metadata to the response; observe_with_uia adds UIA data.
    UIPI/activation failures are errors and input is never retried.
    """
    if action in _ACTION_ALIASES:
        action, alias_defaults = _ACTION_ALIASES[action]
        button = alias_defaults.get("button", button)
    if action not in _CANONICAL_ACTIONS:
        raise ValueError(f"Unknown desktop action: {action}")
    if coordinate_space not in COORDINATE_SPACES:
        raise ValueError(f"Unknown coordinate_space: {coordinate_space}; use one of {sorted(COORDINATE_SPACES)}")
    window_resolution = None
    if window is not None:
        win_info, window_resolution = _resolve_window(window)
        if hwnd is not None and hwnd != win_info["hwnd"]:
            raise ValueError("window and hwnd select different windows; pass only one")
        hwnd = win_info["hwnd"]
        if expected_pid is None:
            expected_pid = win_info["pid"]
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
            if modifiers and not pixel_action:
                raise ValueError("modifiers apply to pointer actions only; put modifier keys in keys for hotkey/press_key")
            resolved = None
            if pixel_action and any(v is not None for v in (x, y, end_x, end_y)):
                if coordinate_space == "screen":
                    reference = None
                elif snapshot and snapshot.get("bounds"):
                    reference = snapshot["bounds"]  # the image the caller was looking at
                elif hwnd is not None:
                    reference = _client_bounds(user, hwnd)  # default capture area
                else:
                    reference = None
                x, y = _convert_coordinates(coordinate_space, x, y, reference)
                end_x, end_y = _convert_coordinates(coordinate_space, end_x, end_y, reference)
                resolved = {"x": x, "y": y, "end_x": end_x, "end_y": end_y, "coordinate_space": "screen"}
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
            if snapshot and hwnd is None and action in ("text", "key", "press_key"):
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
                    button=button, delta_x=delta_x, delta_y=delta_y, text=text, keys=keys, duration_ms=duration_ms, modifiers=modifiers)
            return _action_response(
                {**result, "ok": True, "action": action, "timestamp": _now(), "observation_id": observation_id,
                 "hwnd": hwnd, "target_pid": info["pid"] if info else None,
                 "coordinate_space": coordinate_space, "resolved_screen_coordinates": resolved,
                 "window_resolution": window_resolution,
                 "target_guard": "verified" if guard_element is not None else "not_requested",
                 "foreground_hwnd": user.GetForegroundWindow()},
                observe_after=observe_after, with_uia=observe_with_uia,
            )


TOOLS = [desktop_observe, desktop_act]
