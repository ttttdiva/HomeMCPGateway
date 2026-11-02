"""Gateway-owned Playwright sessions, isolated contexts and async MCP operations."""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from contextlib import asynccontextmanager
import hashlib
import io
import json
import math
import re
import statistics
import threading
import time
from typing import Any
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
import uuid
import weakref

from mcp.types import CallToolResult

from .browser_agent import (
    ensure_attached_page_visible,
    BrowserAgentError,
    execute_browser_plan,
    navigate_page,
    read_page_visibility_state,
    safe_exception_summary,
)
from .jev import JEV_DEFAULT_MODEL
from .qa_common import png_result

_SESSIONS: dict[str, dict] = {}
_RELEASED_SESSIONS: dict[str, str] = {}
_RELEASED_SESSION_IDS: deque[str] = deque()
_RELEASED_SESSION_LIMIT = 256
_KNOWN_TAB_HANDLE_LIMIT = 512
_MAX_ACTIVE_MEASUREMENTS_PER_SESSION = 4
_MAX_MEASUREMENT_FRAMES = 120
_MAX_MEASUREMENT_DURATION_MS = 10_000
_MAX_MEASUREMENT_INTERVAL_MS = 1_000
_MIN_MEASUREMENT_INTERVAL_MS = 16
_MAX_MEASUREMENT_FRAME_BYTES = 8 * 1024 * 1024
_MAX_MEASUREMENT_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_MEASUREMENT_FRAME_WIDTH = 1_280
_MAX_MEASUREMENT_FRAME_HEIGHT = 720
_MAX_MEASUREMENT_CONSECUTIVE_FAILURES = 5
_MAX_TRACK_SELECTORS = 20
_MAX_TRACK_ELEMENTS_PER_SELECTOR = 10
_MAX_TRACK_SAMPLES = 120
_MAX_TRACK_TOTAL_ELEMENTS = 50
_MAX_MEASUREMENT_MANIFEST_BYTES = 8 * 1024 * 1024
_MIN_VIEWPORT_WIDTH = 320
_MAX_VIEWPORT_WIDTH = 3_840
_MIN_VIEWPORT_HEIGHT = 240
_MAX_VIEWPORT_HEIGHT = 2_160
_MEASUREMENTS: dict[str, dict[str, Any]] = {}
_EDGE_CONNECTIONS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_EDGE_CONNECTIONS_LOCK = threading.RLock()
_BROWSER_FIND_SAFE_ATTRIBUTES = {
    "id", "name", "type", "role", "class", "placeholder", "title", "alt",
    "href", "src", "action", "formaction", "poster", "cite", "background",
    "checked", "disabled", "required", "multiple", "readonly", "autocomplete",
    "aria-label", "aria-labelledby", "aria-describedby", "aria-checked",
    "aria-selected", "aria-disabled", "aria-hidden",
}
_BROWSER_FIND_URL_ATTRIBUTES = {
    "href", "src", "action", "formaction", "poster", "cite", "background",
}


def _remember_released(session_id: str, mode: str) -> None:
    if session_id in _RELEASED_SESSIONS:
        return
    if len(_RELEASED_SESSION_IDS) >= _RELEASED_SESSION_LIMIT:
        expired = _RELEASED_SESSION_IDS.popleft()
        _RELEASED_SESSIONS.pop(expired, None)
    _RELEASED_SESSION_IDS.append(session_id)
    _RELEASED_SESSIONS[session_id] = mode


def _safe_browser_url(value: str) -> str:
    """Return a bounded URL without userinfo, query values, or fragments."""
    try:
        parsed = urlsplit(str(value or ""))
        scheme = parsed.scheme[:24].lower()
        if scheme in {"data", "javascript", "blob"}:
            return f"{scheme}:"
        if scheme == "file":
            return "file:"
        if not scheme:
            return ""
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = hostname + (f":{port}" if port is not None else "")
        if not hostname and parsed.netloc:
            netloc = ""
        path = parsed.path if scheme in {"http", "https", "ws", "wss", "about"} else ""
        return urlunsplit((scheme, netloc, path, "", ""))[:1_000]
    except (TypeError, ValueError):
        return ""


class _SharedEdgeConnection:
    """One Gateway-side Playwright client to the per-user Edge broker."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.connect_task: asyncio.Task | None = None
        self.driver: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.browser_version = ""
        self.failure_code: str | None = None
        self.session_ids: set[str] = set()


def _shared_edge_connection() -> _SharedEdgeConnection:
    """Return this event loop's shared Edge client without crossing loop locks."""
    loop = asyncio.get_running_loop()
    with _EDGE_CONNECTIONS_LOCK:
        state = _EDGE_CONNECTIONS.get(loop)
        if state is None:
            state = _SharedEdgeConnection()
            _EDGE_CONNECTIONS[loop] = state
        return state


def _browser_is_connected(browser: Any) -> bool:
    if browser is None:
        return False
    try:
        return bool(browser.is_connected())
    except AttributeError:
        return True
    except Exception:
        return False


def _is_retryable_broker_state(code: str) -> bool:
    # Broker responses only query its latched state. Retrying this control-plane
    # lookup cannot create another CDP attempt; the broker requires an explicit
    # reset before it can retry a failed Edge connection. Keep local Playwright
    # client failures sticky so a broken client does not spin up repeatedly.
    return (
        code.startswith("edge_broker_")
        and not code.startswith("edge_broker_client_")
        and code != "edge_broker_default_context_unavailable"
    )


async def _connect_gateway_client(state: _SharedEdgeConnection, timeout_sec: float) -> None:
    """Connect to the long-lived local broker using Playwright protocol only."""
    from playwright.async_api import async_playwright

    from .edge_cdp_broker import get_browser_endpoint

    driver = None
    browser = None
    try:
        endpoint, version = await get_browser_endpoint(timeout_sec)
        driver = await async_playwright().start()
        browser = await driver.chromium.connect(
            endpoint,
            timeout=int(timeout_sec * 1_000),
        )
        contexts = browser.contexts
        if not contexts:
            raise RuntimeError("edge_broker_default_context_unavailable")
        if not version:
            raise RuntimeError("edge_broker_version_unavailable")
        state.driver = driver
        state.browser = browser
        state.context = contexts[0]
        state.browser_version = str(version)[:200]
    except asyncio.CancelledError:
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                pass
        if driver is not None:
            try:
                await driver.stop()
            except Exception:
                pass
        raise
    except Exception as exc:
        if browser is not None:
            try:
                # This is a Playwright-protocol client disconnect from the local
                # broker. It never sends a new CDP connection or Edge close.
                await browser.close()
            except Exception:
                pass
        if driver is not None:
            try:
                await driver.stop()
            except Exception:
                pass
        code = str(exc)
        if re.fullmatch(r"edge_broker_[a-z0-9_]{1,64}", code):
            raise RuntimeError(code) from None
        if "timeout" in type(exc).__name__.casefold():
            raise RuntimeError("edge_broker_client_connect_timeout") from None
        raise RuntimeError("edge_broker_client_connect_failed") from None


async def _ensure_shared_edge_connection(timeout_sec: float) -> tuple[_SharedEdgeConnection, bool]:
    """Single-flight the Gateway client; broker owns the only Edge CDP socket."""
    state = _shared_edge_connection()
    if _browser_is_connected(state.browser):
        return state, True

    async with state.lock:
        if _browser_is_connected(state.browser):
            return state, True
        if state.failure_code:
            raise RuntimeError(state.failure_code) from None
        task = state.connect_task
        if task is None:
            task = asyncio.create_task(_connect_gateway_client(state, timeout_sec))
            state.connect_task = task

    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout_sec)
    except asyncio.TimeoutError:
        # Keep the in-flight task: a caller retry joins it instead of opening
        # another connection. The broker also keeps its single CDP attempt.
        raise RuntimeError("edge_broker_connecting") from None
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        code = str(exc)
        async with state.lock:
            if state.connect_task is task and task.done():
                state.connect_task = None
                if not _is_retryable_broker_state(code):
                    state.failure_code = code if re.fullmatch(r"edge_broker_[a-z0-9_]{1,64}", code) else "edge_broker_client_connect_failed"
        raise RuntimeError(code if re.fullmatch(r"edge_broker_[a-z0-9_]{1,64}", code) else "edge_broker_client_connect_failed") from None

    async with state.lock:
        if state.connect_task is task:
            state.connect_task = None
        if not _browser_is_connected(state.browser):
            state.failure_code = "edge_broker_client_disconnected"
            raise RuntimeError(state.failure_code) from None
    return state, False


def _tab_handle(target_id: str) -> str:
    return "edge_" + hashlib.sha256(target_id.encode("utf-8")).hexdigest()[:24]


def _remember_tab_handle(session: dict[str, Any], handle: str) -> None:
    known = session.setdefault("known_tab_handles", set())
    order = session.setdefault("known_tab_handle_order", deque())
    if handle in known:
        return
    if len(order) >= _KNOWN_TAB_HANDLE_LIMIT:
        known.discard(order.popleft())
    order.append(handle)
    known.add(handle)


async def _attached_tab_inventory(session: dict[str, Any]) -> list[dict[str, Any]]:
    context = session["context"]
    tabs: list[dict[str, Any]] = []
    pages: dict[str, Any] = {}
    for page in list(context.pages):
        try:
            if page.is_closed():
                continue
            target_id = await _attached_page_target_id(context, page)
            if not target_id or len(target_id) > 256:
                continue
            handle = _tab_handle(target_id)
            pages[handle] = page
            tabs.append({
                "tab_handle": handle,
                "url": _safe_browser_url(page.url),
                "selected": page is session.get("page"),
            })
            _remember_tab_handle(session, handle)
        except Exception:
            # A tab can close during enumeration; it stays absent and can never
            # be replaced with an index-based or newest-page fallback.
            continue
    session["tab_pages"] = pages
    return tabs


async def _attached_page_target_id(context: Any, page: Any) -> str:
    target_session = await context.new_cdp_session(page)
    try:
        result = await target_session.send("Target.getTargetInfo")
        return str(result.get("targetInfo", {}).get("targetId", ""))
    finally:
        await target_session.detach()


def _observe_selected_page(session: dict[str, Any], page: Any) -> None:
    if not session.get("attached"):
        return
    previous = session.get("observed_page")
    if previous is page:
        return
    handlers = session.get("observer_handlers")
    if previous is not None and handlers is not None:
        try:
            previous.remove_listener("console", handlers[0])
            previous.remove_listener("pageerror", handlers[1])
        except Exception:
            pass
        session["console"].clear()
        session["errors"].clear()

    def on_console(message):
        session["console"].append({
            "type": message.type,
            "text": "[redacted]",
            "location": {
                **{key: value for key, value in message.location.items() if key != "url"},
                "url": _safe_browser_url(message.location.get("url", "")),
            },
        })

    def on_page_error(error):
        session["errors"].append({"type": type(error).__name__[:80]})

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    session["observed_page"] = page
    session["observer_handlers"] = (on_console, on_page_error)


def _unobserve_selected_page(session: dict[str, Any]) -> None:
    page = session.get("observed_page")
    handlers = session.get("observer_handlers")
    if page is not None and handlers is not None:
        try:
            page.remove_listener("console", handlers[0])
            page.remove_listener("pageerror", handlers[1])
        except Exception:
            pass
    session["observed_page"] = None
    session["observer_handlers"] = None


def _require_selected_page(session: dict[str, Any]) -> Any:
    if not session.get("attached"):
        return session["page"]
    page = session.get("page")
    if page is None:
        if session.get("selected_tab_handle"):
            raise ValueError("browser_tab_closed")
        raise ValueError("browser_tab_selection_required")
    try:
        if page.is_closed():
            raise ValueError("browser_tab_closed")
    except ValueError:
        raise
    except Exception:
        raise ValueError("browser_tab_closed") from None
    return page


@asynccontextmanager
async def _session(session_id: str, *, require_page: bool = True):
    session = _SESSIONS.get(session_id)
    if session is None:
        raise ValueError("Browser session not found; start a new session after Gateway restart")
    async with session["lock"]:
        if session_id not in _SESSIONS:
            raise ValueError("Browser session has been closed")
        if require_page:
            _require_selected_page(session)
        try:
            yield session
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session.get("attached"):
                code = str(exc)
                if code.startswith((
                    "browser_drag_", "browser_capture_frames_", "browser_track_boxes_", "browser_measure_",
                    "browser_viewport_", "browser_set_viewport_", "browser_reset_viewport_",
                    "browser_click_", "browser_fill_", "browser_keyboard_", "browser_find_",
                )):
                    raise
                if code in {
                    "browser_tab_closed",
                    "browser_tab_not_found",
                    "browser_tab_not_owned",
                    "browser_tab_create_failed",
                    "browser_tab_close_failed",
                    "browser_tab_selection_required",
                    "browser_session_not_attached",
                    "browser_element_filter_invalid",
                }:
                    raise
                try:
                    if session.get("page") is None or session["page"].is_closed():
                        detail = safe_exception_summary(exc)
                        raise ValueError(
                            f"browser_tab_closed: {detail['type']}: {detail['message']}"
                        ) from None
                except ValueError:
                    raise
                except Exception:
                    raise ValueError("browser_tab_closed") from None
                if code.startswith("browser_navigation_"):
                    raise
                if isinstance(exc, BrowserAgentError) and re.fullmatch(r"[a-z][a-z0-9_]{2,63}", code):
                    # Our own fixed failure codes (browser_plan_invalid, jev_low_confidence, ...)
                    # carry no page data; keep them instead of the generic wrapper text.
                    raise
                detail = safe_exception_summary(exc)
                raise RuntimeError(
                    f"browser_action_failed: {detail['type']}: {detail['message']}"
                ) from None
            raise


async def browser_attach_edge(timeout_sec: float = 5) -> dict[str, Any]:
    """Create a caller session over the persistent per-user Edge broker.

    This is the only supported Gateway/Jev route to an existing Edge. The
    Gateway client talks to the broker's authenticated IPC and loopback
    Playwright endpoint; it must never read Edge's DevTools endpoint file or
    open a direct CDP connection to Edge.
    """
    if type(timeout_sec) not in (int, float) or not math.isfinite(timeout_sec) or not 1 <= timeout_sec <= 60:
        raise ValueError("timeout_sec must be between 1 and 60")
    state, reused_connection = await _ensure_shared_edge_connection(float(timeout_sec))
    session_id = str(uuid.uuid4())
    session = {
        "driver": state.driver,
        "browser": state.browser,
        "context": state.context,
        "edge_connection_state": state,
        "shared_edge": True,
        "page": None,
        "attached": True,
        "borrowed_context": True,
        "borrowed_pages": True,
        "browser_version": state.browser_version,
        "console": deque(maxlen=1000),
        "errors": deque(maxlen=1000),
        "known_tab_handles": set(),
        "known_tab_handle_order": deque(),
        "tab_pages": {},
        "owned_tab_handles": set(),
        "owned_tab_pages": {},
        "closed_owned_tab_handles": set(),
        "viewport_overrides": {},
        "observed_page": None,
        "observer_handlers": None,
        "lock": asyncio.Lock(),
    }
    try:
        tabs = await _attached_tab_inventory(session)
    except Exception:
        # The process-wide broker client remains reusable even if this caller's
        # initial inventory raced a closing tab.
        raise RuntimeError("edge_broker_tab_inventory_failed") from None
    _SESSIONS[session_id] = session
    state.session_ids.add(session_id)
    return {
        "session_id": session_id,
        "mode": "attached",
        "browser": "Microsoft Edge",
        "browser_version": state.browser_version,
        "tab_count": len(tabs),
        "tabs": tabs,
        "selected_tab_handle": "",
        "shared_connection_reused": reused_connection,
    }


async def browser_list_tabs(session_id: str) -> dict[str, Any]:
    """List safe, stable handles for the borrowed Edge tabs without reading their contents."""
    async with _session(session_id, require_page=False) as session:
        if not session.get("attached"):
            raise ValueError("browser_session_not_attached")
        tabs = await _attached_tab_inventory(session)
        selected_handle = session.get("selected_tab_handle", "")
        selected_page = session.get("page")
        selected_closed = bool(selected_handle and (selected_page is None or selected_page.is_closed()))
        return {
            "session_id": session_id,
            "mode": "attached",
            "tabs": tabs,
            "selected_tab_handle": selected_handle,
            "selected_tab_closed": selected_closed,
        }


async def browser_select_tab(session_id: str, tab_handle: str) -> dict[str, Any]:
    """Select one explicitly listed borrowed tab; never substitutes another tab."""
    if not isinstance(tab_handle, str) or not re.fullmatch(r"edge_[0-9a-f]{24}", tab_handle):
        raise ValueError("browser_tab_not_found")
    async with _session(session_id, require_page=False) as session:
        if not session.get("attached"):
            raise ValueError("browser_session_not_attached")
        tabs = await _attached_tab_inventory(session)
        page = session.get("tab_pages", {}).get(tab_handle)
        if page is None:
            was_selected = tab_handle == session.get("selected_tab_handle")
            code = "browser_tab_closed" if was_selected or tab_handle in session.get("known_tab_handles", set()) else "browser_tab_not_found"
            raise ValueError(code)
        if page.is_closed():
            raise ValueError("browser_tab_closed")
        session["page"] = page
        session["selected_tab_handle"] = tab_handle
        _observe_selected_page(session, page)
        selected = next((tab for tab in tabs if tab["tab_handle"] == tab_handle), None)
        return {
            "session_id": session_id,
            "mode": "attached",
            "selected_tab_handle": tab_handle,
            "url": selected["url"] if selected else _safe_browser_url(page.url),
        }


async def browser_open_tab(session_id: str) -> dict[str, Any]:
    """Create a blank caller-owned tab and select it as this attached session's page."""
    async with _session(session_id, require_page=False) as session:
        if not session.get("attached"):
            raise ValueError("browser_session_not_attached")
        page = None
        handle = ""
        previous_page = session.get("page")
        previous_handle = session.get("selected_tab_handle", "")
        try:
            page = await session["context"].new_page()
            target_id = await _attached_page_target_id(session["context"], page)
            if not target_id or len(target_id) > 256:
                raise RuntimeError("browser_tab_target_unavailable")
            handle = _tab_handle(target_id)
            existing = session.get("tab_pages", {}).get(handle)
            if existing is not None and existing is not page:
                raise RuntimeError("browser_tab_target_collision")
            _remember_tab_handle(session, handle)
            session.setdefault("tab_pages", {})[handle] = page
            session.setdefault("owned_tab_handles", set()).add(handle)
            session.setdefault("owned_tab_pages", {})[handle] = page
            # Opening a QA tab means subsequent caller actions target it.
            # This changes only the caller-local page selection, not the OS window.
            session["page"] = page
            session["selected_tab_handle"] = handle
            _observe_selected_page(session, page)
            return {
                "session_id": session_id,
                "mode": "attached",
                "tab_handle": handle,
                "url": _safe_browser_url(page.url),
                "selected": True,
                "owned_by_session": True,
            }
        except asyncio.CancelledError:
            if page is not None:
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            raise
        except Exception:
            if page is not None:
                if session.get("observed_page") is page:
                    _unobserve_selected_page(session)
                if session.get("page") is page:
                    session["page"] = previous_page
                    session["selected_tab_handle"] = previous_handle
                    try:
                        if previous_page is None or previous_page.is_closed():
                            session["page"] = None
                            session["selected_tab_handle"] = ""
                        else:
                            _observe_selected_page(session, previous_page)
                    except Exception:
                        session["page"] = None
                        session["selected_tab_handle"] = ""
                if handle:
                    session.setdefault("tab_pages", {}).pop(handle, None)
                    session.setdefault("owned_tab_handles", set()).discard(handle)
                    session.setdefault("owned_tab_pages", {}).pop(handle, None)
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
            raise RuntimeError("browser_tab_create_failed") from None


async def browser_close_tab(session_id: str, tab_handle: str) -> dict[str, Any]:
    """Close only a tab explicitly created by browser_open_tab in this same session."""
    if not isinstance(tab_handle, str) or not re.fullmatch(r"edge_[0-9a-f]{24}", tab_handle):
        raise ValueError("browser_tab_not_found")
    async with _session(session_id, require_page=False) as session:
        if not session.get("attached"):
            raise ValueError("browser_session_not_attached")
        owned = session.setdefault("owned_tab_handles", set())
        closed = session.setdefault("closed_owned_tab_handles", set())
        if tab_handle in closed:
            return {
                "session_id": session_id,
                "tab_handle": tab_handle,
                "closed": True,
                "already_closed": True,
                "owned_by_session": True,
            }
        if tab_handle not in owned:
            raise ValueError("browser_tab_not_owned")

        page = session.setdefault("owned_tab_pages", {}).get(tab_handle)
        already_closed = page is None
        if page is not None:
            try:
                already_closed = page.is_closed()
            except Exception:
                raise RuntimeError("browser_tab_close_failed") from None
            if not already_closed:
                await _stop_session_measurements(session_id, tab_handle=tab_handle)
            await _clear_owned_viewport_override(session, tab_handle, page)
            if not already_closed:
                if session.get("observed_page") is page:
                    _unobserve_selected_page(session)
                try:
                    await page.close()
                except Exception:
                    raise RuntimeError("browser_tab_close_failed") from None

        owned.discard(tab_handle)
        closed.add(tab_handle)
        session.setdefault("owned_tab_pages", {}).pop(tab_handle, None)
        session.setdefault("tab_pages", {}).pop(tab_handle, None)
        if session.get("page") is page:
            session["page"] = None
        return {
            "session_id": session_id,
            "tab_handle": tab_handle,
            "closed": True,
            "already_closed": already_closed,
            "owned_by_session": True,
        }


def _drag_url_origin(parsed: Any) -> tuple[str, str, int | None] | None:
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username is not None or parsed.password is not None:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    if port is None:
        port = 443 if scheme == "https" else 80
    return scheme, parsed.hostname.lower(), port


def _drag_url_matches(current_url: str, expected_url: str, expected_url_prefix: str) -> bool:
    try:
        current = urlsplit(current_url)
        expected = urlsplit(expected_url or expected_url_prefix)
    except (TypeError, ValueError):
        return False
    current_origin = _drag_url_origin(current)
    expected_origin = _drag_url_origin(expected)
    if current_origin is None or current_origin != expected_origin:
        return False
    if expected_url:
        return current_url == expected_url
    if expected.query or expected.fragment:
        return False
    prefix = expected.path.rstrip("/")
    path = current.path.rstrip("/")
    return path == prefix or path.startswith(prefix + "/")


def _validate_owned_url_guard(
    expected_url: str,
    expected_url_prefix: str,
    error_prefix: str,
) -> None:
    """Validate the common exact-or-path-prefix URL guard used by owned-tab tools."""
    if not isinstance(expected_url, str) or not isinstance(expected_url_prefix, str):
        raise ValueError(f"{error_prefix}url_guard_invalid")
    if bool(expected_url) == bool(expected_url_prefix):
        raise ValueError(
            f"{error_prefix}url_guard_required" if not expected_url else f"{error_prefix}url_guard_invalid"
        )
    guard = expected_url or expected_url_prefix
    try:
        parts = urlsplit(guard)
    except ValueError:
        raise ValueError(f"{error_prefix}url_guard_invalid") from None
    if _drag_url_origin(parts) is None:
        raise ValueError(f"{error_prefix}url_guard_invalid")
    if expected_url_prefix and (parts.query or parts.fragment):
        raise ValueError(f"{error_prefix}url_guard_invalid")


def _require_owned_attached_page(
    session: dict[str, Any],
    tab_handle: str,
    error_prefix: str,
) -> Any:
    """Return only a live tab explicitly created by this attached caller."""
    if not isinstance(tab_handle, str) or not re.fullmatch(r"edge_[0-9a-f]{24}", tab_handle):
        raise ValueError(f"{error_prefix}tab_not_owned")
    if tab_handle in session.get("closed_owned_tab_handles", set()):
        raise ValueError("browser_tab_closed")
    if not session.get("attached") or tab_handle not in session.get("owned_tab_handles", set()):
        raise ValueError(f"{error_prefix}tab_not_owned")
    page = session.get("owned_tab_pages", {}).get(tab_handle)
    if page is None:
        raise ValueError("browser_tab_closed")
    try:
        if page.is_closed():
            raise ValueError("browser_tab_closed")
    except ValueError:
        raise
    except Exception:
        raise ValueError("browser_tab_closed") from None
    return page


def _guard_attached_element_page(
    session: dict[str, Any],
    expected_url: str,
    expected_url_prefix: str,
    error_prefix: str,
) -> Any:
    """Require an owned selected tab and URL guard for attached element tools."""
    if not session.get("attached"):
        return _require_selected_page(session)
    _validate_owned_url_guard(expected_url, expected_url_prefix, error_prefix)
    handle = session.get("selected_tab_handle", "")
    page = _require_owned_attached_page(session, handle, error_prefix)
    if page is not session.get("page"):
        raise ValueError(f"{error_prefix}tab_not_owned")
    if not _drag_url_matches(str(getattr(page, "url", "") or ""), expected_url, expected_url_prefix):
        raise ValueError(f"{error_prefix}url_mismatch")
    return page


def _viewport_dimensions(metrics: dict[str, Any]) -> tuple[float, float]:
    viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
    layout = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
    width = float(viewport.get("clientWidth") or layout.get("clientWidth") or 0)
    height = float(viewport.get("clientHeight") or layout.get("clientHeight") or 0)
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("browser_viewport_metrics_unavailable")
    return width, height


async def _read_page_viewport(cdp_session: Any) -> dict[str, Any]:
    metrics = await cdp_session.send("Page.getLayoutMetrics")
    width, height = _viewport_dimensions(metrics)
    # Page.getLayoutMetrics reports CSS dimensions but not the original DSF.
    return {"width": round(width, 2), "height": round(height, 2), "device_scale_factor": None}


async def _new_page_cdp_session(page: Any, error_code: str) -> Any:
    try:
        return await page.context.new_cdp_session(page)
    except Exception:
        raise ValueError(error_code) from None


def _viewport_has_active_measurement(session_id: str, tab_handle: str) -> bool:
    return any(
        row["session_id"] == session_id
        and row["tab_handle"] == tab_handle
        and not row["done"].is_set()
        for row in _MEASUREMENTS.values()
    )


async def _clear_owned_viewport_override(
    session: dict[str, Any],
    tab_handle: str,
    page: Any,
) -> dict[str, Any] | None:
    overrides = session.setdefault("viewport_overrides", {})
    state = overrides.get(tab_handle)
    if state is None:
        return None
    if page is None:
        page = state.get("page")
    if page is None:
        overrides.pop(tab_handle, None)
        return None
    try:
        is_closed = page.is_closed()
    except Exception:
        is_closed = False
    if is_closed:
        cdp_session = state.get("cdp_session")
        if cdp_session is not None:
            try:
                await cdp_session.detach()
            except Exception:
                pass
        overrides.pop(tab_handle, None)
        return None
    cdp_session = state.get("cdp_session")
    if cdp_session is None:
        cdp_session = await _new_page_cdp_session(page, "browser_viewport_cleanup_failed")
    try:
        await cdp_session.send("Emulation.clearDeviceMetricsOverride")
        effective = await _read_page_viewport(cdp_session)
    except Exception:
        raise ValueError("browser_viewport_cleanup_failed") from None
    try:
        await cdp_session.detach()
    except Exception:
        pass
    overrides.pop(tab_handle, None)
    return effective


async def _clear_session_viewport_overrides(session: dict[str, Any]) -> None:
    overrides = session.setdefault("viewport_overrides", {})
    for tab_handle, state in list(overrides.items()):
        try:
            await _clear_owned_viewport_override(session, tab_handle, state["page"])
        except Exception:
            raise ValueError("browser_viewport_cleanup_failed") from None


async def _apply_device_metrics(
    cdp_session: Any,
    viewport: dict[str, Any],
) -> None:
    await cdp_session.send("Emulation.setDeviceMetricsOverride", {
        "width": viewport["width"],
        "height": viewport["height"],
        "deviceScaleFactor": viewport["device_scale_factor"],
        "mobile": False,
    })


async def browser_set_viewport(
    session_id: str,
    tab_handle: str,
    width: int,
    height: int,
    device_scale_factor: float = 1.0,
    expected_url: str = "",
    expected_url_prefix: str = "",
) -> dict[str, Any]:
    """Apply device metrics to one caller-owned attached tab using its target CDP session."""
    _validate_owned_url_guard(expected_url, expected_url_prefix, "browser_set_viewport_")
    if type(width) is not int or not _MIN_VIEWPORT_WIDTH <= width <= _MAX_VIEWPORT_WIDTH:
        raise ValueError("browser_set_viewport_width_invalid")
    if type(height) is not int or not _MIN_VIEWPORT_HEIGHT <= height <= _MAX_VIEWPORT_HEIGHT:
        raise ValueError("browser_set_viewport_height_invalid")
    if type(device_scale_factor) not in {int, float} or not math.isfinite(device_scale_factor) or not 0.5 <= device_scale_factor <= 3:
        raise ValueError("browser_set_viewport_device_scale_factor_invalid")
    async with _session(session_id, require_page=False) as session:
        page = _require_owned_attached_page(session, tab_handle, "browser_set_viewport_")
        if not _drag_url_matches(str(getattr(page, "url", "") or ""), expected_url, expected_url_prefix):
            raise ValueError("browser_set_viewport_url_mismatch")
        if _viewport_has_active_measurement(session_id, tab_handle):
            raise ValueError("browser_viewport_measurement_active")
        overrides = session.setdefault("viewport_overrides", {})
        previous = overrides.get(tab_handle)
        cdp_session = previous.get("cdp_session") if previous else None
        created_cdp_session = cdp_session is None
        if cdp_session is None:
            cdp_session = await _new_page_cdp_session(page, "browser_set_viewport_failed")
        try:
            original = previous["original"] if previous else await _read_page_viewport(cdp_session)
            requested = {
                "width": width,
                "height": height,
                "device_scale_factor": float(device_scale_factor),
            }
            overrides[tab_handle] = {
                "page": page,
                "original": original,
                "current": requested,
                "cdp_session": cdp_session,
                "pending": True,
            }
            await _apply_device_metrics(cdp_session, requested)
            effective_width, effective_height = _viewport_dimensions(
                await cdp_session.send("Page.getLayoutMetrics")
            )
        except Exception:
            restored = False
            try:
                if previous is None:
                    await cdp_session.send("Emulation.clearDeviceMetricsOverride")
                    overrides.pop(tab_handle, None)
                else:
                    await _apply_device_metrics(cdp_session, previous["current"])
                    overrides[tab_handle] = previous
                restored = True
            except Exception:
                if previous is not None:
                    overrides[tab_handle] = previous
            if not restored and previous is None:
                overrides[tab_handle] = {
                    "page": page,
                    "original": original if "original" in locals() else None,
                    "current": requested if "requested" in locals() else None,
                    "cdp_session": cdp_session,
                    "pending": True,
                }
            if created_cdp_session and restored:
                try:
                    await cdp_session.detach()
                except Exception:
                    pass
            raise ValueError("browser_set_viewport_failed") from None
        effective = {
            "width": round(effective_width, 2),
            "height": round(effective_height, 2),
            "device_scale_factor": float(device_scale_factor),
        }
        overrides[tab_handle] = {
            "page": page,
            "original": original,
            "current": effective,
            "cdp_session": cdp_session,
            "pending": False,
        }
        return {
            "session_id": session_id,
            "tab_handle": tab_handle,
            "url": _safe_browser_url(str(getattr(page, "url", "") or "")),
            "effective_viewport": effective,
            "original_viewport": original,
            "device_scale_factor_readback_available": False,
        }


async def browser_reset_viewport(
    session_id: str,
    tab_handle: str,
    expected_url: str = "",
    expected_url_prefix: str = "",
) -> dict[str, Any]:
    """Clear device-metrics emulation for one caller-owned attached tab."""
    _validate_owned_url_guard(expected_url, expected_url_prefix, "browser_reset_viewport_")
    async with _session(session_id, require_page=False) as session:
        page = _require_owned_attached_page(session, tab_handle, "browser_reset_viewport_")
        if not _drag_url_matches(str(getattr(page, "url", "") or ""), expected_url, expected_url_prefix):
            raise ValueError("browser_reset_viewport_url_mismatch")
        if _viewport_has_active_measurement(session_id, tab_handle):
            raise ValueError("browser_viewport_measurement_active")
        state = session.setdefault("viewport_overrides", {}).get(tab_handle)
        cdp_session = state.get("cdp_session") if state else None
        if cdp_session is None:
            cdp_session = await _new_page_cdp_session(page, "browser_reset_viewport_failed")
        try:
            await cdp_session.send("Emulation.clearDeviceMetricsOverride")
            effective = await _read_page_viewport(cdp_session)
        except Exception:
            raise ValueError("browser_reset_viewport_failed") from None
        session.setdefault("viewport_overrides", {}).pop(tab_handle, None)
        try:
            await cdp_session.detach()
        except Exception:
            pass
        return {
            "session_id": session_id,
            "tab_handle": tab_handle,
            "url": _safe_browser_url(str(getattr(page, "url", "") or "")),
            "cleared": True,
            "effective_viewport": effective,
            "restored_viewport": state["original"] if state else None,
            "had_tracked_override": state is not None,
        }


async def _drag_locator(page: Any, selector: str, role: str, timeout_ms: int) -> Any:
    try:
        locator = page.locator(selector)
        count = await locator.count()
    except Exception:
        raise ValueError(f"browser_drag_{role}_selector_invalid") from None
    if count == 0:
        raise ValueError(f"browser_drag_{role}_not_found")
    if count != 1:
        raise ValueError(f"browser_drag_{role}_ambiguous")
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout_ms)
    except Exception:
        raise ValueError(f"browser_drag_{role}_not_visible") from None
    return locator


async def _drag_box(locator: Any, role: str) -> dict[str, float]:
    try:
        box = await locator.bounding_box()
    except Exception:
        raise ValueError(f"browser_drag_{role}_not_visible") from None
    if not isinstance(box, dict):
        raise ValueError(f"browser_drag_{role}_not_visible")
    try:
        x, y = float(box["x"]), float(box["y"])
        width, height = float(box["width"]), float(box["height"])
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"browser_drag_{role}_not_visible") from None
    if not all(math.isfinite(value) for value in (x, y, width, height)) or width <= 0 or height <= 0:
        raise ValueError(f"browser_drag_{role}_not_visible")
    return {"x": x, "y": y, "width": width, "height": height}


def _drag_offset_pixels(value: int | float | None, extent: float, axis: str) -> float:
    """Resolve an offset where [0, 1] is a box ratio and >1 is CSS pixels."""
    if value is None:
        return extent / 2
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError(f"browser_drag_target_offset_{axis}_invalid")
    offset = float(value) * extent if value <= 1 else float(value)
    if offset > extent:
        raise ValueError(f"browser_drag_target_offset_{axis}_invalid")
    # Keep inclusive ratio/pixel edges inside the element's hit-test box.
    return min(offset, max(0.0, extent - 0.01))


async def _drag_viewport_size(
    page: Any,
    error_code: str = "browser_drag_viewport_unavailable",
    cdp_session: Any = None,
) -> tuple[float, float]:
    context = getattr(page, "context", None)
    if cdp_session is not None:
        try:
            metrics = await cdp_session.send("Page.getLayoutMetrics")
            viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
            layout = metrics.get("cssLayoutViewport") or metrics.get("layoutViewport") or {}
            width = float(viewport.get("clientWidth") or layout.get("clientWidth") or 0)
            height = float(viewport.get("clientHeight") or layout.get("clientHeight") or 0)
            if width > 0 and height > 0:
                return width, height
        except Exception:
            pass
    elif context is not None:
        owned_cdp_session = None
        try:
            owned_cdp_session = await context.new_cdp_session(page)
            metrics = await owned_cdp_session.send("Page.getLayoutMetrics")
            width, height = _viewport_dimensions(metrics)
            return width, height
        except Exception:
            pass
        finally:
            if owned_cdp_session is not None:
                try:
                    await owned_cdp_session.detach()
                except Exception:
                    pass
    size = getattr(page, "viewport_size", None)
    if isinstance(size, dict):
        try:
            width, height = float(size["width"]), float(size["height"])
            if width > 0 and height > 0:
                return width, height
        except (KeyError, TypeError, ValueError):
            pass
    raise ValueError(error_code)


async def _perform_browser_drag(
    page: Any,
    *,
    source_selector: str,
    target_selector: str,
    steps: int,
    timeout_sec: float,
    hold_ms: int,
    modifiers: list[str],
    settle_ms: int = 0,
    target_offset_x: int | float | None = None,
    target_offset_y: int | float | None = None,
    release_delay_ms: int = 0,
    viewport_cdp_session: Any = None,
) -> dict[str, Any]:
    timeout_ms = int(timeout_sec * 1_000)
    source = await _drag_locator(page, source_selector, "source", timeout_ms)
    target = await _drag_locator(page, target_selector, "target", timeout_ms)
    source_box = await _drag_box(source, "source")
    target_box = await _drag_box(target, "target")
    viewport_width, viewport_height = await _drag_viewport_size(
        page, cdp_session=viewport_cdp_session
    )
    source_center = (source_box["x"] + source_box["width"] / 2, source_box["y"] + source_box["height"] / 2)
    target_center = (target_box["x"] + target_box["width"] / 2, target_box["y"] + target_box["height"] / 2)
    target_point = (
        target_box["x"] + _drag_offset_pixels(target_offset_x, target_box["width"], "x"),
        target_box["y"] + _drag_offset_pixels(target_offset_y, target_box["height"], "y"),
    )
    for role, point in (("source", source_center), ("target", target_point)):
        if not (0 <= point[0] < viewport_width and 0 <= point[1] < viewport_height):
            raise ValueError(f"browser_drag_{role}_outside_viewport")

    pressed_modifiers: list[str] = []
    mouse_pressed = False
    mouse_up_at: float | None = None
    try:
        for modifier in modifiers:
            await page.keyboard.down(modifier)
            pressed_modifiers.append(modifier)
        await page.mouse.move(*source_center)
        await page.mouse.down(button="left")
        mouse_pressed = True
        if hold_ms:
            await asyncio.sleep(hold_ms / 1_000)
        await page.mouse.move(*target_point, steps=steps)
        if release_delay_ms:
            await asyncio.sleep(release_delay_ms / 1_000)
        await page.mouse.up(button="left")
        mouse_pressed = False
        mouse_up_at = time.monotonic()
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ValueError("browser_drag_failed") from None
    finally:
        if mouse_pressed:
            try:
                await page.mouse.up(button="left")
            except Exception:
                pass
        for modifier in reversed(pressed_modifiers):
            try:
                await page.keyboard.up(modifier)
            except Exception:
                pass

    if mouse_up_at is None:
        raise ValueError("browser_drag_failed")
    if settle_ms:
        await asyncio.sleep(settle_ms / 1_000)

    safe_url = _safe_browser_url(str(getattr(page, "url", "") or ""))
    parsed = urlsplit(safe_url)
    result = {
        "action": "drag",
        "source": {"resolved": True, "center": {"x": round(source_center[0], 2), "y": round(source_center[1], 2)}},
        "target": {
            "resolved": True,
            "center": {"x": round(target_center[0], 2), "y": round(target_center[1], 2)},
            "drop_point": {"x": round(target_point[0], 2), "y": round(target_point[1], 2)},
        },
        "target_offset": {
            "requested_x": target_offset_x,
            "requested_y": target_offset_y,
            "resolved_x_px": round(target_point[0] - target_box["x"], 2),
            "resolved_y_px": round(target_point[1] - target_box["y"], 2),
        },
        "steps": steps,
        "hold_ms": hold_ms,
        "release_delay_ms": release_delay_ms,
        "settle_ms": settle_ms,
        "modifiers": list(modifiers),
        "final_url": {"host": parsed.netloc, "path": parsed.path},
    }
    result["release_to_return_ms"] = round((time.monotonic() - mouse_up_at) * 1_000, 2)
    result["_mouse_up_at_monotonic"] = mouse_up_at
    return result


async def browser_drag(
    session_id: str,
    tab_handle: str,
    source_selector: str,
    target_selector: str,
    expected_url: str = "",
    expected_url_prefix: str = "",
    steps: int = 12,
    timeout_sec: float = 10,
    hold_ms: int = 100,
    settle_ms: int = 0,
    modifiers: list[str] | None = None,
    target_offset_x: int | float | None = None,
    target_offset_y: int | float | None = None,
    release_delay_ms: int = 0,
) -> dict[str, Any]:
    """Drag between CSS-selected elements on a caller-owned attached tab only."""
    if not isinstance(source_selector, str) or not source_selector.strip() or len(source_selector) > 2_000:
        raise ValueError("browser_drag_source_selector_invalid")
    if not isinstance(target_selector, str) or not target_selector.strip() or len(target_selector) > 2_000:
        raise ValueError("browser_drag_target_selector_invalid")
    _validate_owned_url_guard(expected_url, expected_url_prefix, "browser_drag_")
    if type(steps) is not int or not 1 <= steps <= 120:
        raise ValueError("browser_drag_steps_invalid")
    if type(timeout_sec) not in {int, float} or not math.isfinite(timeout_sec) or not 0.1 <= timeout_sec <= 120:
        raise ValueError("browser_drag_timeout_invalid")
    if type(hold_ms) is not int or not 0 <= hold_ms <= 5_000:
        raise ValueError("browser_drag_hold_invalid")
    if type(settle_ms) is not int or not 0 <= settle_ms <= 5_000:
        raise ValueError("browser_drag_settle_invalid")
    if type(release_delay_ms) is not int or not 0 <= release_delay_ms <= 5_000:
        raise ValueError("browser_drag_release_delay_invalid")
    for axis, value in (("x", target_offset_x), ("y", target_offset_y)):
        if value is not None and (
            type(value) not in {int, float} or not math.isfinite(value) or value < 0
        ):
            raise ValueError(f"browser_drag_target_offset_{axis}_invalid")
    if modifiers is None:
        modifiers = []
    if (
        not isinstance(modifiers, list)
        or len(modifiers) > 4
        or any(not isinstance(modifier, str) or modifier not in {"Alt", "Control", "Meta", "Shift"} for modifier in modifiers)
        or len(set(modifiers)) != len(modifiers)
    ):
        raise ValueError("browser_drag_modifiers_invalid")
    async with _session(session_id, require_page=False) as session:
        page = _require_owned_attached_page(session, tab_handle, "browser_drag_")
        current_url = str(getattr(page, "url", "") or "")
        if not _drag_url_matches(current_url, expected_url, expected_url_prefix):
            raise ValueError("browser_drag_url_mismatch")
        visibility = await ensure_attached_page_visible(session, page, tab_handle=tab_handle)
        viewport_override = session.get("viewport_overrides", {}).get(tab_handle)
        result = await _perform_browser_drag(
            page,
            source_selector=source_selector,
            target_selector=target_selector,
            steps=steps,
            timeout_sec=float(timeout_sec),
            hold_ms=hold_ms,
            modifiers=modifiers,
            settle_ms=settle_ms,
            target_offset_x=target_offset_x,
            target_offset_y=target_offset_y,
            release_delay_ms=release_delay_ms,
            viewport_cdp_session=viewport_override.get("cdp_session") if viewport_override else None,
        )
        mouse_up_at = result.pop("_mouse_up_at_monotonic", None)
        final_url = str(getattr(page, "url", "") or "")
        result["url_guard_matched_after_settle"] = _drag_url_matches(final_url, expected_url, expected_url_prefix)
        if mouse_up_at is not None:
            result["release_to_return_ms"] = round((time.monotonic() - mouse_up_at) * 1_000, 2)
        if visibility is not None:
            result["tab_visibility"] = visibility
        return result


def _measurement_output_directory(session_id: str, kind: str, out_subdir: str) -> Path:
    if not isinstance(out_subdir, str) or len(out_subdir) > 240 or "\x00" in out_subdir:
        raise ValueError("browser_measure_output_path_invalid")
    normalized = out_subdir.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise ValueError("browser_measure_output_path_invalid")
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." or ":" in part for part in parts):
        raise ValueError("browser_measure_output_path_invalid")

    repo_root = Path(__file__).resolve().parents[1]
    local_root = repo_root / ".runtime"
    is_junction = getattr(local_root, "is_junction", lambda: False)
    if local_root.is_symlink() or is_junction():
        raise ValueError("browser_measure_output_path_invalid")
    local_resolved = local_root.resolve()
    caller_key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    folder_name = f"{kind}-{stamp}-{uuid.uuid4().hex[:12]}"
    candidate = local_root.joinpath(*parts, "browser-measurements", caller_key, folder_name)
    resolved = candidate.resolve()
    try:
        resolved.relative_to(local_resolved)
    except ValueError:
        raise ValueError("browser_measure_output_path_invalid") from None
    try:
        resolved.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        raise ValueError("browser_measure_output_path_unavailable") from None
    except OSError:
        raise ValueError("browser_measure_output_path_unavailable") from None
    return resolved


def _measurement_check_page(
    page: Any,
    expected_url: str,
    expected_url_prefix: str,
    url_mismatch_code: str,
) -> str:
    try:
        if page.is_closed():
            raise ValueError("browser_tab_closed")
    except ValueError:
        raise
    except Exception:
        raise ValueError("browser_tab_closed") from None
    current_url = str(getattr(page, "url", "") or "")
    if not _drag_url_matches(current_url, expected_url, expected_url_prefix):
        raise ValueError(url_mismatch_code)
    return _safe_browser_url(current_url)


def _image_luma_thumb(data: bytes) -> tuple[list[int], int, int, int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        original_width, original_height = image.size
        thumb = image.convert("L").resize((160, 90), Image.Resampling.BILINEAR)
        pixels = list(thumb.get_flattened_data() if hasattr(thumb, "get_flattened_data") else thumb.getdata())
    return pixels, original_width, original_height, 160, 90


def _normalize_measurement_frame(data: bytes, image_format: str, quality: int) -> tuple[bytes, int, int]:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.thumbnail((_MAX_MEASUREMENT_FRAME_WIDTH, _MAX_MEASUREMENT_FRAME_HEIGHT), Image.Resampling.LANCZOS)
        width, height = image.size
        output = io.BytesIO()
        if image_format == "jpeg":
            image.convert("RGB").save(output, format="JPEG", quality=quality, optimize=True)
        else:
            image.save(output, format="PNG", optimize=True)
        return output.getvalue(), width, height


def _frame_pair_metrics(previous: bytes, current: bytes) -> dict[str, Any]:
    old, width, height, sample_width, sample_height = _image_luma_thumb(previous)
    new, new_width, new_height, _, _ = _image_luma_thumb(current)
    if (width, height) != (new_width, new_height):
        return {
            "change_ratio": 1.0,
            "changed_bbox_px": None,
            "estimated_vertical_shift_px": None,
            "dimensions_changed": True,
        }
    threshold = 22
    changed: list[tuple[int, int, int, int]] = []
    changed_indexes: set[int] = set()
    for index, (before, after) in enumerate(zip(old, new)):
        delta = after - before
        magnitude = abs(delta)
        if magnitude < threshold:
            continue
        x, y = index % sample_width, index // sample_width
        changed.append((x, y, magnitude, 1 if delta > 0 else -1))
        changed_indexes.add(index)

    bbox = None
    if changed:
        min_x = min(point[0] for point in changed)
        min_y = min(point[1] for point in changed)
        max_x = max(point[0] for point in changed)
        max_y = max(point[1] for point in changed)
        bbox = {
            "x": round(min_x * width / sample_width, 2),
            "y": round(min_y * height / sample_height, 2),
            "width": round((max_x - min_x + 1) * width / sample_width, 2),
            "height": round((max_y - min_y + 1) * height / sample_height, 2),
        }

    components = []
    shift_candidates: list[tuple[float, int]] = []
    pending = set(changed_indexes)
    while pending:
        seed = pending.pop()
        component_indexes = {seed}
        stack = [seed]
        while stack:
            index = stack.pop()
            x, y = index % sample_width, index // sample_width
            for neighbor_y in range(max(0, y - 1), min(sample_height, y + 2)):
                for neighbor_x in range(max(0, x - 1), min(sample_width, x + 2)):
                    neighbor = neighbor_y * sample_width + neighbor_x
                    if neighbor in pending:
                        pending.remove(neighbor)
                        component_indexes.add(neighbor)
                        stack.append(neighbor)
        if len(component_indexes) < 3:
            continue
        coordinates = [(index % sample_width, index // sample_width) for index in component_indexes]
        component_min_x = min(point[0] for point in coordinates)
        component_min_y = min(point[1] for point in coordinates)
        component_max_x = max(point[0] for point in coordinates)
        component_max_y = max(point[1] for point in coordinates)
        component_positive = [(index // sample_width, abs(new[index] - old[index])) for index in component_indexes if new[index] > old[index]]
        component_negative = [(index // sample_width, abs(new[index] - old[index])) for index in component_indexes if new[index] < old[index]]
        all_changes = component_positive + component_negative
        component_center = sum(y * weight for y, weight in all_changes) / sum(weight for _, weight in all_changes)
        component_center_px = component_center * height / sample_height
        component_sign = 1 if component_positive and not component_negative else -1 if component_negative and not component_positive else 0
        component_shift = None
        if component_positive and component_negative:
            positive_center = sum(y * weight for y, weight in component_positive) / sum(weight for _, weight in component_positive)
            negative_center = sum(y * weight for y, weight in component_negative) / sum(weight for _, weight in component_negative)
            component_shift = round((negative_center - positive_center) * height / sample_height, 2)
            shift_candidates.append((component_shift, sum(weight for _, weight in all_changes)))
        region = {
            "changed_bbox_px": {
                "x": round(component_min_x * width / sample_width, 2),
                "y": round(component_min_y * height / sample_height, 2),
                "width": round((component_max_x - component_min_x + 1) * width / sample_width, 2),
                "height": round((component_max_y - component_min_y + 1) * height / sample_height, 2),
            },
            "area_ratio": round(len(component_indexes) / (sample_width * sample_height), 6),
            "estimated_vertical_shift_px": component_shift,
            "delta_sign": component_sign,
            "center_y_px": round(component_center_px, 2),
            "change_weight": sum(weight for _, weight in all_changes),
        }
        components.append(region)
    components.sort(key=lambda item: item["area_ratio"], reverse=True)

    for index, first in enumerate(components):
        for second in components[index + 1:]:
            if first["delta_sign"] == 0 or second["delta_sign"] == 0 or first["delta_sign"] == second["delta_sign"]:
                continue
            positive, negative = (first, second) if first["delta_sign"] > 0 else (second, first)
            first_box = positive["changed_bbox_px"]
            second_box = negative["changed_bbox_px"]
            overlap = max(0.0, min(first_box["x"] + first_box["width"], second_box["x"] + second_box["width"]) - max(first_box["x"], second_box["x"]))
            denominator = min(first_box["width"], second_box["width"])
            if denominator > 0 and overlap / denominator >= 0.7:
                top, bottom = (positive, negative) if positive["center_y_px"] < negative["center_y_px"] else (negative, positive)
                band_size = (top["changed_bbox_px"]["height"] + bottom["changed_bbox_px"]["height"]) / 2
                direction = 1 if top["delta_sign"] > 0 and bottom["delta_sign"] < 0 else -1
                shift_candidates.append((direction * band_size, min(positive["change_weight"], negative["change_weight"])))
    shift = max(shift_candidates, key=lambda item: item[1])[0] if shift_candidates else None

    return {
        "change_ratio": round(len(changed) / (sample_width * sample_height), 6),
        "changed_bbox_px": bbox,
        "estimated_vertical_shift_px": shift,
        "changed_regions": components[:12],
        "dimensions_changed": False,
    }


def _bbox_iou(first: dict[str, Any] | None, second: dict[str, Any] | None) -> float:
    if not isinstance(first, dict) or not isinstance(second, dict):
        return 0.0
    left = max(first["x"], second["x"])
    top = max(first["y"], second["y"])
    right = min(first["x"] + first["width"], second["x"] + second["width"])
    bottom = min(first["y"] + first["height"], second["y"] + second["height"])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = first["width"] * first["height"] + second["width"] * second["height"] - intersection
    return intersection / union if union > 0 else 0.0


def _analyze_frame_series(
    comparisons: list[dict[str, Any]],
    flicker_diff_threshold: float,
    vertical_shift_threshold_px: float,
    window_comparisons: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    flicker_regions: list[tuple[int, dict[str, Any]]] = []
    shift_pairs = []
    for pair in comparisons:
        pair_shift = pair.get("estimated_vertical_shift_px")
        for region in pair.get("changed_regions", []):
            shift = region.get("estimated_vertical_shift_px")
            bbox = region["changed_bbox_px"]
            # A moving box creates thin leading/trailing edge bands. Do not
            # report those narrow bands as a stationary blink region when the
            # same frame pair already shows substantial vertical translation.
            if pair_shift is not None and abs(pair_shift) >= vertical_shift_threshold_px / 2 and bbox["height"] <= max(8.0, vertical_shift_threshold_px * 2):
                continue
            if region["area_ratio"] >= flicker_diff_threshold and (shift is None or abs(shift) < vertical_shift_threshold_px / 2):
                flicker_regions.append((pair["pair_index"], bbox))
        shift = pair_shift
        if shift is not None and abs(shift) >= vertical_shift_threshold_px:
            shift_pairs.append((pair.get("to_t_ms", pair["pair_index"]), shift))
    for pair in window_comparisons or []:
        shift = pair.get("estimated_vertical_shift_px")
        if shift is not None and abs(shift) >= vertical_shift_threshold_px:
            shift_pairs.append((pair.get("to_t_ms", pair["pair_index"]), shift))
    shift_pairs.sort(key=lambda row: row[0])
    reversals = 0
    previous_sign = 0
    for _, shift in shift_pairs:
        sign = 1 if shift > 0 else -1
        if previous_sign and sign != previous_sign:
            reversals += 1
        previous_sign = sign
    region_pair_counts = []
    for pair_index, bbox in flicker_regions:
        overlaps = {
            other_pair for other_pair, other_bbox in flicker_regions
            if other_pair != pair_index and _bbox_iou(bbox, other_bbox) >= 0.85
        }
        region_pair_counts.append(len(overlaps) + 1)
    recurring_regions = max(region_pair_counts, default=0)
    flicker_bbox = None
    if region_pair_counts:
        best_index = region_pair_counts.index(recurring_regions)
        flicker_bbox = flicker_regions[best_index][1]
    return {
        "flicker_detected": recurring_regions >= 2,
        "flicker_evidence": {
            "repeated_changed_region_pairs": recurring_regions,
            "minimum_change_ratio": flicker_diff_threshold,
            "region_bbox_px": flicker_bbox,
        },
        "vertical_jitter_detected": len(shift_pairs) >= 2 and reversals > 0,
        "vertical_jitter_evidence": {
            "qualifying_shift_pairs": len(shift_pairs),
            "direction_reversals_observed": reversals > 0,
            "direction_reversal_count": reversals,
            "threshold_px": vertical_shift_threshold_px,
            "max_absolute_estimated_shift_px": round(max((abs(value) for _, value in shift_pairs), default=0.0), 2),
        },
    }


def _write_measurement_manifest(record: dict[str, Any], result: dict[str, Any]) -> str:
    manifest = {
        "measurement_id": record["measurement_id"],
        "kind": record["kind"],
        "created_utc": record["created_utc"],
        "started_utc": record["created_utc"],
        "completed_utc": datetime.now(timezone.utc).isoformat(),
        "url": result.get("url", ""),
        "sample_count": result.get("sample_count", 0),
        "error_code": result.get("error_code"),
        "result": result,
    }
    try:
        encoded = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError):
        record["manifest_error"] = "browser_measure_manifest_invalid"
        return ""
    if len(encoded) > _MAX_MEASUREMENT_MANIFEST_BYTES:
        record["manifest_error"] = "browser_measure_manifest_too_large"
        return ""
    path = record["output_directory"] / "manifest.json"
    try:
        with path.open("xb") as output:
            output.write(encoded)
    except OSError:
        record["manifest_error"] = "browser_measure_manifest_write_failed"
        return ""
    return str(path)


def _measurement_result(record: dict[str, Any]) -> dict[str, Any]:
    result = dict(record.get("result") or {})
    error_code = record.get("error_code") or record.get("manifest_error")
    result.update({
        "measurement_id": record["measurement_id"],
        "kind": record["kind"],
        "started_utc": record["created_utc"],
        "completed": error_code is None,
        "error_code": error_code,
        "manifest_path": record.get("manifest_path", ""),
        "output_directory": str(record["output_directory"]),
    })
    return result


async def _wait_measurement_tick(stop_event: asyncio.Event, timeout: float) -> None:
    if timeout <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


def _percentile95(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)], 2)


def _frame_timing_summary(
    requested_interval_ms: int,
    saved_frames: list[dict[str, Any]],
    attempts: list[dict[str, Any]],
    dropped_frames: int,
    failed_frames: int,
) -> dict[str, Any]:
    times = [float(row["t_ms"]) for row in saved_frames]
    intervals = [max(0.0, end - start) for start, end in zip(times, times[1:])]
    durations = [float(row["capture_duration_ms"]) for row in attempts]
    actual = {
        "min": round(min(intervals), 2) if intervals else None,
        "median": round(statistics.median(intervals), 2) if intervals else None,
        "p95": _percentile95(intervals),
        "max": round(max(intervals), 2) if intervals else None,
    }
    capture = {
        "median": round(statistics.median(durations), 2) if durations else None,
        "max": round(max(durations), 2) if durations else None,
    }
    warnings = []
    if actual["median"] is not None and actual["median"] > requested_interval_ms * 1.5:
        warnings.append("interval_not_achievable")
    if dropped_frames:
        warnings.append("frames_dropped")
    if failed_frames:
        warnings.append("capture_failures")
    return {
        "requested_interval_ms": requested_interval_ms,
        "actual_interval_ms": actual,
        "capture_duration_ms": capture,
        "dropped_frames": dropped_frames,
        "failed_frames": failed_frames,
        "warnings": warnings,
        "warning": warnings[0] if warnings else None,
    }


async def _capture_frames_worker(record: dict[str, Any]) -> None:
    page = record["page"]
    cdp_session = None
    owns_cdp_session = record.get("viewport_cdp_session") is None
    attempts: list[dict[str, Any]] = []
    raw_frames: list[dict[str, Any]] = []
    saved_frames: list[dict[str, Any]] = []
    comparisons: list[dict[str, Any]] = []
    window_comparisons: list[dict[str, Any]] = []
    started = record["started_monotonic"]
    clip = record["clip_rect"]
    safe_url = ""
    error_code = None
    stop_reason = "duration_limit"
    dropped_frames = 0
    failed_frames = 0
    raw_bytes_used = 0
    try:
        safe_url = _measurement_check_page(
            page, record["expected_url"], record["expected_url_prefix"], "browser_capture_frames_url_mismatch"
        )
        cdp_session = record.get("viewport_cdp_session")
        if cdp_session is None:
            cdp_session = await page.context.new_cdp_session(page)
        if record["clip_selector"]:
            try:
                locator = page.locator(record["clip_selector"])
                count = await locator.count()
            except Exception:
                raise ValueError("browser_capture_frames_clip_selector_invalid") from None
            if count == 0:
                raise ValueError("browser_capture_frames_clip_not_found")
            if count != 1:
                raise ValueError("browser_capture_frames_clip_ambiguous")
            try:
                box = await locator.bounding_box()
            except Exception:
                raise ValueError("browser_capture_frames_clip_not_visible") from None
            clip = _validated_clip_box(box)
        if clip is not None:
            viewport_width, viewport_height = await _drag_viewport_size(
                page, "browser_capture_frames_viewport_unavailable", cdp_session=cdp_session
            )
            if clip["x"] < 0 or clip["y"] < 0 or clip["x"] + clip["width"] > viewport_width or clip["y"] + clip["height"] > viewport_height:
                raise ValueError("browser_capture_frames_clip_outside_viewport")
        interval_sec = record["interval_ms"] / 1_000
        slot_index = 0
        consecutive_failures = 0
        while slot_index < record["max_samples"]:
            elapsed_ms = (time.monotonic() - started) * 1_000
            if record["stop_event"].is_set():
                stop_reason = "caller_stop"
                break
            if elapsed_ms >= record["duration_ms"]:
                stop_reason = "duration_limit"
                break
            # Keep slots on the original clock. Overdue slots are dropped so a
            # slow capture never shifts every later frame by its own duration.
            # Windows timer wakeups can overshoot a short interval by several
            # milliseconds; absorb that jitter without allowing cumulative drift.
            tolerance = min(0.015, interval_sec / 2)
            while slot_index < record["max_samples"]:
                scheduled_at = started + slot_index * interval_sec
                now = time.monotonic()
                if now < scheduled_at:
                    await _wait_measurement_tick(record["stop_event"], scheduled_at - now)
                    if record["stop_event"].is_set():
                        stop_reason = "caller_stop"
                        break
                    continue
                if now - scheduled_at > tolerance:
                    slot_index += 1
                    dropped_frames += 1
                    continue
                break
            if record["stop_event"].is_set():
                stop_reason = "caller_stop"
                break
            if slot_index >= record["max_samples"]:
                stop_reason = "frame_limit"
                break
            elapsed_ms = (time.monotonic() - started) * 1_000
            if elapsed_ms >= record["duration_ms"]:
                stop_reason = "duration_limit"
                break
            safe_url = _measurement_check_page(
                page, record["expected_url"], record["expected_url_prefix"], "browser_capture_frames_url_mismatch"
            )
            frame_index = slot_index
            slot_index += 1
            capture_started = time.monotonic()
            capture_error = None
            image_data = b""
            params: dict[str, Any] = {
                "format": record["format"],
                "fromSurface": True,
                "captureBeyondViewport": False,
            }
            if record["format"] == "jpeg":
                params["quality"] = record["quality"]
            if clip is not None:
                params["clip"] = {**clip, "scale": 1}
            try:
                remaining_sec = max(0.1, record["duration_ms"] / 1_000 - (capture_started - started))
                response = await asyncio.wait_for(
                    cdp_session.send("Page.captureScreenshot", params), timeout=min(5.0, remaining_sec)
                )
                image_data = base64.b64decode(response.get("data", ""), validate=True)
                if not image_data:
                    capture_error = "browser_capture_frames_capture_failed"
            except asyncio.CancelledError:
                raise
            except Exception:
                capture_error = "browser_capture_frames_capture_failed"
            capture_finished = time.monotonic()
            capture_duration_ms = round((capture_finished - capture_started) * 1_000, 2)
            t_ms = round(((capture_started + capture_finished) / 2 - started) * 1_000, 2)
            slot = {
                "frame_index": frame_index,
                "t_ms": t_ms,
                "capture_duration_ms": capture_duration_ms,
                "error_code": capture_error,
            }
            attempts.append(slot)
            if capture_error:
                failed_frames += 1
                consecutive_failures += 1
                record["ready"].set()
                if consecutive_failures >= _MAX_MEASUREMENT_CONSECUTIVE_FAILURES:
                    error_code = "browser_capture_frames_consecutive_failures"
                    stop_reason = "consecutive_capture_failures"
                    break
                continue
            if len(image_data) > _MAX_MEASUREMENT_FRAME_BYTES:
                slot["error_code"] = "browser_measure_frame_size_limit"
                failed_frames += 1
                error_code = slot["error_code"]
                stop_reason = "frame_size_limit"
                record["ready"].set()
                break
            if raw_bytes_used + len(image_data) > _MAX_MEASUREMENT_TOTAL_BYTES:
                slot["error_code"] = "browser_measure_total_size_limit"
                failed_frames += 1
                error_code = slot["error_code"]
                stop_reason = "total_size_limit"
                record["ready"].set()
                break
            raw_bytes_used += len(image_data)
            raw_frames.append({"slot": slot, "data": image_data})
            consecutive_failures = 0
            record["ready"].set()
        if stop_reason == "duration_limit" and slot_index >= record["max_samples"]:
            stop_reason = "frame_limit"
    except asyncio.CancelledError:
        error_code = "browser_measure_cancelled"
        stop_reason = "cancelled"
    except ValueError as exc:
        code = str(exc)
        error_code = code if re.fullmatch(r"browser_[a-z0-9_]{1,80}", code) else "browser_capture_frames_failed"
        stop_reason = "measurement_error"
    except Exception:
        error_code = "browser_capture_frames_failed"
        stop_reason = "measurement_error"
    finally:
        if cdp_session is not None and owns_cdp_session:
            try:
                await cdp_session.detach()
            except Exception:
                pass

    previous_image: bytes | None = None
    saved_image_rows: list[tuple[dict[str, Any], bytes]] = []
    for capture in raw_frames:
        slot = capture["slot"]
        image_data = capture["data"]
        try:
            image_data, image_width, image_height = _normalize_measurement_frame(
                image_data, record["format"], record["quality"]
            )
            if len(image_data) > _MAX_MEASUREMENT_FRAME_BYTES:
                raise ValueError("browser_measure_frame_size_limit")
            if record["bytes_used"] + len(image_data) > _MAX_MEASUREMENT_TOTAL_BYTES:
                raise ValueError("browser_measure_total_size_limit")
            filename = f"frame-{slot['frame_index'] + 1:03d}.{record['format']}"
            (record["output_directory"] / filename).write_bytes(image_data)
        except ValueError as exc:
            slot["error_code"] = str(exc)
            failed_frames += 1
            if error_code is None and slot["error_code"] == "browser_measure_total_size_limit":
                error_code = slot["error_code"]
                stop_reason = "total_size_limit"
                break
            continue
        except Exception:
            slot["error_code"] = "browser_capture_frames_write_failed"
            failed_frames += 1
            continue
        record["bytes_used"] += len(image_data)
        slot.update({
            "file": filename,
            "bytes": len(image_data),
            "width_px": image_width,
            "height_px": image_height,
            "error_code": None,
        })
        if previous_image is not None:
            previous_slot = saved_frames[-1]
            pair = _frame_pair_metrics(previous_image, image_data)
            pair["pair_index"] = len(comparisons)
            pair["from_frame"] = previous_slot["frame_index"]
            pair["to_frame"] = slot["frame_index"]
            pair["from_t_ms"] = previous_slot["t_ms"]
            pair["to_t_ms"] = slot["t_ms"]
            pair["actual_interval_ms"] = round(slot["t_ms"] - previous_slot["t_ms"], 2)
            pair["comparison_kind"] = "adjacent"
            comparisons.append(pair)
        window_ms = max(200, record["interval_ms"] * 3)
        prior = next(
            (
                (prior_slot, prior_image)
                for prior_slot, prior_image in reversed(saved_image_rows)
                if slot["t_ms"] - prior_slot["t_ms"] >= window_ms
            ),
            None,
        )
        if prior is not None:
            prior_slot, prior_image = prior
            pair = _frame_pair_metrics(prior_image, image_data)
            pair["pair_index"] = len(window_comparisons)
            pair["from_frame"] = prior_slot["frame_index"]
            pair["to_frame"] = slot["frame_index"]
            pair["from_t_ms"] = prior_slot["t_ms"]
            pair["to_t_ms"] = slot["t_ms"]
            pair["actual_interval_ms"] = round(slot["t_ms"] - prior_slot["t_ms"], 2)
            pair["comparison_kind"] = "window"
            window_comparisons.append(pair)
        saved_frames.append(slot)
        previous_image = image_data
        saved_image_rows.append((slot, image_data))
        capture["data"] = b""

    analysis = _analyze_frame_series(
        comparisons,
        record["flicker_diff_threshold"],
        record["vertical_shift_threshold_px"],
        window_comparisons,
    )
    timing = _frame_timing_summary(record["interval_ms"], saved_frames, attempts, dropped_frames, failed_frames)
    record["ready"].set()
    record["result"] = {
        "url": safe_url,
        "sample_count": len(saved_frames),
        "attempted_frames": len(attempts),
        "frames": attempts,
        "comparisons": comparisons,
        "window_comparisons": window_comparisons,
        "analysis": analysis,
        "stop_reason": stop_reason,
        "bytes_used": record["bytes_used"],
        **timing,
    }
    record["error_code"] = error_code
    record["manifest_path"] = _write_measurement_manifest(record, record["result"])
    record["done"].set()


def _validated_clip_box(box: Any) -> dict[str, float]:
    if not isinstance(box, dict):
        raise ValueError("browser_capture_frames_clip_not_visible")
    try:
        raw_values = {key: box[key] for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        raise ValueError("browser_capture_frames_clip_invalid") from None
    if any(type(value) not in {int, float} for value in raw_values.values()):
        raise ValueError("browser_capture_frames_clip_invalid")
    values = {key: float(value) for key, value in raw_values.items()}
    if not all(math.isfinite(value) for value in values.values()) or values["x"] < 0 or values["y"] < 0 or values["width"] <= 0 or values["height"] <= 0:
        raise ValueError("browser_capture_frames_clip_invalid")
    return values


async def _track_boxes_sample(page: Any, selectors: list[str], previous_slots: list[int], viewport: tuple[float, float]) -> tuple[list[dict[str, Any]], list[int]]:
    rows: list[dict[str, Any]] = []
    slot_counts: list[int] = []
    for selector_index, selector in enumerate(selectors):
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            raise ValueError("browser_track_boxes_selector_invalid") from None
        slot_count = max(previous_slots[selector_index], min(count, _MAX_TRACK_ELEMENTS_PER_SELECTOR))
        slot_counts.append(slot_count)
        for element_index in range(slot_count):
            visible = False
            box = None
            if element_index < count:
                item = locator.nth(element_index)
                try:
                    visible = bool(await item.is_visible())
                    box = await item.bounding_box() if visible else None
                    visible = visible and box is not None
                except Exception:
                    visible = False
                    box = None
            if isinstance(box, dict):
                try:
                    x, y, width, height = (float(box[key]) for key in ("x", "y", "width", "height"))
                    valid = all(math.isfinite(value) for value in (x, y, width, height)) and width > 0 and height > 0
                except (KeyError, TypeError, ValueError):
                    valid = False
                if valid:
                    visible = visible and x + width > 0 and y + height > 0 and x < viewport[0] and y < viewport[1]
                    if visible:
                        box = {"x": round(x, 2), "y": round(y, 2), "width": round(width, 2), "height": round(height, 2)}
                    else:
                        box = None
                else:
                    visible = False
                    box = None
            rows.append({"selector_index": selector_index, "element_index": element_index, "visible": visible, "box": box})
    return rows, slot_counts


def _summarize_box_samples(samples: list[dict[str, Any]], vertical_threshold_px: float) -> list[dict[str, Any]]:
    keys: set[tuple[int, int]] = set()
    for sample in samples:
        for row in sample["elements"]:
            keys.add((row["selector_index"], row["element_index"]))
    summaries = []
    for selector_index, element_index in sorted(keys):
        series = []
        for sample in samples:
            row = next((item for item in sample["elements"] if item["selector_index"] == selector_index and item["element_index"] == element_index), None)
            if row is None:
                row = {"visible": False, "box": None}
            box = row["box"]
            series.append({
                "t_ms": sample["t_ms"],
                "x": box["x"] if box else None,
                "y": box["y"] if box else None,
                "width": box["width"] if box else None,
                "height": box["height"] if box else None,
                "visible": bool(row["visible"]),
            })
        visible_rows = [row for row in series if row["visible"] and isinstance(row["y"], (int, float))]
        ys = [row["y"] for row in visible_rows]
        reversals = 0
        previous_direction = 0
        max_jump = 0.0
        reappearances = 0
        ever_visible = False
        previous_visible = None
        previous_box = None
        for row in series:
            if row["visible"]:
                if previous_visible is False and ever_visible:
                    reappearances += 1
                box = row
                if previous_box is not None:
                    delta_y = box["y"] - previous_box["y"]
                    if abs(delta_y) >= 0.5:
                        direction = 1 if delta_y > 0 else -1
                        if previous_direction and direction != previous_direction:
                            reversals += 1
                        previous_direction = direction
                    jump = math.hypot(box["x"] - previous_box["x"], box["y"] - previous_box["y"])
                    max_jump = max(max_jump, jump)
                previous_box = box
                ever_visible = True
            else:
                previous_box = None
                previous_direction = 0
            previous_visible = row["visible"]
        variation = max(ys) - min(ys) if ys else 0.0
        summaries.append({
            "selector_index": selector_index,
            "element_index": element_index,
            "samples": series,
            "y_max_variation_px": round(variation, 2),
            "y_reversal_count": reversals,
            "disappear_reappear_count": reappearances,
            "max_jump_px": round(max_jump, 2),
            "flicker_detected": reappearances > 0,
            "vertical_jitter_detected": variation >= vertical_threshold_px and reversals > 0,
        })
    return summaries


async def _track_boxes_worker(record: dict[str, Any]) -> None:
    page = record["page"]
    started = record["started_monotonic"]
    samples: list[dict[str, Any]] = []
    previous_slots = [0] * len(record["selectors"])
    safe_url = ""
    error_code = None
    stop_reason = "duration_limit"
    try:
        safe_url = _measurement_check_page(
            page, record["expected_url"], record["expected_url_prefix"], "browser_track_boxes_url_mismatch"
        )
        viewport = await _drag_viewport_size(
            page,
            "browser_track_boxes_viewport_unavailable",
            cdp_session=record.get("viewport_cdp_session"),
        )
        for selector_index, selector in enumerate(record["selectors"]):
            try:
                count = await page.locator(selector).count()
            except Exception:
                raise ValueError("browser_track_boxes_selector_invalid") from None
            if count == 0:
                raise ValueError("browser_track_boxes_selector_not_found")
            previous_slots[selector_index] = min(count, _MAX_TRACK_ELEMENTS_PER_SELECTOR)
        if sum(previous_slots) > _MAX_TRACK_TOTAL_ELEMENTS:
            raise ValueError("browser_track_boxes_element_limit")
        interval_sec = record["interval_ms"] / 1_000
        next_sample_at = started
        while len(samples) < record["max_samples"]:
            elapsed_ms = (time.monotonic() - started) * 1_000
            if record["stop_event"].is_set():
                stop_reason = "caller_stop"
                break
            if elapsed_ms >= record["duration_ms"]:
                stop_reason = "duration_limit"
                break
            safe_url = _measurement_check_page(
                page, record["expected_url"], record["expected_url_prefix"], "browser_track_boxes_url_mismatch"
            )
            rows, counts = await _track_boxes_sample(page, record["selectors"], previous_slots, viewport)
            if sum(counts) > _MAX_TRACK_TOTAL_ELEMENTS:
                raise ValueError("browser_track_boxes_element_limit")
            previous_slots = counts
            samples.append({"t_ms": round((time.monotonic() - started) * 1_000, 2), "elements": rows})
            record["ready"].set()
            if len(samples) >= record["max_samples"]:
                stop_reason = "sample_limit"
                break
            next_sample_at += interval_sec
            now = time.monotonic()
            if next_sample_at < now:
                next_sample_at = now + interval_sec
            await _wait_measurement_tick(record["stop_event"], min(next_sample_at - now, max(0.0, record["duration_ms"] / 1_000 - (now - started))))
    except asyncio.CancelledError:
        error_code = "browser_measure_cancelled"
        stop_reason = "cancelled"
        raise
    except ValueError as exc:
        code = str(exc)
        error_code = code if re.fullmatch(r"browser_[a-z0-9_]{1,80}", code) else "browser_track_boxes_failed"
    except Exception:
        error_code = "browser_track_boxes_failed"
    finally:
        record["ready"].set()
        result = {
            "url": safe_url,
            "sample_count": len(samples),
            "stop_reason": stop_reason,
            "elements": _summarize_box_samples(samples, record["vertical_jitter_threshold_px"]),
        }
        record["result"] = result
        record["error_code"] = error_code
        record["manifest_path"] = _write_measurement_manifest(record, result)
        record["done"].set()


def _validate_measurement_limits(
    error_prefix: str,
    interval_ms: int,
    sample_count: int,
    duration_ms: int,
) -> None:
    if type(interval_ms) is not int or not _MIN_MEASUREMENT_INTERVAL_MS <= interval_ms <= _MAX_MEASUREMENT_INTERVAL_MS:
        raise ValueError(f"{error_prefix}interval_invalid")
    if type(sample_count) is not int or not 1 <= sample_count <= _MAX_MEASUREMENT_FRAMES:
        raise ValueError(f"{error_prefix}sample_limit_invalid")
    if type(duration_ms) is not int or not 100 <= duration_ms <= _MAX_MEASUREMENT_DURATION_MS:
        raise ValueError(f"{error_prefix}duration_invalid")


async def _start_owned_measurement(
    session_id: str,
    tab_handle: str,
    expected_url: str,
    expected_url_prefix: str,
    kind: str,
    worker,
    params: dict[str, Any],
    out_subdir: str,
) -> dict[str, Any]:
    error_prefix = "browser_capture_frames_" if kind == "frames" else "browser_track_boxes_"
    _validate_owned_url_guard(expected_url, expected_url_prefix, error_prefix)
    async with _session(session_id, require_page=False) as session:
        page = _require_owned_attached_page(session, tab_handle, error_prefix)
        safe_url = _measurement_check_page(page, expected_url, expected_url_prefix, f"{error_prefix}url_mismatch")
        active = [
            measurement for measurement in _MEASUREMENTS.values()
            if measurement["session_id"] == session_id and not measurement["done"].is_set()
        ]
        if len(active) >= _MAX_ACTIVE_MEASUREMENTS_PER_SESSION:
            raise ValueError("browser_measure_active_limit")
        output_directory = _measurement_output_directory(session_id, kind, out_subdir)
        measurement_id = uuid.uuid4().hex
        viewport_override = session.get("viewport_overrides", {}).get(tab_handle)
        record = {
            "measurement_id": measurement_id,
            "session_id": session_id,
            "tab_handle": tab_handle,
            "page": page,
            "viewport_cdp_session": viewport_override.get("cdp_session") if viewport_override else None,
            "kind": kind,
            "expected_url": expected_url,
            "expected_url_prefix": expected_url_prefix,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "started_monotonic": time.monotonic(),
            "output_directory": output_directory,
            "bytes_used": 0,
            "stop_event": asyncio.Event(),
            "ready": asyncio.Event(),
            "done": asyncio.Event(),
            "result": None,
            "error_code": None,
            "manifest_path": "",
            **params,
        }
        _MEASUREMENTS[measurement_id] = record
        record["task"] = asyncio.create_task(worker(record), name=f"browser-measure-{kind}-{measurement_id[:8]}")
        try:
            await asyncio.wait_for(record["ready"].wait(), timeout=5)
        except asyncio.TimeoutError:
            record["stop_event"].set()
            record["task"].cancel()
            await asyncio.gather(record["task"], return_exceptions=True)
            _MEASUREMENTS.pop(measurement_id, None)
            raise ValueError("browser_measure_start_timeout") from None
        if record["error_code"] and not record["result"].get("sample_count", 0):
            code = record["error_code"]
            _MEASUREMENTS.pop(measurement_id, None)
            raise ValueError(code)
        return {
            "measurement_id": measurement_id,
            "kind": kind,
            "started": True,
            "tab_handle": tab_handle,
            "url": safe_url,
            "started_utc": record["created_utc"],
            "output_directory": str(output_directory),
            "sampling_in_background": True,
        }


async def _stop_owned_measurement(session_id: str, measurement_id: str, kind: str) -> dict[str, Any]:
    if not isinstance(measurement_id, str) or not re.fullmatch(r"[0-9a-f]{32}", measurement_id):
        raise ValueError("browser_measure_not_found")
    async with _session(session_id, require_page=False) as session:
        if not session.get("attached"):
            raise ValueError("browser_measure_tab_not_owned")
        record = _MEASUREMENTS.get(measurement_id)
        if record is None or record["session_id"] != session_id or record["kind"] != kind:
            raise ValueError("browser_measure_not_found")
        record["stop_event"].set()
        await asyncio.gather(record["task"], return_exceptions=True)
        result = _measurement_result(record)
        _MEASUREMENTS.pop(measurement_id, None)
        return result


async def browser_capture_frames_start(
    session_id: str,
    tab_handle: str,
    expected_url: str = "",
    expected_url_prefix: str = "",
    interval_ms: int = 100,
    max_frames: int = 60,
    duration_ms: int = 3_000,
    clip_selector: str = "",
    clip_rect: dict[str, float] | None = None,
    format: str = "jpeg",
    quality: int = 45,
    flicker_diff_threshold: float = 0.004,
    vertical_shift_threshold_px: float = 4.0,
    out_subdir: str = "",
) -> dict[str, Any]:
    """Start bounded read-only screenshot sampling on this caller's own Edge tab."""
    _validate_measurement_limits("browser_capture_frames_", interval_ms, max_frames, duration_ms)
    if clip_rect is not None and clip_selector:
        raise ValueError("browser_capture_frames_clip_invalid")
    if not isinstance(clip_selector, str) or len(clip_selector) > 2_000:
        raise ValueError("browser_capture_frames_clip_selector_invalid")
    normalized_clip = _validated_clip_box(clip_rect) if clip_rect is not None else None
    if not isinstance(format, str) or format not in {"jpeg", "png"}:
        raise ValueError("browser_capture_frames_format_invalid")
    if type(quality) is not int or not 20 <= quality <= 90:
        raise ValueError("browser_capture_frames_quality_invalid")
    if type(flicker_diff_threshold) not in {int, float} or not math.isfinite(flicker_diff_threshold) or not 0.0001 <= flicker_diff_threshold <= 0.5:
        raise ValueError("browser_capture_frames_flicker_threshold_invalid")
    if type(vertical_shift_threshold_px) not in {int, float} or not math.isfinite(vertical_shift_threshold_px) or not 0.5 <= vertical_shift_threshold_px <= 500:
        raise ValueError("browser_capture_frames_shift_threshold_invalid")
    return await _start_owned_measurement(
        session_id, tab_handle, expected_url, expected_url_prefix, "frames", _capture_frames_worker,
        {
            "interval_ms": interval_ms,
            "max_samples": max_frames,
            "duration_ms": duration_ms,
            "clip_selector": clip_selector,
            "clip_rect": normalized_clip,
            "format": format,
            "quality": quality,
            "flicker_diff_threshold": float(flicker_diff_threshold),
            "vertical_shift_threshold_px": float(vertical_shift_threshold_px),
        },
        out_subdir,
    )


async def browser_capture_frames_stop(session_id: str, measurement_id: str) -> dict[str, Any]:
    """Stop screenshot sampling and return sanitized frame comparisons and paths."""
    return await _stop_owned_measurement(session_id, measurement_id, "frames")


async def browser_track_boxes_start(
    session_id: str,
    tab_handle: str,
    selectors: list[str],
    expected_url: str = "",
    expected_url_prefix: str = "",
    interval_ms: int = 50,
    max_samples: int = 60,
    duration_ms: int = 3_000,
    vertical_jitter_threshold_px: float = 4.0,
    out_subdir: str = "",
) -> dict[str, Any]:
    """Start bounded read-only CSS bounding-box sampling on an owned Edge tab."""
    _validate_measurement_limits("browser_track_boxes_", interval_ms, max_samples, duration_ms)
    if not isinstance(selectors, list) or not 1 <= len(selectors) <= _MAX_TRACK_SELECTORS:
        raise ValueError("browser_track_boxes_selectors_invalid")
    if any(not isinstance(selector, str) or not selector.strip() or len(selector) > 2_000 for selector in selectors):
        raise ValueError("browser_track_boxes_selector_invalid")
    if type(vertical_jitter_threshold_px) not in {int, float} or not math.isfinite(vertical_jitter_threshold_px) or not 0.5 <= vertical_jitter_threshold_px <= 500:
        raise ValueError("browser_track_boxes_shift_threshold_invalid")
    return await _start_owned_measurement(
        session_id, tab_handle, expected_url, expected_url_prefix, "boxes", _track_boxes_worker,
        {
            "selectors": list(selectors),
            "interval_ms": interval_ms,
            "max_samples": max_samples,
            "duration_ms": duration_ms,
            "vertical_jitter_threshold_px": float(vertical_jitter_threshold_px),
        },
        out_subdir,
    )


async def browser_track_boxes_stop(session_id: str, measurement_id: str) -> dict[str, Any]:
    """Stop box sampling and return selector-indexed visibility and motion summaries."""
    return await _stop_owned_measurement(session_id, measurement_id, "boxes")


async def browser_detach(session_id: str) -> dict[str, Any]:
    """Release this logical caller session without disconnecting the shared Edge broker."""
    session = _SESSIONS.get(session_id)
    if session is None:
        if _RELEASED_SESSIONS.get(session_id) == "attached":
            return {"session_id": session_id, "detached": True, "already_detached": True}
        raise ValueError("Browser session not found")
    async with _session(session_id, require_page=False) as locked:
        if not locked.get("attached"):
            raise ValueError("browser_session_not_attached")
        await _release_session(session_id, locked)
    return {
        "session_id": session_id,
        "detached": True,
        "external_browser_closed": False,
        "shared_connection_retained": True,
    }


async def _release_session(session_id: str, session: dict[str, Any]) -> None:
    if session.get("released"):
        return
    mode = "attached" if session.get("attached") else "owned"
    await _stop_session_measurements(session_id, discard=True)
    await _clear_session_viewport_overrides(session)
    session["released"] = True
    if session.get("attached"):
        _unobserve_selected_page(session)
    if session.get("attached") and session.get("shared_edge"):
        state = session.get("edge_connection_state")
        if state is not None:
            state.session_ids.discard(session_id)
        _SESSIONS.pop(session_id, None)
        _remember_released(session_id, mode)
        return
    try:
        # This is a Playwright protocol client close. It does not close the
        # borrowed Edge process or its browser context.
        await session["browser"].close()
    except Exception:
        pass
    finally:
        try:
            await session["driver"].stop()
        except Exception:
            pass
        _SESSIONS.pop(session_id, None)
        _remember_released(session_id, mode)


async def _stop_session_measurements(
    session_id: str,
    *,
    tab_handle: str | None = None,
    discard: bool = False,
) -> None:
    records = [
        record for record in list(_MEASUREMENTS.values())
        if record["session_id"] == session_id and (tab_handle is None or record["tab_handle"] == tab_handle)
    ]
    for record in records:
        record["stop_event"].set()
    if records:
        await asyncio.gather(*(record["task"] for record in records), return_exceptions=True)
    if discard:
        for record in records:
            _MEASUREMENTS.pop(record["measurement_id"], None)


async def browser_start(headless: bool = True, browser_type: str = "chromium", channel: str | None = None,
                        executable_path: str | None = None, width: int = 1280, height: int = 800,
                        timeout_sec: float = 30) -> dict[str, Any]:
    """Launch an isolated Playwright browser/context/page. headless=False opens a visible QA window. channel='msedge' can use installed Edge."""
    from playwright.async_api import async_playwright
    if browser_type not in ("chromium", "firefox", "webkit"):
        raise ValueError("browser_type must be chromium, firefox or webkit")
    driver = await async_playwright().start()
    browser = None
    try:
        browser = await getattr(driver, browser_type).launch(headless=headless, channel=channel,
                        executable_path=executable_path, timeout=timeout_sec * 1000)
        context = await browser.new_context(viewport={"width": width, "height": height})
        context.set_default_timeout(timeout_sec * 1000)
        page = await context.new_page()
        session_id = str(uuid.uuid4())
        console, errors = deque(maxlen=1000), deque(maxlen=1000)
        def observe(p):
            p.on("console", lambda message: console.append({"type": message.type, "text": message.text,
                                                            "location": message.location}))
            p.on("pageerror", lambda error: errors.append(str(error)))
        observe(page)
        context.on("page", observe)
        _SESSIONS[session_id] = {"driver": driver, "browser": browser, "context": context, "page": page,
                                 "attached": False, "console": console, "errors": errors,
                                 "lock": asyncio.Lock()}
        return {"session_id": session_id, "headless": headless, "browser_type": browser_type,
                "version": browser.version, "url": page.url}
    except Exception as exc:
        if browser:
            await browser.close()
        await driver.stop()
        raise RuntimeError(f"Browser launch failed: {exc}. Install browsers with this Python: -m playwright install chromium, or use channel='msedge'.") from exc


async def browser_navigate(session_id: str, url: str, wait_until: str = "domcontentloaded", timeout_sec: float = 30) -> dict[str, Any]:
    """Navigate the session page to any URL. wait_until: commit, domcontentloaded, load or networkidle."""
    async with _session(session_id) as s:
        try:
            response, download_started = await navigate_page(
                s["page"], url, wait_until=wait_until, timeout_ms=int(timeout_sec * 1_000)
            )
        except Exception as exc:
            detail = safe_exception_summary(exc)
            raise RuntimeError(
                f"browser_navigation_failed: {detail['type']}: {detail['message']}"
            ) from None
        current_url = str(getattr(s["page"], "url", "") or "")
        safe_url = _safe_browser_url(current_url) if s.get("attached") else current_url
        if download_started:
            return {
                "code": "browser_navigation_download",
                "download_started": True,
                "url": safe_url,
                "status": response.status if response else None,
            }
        result = {"url": safe_url, "status": response.status if response else None}
        if not s.get("attached"):
            try:
                result["title"] = await s["page"].title()
            except Exception:
                result["title"] = ""
        return result


async def browser_page_text(session_id: str, selector: str = "body", max_chars: int = 20000) -> dict[str, Any]:
    """Read rendered text from a Playwright selector (CSS/text/role syntax). max_chars=0 returns all text."""
    if max_chars < 0:
        raise ValueError("max_chars must be nonnegative")
    async with _session(session_id) as s:
        text = await s["page"].locator(selector).inner_text()
        url = _safe_browser_url(s["page"].url) if s.get("attached") else s["page"].url
        return {"url": url, "text": text[:max_chars] if max_chars else text, "truncated": bool(max_chars and len(text) > max_chars)}


async def browser_find(
    session_id: str,
    selector: str,
    max_results: int = 50,
    guard_url: str = "",
    guard_url_prefix: str = "",
) -> dict[str, Any]:
    """Find DOM elements using a Playwright selector. Return count and bounded tag/text/attributes/visibility/bounds summaries."""
    if type(max_results) is not int or not 1 <= max_results <= 50:
        raise ValueError("browser_find_limit_invalid")
    async with _session(session_id) as s:
        page = _guard_attached_element_page(
            s, guard_url, guard_url_prefix, "browser_find_"
        )
        locator = page.locator(selector)
        count = await locator.count()
        elements = await locator.evaluate_all("""(nodes, options) => nodes.slice(0, options.limit).map((e, index) => {
          const r = e.getBoundingClientRect(); const style = getComputedStyle(e);
          const formControl = ['INPUT','TEXTAREA','SELECT'].includes(e.tagName);
          return {index, tag: e.tagName,
            text: options.redactSecrets && formControl ? '' : (e.innerText || e.textContent || '').slice(0, 2000),
            attributes: Object.fromEntries(Array.from(e.attributes)
              .filter(a => !options.redactSecrets || a.name.toLowerCase() !== 'value')
              .map(a => [a.name, a.value])),
            visible: !!(r.width && r.height && style.visibility !== 'hidden' && style.display !== 'none'),
            bounds: {x:r.x, y:r.y, width:r.width, height:r.height}};
        })""", {"limit": max_results, "redactSecrets": bool(s.get("attached"))})
        if s.get("attached"):
            for element in elements:
                if not isinstance(element, dict):
                    continue
                if str(element.get("tag", "")).upper() in {"INPUT", "TEXTAREA", "SELECT"}:
                    element["text"] = ""
                attributes = element.get("attributes")
                if not isinstance(attributes, dict):
                    element["attributes"] = {}
                    continue
                safe_attributes = {}
                for name, value in attributes.items():
                    key = str(name).lower()
                    if key not in _BROWSER_FIND_SAFE_ATTRIBUTES:
                        continue
                    if key in _BROWSER_FIND_URL_ATTRIBUTES:
                        safe_attributes[name] = _safe_browser_url(str(value))
                    else:
                        safe_attributes[name] = value
                element["attributes"] = safe_attributes
        return {"count": count, "elements": elements, "truncated": count > max_results}


def _verify_budget(seconds):
    import math
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 30:
        raise ValueError("verify_timeout_sec must be in (0, 30]")


def _validate_element_filters(within: str | None, test_id: str | None, nth: int | None) -> None:
    if within is not None and (
        not isinstance(within, str) or not within.strip() or len(within) > 512
    ):
        raise ValueError("browser_element_filter_invalid")
    if test_id is not None and (
        not isinstance(test_id, str) or not test_id.strip() or len(test_id) > 256
    ):
        raise ValueError("browser_element_filter_invalid")
    if nth is not None and (type(nth) is not int or not 0 <= nth < 100):
        raise ValueError("browser_element_filter_invalid")


def _filtered_element_locator(
    page: Any,
    selector: str,
    *,
    within: str | None,
    test_id: str | None,
    nth: int | None,
) -> Any:
    _validate_element_filters(within, test_id, nth)
    if not within:
        locator = page.locator(selector)
    else:
        scope = within
        if scope.startswith("css:"):
            scope = scope[4:]
            if not scope:
                raise ValueError("browser_element_filter_invalid")
            scope_locator = page.locator(scope)
        elif scope.startswith(("#", ".", "[", "*", ":", ">")):
            scope_locator = page.locator(scope)
        else:
            if scope.startswith("region:"):
                scope = scope[len("region:"):].strip()
                if not scope:
                    raise ValueError("browser_element_filter_invalid")
            scope_locator = page.get_by_role("region", name=scope, exact=True)
        locator = scope_locator.locator(selector)
    if test_id is not None:
        locator = locator.and_(page.get_by_test_id(test_id))
    if nth is not None:
        locator = locator.nth(nth)
    return locator


async def _verify_effect(page, *, locator=None, value=None, text=None, url=None,
                         checked=None, timeout=5):
    from playwright.async_api import expect
    import time
    deadline = time.monotonic() + timeout
    def remaining():
        return max(1, (deadline - time.monotonic()) * 1000)
    async def verify():
        if value is not None:
            # Compare locally; neither expected nor actual values enter errors.
            await expect(locator).to_have_js_property("isConnected", True, timeout=remaining())
            is_editable = await locator.evaluate("e => e.isContentEditable")
            if is_editable:
                await expect(locator).to_have_js_property("innerText", value, timeout=remaining())
            else:
                await expect(locator).to_have_value(value, timeout=remaining())
        if text is not None:
            await expect(locator or page.locator('body')).to_contain_text(text, timeout=remaining())
        if url is not None:
            # String URL patterns normally accept globs; exact predicate keeps
            # the caller's literal URL an actual postcondition.
            await page.wait_for_url(lambda current: current == url, timeout=remaining())
        if checked is not None:
            await expect(locator).to_be_checked(checked=checked, timeout=remaining())
        return {"status": "verified", "kind": "explicit_postcondition"}
    try:
        return await asyncio.wait_for(verify(), timeout=timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        return {"status": "failed", "kind": "explicit_postcondition", "reason": "browser_expectation_failed"}


async def browser_click(session_id: str, selector: str, button: str = "left", timeout_sec: float = 30,
                        expected_text: str | None = None, expected_url: str | None = None,
                        expected_checked: bool | None = None, result_selector: str = "body",
                        verify_timeout_sec: float = 5, within: str | None = None,
                        test_id: str | None = None, nth: int | None = None,
                        guard_url: str = "", guard_url_prefix: str = "") -> dict[str, Any]:
    """Click after normal Playwright actionability. Optional explicit postconditions
    are checked without another click. A failed verification means input already
    ran; observe before retrying. No goal inference or browser/session switching.
    """
    _verify_budget(verify_timeout_sec)
    async with _session(session_id) as s:
        page = _guard_attached_element_page(
            s, guard_url, guard_url_prefix, "browser_click_"
        )
        locator = _filtered_element_locator(
            page, selector, within=within, test_id=test_id, nth=nth
        )
        deadline = time.monotonic() + float(timeout_sec)
        visibility = await ensure_attached_page_visible(
            s, page, timeout_seconds=min(1.0, max(0.0, deadline - time.monotonic()))
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if s.get("attached") and await read_page_visibility_state(page) == "hidden":
                    raise ValueError("browser_click_tab_hidden")
                raise TimeoutError("browser_click_timeout")
            try:
                click_timeout_ms = (
                    min(remaining, 3.0) * 1000 if visibility is not None else remaining * 1000
                )
                await locator.click(button=button, timeout=click_timeout_ms)
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                timeout_error = (
                    type(exc).__name__ == "TimeoutError"
                    or "timeout" in str(exc).casefold()
                    or "timed out" in str(exc).casefold()
                )
                call_log = str(exc).casefold().split("call log:", 1)
                click_started = "performing click action" in call_log[-1]
                hidden = (
                    bool(s.get("attached"))
                    and await read_page_visibility_state(page) == "hidden"
                )
                if timeout_error and not click_started and not hidden and time.monotonic() < deadline:
                    # The 3 s slice ended before input was dispatched; keep waiting
                    # for actionability so the caller's timeout_sec is honoured.
                    continue
                if timeout_error and not click_started and hidden:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("browser_click_tab_hidden") from None
                    visibility = await ensure_attached_page_visible(
                        s, page, timeout_seconds=min(1.0, remaining)
                    ) or visibility
                    if time.monotonic() < deadline:
                        continue
                    if await read_page_visibility_state(page) == "hidden":
                        raise ValueError("browser_click_tab_hidden") from None
                if hidden:
                    raise ValueError("browser_click_tab_hidden") from None
                raise
        verification = {"status": "unknown", "kind": "application_effect"}
        if expected_text is not None or expected_url is not None or expected_checked is not None:
            verify_locator = locator if expected_checked is not None else page.locator(result_selector)
            verification = await _verify_effect(page, locator=verify_locator, text=expected_text, url=expected_url,
                                                 checked=expected_checked, timeout=verify_timeout_sec)
        url = _safe_browser_url(page.url) if s.get("attached") else page.url
        result = {"clicked": selector, "url": url, "executed": True,
                "execution_status": "executed", "verification": verification,
                "retry_policy": "observe_before_retry"}
        if visibility is not None:
            result["tab_visibility"] = visibility
        return result


async def browser_fill(session_id: str, selector: str, value: str, timeout_sec: float = 30,
                       verify_value: bool = True, verify_timeout_sec: float = 5,
                       within: str | None = None, test_id: str | None = None,
                       nth: int | None = None, guard_url: str = "",
                       guard_url_prefix: str = "") -> dict[str, Any]:
    """Fill input/textarea/contenteditable and read back within the workspace browser session without submitting.
    No entered value is returned or included in verification diagnostics.
    """
    _verify_budget(verify_timeout_sec)
    async with _session(session_id) as s:
        page = _guard_attached_element_page(
            s, guard_url, guard_url_prefix, "browser_fill_"
        )
        visibility = await ensure_attached_page_visible(s, page)
        locator = _filtered_element_locator(
            page, selector, within=within, test_id=test_id, nth=nth
        )
        try:
            await locator.fill(value, timeout=timeout_sec * 1000)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Playwright call logs can echo the filled value. Do not expose it
            # through the MCP error wrapper; a dispatch outcome can be unknown.
            raise RuntimeError("browser_fill_outcome_unknown; observe before retrying") from None
        verification = await _verify_effect(page, locator=locator, value=value, timeout=verify_timeout_sec) if verify_value else {
            "status": "unknown", "kind": "value"}
        result = {"filled": selector, "executed": True, "execution_status": "executed",
                "verification": verification, "retry_policy": "observe_before_retry"}
        if visibility is not None:
            result["tab_visibility"] = visibility
        return result


async def browser_keyboard(
    session_id: str,
    key: str = "",
    text: str | None = None,
    guard_url: str = "",
    guard_url_prefix: str = "",
) -> dict[str, Any]:
    """Press a key/chord such as Enter or Control+A, or insert literal text at the focused element."""
    async with _session(session_id) as s:
        page = _guard_attached_element_page(
            s, guard_url, guard_url_prefix, "browser_keyboard_"
        )
        visibility = await ensure_attached_page_visible(s, page)
        if text is not None:
            await page.keyboard.insert_text(text)
        elif key:
            await page.keyboard.press(key)
        else:
            raise ValueError("browser_keyboard_invalid")
        result = {"sent": True}
        if visibility is not None:
            result["tab_visibility"] = visibility
        return result


async def browser_run_plan(
    steps: list[dict[str, Any]],
    session_id: str = "",
    goal: str = "",
    start_url: str = "",
    completion_text: str = "",
    completion_url: str = "",
    headless: bool = True,
    browser_type: str = "chromium",
    channel: str | None = None,
    executable_path: str | None = None,
    width: int = 1280,
    height: int = 800,
    close_on_finish: bool = False,
    max_steps: int = 24,
    timeout_sec: float = 180,
    action_timeout_sec: float = 30,
    jev_model: str = JEV_DEFAULT_MODEL,
    jev_confidence: float = 0.50,
    jev_timeout_sec: float = 5,
) -> dict[str, Any]:
    """Run one complete browser strategy in the workspace browser session. Supply ordered semantic steps; Jev selects each current DOM target, while Gateway code executes and verifies the actions. Omit session_id to create a browser automatically."""
    if type(timeout_sec) not in {int, float} or not 1 <= timeout_sec <= 900:
        raise ValueError("timeout_sec must be between 1 and 900")
    created_session = False
    if not session_id:
        started = await browser_start(
            headless=headless,
            browser_type=browser_type,
            channel=channel,
            executable_path=executable_path,
            width=width,
            height=height,
            timeout_sec=min(30, timeout_sec),
        )
        session_id = started["session_id"]
        created_session = True
    attached_session = bool(_SESSIONS.get(session_id, {}).get("attached"))

    try:
        async def run() -> dict[str, Any]:
            async with _session(session_id) as session:
                return await execute_browser_plan(
                    session,
                    steps,
                    goal=goal,
                    start_url=start_url,
                    completion_text=completion_text,
                    completion_url=completion_url,
                    max_steps=max_steps,
                    jev_model=jev_model,
                    jev_confidence=jev_confidence,
                    jev_timeout_seconds=jev_timeout_sec,
                    action_timeout_seconds=action_timeout_sec,
                )

        try:
            result = await asyncio.wait_for(run(), timeout=float(timeout_sec))
        except asyncio.TimeoutError:
            result = {
                "status": "failed",
                "reason": "browser_timeout",
                "success": False,
                "completion_verified": False,
                "trace": [{"event": "run_stopped", "reason": "browser_timeout"}],
                "observation": {},
            }
        result.update(
            {
                "session_id": session_id,
                "created_session": created_session,
                "mode": "attached" if attached_session else "isolated",
            }
        )
        return result
    finally:
        if close_on_finish and session_id in _SESSIONS:
            await browser_close(session_id)


async def browser_screenshot(session_id: str, full_page: bool = True, selector: str | None = None,
                             timeout_sec: float = 30) -> CallToolResult:
    """Return the page or a selected element as native MCP PNG ImageContent and dimensions."""
    async with _session(session_id) as s:
        target = s["page"].locator(selector) if selector else s["page"]
        data = await target.screenshot(type="png", timeout=timeout_sec * 1000, **({} if selector else {"full_page": full_page}))
        url = _safe_browser_url(s["page"].url) if s.get("attached") else s["page"].url
        return png_result(data, session_id=session_id, url=url, selector=selector)


async def browser_errors(session_id: str, clear: bool = False) -> dict[str, Any]:
    """Read console messages (including errors) and uncaught page errors. Each buffer retains the latest 1000 entries; clear optionally drains them."""
    async with _session(session_id) as s:
        result = {"console": list(s["console"]), "page_errors": list(s["errors"])}
        if clear:
            s["console"].clear()
            s["errors"].clear()
        return result


async def browser_close(session_id: str) -> dict[str, Any]:
    """Close an owned browser, or disconnect the client from a borrowed Edge session."""
    session = _SESSIONS.get(session_id)
    if session is None:
        if session_id in _RELEASED_SESSIONS:
            return {"session_id": session_id, "closed": True, "already_closed": True}
        raise ValueError("Browser session not found")
    async with _session(session_id, require_page=False) as locked:
        await _release_session(session_id, locked)
        attached = bool(locked.get("attached"))
    result = {"session_id": session_id, "closed": True}
    if attached:
        result["external_browser_closed"] = False
        result["shared_connection_retained"] = True
    return result


async def _shutdown_shared_edge_client(state: _SharedEdgeConnection) -> None:
    """Disconnect this Gateway process from the broker; never stops the broker."""
    async with state.lock:
        task = state.connect_task
        state.connect_task = None
        browser, driver = state.browser, state.driver
        state.browser = None
        state.driver = None
        state.context = None
        state.browser_version = ""
        state.failure_code = None
        state.session_ids.clear()
    if task is not None and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    if browser is not None:
        try:
            # Browser.close for a connected Playwright client disconnects it
            # from the broker. The broker alone owns the Edge CDP connection.
            await browser.close()
        except Exception:
            pass
    if driver is not None:
        try:
            await driver.stop()
        except Exception:
            pass


async def close_all() -> None:
    for session_id in list(_SESSIONS):
        try:
            await browser_close(session_id)
        except Exception:
            pass
    loop = asyncio.get_running_loop()
    with _EDGE_CONNECTIONS_LOCK:
        state = _EDGE_CONNECTIONS.get(loop)
    if state is not None:
        await _shutdown_shared_edge_client(state)
        with _EDGE_CONNECTIONS_LOCK:
            _EDGE_CONNECTIONS.pop(loop, None)


TOOLS = [browser_start, browser_attach_edge, browser_open_tab, browser_list_tabs, browser_select_tab,
         browser_close_tab, browser_drag, browser_set_viewport, browser_reset_viewport,
         browser_capture_frames_start, browser_capture_frames_stop,
         browser_track_boxes_start, browser_track_boxes_stop, browser_detach,
         browser_run_plan, browser_navigate, browser_page_text, browser_find, browser_click,
         browser_fill, browser_keyboard, browser_screenshot, browser_errors, browser_close]
