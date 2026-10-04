"""Windows physical input. No browser, clipboard, or synthetic window messages.

Implemented from the Win32 SendInput/INPUT documentation. All coordinates are
physical pixels on the virtual desktop, including monitors left of the primary.
"""
from __future__ import annotations

import ctypes as c
from ctypes import wintypes as w
import math
import time

U32 = c.c_uint32
I32 = c.c_int32
U16 = c.c_uint16


class MOUSEINPUT(c.Structure):
    _fields_ = [("dx", I32), ("dy", I32), ("mouseData", U32), ("dwFlags", U32),
                ("time", U32), ("dwExtraInfo", c.c_size_t)]


class KEYBDINPUT(c.Structure):
    _fields_ = [("wVk", U16), ("wScan", U16), ("dwFlags", U32),
                ("time", U32), ("dwExtraInfo", c.c_size_t)]


class HARDWAREINPUT(c.Structure):
    _fields_ = [("uMsg", U32), ("wParamL", U16), ("wParamH", U16)]


class _PAYLOAD(c.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(c.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", U32), ("payload", _PAYLOAD)]


MOVE, ABSOLUTE, VIRTUALDESK = 0x0001, 0x8000, 0x4000
KEYUP, UNICODE, EXTENDED = 0x0002, 0x0004, 0x0001
BUTTONS = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}
# Modifiers use the side-specific VKs (VK_LCONTROL/VK_LSHIFT/VK_LMENU). Windows
# reports the generic VK_CONTROL/VK_SHIFT/VK_MENU to applications either way, but
# apps such as Blender query GetKeyState(VK_LSHIFT) etc. directly.
KEYS = {
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "escape": 0x1B, "esc": 0x1B,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D, "ins": 0x2D, "space": 0x20,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22,
    "ctrl": 0xA2, "control": 0xA2, "lctrl": 0xA2, "shift": 0xA0, "lshift": 0xA0, "alt": 0xA4, "lalt": 0xA4,
    "win": 0x5B, "windows": 0x5B, "meta": 0x5B, "lwin": 0x5B,
    "rctrl": 0xA3, "ralt": 0xA5, "rshift": 0xA1, "rwin": 0x5C,
    "capslock": 0x14, "numlock": 0x90, "scrolllock": 0x91,
    "printscreen": 0x2C, "pause": 0x13, "apps": 0x5D,
    # Numeric keypad (e.g. Blender view shortcuts). Supply 'NumPad0'..'NumPad9'.
    **{f"numpad{i}": 0x60 + i for i in range(10)},
    "numpadmultiply": 0x6A, "numpadadd": 0x6B, "numpadsubtract": 0x6D,
    "numpaddecimal": 0x6E, "numpaddivide": 0x6F,
    **{f"f{i}": 0x6F + i for i in range(1, 25)},
}
EXTENDED_KEYS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
                 0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F, 0x90, 0xA3, 0xA5}
MODIFIER_VKS = {0xA0: "shift", 0xA1: "rshift", 0xA2: "ctrl", 0xA3: "rctrl", 0xA4: "alt", 0xA5: "ralt",
                0x5B: "win", 0x5C: "rwin"}


def configure(user):
    for name, args, result in (
        ("SendInput", [U32, c.POINTER(INPUT), c.c_int], U32),
        ("GetKeyboardLayout", [U32], c.c_void_p),
        ("VkKeyScanExW", [w.WCHAR, c.c_void_p], c.c_int16),
        ("MapVirtualKeyExW", [U32, U32, c.c_void_p], U32),
        ("ShowWindow", [w.HWND, c.c_int], w.BOOL),
        ("SetForegroundWindow", [w.HWND], w.BOOL),
        ("BringWindowToTop", [w.HWND], w.BOOL),
        ("AttachThreadInput", [U32, U32, w.BOOL], w.BOOL),
        ("GetCursorPos", [c.POINTER(w.POINT)], w.BOOL),
    ):
        fn = getattr(user, name)
        fn.argtypes, fn.restype = args, result
    return user


def virtual_bounds(user) -> dict:
    left, top, width, height = (user.GetSystemMetrics(i) for i in (76, 77, 78, 79))
    if width <= 0 or height <= 0:
        raise RuntimeError("No interactive virtual desktop is available")
    return dict(left=left, top=top, width=width, height=height,
                right=left + width, bottom=top + height)


def normalize(x: int, y: int, bounds: dict) -> tuple[int, int]:
    """Map physical pixels to Win32's inclusive 0..65535 virtual desktop range."""
    if bounds["width"] <= 0 or bounds["height"] <= 0:
        raise ValueError("Empty virtual desktop")
    # Win32 clamps out-of-desktop input too. This is coordinate conversion, not
    # an application/coordinate access policy.
    nx = round((x - bounds["left"]) * 65535 / max(1, bounds["width"] - 1))
    ny = round((y - bounds["top"]) * 65535 / max(1, bounds["height"] - 1))
    return max(0, min(65535, nx)), max(0, min(65535, ny))


def mouse(flags: int, x: int = 0, y: int = 0, data: int = 0) -> INPUT:
    return INPUT(type=0, mi=MOUSEINPUT(x, y, data & 0xFFFFFFFF, flags, 0, 0))


def key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=1, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def unicode_inputs(text: str) -> list[INPUT]:
    """UTF-16 units, including surrogate pairs; independent of keyboard/IME layout."""
    data = text.encode("utf-16-le")
    result = []
    for offset in range(0, len(data), 2):
        unit = int.from_bytes(data[offset:offset + 2], "little")
        result.extend((key(scan=unit, flags=UNICODE), key(scan=unit, flags=UNICODE | KEYUP)))
    return result


def _integrity(pid: int) -> int | None:
    """Best-effort diagnostic only; never changes process tokens or privileges."""
    try:
        import win32api
        import win32con
        import win32security
        process = win32api.OpenProcess(0x1000, False, pid)
        try:
            token = win32security.OpenProcessToken(process, win32con.TOKEN_QUERY)
            try:
                sid = win32security.GetTokenInformation(token, win32security.TokenIntegrityLevel)
                if isinstance(sid, tuple):
                    sid = sid[0]
                return sid.GetSubAuthority(sid.GetSubAuthorityCount() - 1)
            finally:
                token.Close()
        finally:
            process.Close()
    except Exception:
        return None


def send(user, events: list[INPUT], hwnd: int | None = None) -> int:
    if not events:
        return 0
    c.set_last_error(0)
    array = (INPUT * len(events))(*events)
    sent = user.SendInput(len(events), array, c.sizeof(INPUT))
    error = c.get_last_error()
    if sent != len(events):
        # Release only keys/buttons whose downs were accepted in this call. Do
        # not replay a partially accepted command or leave modifiers held down.
        held = {}
        for event in events[:sent]:
            if event.type == 1:
                ident = (event.ki.wVk, event.ki.wScan, event.ki.dwFlags & ~KEYUP)
                if event.ki.dwFlags & KEYUP:
                    held.pop(ident, None)
                else:
                    held[ident] = key(event.ki.wVk, event.ki.wScan, event.ki.dwFlags | KEYUP)
            else:
                for down, up in BUTTONS.values():
                    if event.mi.dwFlags & down:
                        held[("mouse", down)] = mouse(up)
                    if event.mi.dwFlags & up:
                        held.pop(("mouse", down), None)
        releases = list(reversed(list(held.values())))
        if releases:
            user.SendInput(len(releases), (INPUT * len(releases))(*releases), c.sizeof(INPUT))
        import os
        target_pid = w.DWORD()
        user.GetWindowThreadProcessId(hwnd or user.GetForegroundWindow(), c.byref(target_pid))
        own, target = _integrity(os.getpid()), _integrity(target_pid.value)
        if own is not None and target is not None and target > own:
            detail = f"Possible UIPI: target integrity {target} is higher than Gateway integrity {own}; this alone does not establish the failure cause."
        else:
            detail = "Possible UIPI/integrity-level restriction or a locked/non-interactive desktop; Windows does not identify UIPI in GetLastError."
        raise RuntimeError(
            f"SendInput failed: inserted {sent}/{len(events)} events; win32_error={error}; "
            f"target_hwnd={hwnd}; target_pid={target_pid.value}; foreground_hwnd={user.GetForegroundWindow()}; "
            f"gateway_integrity={own}; target_integrity={target}. {detail} No action was retried."
        )
    return sent


def keyboard_layout(user, hwnd: int | None):
    thread = user.GetWindowThreadProcessId(hwnd or user.GetForegroundWindow(), None)
    return user.GetKeyboardLayout(thread)


def chord_inputs(user, keys: list[str], layout) -> list[INPUT]:
    if not keys or any(not isinstance(k, str) or not k for k in keys):
        raise ValueError("keys must be a nonempty list, e.g. ['Ctrl', 'a'] or ['Enter']")
    vks = []
    for name in keys:
        lowered = name.lower()
        if lowered in KEYS:
            candidates = [KEYS[lowered]]
        elif len(name) == 1:
            # Letter case denotes the same shortcut key. Request Shift explicitly.
            character = name.lower() if name.isascii() and name.isalpha() else name
            if ord(character) > 0xFFFF:
                raise ValueError("Supplementary Unicode is literal text, not a shortcut key; use action='text'")
            mapped = user.VkKeyScanExW(character, layout)
            if mapped == -1 or (mapped >> 8) & ~7:
                raise ValueError(f"Key {name!r} is not representable in the target keyboard layout; use literal text instead")
            candidates = [vk for mask, vk in ((2, 0xA2), (4, 0xA4), (1, 0xA0)) if (mapped >> 8) & mask]
            candidates.append(mapped & 0xFF)
        else:
            raise ValueError(f"Unknown key {name!r}; use named keys, F1-F24, or single characters")
        for vk in candidates:
            if vk not in vks:
                vks.append(vk)
    downs = [key(vk, user.MapVirtualKeyExW(vk, 0, layout), EXTENDED if vk in EXTENDED_KEYS else 0) for vk in vks]
    return downs + [key(e.ki.wVk, e.ki.wScan, e.ki.dwFlags | KEYUP) for e in reversed(downs)]


def activate(user, hwnd: int, restore_only: bool = False) -> None:
    if not user.IsWindow(hwnd):
        raise ValueError(f"Window HWND {hwnd} no longer exists")
    if user.IsIconic(hwnd):
        user.ShowWindow(hwnd, 9)  # SW_RESTORE
        deadline = time.monotonic() + 2
        while user.IsIconic(hwnd) and time.monotonic() < deadline:
            time.sleep(.02)
        if user.IsIconic(hwnd):
            raise RuntimeError(f"Could not restore minimized HWND {hwnd}")
    if restore_only or user.GetForegroundWindow() == hwnd:
        return
    user.SetForegroundWindow(hwnd)
    if user.GetForegroundWindow() != hwnd:
        kernel = c.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentThreadId.restype = U32
        current = kernel.GetCurrentThreadId()
        foreground = user.GetWindowThreadProcessId(user.GetForegroundWindow(), None)
        target = user.GetWindowThreadProcessId(hwnd, None)
        attached = []
        try:
            for thread in set((foreground, target)) - {0, current}:
                if user.AttachThreadInput(current, thread, True):
                    attached.append(thread)
            user.BringWindowToTop(hwnd)
            user.SetForegroundWindow(hwnd)
        finally:
            for thread in attached:
                user.AttachThreadInput(current, thread, False)
    deadline = time.monotonic() + 1
    while user.GetForegroundWindow() != hwnd and time.monotonic() < deadline:
        time.sleep(.02)
    if user.GetForegroundWindow() != hwnd:
        raise RuntimeError(f"Windows denied foreground activation of HWND {hwnd}; input was not sent. Check foreground-lock or UIPI restrictions.")


def modifier_events(user, names, layout) -> tuple[list[INPUT], list[INPUT]]:
    """Down/up events that hold Ctrl/Shift/Alt/Win around a pointer action."""
    if not names:
        return [], []
    if isinstance(names, str) or any(not isinstance(n, str) for n in names):
        raise ValueError("modifiers must be a list such as ['Ctrl', 'Shift']")
    vks = []
    for name in names:
        vk = KEYS.get(name.lower())
        if vk not in MODIFIER_VKS:
            raise ValueError(f"Unknown modifier {name!r}; use Ctrl, Shift, Alt or Win")
        if vk not in vks:
            vks.append(vk)
    downs = [key(vk, user.MapVirtualKeyExW(vk, 0, layout), EXTENDED if vk in EXTENDED_KEYS else 0) for vk in vks]
    return downs, [key(e.ki.wVk, e.ki.wScan, e.ki.dwFlags | KEYUP) for e in reversed(downs)]


# Applications that poll modifier/button state (Blender, games, CAD) need a
# moment between the pointer move, the button press and the first drag motion.
SETTLE_SEC = 0.03


def perform(user, action: str, *, hwnd: int | None = None, x=None, y=None,
            end_x=None, end_y=None, button="left", delta_x=0, delta_y=0,
            text="", keys=None, duration_ms=350, modifiers=None) -> dict:
    configure(user)
    if button not in BUTTONS:
        raise ValueError(f"Unknown mouse button: {button}")
    if not math.isfinite(duration_ms) or duration_ms < 0:
        raise ValueError("duration_ms must be finite and nonnegative")
    pointer = action in ("move", "click", "double_click", "drag", "scroll")
    if modifiers and not pointer:
        raise ValueError("modifiers apply to pointer actions only; put modifier keys in keys for hotkey/press_key")
    bounds = virtual_bounds(user)
    layout = keyboard_layout(user, hwnd)
    mod_downs, mod_ups = modifier_events(user, modifiers, layout)
    count = 0

    def emit(events):
        nonlocal count
        count += send(user, events, hwnd)

    def positioned(px, py):
        if px is None or py is None:
            raise ValueError(f"{action} requires both x and y physical-pixel coordinates")
        nx, ny = normalize(px, py, bounds)
        return mouse(MOVE | ABSOLUTE | VIRTUALDESK, nx, ny)

    def move_to(px, py):
        emit([positioned(px, py)])

    def run():
        if action == "text":
            events = unicode_inputs(text)
            # Bounded SendInput batches keep long literal input practical.
            for offset in range(0, len(events), 512):
                emit(events[offset:offset + 512])
        elif action == "key":
            emit(chord_inputs(user, keys, layout))
        elif action == "press_key":
            # Validate the whole sequence first so a typo cannot half-run it.
            sequence = [chord_inputs(user, [name], layout) for name in (keys or [])]
            if not sequence:
                raise ValueError("press_key requires keys, e.g. ['Enter'] or ['Down', 'Down', 'Enter']")
            for index, events in enumerate(sequence):
                if index:
                    time.sleep(SETTLE_SEC)
                emit(events)
        elif action == "move":
            move_to(x, y)
        elif action in ("click", "double_click"):
            down, up = BUTTONS[button]
            # One SendInput batch prevents physical/other injected mouse events
            # from interleaving between positioning and the two button sequences.
            # Zero timestamps let Windows timestamp the uninterrupted input stream.
            clicks = 2 if action == "double_click" else 1
            emit([positioned(x, y)] + [mouse(down), mouse(up)] * clicks)
        elif action == "drag":
            if None in (x, y, end_x, end_y):
                raise ValueError("drag requires x, y, end_x and end_y")
            move_to(x, y)
            time.sleep(SETTLE_SEC)
            down, up = BUTTONS[button]
            emit([mouse(down)])
            try:
                time.sleep(SETTLE_SEC)
                steps = max(2, min(240, round(duration_ms / 16)))
                for step in range(1, steps + 1):
                    time.sleep(duration_ms / 1000 / steps)
                    move_to(round(x + (end_x - x) * step / steps), round(y + (end_y - y) * step / steps))
                time.sleep(SETTLE_SEC)
            finally:
                emit([mouse(up)])
        elif action == "scroll":
            if x is not None or y is not None:
                move_to(x, y)
                time.sleep(SETTLE_SEC)
            if not delta_x and not delta_y:
                raise ValueError("scroll requires delta_y (positive up) or delta_x (positive right); 120 is one wheel notch")
            if delta_y:
                emit([mouse(0x0800, data=delta_y)])
            if delta_x:
                emit([mouse(0x1000, data=delta_x)])
        else:
            raise ValueError(f"Unknown SendInput action: {action}")

    if mod_downs:
        emit(mod_downs)
        time.sleep(SETTLE_SEC)
    try:
        run()
    finally:
        # Never leave Ctrl/Shift/Alt logically held, even if the action failed.
        if mod_downs:
            emit(mod_ups)
    point = w.POINT()
    cursor = {"x": point.x, "y": point.y} if user.GetCursorPos(c.byref(point)) else None
    return dict(backend="SendInput", events_inserted=count, cursor=cursor,
                keyboard_layout=hex(layout or 0), virtual_bounds=bounds,
                modifiers=list(modifiers or []))
