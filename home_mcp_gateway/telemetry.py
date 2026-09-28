"""Low-overhead, privacy-conscious timing telemetry for MCP tool calls."""
from __future__ import annotations

from collections import defaultdict
from contextvars import ContextVar
from datetime import datetime, timedelta
import functools
import inspect
import json
import math
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, get_type_hints
from urllib.parse import urlsplit, urlunsplit
import uuid


_WRITE_LOCK = threading.Lock()
_REQUEST_CONTEXT: ContextVar[dict[str, Any] | None] = ContextVar("home_mcp_request_context", default=None)
_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LOG_ROOT = _REPO_ROOT / ".runtime" / "tool_logs"
_SAFE_LITERAL_KEYS = {
    "algorithm", "case_sensitive", "cwd", "destination", "file_glob", "force",
    "full_page", "host", "job_id", "max_chars", "max_output_chars", "max_results",
    "method", "name", "overwrite", "parents", "path", "pid", "port", "recursive",
    "remote", "repository_path", "root", "serial", "session_id", "source",
    "timeout_sec", "verify_tls", "wait_until", "task_id", "workspace", "mode", "wait_seconds",
}
_SENSITIVE_KEYS = {
    "body", "body_base64", "command", "code", "env", "headers", "password",
    "query", "script", "secret", "text", "token", "value",
}


def _enabled() -> bool:
    return os.environ.get("HOME_MCP_TOOL_LOG", "1").strip().lower() not in {"0", "false", "no", "off"}


def _log_root() -> Path:
    override = os.environ.get("HOME_MCP_TOOL_LOG_DIR", "").strip()
    return Path(override).expanduser() if override else _DEFAULT_LOG_ROOT


def current_log_path(now: datetime | None = None) -> Path:
    now = now or datetime.now().astimezone()
    day = now.strftime("%Y-%m-%d")
    return _log_root() / day / f"gateway-{os.getpid()}.jsonl"


def _safe_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    except Exception:
        return f"<url chars={len(value)}>"


def _summarize_arg(name: str, value: Any) -> Any:
    lowered = name.lower()
    if any(part in lowered for part in _SENSITIVE_KEYS):
        if isinstance(value, str):
            return {"type": "str", "chars": len(value)}
        if isinstance(value, dict):
            return {"type": "dict", "items": len(value), "keys": sorted(map(str, value.keys()))[:20]}
        if isinstance(value, (list, tuple, set)):
            return {"type": type(value).__name__, "items": len(value)}
        return {"type": type(value).__name__}

    if lowered == "url" and isinstance(value, str):
        return _safe_url(value)

    if lowered in _SAFE_LITERAL_KEYS:
        if isinstance(value, str):
            return value[:500]
        if isinstance(value, (bool, int, float)) or value is None:
            return value

    if isinstance(value, str):
        return {"type": "str", "chars": len(value)}
    if isinstance(value, dict):
        return {"type": "dict", "items": len(value), "keys": sorted(map(str, value.keys()))[:20]}
    if isinstance(value, (list, tuple, set)):
        return {"type": type(value).__name__, "items": len(value)}
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return {"type": type(value).__name__}


def _summarize_result(result: Any) -> dict[str, Any]:
    if isinstance(result, dict):
        summary: dict[str, Any] = {"type": "dict", "keys": sorted(map(str, result.keys()))[:40]}
        for key in ("ok", "returncode", "timed_out", "timeout_sec", "duration_sec", "truncated",
                    "bytes", "size", "pid", "job_id", "session_id", "ready", "status", "task_id"):
            value = result.get(key)
            if isinstance(value, (bool, int, float, str)) or value is None:
                if isinstance(value, str) and len(value) > 200:
                    summary[key] = {"type": "str", "chars": len(value)}
                else:
                    summary[key] = value
        return summary
    return {"type": type(result).__name__}


def _append_event(event: dict[str, Any]) -> None:
    if not _enabled():
        return
    path = current_log_path()
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _WRITE_LOCK:
            with path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
    except Exception:
        pass


class ToolTelemetryMiddleware:
    """Capture MCP request identity for the tool timing record."""

    async def __call__(self, ctx: Any, call_next: Callable[..., Any]) -> Any:
        # ``ServerRequestContext.session`` is a per-request ``ServerSession``
        # proxy in current MCP SDKs.  The proxy does not expose
        # ``session_id`` itself; the transport connection owns it.  Keep the
        # fallbacks generic so telemetry remains compatible with older SDK
        # context shapes without importing a version-specific MCP type.
        request_id = _context_value(ctx, "request_id")
        session_id = _context_session_id(ctx)
        received_at = datetime.now().astimezone()
        received_perf = time.perf_counter()
        payload = {
            "mcp_request_id": _safe_text(request_id, None),
            "mcp_method": _safe_text(_context_value(ctx, "method"), ""),
            "mcp_session_id": _safe_text(session_id, None),
            # These monotonic values stay in the ContextVar only; wall-clock
            # timestamps are emitted in the event for operator correlation.
            "_gateway_received_at": received_at.isoformat(),
            "_gateway_received_perf": received_perf,
        }
        token = _REQUEST_CONTEXT.set(payload)
        try:
            return await call_next(ctx)
        finally:
            _REQUEST_CONTEXT.reset(token)


def _context_value(ctx: Any, name: str) -> Any:
    """Read a context attribute while tolerating SDK property differences."""
    try:
        return getattr(ctx, name, None)
    except Exception:
        return None


def _safe_text(value: Any, default: str | None = "") -> str | None:
    """Stringify metadata without allowing a malformed SDK object to escape."""
    if value is None:
        return default
    try:
        return str(value)
    except Exception:
        return default


def _context_session_id(ctx: Any) -> Any:
    """Return the MCP transport session id when the SDK exposes one.

    Stdio and stateless HTTP transports legitimately have no session id.  The
    private ``ServerSession._connection`` fallback is intentional: the MCP
    SDK currently stores the transport connection there but does not expose a
    ``ServerSession.session_id`` property to server middleware.
    """
    request_context = _context_value(ctx, "request_context")
    owners = [ctx, request_context]
    # High-level MCP Context exposes the low-level session through
    # ``ctx.request_context.session``.  Inspect both this shape and the
    # low-level middleware shape without importing SDK-version-specific types.
    owners.extend((_context_value(ctx, "session"), _context_value(request_context, "session")))
    for owner in owners:
        for candidate in (
            owner,
            _context_value(owner, "connection"),
            _context_value(owner, "_connection"),
        ):
            value = _context_value(candidate, "session_id")
            if value is not None:
                return value
    return None


def instrument_tool(target: Callable[..., Any]) -> Callable[..., Any]:
    """Wrap one MCP tool while preserving its discovery schema."""
    if getattr(target, "__home_mcp_instrumented__", False):
        return target

    hints = get_type_hints(target)
    signature = inspect.signature(target, eval_str=True)
    tool_name = target.__name__

    def finish(started_at: datetime, started_perf: float, request_id: str,
               kwargs: dict[str, Any], request_context: dict[str, Any],
               result: Any = None, error: BaseException | None = None) -> None:
        finished_perf = time.perf_counter()
        tool_duration_ms = round((finished_perf - started_perf) * 1000, 3)
        received_perf = request_context.get("_gateway_received_perf")
        queue_wait_ms = None
        internal_duration_ms = None
        if isinstance(received_perf, (int, float)):
            # The middleware is the first timestamp available inside the
            # Gateway.  This is dispatch/queue time after Gateway receipt, not
            # the time spent in ChatGPT or a remote plugin before receipt.
            queue_wait_ms = round(max(0.0, (started_perf - received_perf) * 1000), 3)
            internal_duration_ms = round(max(0.0, (finished_perf - received_perf) * 1000), 3)
        event: dict[str, Any] = {
            "schema": 1,
            "request_id": request_id,
            "gateway_pid": os.getpid(),
            "mcp_request_id": request_context.get("mcp_request_id"),
            "mcp_method": request_context.get("mcp_method"),
            "mcp_session_id": request_context.get("mcp_session_id"),
            "tool": tool_name,
            "started_at": started_at.isoformat(),
            # ``started_at`` and ``duration_ms`` are retained as the original
            # tool execution fields for log consumers.  The additional fields
            # make the measured Gateway portions explicit.
            "request_received_at": request_context.get("_gateway_received_at"),
            "tool_started_at": started_at.isoformat(),
            "queue_wait_ms": queue_wait_ms,
            "tool_duration_ms": tool_duration_ms,
            "internal_duration_ms": internal_duration_ms,
            "duration_ms": tool_duration_ms,
            "args": {key: _summarize_arg(key, value) for key, value in kwargs.items()},
            "outcome": "error" if error is not None else "ok",
        }
        if error is not None:
            event["error_type"] = type(error).__name__
        else:
            event["result"] = _summarize_result(result)
        _append_event(event)

    if inspect.iscoroutinefunction(target):
        @functools.wraps(target)
        async def async_invoke(**kwargs):
            started_at = datetime.now().astimezone()
            started_perf = time.perf_counter()
            request_id = uuid.uuid4().hex[:16]
            request_context = dict(_REQUEST_CONTEXT.get() or {})
            try:
                result = await target(**kwargs)
            except BaseException as exc:
                finish(started_at, started_perf, request_id, kwargs, request_context, error=exc)
                raise
            finish(started_at, started_perf, request_id, kwargs, request_context, result=result)
            return result
        wrapped = async_invoke
    else:
        @functools.wraps(target)
        def sync_invoke(**kwargs):
            started_at = datetime.now().astimezone()
            started_perf = time.perf_counter()
            request_id = uuid.uuid4().hex[:16]
            request_context = dict(_REQUEST_CONTEXT.get() or {})
            try:
                result = target(**kwargs)
            except BaseException as exc:
                finish(started_at, started_perf, request_id, kwargs, request_context, error=exc)
                raise
            finish(started_at, started_perf, request_id, kwargs, request_context, result=result)
            return result
        wrapped = sync_invoke

    wrapped.__signature__ = signature
    wrapped.__annotations__ = hints
    wrapped.__home_mcp_instrumented__ = True
    return wrapped


def tracked_tool(server: Any, **tool_kwargs: Any):
    """MCP decorator that adds timing telemetry without changing the tool schema."""
    def decorate(target: Callable[..., Any]):
        return server.tool(**tool_kwargs)(instrument_tool(target))
    return decorate


def log_info() -> dict[str, Any]:
    path = current_log_path()
    return {
        "enabled": _enabled(),
        "gateway_pid": os.getpid(),
        "current_log": str(path),
        "log_root": str(_log_root()),
        "exists": path.exists(),
        "bytes": path.stat().st_size if path.exists() else 0,
        "format": "jsonl",
        "privacy": "argument values that may contain commands, code, text, bodies, credentials, or queries are summarized, not stored",
    }


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, math.ceil(len(ordered) * fraction) - 1))
    return ordered[index]


def summarize_logs(window_minutes: int = 60, top_n: int = 20) -> dict[str, Any]:
    if window_minutes <= 0 or window_minutes > 7 * 24 * 60:
        raise ValueError("window_minutes must be between 1 and 10080")
    if top_n <= 0 or top_n > 100:
        raise ValueError("top_n must be between 1 and 100")

    cutoff = datetime.now().astimezone() - timedelta(minutes=window_minutes)
    stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"durations": [], "errors": 0})
    matched_events = 0
    files_read = 0
    root = _log_root()

    if root.exists():
        for path in sorted(root.glob("*/*.jsonl")):
            try:
                day = datetime.strptime(path.parent.name, "%Y-%m-%d").date()
                if day < cutoff.date():
                    continue
            except ValueError:
                continue
            try:
                files_read += 1
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        try:
                            event = json.loads(line)
                            started = datetime.fromisoformat(event["started_at"])
                            if started < cutoff:
                                continue
                            tool = str(event["tool"])
                            duration = float(event.get("duration_ms", 0.0))
                        except Exception:
                            continue
                        matched_events += 1
                        bucket = stats[tool]
                        bucket["durations"].append(duration)
                        if event.get("outcome") != "ok":
                            bucket["errors"] += 1
            except OSError:
                continue

    rows = []
    for tool, bucket in stats.items():
        durations = bucket["durations"]
        rows.append({
            "tool": tool,
            "calls": len(durations),
            "errors": bucket["errors"],
            "total_ms": round(sum(durations), 3),
            "avg_ms": round(sum(durations) / len(durations), 3),
            "p95_ms": round(_percentile(durations, 0.95), 3),
            "max_ms": round(max(durations), 3),
        })
    rows.sort(key=lambda row: (row["total_ms"], row["max_ms"]), reverse=True)

    return {
        "window_minutes": window_minutes,
        "cutoff": cutoff.isoformat(),
        "events": matched_events,
        "files_read": files_read,
        "tools": rows[:top_n],
        "sort": "total_ms_desc",
    }
