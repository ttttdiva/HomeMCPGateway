"""Gateway-owned Playwright sessions, isolated contexts and async MCP operations."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from typing import Any
import uuid

from mcp.types import CallToolResult

from .browser_agent import execute_browser_plan
from .jev import JEV_DEFAULT_MODEL
from .qa_common import png_result

_SESSIONS: dict[str, dict] = {}


@asynccontextmanager
async def _session(session_id: str):
    session = _SESSIONS.get(session_id)
    if session is None:
        raise ValueError("Browser session not found; start a new session after Gateway restart")
    async with session["lock"]:
        if session_id not in _SESSIONS:
            raise ValueError("Browser session has been closed")
        yield session


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
                                 "console": console, "errors": errors, "lock": asyncio.Lock()}
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
        response = await s["page"].goto(url, wait_until=wait_until, timeout=timeout_sec * 1000)
        return {"url": s["page"].url, "title": await s["page"].title(), "status": response.status if response else None}


async def browser_page_text(session_id: str, selector: str = "body", max_chars: int = 20000) -> dict[str, Any]:
    """Read rendered text from a Playwright selector (CSS/text/role syntax). max_chars=0 returns all text."""
    if max_chars < 0:
        raise ValueError("max_chars must be nonnegative")
    async with _session(session_id) as s:
        text = await s["page"].locator(selector).inner_text()
        return {"url": s["page"].url, "text": text[:max_chars] if max_chars else text, "truncated": bool(max_chars and len(text) > max_chars)}


async def browser_find(session_id: str, selector: str, max_results: int = 50) -> dict[str, Any]:
    """Find DOM elements using a Playwright selector. Return count and bounded tag/text/attributes/visibility/bounds summaries."""
    if max_results <= 0:
        raise ValueError("max_results must be positive")
    async with _session(session_id) as s:
        locator = s["page"].locator(selector)
        count = await locator.count()
        elements = await locator.evaluate_all("""(nodes, limit) => nodes.slice(0, limit).map((e, index) => {
          const r = e.getBoundingClientRect(); const style = getComputedStyle(e);
          return {index, tag: e.tagName, text: (e.innerText || e.textContent || '').slice(0, 2000),
            attributes: Object.fromEntries(Array.from(e.attributes, a => [a.name, a.value])),
            visible: !!(r.width && r.height && style.visibility !== 'hidden' && style.display !== 'none'),
            bounds: {x:r.x, y:r.y, width:r.width, height:r.height}};
        })""", max_results)
        return {"count": count, "elements": elements, "truncated": count > max_results}


def _verify_budget(seconds):
    import math
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or not 0 < seconds <= 30:
        raise ValueError("verify_timeout_sec must be in (0, 30]")


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
                        verify_timeout_sec: float = 5) -> dict[str, Any]:
    """Click after normal Playwright actionability. Optional explicit postconditions
    are checked without another click. A failed verification means input already
    ran; observe before retrying. No goal inference or browser/session switching.
    """
    _verify_budget(verify_timeout_sec)
    async with _session(session_id) as s:
        page = s["page"]
        await page.locator(selector).click(button=button, timeout=timeout_sec * 1000)
        verification = {"status": "unknown", "kind": "application_effect"}
        if expected_text is not None or expected_url is not None or expected_checked is not None:
            locator = page.locator(selector if expected_checked is not None else result_selector)
            verification = await _verify_effect(page, locator=locator, text=expected_text, url=expected_url,
                                                 checked=expected_checked, timeout=verify_timeout_sec)
        return {"clicked": selector, "url": page.url, "executed": True,
                "execution_status": "executed", "verification": verification,
                "retry_policy": "observe_before_retry"}


async def browser_fill(session_id: str, selector: str, value: str, timeout_sec: float = 30,
                       verify_value: bool = True, verify_timeout_sec: float = 5) -> dict[str, Any]:
    """Fill input/textarea/contenteditable and read back within the workspace browser session without submitting.
    No entered value is returned or included in verification diagnostics.
    """
    _verify_budget(verify_timeout_sec)
    async with _session(session_id) as s:
        locator = s["page"].locator(selector)
        try:
            await locator.fill(value, timeout=timeout_sec * 1000)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Playwright call logs can echo the filled value. Do not expose it
            # through the MCP error wrapper; a dispatch outcome can be unknown.
            raise RuntimeError("browser_fill_outcome_unknown; observe before retrying") from None
        verification = await _verify_effect(s['page'], locator=locator, value=value, timeout=verify_timeout_sec) if verify_value else {
            "status": "unknown", "kind": "value"}
        return {"filled": selector, "executed": True, "execution_status": "executed",
                "verification": verification, "retry_policy": "observe_before_retry"}


async def browser_keyboard(session_id: str, key: str = "", text: str | None = None) -> dict[str, Any]:
    """Press a key/chord such as Enter or Control+A, or insert literal text at the focused element."""
    async with _session(session_id) as s:
        if text is not None:
            await s["page"].keyboard.insert_text(text)
        elif key:
            await s["page"].keyboard.press(key)
        else:
            raise ValueError("Provide key or text")
        return {"sent": True}


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
        return png_result(data, session_id=session_id, url=s["page"].url, selector=selector)


async def browser_errors(session_id: str, clear: bool = False) -> dict[str, Any]:
    """Read console messages (including errors) and uncaught page errors. Each buffer retains the latest 1000 entries; clear optionally drains them."""
    async with _session(session_id) as s:
        result = {"console": list(s["console"]), "page_errors": list(s["errors"])}
        if clear:
            s["console"].clear()
            s["errors"].clear()
        return result


async def browser_close(session_id: str) -> dict[str, Any]:
    """Close a browser session and release its context and Playwright driver."""
    async with _session(session_id) as s:
        try:
            await s["browser"].close()
        finally:
            try:
                await s["driver"].stop()
            finally:
                _SESSIONS.pop(session_id, None)
    return {"session_id": session_id, "closed": True}


async def close_all() -> None:
    for session_id in list(_SESSIONS):
        try:
            await browser_close(session_id)
        except Exception:
            pass


TOOLS = [browser_start, browser_run_plan, browser_navigate, browser_page_text, browser_find, browser_click,
         browser_fill, browser_keyboard, browser_screenshot, browser_errors, browser_close]
