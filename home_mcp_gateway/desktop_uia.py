"""Native UIAutomationCore COM in a disposable, timeout-controlled MTA worker.

Only JSON crosses the process boundary; COM pointers never cross apartments or
survive a worker exit. A stalled application/provider cannot hang the Gateway.
The module can be imported on non-Windows hosts without importing comtypes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


PATTERN_ACTIONS = {
    "Invoke": ("invoke",),
    "Value": ("set_value",),
    "Toggle": ("toggle",),
    "SelectionItem": ("select", "add_to_selection", "remove_from_selection"),
    "Selection": (),
    "ExpandCollapse": ("expand", "collapse"),
    "ScrollItem": ("scroll_into_view",),
}
METHODS = {
    "invoke": ("Invoke", "Invoke"),
    "set_value": ("Value", "SetValue"),
    "toggle": ("Toggle", "Toggle"),
    "select": ("SelectionItem", "Select"),
    "add_to_selection": ("SelectionItem", "AddToSelection"),
    "remove_from_selection": ("SelectionItem", "RemoveFromSelection"),
    "expand": ("ExpandCollapse", "Expand"),
    "collapse": ("ExpandCollapse", "Collapse"),
    "scroll_into_view": ("ScrollItem", "ScrollIntoView"),
}
BACKEND = "UIAutomationCore.COM"


def request(payload: dict, timeout_sec: float = 20) -> dict:
    if os.name != "nt":
        raise RuntimeError("UI Automation requires Windows")
    try:
        result = subprocess.run(
            [sys.executable, "-X", "utf8", str(Path(__file__).resolve())],
            input=json.dumps(payload, ensure_ascii=True), encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_sec,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"UI Automation provider timed out after {timeout_sec}s; worker terminated. "
            "An action may already have run; observe before retrying."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"Could not launch UI Automation worker: {exc}") from exc
    try:
        data = json.loads(result.stdout.lstrip("\ufeff"))
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
    except (ValueError, TypeError) as exc:
        raise RuntimeError(
            f"UI Automation worker returned invalid JSON (exit {result.returncode}): {result.stderr[-2000:]}"
        ) from exc
    if result.returncode or data.get("ok") is False:
        raise RuntimeError(f"UI Automation: {data.get('error', result.stderr[-2000:])}")
    return data


def _identity(element, roles: dict) -> dict:
    return {
        "runtime_id": list(element.GetRuntimeId() or ()),
        "role": roles.get(element.CurrentControlType, str(element.CurrentControlType)),
        "name": element.CurrentName or "",
        "automation_id": element.CurrentAutomationId or "",
        "process_id": element.CurrentProcessId,
        "native_hwnd": int(element.CurrentNativeWindowHandle or 0),
    }


def _pattern(element, types, name):
    pointer = element.GetCurrentPattern(getattr(types, f"UIA_{name}PatternId"))
    if not pointer:
        return None
    return pointer.QueryInterface(getattr(types, f"IUIAutomation{name}Pattern"))


def _inspect(element, types, identity: dict, depth: int, index: int) -> dict:
    rect = element.CurrentBoundingRectangle
    state, patterns, actions, errors = {}, [], [], []
    # Report all advertised patterns, not only the ones with action wrappers.
    for constant in dir(types):
        if constant.startswith("UIA_Is") and constant.endswith("PatternAvailablePropertyId"):
            name = constant[len("UIA_Is"):-len("PatternAvailablePropertyId")]
            try:
                if element.GetCurrentPropertyValue(getattr(types, constant)):
                    patterns.append(name)
            except Exception as exc:
                if len(errors) < 10:
                    errors.append(f"{name}: {exc}")
    for name, operations in PATTERN_ACTIONS.items():
        if name not in patterns:
            continue
        try:
            p = _pattern(element, types, name)
            if not p:
                continue
            actions.extend(operations)
            if name == "Value":
                state["value"] = (p.CurrentValue or "")[:2048]
                state["value_read_only"] = bool(p.CurrentIsReadOnly)
                if state["value_read_only"]:
                    actions.remove("set_value")
            elif name == "Toggle":
                state["toggle_state"] = {0: "Off", 1: "On", 2: "Indeterminate"}.get(p.CurrentToggleState, "Unknown")
            elif name == "SelectionItem":
                state["selected"] = bool(p.CurrentIsSelected)
            elif name == "Selection":
                state["can_select_multiple"] = bool(p.CurrentCanSelectMultiple)
            elif name == "ExpandCollapse":
                state["expand_collapse_state"] = {
                    0: "Collapsed", 1: "Expanded", 2: "PartiallyExpanded", 3: "LeafNode"
                }.get(p.CurrentExpandCollapseState, "Unknown")
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    focusable = bool(element.CurrentIsKeyboardFocusable)
    if focusable:
        actions.append("focus")
    # Some legacy MSAA proxies return an empty runtime ID. Never treat all such
    # controls as the same element, or fall back to a potentially reused index.
    actionable = bool(identity["runtime_id"])
    if not actionable:
        actions = []
        errors.append("No stable UIA runtime ID; use observed bounds with SendInput instead")
    return {
        **identity, "index": index, "ref": f"e{index}", "depth": depth,
        "bounds": {"left": rect.left, "top": rect.top, "right": rect.right, "bottom": rect.bottom,
                   "width": rect.right - rect.left, "height": rect.bottom - rect.top},
        "enabled": bool(element.CurrentIsEnabled), "focusable": focusable,
        "keyboard_focused": bool(element.CurrentHasKeyboardFocus),
        "offscreen": bool(element.CurrentIsOffscreen), "patterns": patterns,
        "actions": actions, "actionable": actionable, "state": state, "errors": errors,
    }


def _operate(element, types, action: str, text: str) -> None:
    if action == "focus":
        element.SetFocus()
        return
    if action not in METHODS:
        raise ValueError(f"Unknown UIA action: {action}")
    pattern_name, method = METHODS[action]
    p = _pattern(element, types, pattern_name)
    if not p:
        raise ValueError(f"UIA pattern {pattern_name} is not supported by this element; observe again or use SendInput")
    if action == "set_value":
        if p.CurrentIsReadOnly:
            raise ValueError("UIA Value pattern is read-only")
        p.SetValue(text)
    else:
        getattr(p, method)()


def _guard_pointer(element, automation, types, payload: dict) -> dict:
    """Attest the observed identity's current geometry and point; no input."""
    rect = element.CurrentBoundingRectangle
    bounds = dict(left=rect.left, top=rect.top, right=rect.right, bottom=rect.bottom,
                  width=rect.right - rect.left, height=rect.bottom - rect.top)
    if bounds != payload["bounds"] or not element.CurrentIsEnabled or element.CurrentIsOffscreen:
        raise ValueError("stale_observation: guarded UIA target moved or became unavailable")
    x, y = payload.get("x"), payload.get("y")
    if type(x) is not int or type(y) is not int or not (
        bounds["left"] <= x < bounds["right"] and bounds["top"] <= y < bounds["bottom"]
    ):
        raise ValueError("target_guard: pointer is outside the observed element")
    hit = automation.ElementFromPoint(types.tagPOINT(x, y))
    walker = automation.RawViewWalker
    for _ in range(64):
        if not hit:
            break
        if automation.CompareElements(element, hit):
            after = element.CurrentBoundingRectangle
            if any(getattr(after, k) != bounds[k] for k in ("left", "top", "right", "bottom")):
                raise ValueError("stale_observation: guarded UIA target moved during hit test")
            return {"ok": True, "backend": BACKEND, "target_guard": "verified",
                    "bounds": bounds, "runtime_id": payload["runtime_id"]}
        hit = walker.GetParentElement(hit)
    raise ValueError("target_guard: observed element is covered or hit-test identity is unavailable")


def _handle(payload: dict, automation, types) -> dict:
    mode = payload.get("mode")
    if mode not in ("observe", "act", "guard"):
        raise ValueError(f"Unknown UIA worker mode: {mode}")
    max_nodes, max_depth = int(payload["max_nodes"]), int(payload["max_depth"])
    max_elements = int(payload.get("max_elements", 80))
    if not 1 <= max_nodes <= 20000 or not 0 <= max_depth <= 64 or not 1 <= max_elements <= 500:
        raise ValueError("Invalid UI Automation traversal limits")
    if mode in ("act", "guard") and not payload.get("runtime_id"):
        raise ValueError("stale_observation: element has no stable runtime ID; observe again or use SendInput")
    roles = {getattr(types, k): k[len("UIA_"):-len("ControlTypeId")]
             for k in dir(types) if k.startswith("UIA_") and k.endswith("ControlTypeId")}
    root = automation.ElementFromHandle(payload["hwnd"])
    if not root or root.CurrentProcessId != payload["expected_pid"]:
        raise ValueError("stale_observation: UIA window disappeared or process ownership changed")
    walker = automation.ControlViewWalker
    stack, entries, errors = [(root, 0)], [], []
    visited, truncated = 0, False
    query, role = payload.get("query", "").casefold(), payload.get("role", "").casefold()
    while stack and visited < max_nodes:
        element, depth = stack.pop()
        visited += 1
        try:
            identity = _identity(element, roles)
        except Exception as exc:
            if len(errors) < 20:
                errors.append(str(exc))
            identity = None
        if identity:
            if mode in ("act", "guard") and identity["runtime_id"] == payload["runtime_id"]:
                for field in ("role", "name", "automation_id", "process_id", "native_hwnd"):
                    if identity[field] != payload[field]:
                        raise ValueError("stale_observation: UIA runtime ID reused or element identity changed")
                if root.CurrentProcessId != payload["expected_pid"]:
                    raise ValueError("stale_observation: UIA ownership changed before action")
                if mode == "guard":
                    return _guard_pointer(element, automation, types, payload)
                _operate(element, types, payload["action"], payload.get("text", ""))
                return {"ok": True, "backend": BACKEND, "action": payload["action"],
                        "runtime_id": identity["runtime_id"], "worker_pid": os.getpid()}
            if mode == "observe" and (not role or identity["role"].casefold() == role) and (
                not query or query in (identity["name"] + " " + identity["automation_id"]).casefold()
            ):
                try:
                    entries.append(_inspect(element, types, identity, depth, len(entries)))
                except Exception as exc:
                    if len(errors) < 20:
                        errors.append(str(exc))
                if len(entries) >= max_elements:
                    truncated = True
                    break
        # Only request one sibling/child at a time: no unbounded FindAll or a
        # prebuilt list of every child. The window's siblings are never visited.
        if depth:
            try:
                sibling = walker.GetNextSiblingElement(element)
                if sibling:
                    stack.append((sibling, depth))
            except Exception as exc:
                if len(errors) < 20:
                    errors.append(str(exc))
        try:
            child = walker.GetFirstChildElement(element)
            if child:
                if depth < max_depth:
                    stack.append((child, depth + 1))
                else:
                    truncated = True
        except Exception as exc:
            if len(errors) < 20:
                errors.append(str(exc))
    if mode in ("act", "guard"):
        raise ValueError("stale_observation: UIA element is gone or outside traversal limits; observe the window again")
    # Repeated IDs are not trustworthy selectors either. Keep them observable,
    # but don't advertise pattern actions that could hit a different element.
    counts = {}
    for entry in entries:
        rid = tuple(entry["runtime_id"])
        counts[rid] = counts.get(rid, 0) + 1
    for entry in entries:
        if entry["runtime_id"] and counts[tuple(entry["runtime_id"])] > 1:
            entry["actions"], entry["actionable"] = [], False
            entry["errors"].append("Duplicate UIA runtime ID; use SendInput instead")
    return {"elements": entries, "visited": visited, "truncated": truncated or bool(stack),
            "errors": errors, "view": "ControlView", "backend": BACKEND, "worker_pid": os.getpid()}


def _main() -> int:
    try:
        # Set BEFORE importing comtypes: it initializes COM on import. This
        # disposable process has no windows and uses the MTA exclusively.
        sys.coinit_flags = 0
        import comtypes.client
        import ctypes
        user = ctypes.WinDLL("user32", use_last_error=True)
        user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        types = comtypes.client.GetModule("UIAutomationCore.dll")
        automation = comtypes.client.CreateObject(types.CUIAutomation8, interface=types.IUIAutomation2)
        # Pattern operations should not synthesize extra focus changes (which
        # can disrupt menus). Focus is a separate explicit desktop_act action.
        automation.AutoSetFocus = False
        result = _handle(json.load(sys.stdin), automation, types)
        code = 0
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        code = 1
    print(json.dumps(result, ensure_ascii=True))
    return code


if __name__ == "__main__":
    raise SystemExit(_main())
