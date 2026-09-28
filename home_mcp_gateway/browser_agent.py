"""High-level browser plan executor with Jev-selected DOM targets.

The MCP caller owns strategy and literal input values. Jev sees only a bounded,
value-free summary of currently actionable DOM candidates and returns one ID.
Code owns execution, retries, verification, budgets, and the stop condition.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import math
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
}


class BrowserAgentError(RuntimeError):
    """Stable high-level execution failure code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BrowserStep:
    action: str
    instruction: str = ""
    value: str = ""
    url: str = ""
    expected_text: str = ""
    expected_url: str = ""


@dataclass(frozen=True)
class CandidateTarget:
    frame: Any
    local_id: str
    summary: Mapping[str, Any]


_OBSERVE_SCRIPT = r"""
({action, limit}) => {
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
  const nonce = Math.random().toString(36).slice(2, 12);
  const candidates = {};
  for (const e of elementsIn(document)) {
    if (targets.size >= limit) break;
    if (!visible(e) || !compatible(e)) continue;
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
        String(option.label || option.textContent || option.value || '').slice(0, 180));
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
        for field in _ALLOWED_STEP_FIELDS - {"action"}:
            value = raw.get(field, "")
            if value is None:
                value = ""
            if not isinstance(value, str):
                raise BrowserAgentError("browser_plan_invalid")
            limit = 16_000 if field == "value" else 8_192 if field in {"url", "expected_url"} else 1_200
            if len(value) > limit:
                raise BrowserAgentError("browser_plan_invalid")
            values[field] = value
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
        steps.append(BrowserStep(action=action, **values))
    return steps


async def _page_snapshot(page: Any, *, max_chars: int = MAX_OBSERVATION_TEXT) -> dict[str, Any]:
    try:
        title = await page.title()
    except Exception:
        title = ""
    try:
        text = await page.locator("body").inner_text(timeout=3_000)
    except Exception:
        text = ""
    return {
        "url": str(getattr(page, "url", "") or ""),
        "title": str(title or "")[:1_000],
        "text": str(text or "")[:max_chars],
        "truncated": len(str(text or "")) > max_chars,
    }


def _jev_safe_url(value: str) -> str:
    """Drop query and fragment before sending a browser URL to Jev."""
    try:
        parsed = urlsplit(str(value or ""))
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))[:1_000]
    except (TypeError, ValueError):
        return ""


async def _observe_candidates(
    page: Any,
    action: str,
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
                {"action": action, "limit": remaining},
            )
        except Exception:
            if frame is page.main_frame:
                raise BrowserAgentError("browser_observation_failed") from None
            continue
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
    if not summaries:
        raise BrowserAgentError("browser_no_candidates")
    return state, summaries, targets


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
    element = await _selected_element(target)
    timeout_ms = action_timeout_seconds * 1_000
    try:
        if step.action == "click":
            await element.click(timeout=timeout_ms)
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
) -> dict[str, Any]:
    if not text and not url:
        return await _page_snapshot(page)
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while True:
        snapshot = await _page_snapshot(page)
        text_ok = not text or " ".join(text.split()) in " ".join(snapshot["text"].split())
        url_ok = not url or snapshot["url"].rstrip("/") == url.rstrip("/")
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
    active_receipt: DecisionReceipt | None = None
    decision_event: dict[str, Any] | None = None

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
            observation = await _page_snapshot(session["page"])
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
            response = await page.goto(
                start_url,
                wait_until="domcontentloaded",
                timeout=action_timeout_seconds * 1_000,
            )
            trace.append(
                {
                    "event": "navigation_completed",
                    "url": page.url,
                    "status": response.status if response else None,
                }
            )
        for number, step in enumerate(steps, 1):
            step_decision_id = ""
            trace.append({"event": "step_started", "step": number, "action": step.action})
            if step.action in _ELEMENT_ACTIONS:
                acted = False
                for attempt in range(2):
                    page = session["page"]
                    state, candidates, targets = await _observe_candidates(page, step.action)
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
                    try:
                        page = await _act_on_target(
                            session,
                            target,
                            step,
                            action_timeout_seconds=float(action_timeout_seconds),
                        )
                        acted = True
                        break
                    except BrowserAgentError as exc:
                        update_receipt(
                            "stale" if exc.code == "browser_stale_element" else "failed",
                            validation_result=exc.code,
                        )
                        if exc.code != "browser_stale_element" or attempt:
                            raise
                    except Exception as exc:
                        stale = _is_stale_error(exc)
                        update_receipt("stale" if stale else "unknown", validation_result="action_interrupted")
                        if attempt or not stale:
                            raise BrowserAgentError("browser_action_failed") from exc
                if not acted:
                    raise BrowserAgentError("browser_stale_element")
            elif step.action == "navigate":
                await session["page"].goto(
                    step.url,
                    wait_until="domcontentloaded",
                    timeout=action_timeout_seconds * 1_000,
                )
            elif step.action == "back":
                await session["page"].go_back(
                    wait_until="domcontentloaded",
                    timeout=action_timeout_seconds * 1_000,
                )
            elif step.action == "press":
                await session["page"].keyboard.press(step.value)
            elif step.action == "scroll":
                distance = -650 if step.value == "up" else 650
                await session["page"].mouse.wheel(0, distance)
            elif step.action == "wait":
                await asyncio.sleep(float(step.value or "0.5"))
            elif step.action == "read":
                observation = await _page_snapshot(session["page"])
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
            )
            update_receipt(
                "succeeded", validation_result=(
                    "postcondition_verified"
                    if step.expected_text or step.expected_url else "action_completed"
                ),
            )
            trace.append({
                "event": "step_completed", "step": number, "action": step.action,
                **({"decision_id": step_decision_id} if step_decision_id else {}),
            })
        observation = await _verify(
            session["page"],
            text=completion_text,
            url=completion_url,
        )
        trace.append({"event": "run_completed"})
        return await result(
            "completed" if completion_text or completion_url else "steps_completed"
        )
    except asyncio.CancelledError:
        update_receipt("cancelled")
        raise
    except (BrowserAgentError, JevError) as exc:
        update_receipt("failed", validation_result=exc.code)
        trace.append({"event": "run_stopped", "error": type(exc).__name__, "reason": str(exc)})
        return await result("failed", str(exc))
    except Exception as exc:
        update_receipt("unknown")
        trace.append({"event": "run_stopped", "error": type(exc).__name__})
        return await result("failed", "browser_agent_failed")
    finally:
        if owns_client and client_started and client is not None:
            await client.__aexit__(None, None, None)
