"""High-level browser plan executor with Jev-selected DOM targets.

The MCP caller owns strategy and literal input values. Jev sees only a bounded,
value-free summary of currently actionable DOM candidates and returns one ID.
Code owns execution, retries, verification, budgets, and the stop condition.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
import re
import time
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from .decision_receipt import (
    DecisionReceipt,
    create_decision_receipt,
    finalize_decision_receipt,
    redact_decision_metadata,
)
from .jev import (
    JEV_DEFAULT_MODEL,
    JevDecisionClient,
    JevError,
    validate_host_choice,
)

MAX_STEPS = 60
MAX_CANDIDATES = 100
MAX_OBSERVATION_TEXT = 20_000
_ELEMENT_ACTIONS = {"click", "type", "fill", "select", "check", "uncheck"}
_ALLOWED_ACTIONS = _ELEMENT_ACTIONS | {
    "navigate",
    "press",
    "scroll",
    "back",
    "wait",
    "read",
    "done",
}
_ALLOWED_STEP_FIELDS = {
    "action",
    "instruction",
    "value",
    "url",
    "expected_text",
    "expected_url",
    "within",
    "test_id",
    "nth",
}


class BrowserAgentError(RuntimeError):
    """Stable high-level execution failure code."""

    def __init__(self, code: str, *, safe_detail: dict[str, Any] | None = None) -> None:
        self.code = code
        self.safe_detail = safe_detail
        super().__init__(code)


@dataclass(frozen=True)
class BrowserStep:
    action: str
    instruction: str = ""
    value: str = ""
    url: str = ""
    expected_text: str = ""
    expected_url: str = ""
    within: str = ""
    test_id: str = ""
    nth: int | None = None


@dataclass(frozen=True)
class CandidateTarget:
    frame: Any
    local_id: str
    summary: Mapping[str, Any]


_OBSERVE_SCRIPT = r"""
({action, limit, within, test_id}) => {
  const root = globalThis;
  const targets = new Map();
  root.__homeMcpJevTargets = targets;
  const visible = e => {
    if (!e || !e.isConnected || e.getClientRects().length === 0 || e.disabled) return false;
    const style = getComputedStyle(e);
    return style.visibility !== 'hidden' && style.display !== 'none' &&
      e.getAttribute('aria-hidden') !== 'true' && e.getAttribute('aria-disabled') !== 'true';
  };
  const label = e => {
    const labelled = (e.getAttribute('aria-labelledby') || '').split(/\s+/)
      .filter(Boolean).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
    const labels = Array.from(e.labels || []).map(node => {
      const copy = node.cloneNode(true);
      copy.querySelectorAll('input,textarea,select,button').forEach(child => child.remove());
      return (copy.textContent || '').trim();
    }).filter(Boolean).join(' ');
    const type = String(e.type || '').toLowerCase();
    const buttonValue = ['submit','button','reset'].includes(type) ? String(e.value || '') : '';
    return e.getAttribute('aria-label') || labelled || labels ||
      e.getAttribute('placeholder') || e.getAttribute('title') ||
      ((!['input','textarea'].includes(e.tagName.toLowerCase()) && !e.isContentEditable) ? e.innerText : '') ||
      buttonValue || e.name || '';
  };
  const compatible = e => {
    const tag = e.tagName.toLowerCase();
    const type = String(e.type || '').toLowerCase();
    const role = String(e.getAttribute('role') || '').toLowerCase();
    if (action === 'type' || action === 'fill') {
      return ((tag === 'input' || tag === 'textarea') &&
        !['hidden','checkbox','radio','submit','button','reset','file'].includes(type)) ||
        e.isContentEditable;
    }
    if (action === 'select') return tag === 'select';
    if (action === 'check') return ['checkbox','radio'].includes(type);
    if (action === 'uncheck') return type === 'checkbox';
    return true;
  };
  const elementsIn = node => {
    const selector = 'a[href],button,input,textarea,select,summary,' +
      '[role="button"],[role="link"],[role="checkbox"],[role="radio"],' +
      '[role="combobox"],[role="textbox"],[contenteditable="true"]';
    const items = Array.from(node.querySelectorAll(selector));
    for (const child of node.querySelectorAll('*')) {
      if (child.shadowRoot) items.push(...elementsIn(child.shadowRoot));
    }
    return items;
  };
  const looksLikeCss = value => ['css:', '#', '.', '[', '*', ':', '>']
    .some(prefix => value.startsWith(prefix));
  const regionFilter = within && within.startsWith('region:') ? within.slice(7).trim() : within;
  const scopeSelector = within && !within.startsWith('region:') && looksLikeCss(within)
    ? (within.startsWith('css:') ? within.slice(4) : within) : '';
  if (scopeSelector) {
    try { document.createElement('div').matches(scopeSelector); }
    catch (_) { return {filter_error: 'within_invalid', candidates: {}}; }
  }
  const normalized = value => String(value || '').replace(/\s+/g, ' ').trim();
  const labelledName = element => {
    const root = element.getRootNode();
    const labelled = (element.getAttribute('aria-labelledby') || '').split(/\s+/)
      .filter(Boolean).map(id => root.getElementById?.(id)?.textContent || '').join(' ');
    return normalized(labelled || element.getAttribute('aria-label') || element.getAttribute('title'));
  };
  const parentOf = element => element.parentElement || element.getRootNode()?.host || null;
  const inScope = element => {
    if (!within) return true;
    for (let ancestor = parentOf(element); ancestor; ancestor = parentOf(ancestor)) {
      if (scopeSelector) {
        if (ancestor.matches(scopeSelector)) return true;
        continue;
      }
      const region = ancestor.getAttribute('role') === 'region' ||
        (ancestor.tagName === 'SECTION' && labelledName(ancestor));
      if (region && labelledName(ancestor) === normalized(regionFilter)) return true;
    }
    return false;
  };
  const nonce = Math.random().toString(36).slice(2, 12);
  const candidates = {};
  for (const e of elementsIn(document)) {
    if (targets.size >= limit) break;
    if (!visible(e) || !compatible(e) || !inScope(e) ||
        (test_id && e.getAttribute('data-testid') !== test_id)) continue;
    const id = `e_${nonce}_${targets.size}`;
    targets.set(id, e);
    const tag = e.tagName.toLowerCase();
    const type = String(e.type || '').toLowerCase();
    const item = {
      tag,
      type,
      role: String(e.getAttribute('role') || ''),
      label: String(label(e) || '').replace(/\s+/g, ' ').trim().slice(0, 220),
      text: String((['input','textarea'].includes(tag) || e.isContentEditable) ? '' : (e.innerText || ''))
        .replace(/\s+/g, ' ').trim().slice(0, 220),
      name: String(e.getAttribute('name') || '').slice(0, 120),
      placeholder: String(e.getAttribute('placeholder') || '').slice(0, 180),
      title: String(e.getAttribute('title') || '').slice(0, 180),
    };
    if (tag === 'a') item.href = String(e.href || '').slice(0, 1000);
    if (tag === 'select') {
      item.options = Array.from(e.options).slice(0, 60).map(option =>
        String(option.label || option.textContent || '').slice(0, 180));
    }
    candidates[id] = item;
  }
  return {candidates};
}
"""

_GET_TARGET_SCRIPT = "id => globalThis.__homeMcpJevTargets?.get(id) || null"


def validate_steps(raw_steps: Any, *, max_steps: int = 24) -> list[BrowserStep]:
    if (
        type(max_steps) is not int
        or not 1 <= max_steps <= MAX_STEPS
        or not isinstance(raw_steps, list)
        or not 1 <= len(raw_steps) <= max_steps
    ):
        raise BrowserAgentError("browser_plan_invalid")
    steps: list[BrowserStep] = []
    for raw in raw_steps:
        if not isinstance(raw, dict) or set(raw) - _ALLOWED_STEP_FIELDS:
            raise BrowserAgentError("browser_plan_invalid")
        action = raw.get("action")
        if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
            raise BrowserAgentError("browser_plan_invalid")
        values: dict[str, str] = {}
        for field in _ALLOWED_STEP_FIELDS - {"action", "nth"}:
            value = raw.get(field, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise BrowserAgentError("browser_plan_invalid")
            limit = 16_000 if field == "value" else 8_192 if field in {"url", "expected_url"} else 512 if field == "within" else 256 if field == "test_id" else 1_200
            if len(value) > limit:
                raise BrowserAgentError("browser_plan_invalid")
            if field in {"within", "test_id"} and field in raw and not value.strip():
                raise BrowserAgentError("browser_plan_invalid")
            values[field] = value
        nth = raw.get("nth")
        if nth is not None and (type(nth) is not int or not 0 <= nth < MAX_CANDIDATES):
            raise BrowserAgentError("browser_plan_invalid")
        if action not in _ELEMENT_ACTIONS and any(field in raw for field in {"within", "test_id", "nth"}):
            raise BrowserAgentError("browser_plan_invalid")
        if action in _ELEMENT_ACTIONS and not values["instruction"].strip():
            raise BrowserAgentError("browser_plan_invalid")
        if action in {"type", "fill"} and "value" not in raw:
            raise BrowserAgentError("browser_plan_invalid")
        if action == "select" and not values["value"]:
            raise BrowserAgentError("browser_plan_invalid")
        if action == "navigate" and not values["url"]:
            raise BrowserAgentError("browser_plan_invalid")
        if action == "press" and not values["value"]:
            raise BrowserAgentError("browser_plan_invalid")
        if action == "scroll" and values["value"] not in {"", "up", "down"}:
            raise BrowserAgentError("browser_plan_invalid")
        if action == "wait":
            try:
                seconds = float(values["value"] or "0.5")
            except ValueError:
                raise BrowserAgentError("browser_plan_invalid") from None
            if not math.isfinite(seconds) or not 0 <= seconds <= 30:
                raise BrowserAgentError("browser_plan_invalid")
        steps.append(BrowserStep(action=action, nth=nth, **values))
    return steps


async def _page_snapshot(
    page: Any,
    *,
    max_chars: int = MAX_OBSERVATION_TEXT,
    redact_url: bool = False,
    include_content: bool = True,
) -> dict[str, Any]:
    title = ""
    text = ""
    if include_content:
        try:
            title = await page.title()
        except Exception:
            title = ""
        try:
            text = await page.locator("body").inner_text(timeout=3_000)
        except Exception:
            text = ""
    page_url = str(getattr(page, "url", "") or "")
    return {
        "url": _jev_safe_url(page_url) if redact_url else page_url,
        "title": str(title or "")[:1_000],
        "text": str(text or "")[:max_chars],
        "truncated": bool(include_content and len(str(text or "")) > max_chars),
    }


def _jev_safe_url(value: str) -> str:
    """Drop URL userinfo, query, and fragment before exposing a browser URL."""
    try:
        parsed = urlsplit(str(value or ""))
        scheme = parsed.scheme[:24].lower()
        if scheme in {"data", "javascript", "blob"}:
            return f"{scheme}:"
        if scheme == "file":
            return "file:"
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        netloc = hostname + (f":{port}" if port is not None else "")
        path = parsed.path if scheme in {"http", "https", "ws", "wss", "about"} else ""
        return urlunsplit((scheme, netloc, path, "", ""))[:1_000]
    except (TypeError, ValueError):
        return ""


def safe_exception_summary(exc: Exception) -> dict[str, str]:
    """Return only fixed diagnostics derived from Playwright's action reason lines."""
    raw = str(exc or "")
    lines = raw.splitlines()
    call_log_at = next((i for i, line in enumerate(lines) if line.strip().casefold() == "call log:"), None)
    call_log = lines[call_log_at + 1:] if call_log_at is not None else lines
    judgments = [
        line.strip().lstrip("- ").casefold()
        for line in call_log
        if line.strip() and "locator resolved to" not in line.casefold()
    ]
    folded = "\n".join(judgments)
    is_timeout = "timeout" in raw.casefold() or "timed out" in raw.casefold()
    if is_timeout:
        waiting = [line for line in judgments if "waiting for" in line]
        last_wait = waiting[-1] if waiting else ""
        if "visible, enabled and stable" in last_wait:
            message = "Timed out waiting for the element to be visible, enabled and stable."
        elif "stable" in last_wait:
            message = "Timed out waiting for the element to be stable."
        elif "visible" in last_wait:
            message = "Timed out waiting for the element to be visible."
        elif "locator" in last_wait or "target" in last_wait:
            message = "Timed out waiting for the target element."
        else:
            message = "Timed out waiting for the browser operation."
    elif "intercepts pointer events" in folded or "intercepts the pointer events" in folded:
        message = "Element action was obstructed by another element."
    elif "element is not visible" in folded or "outside of the viewport" in folded:
        message = "Element was not visible at action time."
    elif "element is not enabled" in folded or "element is not actionable" in folded:
        message = "Element was not actionable at action time."
    elif "element is not stable" in folded:
        message = "Element was not stable at action time."
    elif "strict mode violation" in raw.casefold():
        message = "Target resolution was ambiguous."
    elif "not attached to the dom" in raw.casefold() or "detached from the dom" in raw.casefold():
        message = "Element became detached during the action."
    elif "download is starting" in raw.casefold() or "download started" in raw.casefold():
        message = "Navigation started a download."
    elif re.search(r"\bnet::(ERR_[A-Z0-9_]+)", raw):
        error_code = re.search(r"\bnet::(ERR_[A-Z0-9_]+)", raw).group(1)
        message = f"Navigation failed with net::{error_code}."
    else:
        message = "Browser operation failed; raw details omitted."
    return {"type": type(exc).__name__[:80], "message": message[:240]}


def _call_log_judgments(exc: Exception) -> list[str]:
    """Keep only Playwright actionability lines, excluding resolved locator markup."""
    lines = str(exc or "").splitlines()
    call_log_at = next((i for i, line in enumerate(lines) if line.strip().casefold() == "call log:"), None)
    source = lines[call_log_at + 1:] if call_log_at is not None else lines
    return [
        line.strip().lstrip("- ").casefold()
        for line in source
        if line.strip() and "locator resolved to" not in line.casefold()
    ]


def _click_failure_code(exc: Exception) -> str:
    folded = "\n".join(_call_log_judgments(exc))
    if "intercepts pointer events" in folded or "intercepts the pointer events" in folded:
        return "browser_element_obscured"
    if "element is not visible" in folded or "outside of the viewport" in folded:
        return "browser_element_not_visible"
    if "element is not enabled" in folded or "element is not actionable" in folded:
        return "browser_element_not_actionable"
    if "element is not stable" in folded or "stable" in folded and "waiting for" in folded:
        return "browser_element_unstable"
    raw = str(exc or "").casefold()
    if "not attached to the dom" in raw or "detached from the dom" in raw:
        return "browser_element_stale"
    if "timeout" in raw or "timed out" in raw:
        return "browser_action_timeout"
    return "browser_click_failed"


async def read_page_visibility_state(page: Any) -> str:
    """Read only the browser's built-in visibility state using fixed internal JS."""
    try:
        state = await page.evaluate("document.visibilityState")
    except asyncio.CancelledError:
        raise
    except Exception:
        return "unknown"
    return state if isinstance(state, str) and state in {"visible", "hidden", "prerender", "unloaded"} else "unknown"


async def ensure_attached_page_visible(
    session: dict[str, Any], page: Any, *, timeout_seconds: float = 1.0,
    tab_handle: str | None = None,
) -> dict[str, Any] | None:
    """Bring a selected caller-owned attached tab forward, reporting bounded diagnostics."""
    handle = tab_handle or session.get("selected_tab_handle", "")
    if (
        not session.get("attached")
        or handle not in session.get("owned_tab_handles", set())
        or session.get("owned_tab_pages", {}).get(handle) is not page
        or (tab_handle is None and handle != session.get("selected_tab_handle", ""))
    ):
        return None
    try:
        budget = min(1.0, max(0.0, float(timeout_seconds)))
    except (TypeError, ValueError, OverflowError):
        budget = 1.0
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget
    initial = await read_page_visibility_state(page)
    diagnostic: dict[str, Any] = {
        "attempted": True,
        "initial_state": initial,
        "brought_to_front": False,
        "final_state": initial,
    }
    if initial == "visible":
        return diagnostic
    try:
        await page.bring_to_front()
        diagnostic["brought_to_front"] = True
    except asyncio.CancelledError:
        raise
    except Exception:
        diagnostic["bring_to_front_failed"] = True
    while True:
        state = await read_page_visibility_state(page)
        diagnostic["final_state"] = state
        if state == "visible" or loop.time() >= deadline:
            return diagnostic
        try:
            await asyncio.sleep(min(0.05, max(0.0, deadline - loop.time())))
        except asyncio.CancelledError:
            raise
        except Exception:
            diagnostic["wait_failed"] = True
            return diagnostic


async def navigate_page(page: Any, url: str, *, wait_until: str, timeout_ms: int) -> tuple[Any, bool]:
    """Navigate while recognizing and cancelling browser downloads without saving them."""
    downloads: list[Any] = []
    listener_installed = False

    def on_download(download: Any) -> None:
        downloads.append(download)

    try:
        page.on("download", on_download)
        listener_installed = True
    except Exception:
        pass
    try:
        try:
            response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        except Exception as exc:
            if downloads or "download is starting" in str(exc).casefold():
                return None, True
            raise
        return response, bool(downloads)
    finally:
        if listener_installed:
            try:
                page.remove_listener("download", on_download)
            except Exception:
                pass
        for download in downloads:
            try:
                await download.cancel()
            except Exception:
                pass


async def _observe_candidates(
    page: Any,
    action: str,
    *,
    within: str = "",
    test_id: str = "",
    nth: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, CandidateTarget]]:
    state = {"action": action}
    summaries: dict[str, Any] = {}
    targets: dict[str, CandidateTarget] = {}
    frames = list(page.frames)
    for frame_index, frame in enumerate(frames):
        remaining = MAX_CANDIDATES - len(summaries)
        if remaining <= 0:
            break
        try:
            observed = await frame.evaluate(
                _OBSERVE_SCRIPT,
                {"action": action, "limit": remaining, "within": within, "test_id": test_id},
            )
        except Exception:
            if frame is page.main_frame:
                raise BrowserAgentError("browser_observation_failed") from None
            continue
        if isinstance(observed, dict) and observed.get("filter_error") == "within_invalid":
            raise BrowserAgentError("browser_within_invalid")
        raw = observed.get("candidates", {}) if isinstance(observed, dict) else {}
        if not isinstance(raw, dict):
            continue
        for local_id, summary in raw.items():
            if len(summaries) >= MAX_CANDIDATES:
                break
            if not isinstance(local_id, str) or not isinstance(summary, dict):
                continue
            candidate_id = f"e{len(summaries) + 1}"
            projected = dict(summary)
            if "href" in projected:
                projected["href"] = _jev_safe_url(str(projected.get("href") or ""))
            if frame is not page.main_frame:
                projected["frame_index"] = frame_index
            summaries[candidate_id] = projected
            targets[candidate_id] = CandidateTarget(frame, local_id, projected)
    if nth is not None:
        ordered = list(summaries.items())
        if nth >= len(ordered):
            raise BrowserAgentError("browser_candidate_not_found")
        selected_id, selected_summary = ordered[nth]
        selected_target = targets[selected_id]
        summaries = {"e1": selected_summary}
        targets = {"e1": selected_target}
    if not summaries:
        if within or test_id:
            raise BrowserAgentError("browser_candidate_not_found")
        raise BrowserAgentError("browser_no_candidates")
    return state, summaries, targets


async def _observe_candidates_ready(
    page: Any,
    action: str,
    *,
    timeout_seconds: float,
    within: str = "",
    test_id: str = "",
    nth: int | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, CandidateTarget]]:
    """Wait briefly for client-rendered actionable elements before asking Jev."""
    timeout = min(1.0, max(0.05, float(timeout_seconds)))
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            return await _observe_candidates(page, action, within=within, test_id=test_id, nth=nth)
        except BrowserAgentError as exc:
            if exc.code != "browser_no_candidates":
                raise
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise BrowserAgentError(
                    "browser_element_not_found",
                    safe_detail={"type": "BrowserObservation", "message": "No actionable element appeared during the bounded wait."},
                ) from None
            await asyncio.sleep(min(0.1, remaining))


def _trusted_element_task(step: BrowserStep) -> str:
    target = step.instruction.strip()
    verbs = {
        "type": "Enter text into",
        "fill": "Enter text into",
        "click": "Click",
        "select": "Choose an option in",
        "check": "Check",
        "uncheck": "Uncheck",
    }
    verb = verbs.get(step.action, "Use")
    return f"{verb} the browser element described by this trusted target: {target}"


def _is_stale_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "not attached",
            "detached",
            "stale",
            "no node found",
            "element is not",
        )
    )


async def _selected_element(target: CandidateTarget) -> Any:
    handle = await target.frame.evaluate_handle(_GET_TARGET_SCRIPT, target.local_id)
    element = handle.as_element()
    if element is None:
        await handle.dispose()
        raise BrowserAgentError("browser_stale_element")
    return element


async def _adopt_popup(session: dict[str, Any], before_pages: list[Any]) -> Any:
    if session.get("attached"):
        # A shared Edge context may receive unrelated tabs. Keep the explicit
        # selection and let the caller list/select any popup by its stable ID.
        return session["page"]
    context = session["context"]
    for _ in range(5):
        new_pages = [page for page in context.pages if page not in before_pages]
        if new_pages:
            page = new_pages[-1]
            session["page"] = page
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)
            except Exception:
                pass
            return page
        await asyncio.sleep(0.05)
    return session["page"]


async def _act_on_target(
    session: dict[str, Any],
    target: CandidateTarget,
    step: BrowserStep,
    *,
    action_timeout_seconds: float,
) -> Any:
    page = session["page"]
    before_pages = list(session["context"].pages)
    session.pop("_last_tab_visibility_diagnostic", None)
    visibility = None
    if step.action in {"click", "type", "fill"}:
        visibility = await ensure_attached_page_visible(session, page)
        if visibility is not None:
            session["_last_tab_visibility_diagnostic"] = visibility
    element = await _selected_element(target)
    timeout_ms = action_timeout_seconds * 1_000
    try:
        if step.action == "click":
            try:
                await element.scroll_into_view_if_needed(timeout=timeout_ms)
                await element.click(timeout=timeout_ms)
            except Exception as exc:
                detail = safe_exception_summary(exc)
                if visibility is not None:
                    detail["tab_visibility"] = visibility
                    session.pop("_last_tab_visibility_diagnostic", None)
                raise BrowserAgentError(
                    _click_failure_code(exc),
                    safe_detail=detail,
                ) from None
        elif step.action in {"type", "fill"}:
            await element.fill(step.value, timeout=timeout_ms)
        elif step.action == "select":
            selected = await element.evaluate(
                """(control, requested) => {
                  const option = Array.from(control.options || []).find(item =>
                    item.label === requested || item.value === requested);
                  if (!control.isConnected) return 'stale';
                  const style = getComputedStyle(control);
                  if (control.disabled || control.getAttribute('aria-disabled') === 'true' ||
                      control.getAttribute('aria-hidden') === 'true' ||
                      control.getClientRects().length === 0 || style.visibility === 'hidden' ||
                      style.display === 'none') return 'not_actionable';
                  if (!option || option.disabled || option.parentElement?.disabled) return 'missing';
                  const setter = Object.getOwnPropertyDescriptor(
                    HTMLSelectElement.prototype, 'value').set;
                  setter.call(control, option.value);
                  control.dispatchEvent(new Event('input', {bubbles: true}));
                  control.dispatchEvent(new Event('change', {bubbles: true}));
                  return 'selected';
                }""",
                step.value,
            )
            if selected == "stale":
                raise BrowserAgentError("browser_stale_element")
            if selected == "not_actionable":
                raise BrowserAgentError("browser_element_not_actionable")
            if selected != "selected":
                raise BrowserAgentError("browser_option_not_found")
        elif step.action == "check":
            await element.check(timeout=timeout_ms)
        elif step.action == "uncheck":
            await element.uncheck(timeout=timeout_ms)
        else:
            raise BrowserAgentError("browser_action_unknown")
    finally:
        try:
            await element.dispose()
        except Exception:
            pass
    if step.action == "click":
        page = await _adopt_popup(session, before_pages)
    return page


async def _verify(
    page: Any,
    *,
    text: str = "",
    url: str = "",
    timeout_seconds: float = 10.0,
    redact_url: bool = False,
    include_content: bool = True,
) -> dict[str, Any]:
    if not text and not url:
        return await _page_snapshot(
            page, redact_url=redact_url, include_content=include_content
        )
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        snapshot = await _page_snapshot(
            page, redact_url=redact_url, include_content=include_content or bool(text)
        )
        text_ok = not text or " ".join(text.split()) in " ".join(snapshot["text"].split())
        current_url = str(getattr(page, "url", "") or "")
        url_ok = not url or current_url.rstrip("/") == url.rstrip("/")
        if text_ok and url_ok:
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise BrowserAgentError("browser_expectation_failed")
        await asyncio.sleep(0.25)


async def execute_browser_plan(
    session: dict[str, Any],
    raw_steps: list[dict[str, Any]],
    *,
    goal: str = "",
    start_url: str = "",
    completion_text: str = "",
    completion_url: str = "",
    max_steps: int = 24,
    jev_model: str = JEV_DEFAULT_MODEL,
    jev_confidence: float = 0.50,
    jev_timeout_seconds: float = 5.0,
    action_timeout_seconds: float = 30.0,
    decision_client: Any | None = None,
) -> dict[str, Any]:
    """Execute one complete caller-owned plan inside an existing browser session."""
    if not isinstance(goal, str) or len(goal) > 16_000:
        raise BrowserAgentError("browser_plan_invalid")
    if not isinstance(start_url, str) or len(start_url) > 8_192:
        raise BrowserAgentError("browser_plan_invalid")
    if not isinstance(completion_text, str) or len(completion_text) > 1_200:
        raise BrowserAgentError("browser_plan_invalid")
    if not isinstance(completion_url, str) or len(completion_url) > 8_192:
        raise BrowserAgentError("browser_plan_invalid")
    if (
        type(jev_confidence) not in {int, float}
        or not math.isfinite(jev_confidence)
        or not 0 <= jev_confidence <= 1
    ):
        raise BrowserAgentError("browser_plan_invalid")
    if (
        type(action_timeout_seconds) not in {int, float}
        or not math.isfinite(action_timeout_seconds)
        or not 0 < action_timeout_seconds <= 120
    ):
        raise BrowserAgentError("browser_plan_invalid")

    steps = validate_steps(raw_steps, max_steps=max_steps)
    run_id = uuid4().hex
    trace: list[dict[str, Any]] = []
    observation: dict[str, Any] = {}
    owns_client = decision_client is None
    client = decision_client
    client_started = False
    redact_urls = bool(session.get("attached"))
    include_result_content = not redact_urls or any(step.action == "read" for step in steps)
    active_receipt: DecisionReceipt | None = None
    decision_event: dict[str, Any] | None = None

    def selected_page_closed() -> bool:
        if not session.get("attached"):
            return False
        page = session.get("page")
        if page is None:
            return True
        try:
            return bool(page.is_closed())
        except Exception:
            return True

    def update_receipt(outcome: str, **fields: Any) -> None:
        nonlocal active_receipt, decision_event
        if active_receipt is None:
            return
        active_receipt = finalize_decision_receipt(
            active_receipt, outcome=outcome,
            sensitive_values=(getattr(client, "api_key", ""),), **fields
        )
        if decision_event is not None:
            decision_event["receipt"] = active_receipt.to_dict()
        if outcome != "pending":
            trace.append({
                "event": "decision_outcome",
                "decision_id": active_receipt.decision_id,
                "outcome": outcome,
            })
            active_receipt = None
            decision_event = None

    async def decision_service() -> Any:
        nonlocal client, client_started
        if client is None:
            client = JevDecisionClient(
                model=jev_model,
                timeout_seconds=jev_timeout_seconds,
                max_retries=0,
            )
            await client.__aenter__()
            client_started = True
        return client

    async def result(status: str, reason: str = "") -> dict[str, Any]:
        nonlocal observation
        try:
            observation = await _page_snapshot(
                session["page"],
                redact_url=redact_urls,
                include_content=include_result_content,
            )
        except Exception:
            observation = {}
        return {
            "run_id": run_id,
            "status": status,
            "reason": reason,
            "success": status in {"completed", "steps_completed"},
            "completion_verified": status == "completed",
            "session_page_url": observation.get("url", ""),
            "observation": observation,
            "trace": trace,
            "jev": {
                "model": getattr(client, "model", jev_model) if client is not None else jev_model,
                "attempts": int(getattr(client, "attempts", 0) or 0) if client is not None else 0,
                "input_tokens": int(getattr(client, "input_tokens", 0) or 0) if client is not None else 0,
                "output_tokens": int(getattr(client, "output_tokens", 0) or 0) if client is not None else 0,
            },
        }

    try:
        page = session["page"]
        if start_url:
            navigation_started = time.perf_counter()
            try:
                response, download_started = await navigate_page(
                    page,
                    start_url,
                    wait_until="domcontentloaded",
                    timeout_ms=int(action_timeout_seconds * 1_000),
                )
            except Exception as exc:
                detail = safe_exception_summary(exc)
                trace.append({"event": "navigation_failed", "reason": "browser_navigation_failed", "reason_detail": detail})
                raise BrowserAgentError("browser_navigation_failed", safe_detail=detail) from None
            if download_started:
                trace.append({
                    "event": "navigation_download",
                    "reason": "browser_navigation_download",
                    "url": _jev_safe_url(str(getattr(page, "url", "") or "")),
                })
                return await result("failed", "browser_navigation_download")
            trace.append(
                {
                    "event": "navigation_completed",
                    "url": _jev_safe_url(page.url) if redact_urls else page.url,
                    "status": response.status if response else None,
                    "host_action_ms": round((time.perf_counter() - navigation_started) * 1_000, 3),
                }
            )
        for number, step in enumerate(steps, 1):
            step_decision_id = ""
            host_action_ms = 0.0
            trace.append({"event": "step_started", "step": number, "action": step.action})
            if step.action in _ELEMENT_ACTIONS:
                acted = False
                for attempt in range(2):
                    page = session["page"]
                    state, candidates, targets = await _observe_candidates_ready(
                        page,
                        step.action,
                        timeout_seconds=action_timeout_seconds,
                        within=step.within,
                        test_id=step.test_id,
                        nth=step.nth,
                    )
                    try:
                        chooser = await decision_service()
                        choice = await chooser.choose(
                            state=state,
                            instruction=_trusted_element_task(step),
                            candidates=candidates,
                            decision_kind="browser_dom",
                            candidate_metadata={
                                "action": step.action,
                                "candidate_count": len(candidates),
                            },
                        )
                    except (Exception, asyncio.CancelledError) as exc:
                        active_receipt = create_decision_receipt(
                            decision_kind="browser_dom", state=state, candidates=candidates,
                            threshold=jev_confidence,
                            fallback_reason=exc.code if isinstance(exc, JevError) else "decision_unavailable",
                            validation_result="not_returned",
                            sensitive_values=(getattr(client, "api_key", ""),),
                        )
                        decision_event = {
                            "event": "decision", "step": number, "action": step.action,
                            "decision_id": active_receipt.decision_id,
                        }
                        trace.append(decision_event)
                        raise
                    active_receipt = getattr(choice, "receipt", None)
                    if not isinstance(active_receipt, DecisionReceipt):
                        active_receipt = create_decision_receipt(
                            decision_kind="browser_dom", state=state, candidates=candidates,
                            selected_candidate=getattr(choice, "choice", ""),
                            probabilities=getattr(choice, "probabilities", {}),
                            confidence=getattr(choice, "confidence", None),
                            latency_ms=getattr(choice, "latency_ms", 0.0),
                            sensitive_values=(getattr(client, "api_key", ""),),
                        )
                    step_decision_id = active_receipt.decision_id
                    validation = validate_host_choice(
                        choice,
                        candidates,
                        minimum_confidence=float(jev_confidence),
                    )
                    probabilities = dict(getattr(choice, "probabilities", {}) or {})
                    decision_event = redact_decision_metadata({
                        "event": "decision",
                        "step": number,
                        "action": step.action,
                        "choice": choice.choice,
                        "confidence": choice.confidence,
                        "model": choice.model,
                        "selected_probability": probabilities.get(choice.choice),
                        "probabilities": probabilities,
                        "candidate": candidates.get(choice.choice, {}),
                        "decision_kind": getattr(
                            choice, "decision_kind", "browser_dom"
                        ),
                        "latency_ms": getattr(choice, "latency_ms", 0.0),
                        "validation_result": validation.status,
                        "decision_id": step_decision_id,
                    }, sensitive_values=(getattr(client, "api_key", ""),))
                    trace.append(decision_event)
                    update_receipt("pending", validation_result=validation.status, threshold=jev_confidence)
                    if not validation.valid:
                        update_receipt("abstained" if validation.status == "abstained" else "rejected")
                        if validation.status == "abstained":
                            raise BrowserAgentError("browser_candidate_not_found")
                        if validation.status == "low_confidence":
                            raise BrowserAgentError("jev_low_confidence")
                        raise BrowserAgentError("jev_invalid_response")
                    # The closed-set ID check above is not execution authority.
                    # Resolve it again against the current observation, then
                    # _act_on_target performs freshness/actionability checks.
                    target = targets.get(choice.choice)
                    if target is None:
                        update_receipt("stale", validation_result="target_missing")
                        raise BrowserAgentError("browser_stale_element")
                    update_receipt(
                        "pending", host_action=step.action, host_candidate=choice.choice,
                        validation_result="target_resolved",
                    )
                    action_started = time.perf_counter()
                    try:
                        page = await _act_on_target(
                            session,
                            target,
                            step,
                            action_timeout_seconds=float(action_timeout_seconds),
                        )
                        host_action_ms += (time.perf_counter() - action_started) * 1_000
                        acted = True
                        break
                    except BrowserAgentError as exc:
                        host_action_ms += (time.perf_counter() - action_started) * 1_000
                        visibility = session.pop("_last_tab_visibility_diagnostic", None)
                        if visibility is not None:
                            if exc.safe_detail is None:
                                exc.safe_detail = {"tab_visibility": visibility}
                            else:
                                exc.safe_detail["tab_visibility"] = visibility
                        update_receipt(
                            "stale" if exc.code == "browser_stale_element" else "failed",
                            validation_result=exc.code,
                        )
                        if exc.code != "browser_stale_element" or attempt:
                            raise
                    except Exception as exc:
                        host_action_ms += (time.perf_counter() - action_started) * 1_000
                        if step.action == "click":
                            code = _click_failure_code(exc)
                            detail = safe_exception_summary(exc)
                            update_receipt("failed", validation_result=code)
                            trace.append({
                                "event": "action_failed",
                                "step": number,
                                "action": "click",
                                "reason": code,
                                "reason_detail": detail,
                            **({"tab_visibility": session.pop("_last_tab_visibility_diagnostic")}
                               if session.get("_last_tab_visibility_diagnostic") is not None else {}),
                            })
                            raise BrowserAgentError(code, safe_detail=detail) from None
                        stale = _is_stale_error(exc)
                        update_receipt("stale" if stale else "unknown", validation_result="action_interrupted")
                        if attempt or not stale:
                            detail = safe_exception_summary(exc)
                            visibility = session.pop("_last_tab_visibility_diagnostic", None)
                            if visibility is not None:
                                detail["tab_visibility"] = visibility
                            raise BrowserAgentError("browser_action_failed", safe_detail=detail) from None
                if not acted:
                    raise BrowserAgentError("browser_stale_element")
            elif step.action == "navigate":
                action_started = time.perf_counter()
                try:
                    _response, download_started = await navigate_page(
                        session["page"],
                        step.url,
                        wait_until="domcontentloaded",
                        timeout_ms=int(action_timeout_seconds * 1_000),
                    )
                except Exception as exc:
                    detail = safe_exception_summary(exc)
                    raise BrowserAgentError("browser_navigation_failed", safe_detail=detail) from None
                if download_started:
                    raise BrowserAgentError("browser_navigation_download")
                host_action_ms = (time.perf_counter() - action_started) * 1_000
            elif step.action == "back":
                action_started = time.perf_counter()
                await session["page"].go_back(
                    wait_until="domcontentloaded",
                    timeout=action_timeout_seconds * 1_000,
                )
                host_action_ms = (time.perf_counter() - action_started) * 1_000
            elif step.action == "press":
                action_started = time.perf_counter()
                await session["page"].keyboard.press(step.value)
                host_action_ms = (time.perf_counter() - action_started) * 1_000
            elif step.action == "scroll":
                action_started = time.perf_counter()
                distance = -650 if step.value == "up" else 650
                await session["page"].mouse.wheel(0, distance)
                host_action_ms = (time.perf_counter() - action_started) * 1_000
            elif step.action == "wait":
                action_started = time.perf_counter()
                await asyncio.sleep(float(step.value or "0.5"))
                host_action_ms = (time.perf_counter() - action_started) * 1_000
            elif step.action == "read":
                action_started = time.perf_counter()
                observation = await _page_snapshot(
                    session["page"], redact_url=redact_urls, include_content=True
                )
                host_action_ms = (time.perf_counter() - action_started) * 1_000
                trace.append(
                    {
                        "event": "observation",
                        "step": number,
                        "url": observation.get("url", ""),
                        "title": observation.get("title", ""),
                    }
                )
            elif step.action == "done":
                trace.append({"event": "plan_done", "step": number})
                break
            await _verify(
                session["page"],
                text=step.expected_text,
                url=step.expected_url,
                redact_url=redact_urls,
                include_content=not redact_urls,
            )
            update_receipt(
                "succeeded", validation_result=(
                    "postcondition_verified"
                    if step.expected_text or step.expected_url else "action_completed"
                ),
            )
            trace.append({
                "event": "step_completed", "step": number, "action": step.action,
                "host_action_ms": round(host_action_ms, 3),
                **({"decision_id": step_decision_id} if step_decision_id else {}),
                **({"tab_visibility": session.pop("_last_tab_visibility_diagnostic")}
                   if session.get("_last_tab_visibility_diagnostic") is not None else {}),
            })
        observation = await _verify(
            session["page"],
            text=completion_text,
            url=completion_url,
            redact_url=redact_urls,
            include_content=not redact_urls,
        )
        trace.append({"event": "run_completed"})
        return await result(
            "completed" if completion_text or completion_url else "steps_completed"
        )
    except asyncio.CancelledError:
        update_receipt("cancelled")
        raise
    except (BrowserAgentError, JevError) as exc:
        reason = "browser_tab_closed" if selected_page_closed() else exc.code
        update_receipt("failed", validation_result=reason)
        trace.append({
            "event": "run_stopped",
            "error": type(exc).__name__,
            "reason": reason,
            **({"reason_detail": exc.safe_detail} if isinstance(exc, BrowserAgentError) and exc.safe_detail else {}),
        })
        failed = await result("failed", reason)
        if isinstance(exc, BrowserAgentError) and exc.safe_detail:
            failed["reason_detail"] = exc.safe_detail
        return failed
    except Exception as exc:
        closed = selected_page_closed()
        reason = "browser_tab_closed" if closed else "browser_agent_failed"
        detail = safe_exception_summary(exc)
        update_receipt("failed" if closed else "unknown", validation_result=reason if closed else "")
        trace.append({
            "event": "run_stopped",
            "error": type(exc).__name__,
            "reason": reason,
            "reason_detail": detail,
        })
        failed = await result("failed", reason)
        failed["reason_detail"] = detail
        return failed
    finally:
        if owns_client and client_started and client is not None:
            await client.__aexit__(None, None, None)
