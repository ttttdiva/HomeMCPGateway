"""Bounded, static read-only batches for local Gateway observations.

``local_read_plan`` deliberately has a much smaller surface than the full
Gateway.  A caller supplies an ordered list of operations, all input is
validated before the first operation runs, and execution stops at the first
failure or timeout.  The handlers below are intentionally explicit: this
module must not become an indirect shell/process/write/network executor.

The operations which perform recursive work receive their native timeout and
result/file limits.  No worker thread is used for timeout enforcement.  A
thread cancellation would leave a potentially unbounded synchronous call
running after the MCP request had returned.  ``path_info`` and bounded
streaming ``read_text`` reads are local filesystem operations, while the
recursive search/glob implementations enforce their own cooperative bounds
in ``core``.  The allowlist does not add an ACL: readable paths and explicitly
requested environment values can still contain data that the caller is
authorized to see through the corresponding ordinary read tool.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Callable

from . import core


# Public policy values are kept as constants so tests and operators can see the
# exact contract without depending on implementation details.
DEFAULT_MAX_STEPS = 8
HARD_MAX_STEPS = 16
DEFAULT_TOTAL_TIMEOUT_SEC = 30.0
HARD_TOTAL_TIMEOUT_SEC = 120.0
DEFAULT_STEP_TIMEOUT_SEC = 5.0
HARD_STEP_TIMEOUT_SEC = 15.0
DEFAULT_MAX_RESULT_BYTES = 64 * 1024
HARD_MAX_RESULT_BYTES = 256 * 1024
MIN_MAX_RESULT_BYTES = 512

# The operation-specific caps are intentionally no larger than the aggregate
# output cap.  A read/search/glob with a caller-supplied zero means "unlimited"
# in the lower-level APIs; local plans reject that form instead of inheriting
# an unbounded operation.
MAX_READ_CHARS = 64 * 1024
MAX_SEARCH_RESULTS = 2_000
MAX_SEARCH_FILES = 10_000
MAX_GLOB_RESULTS = 2_000
MAX_ARGUMENT_CHARS = 4 * 1024
READ_CHUNK_CHARS = 8 * 1024

_ALLOWED_OPERATIONS = frozenset(
    {
        "path_info",
        "read_text",
        "search_text",
        "glob_paths",
        "get_environment",
    }
)


class LocalReadPlanValidationError(ValueError):
    """Raised when a complete plan cannot be accepted without execution."""


class LocalReadPlanTimeout(TimeoutError):
    """Raised by an allowlisted operation that reaches its cooperative budget."""


def _invalid(reason: str) -> LocalReadPlanValidationError:
    # Never include operation arguments in validation errors.  They may contain
    # paths, queries, credentials, or other caller data.
    return LocalReadPlanValidationError(f"local_read_plan_invalid: {reason}")


def _is_number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(float(value))


def _bounded_float(value: Any, *, name: str, default: float, hard_max: float) -> float:
    if value is None:
        return default
    if not _is_number(value) or not 0 < float(value) <= hard_max:
        raise _invalid(f"{name}_out_of_range")
    return float(value)


def _bounded_int(value: Any, *, name: str, default: int, hard_max: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 0 < value <= hard_max:
        raise _invalid(f"{name}_out_of_range")
    return value


def _text(value: Any, *, name: str, nonempty: bool = True, max_chars: int = MAX_ARGUMENT_CHARS) -> str:
    if not isinstance(value, str) or (nonempty and not value):
        raise _invalid(f"{name}_invalid")
    if len(value) > max_chars:
        raise _invalid(f"{name}_too_long")
    return value


def _bool(value: Any, *, name: str, default: bool) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise _invalid(f"{name}_invalid")
    return value


def _args(raw: Any, allowed: set[str]) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise _invalid("arguments_invalid")
    unknown = set(raw) - allowed
    if unknown:
        raise _invalid("arguments_not_allowed")
    # A shallow copy prevents a caller mutating a plan while it is running and
    # ensures our validation never mutates the MCP request object.
    return dict(raw)


def _validate_path_info(raw: Any) -> dict[str, Any]:
    args = _args(raw, {"path"})
    return {"path": _text(args.get("path"), name="path")}


def _validate_read_text(raw: Any) -> dict[str, Any]:
    args = _args(raw, {"path", "encoding", "max_chars"})
    max_chars = _bounded_int(
        args.get("max_chars"),
        name="max_chars",
        default=0,
        hard_max=MAX_READ_CHARS,
    )
    # Zero means "complete file" in core.read_text and is not safe in a plan.
    if max_chars <= 0:
        raise _invalid("max_chars_required")
    encoding = args.get("encoding", "utf-8")
    return {
        "path": _text(args.get("path"), name="path"),
        "encoding": _text(encoding, name="encoding", max_chars=128),
        "max_chars": max_chars,
    }


def _validate_search_text(raw: Any) -> dict[str, Any]:
    args = _args(raw, {"root", "query", "file_glob", "case_sensitive", "max_results", "timeout_sec", "max_files"})
    max_results = _bounded_int(
        args.get("max_results"),
        name="max_results",
        default=1,
        hard_max=MAX_SEARCH_RESULTS,
    )
    max_files = _bounded_int(
        args.get("max_files"),
        name="max_files",
        default=MAX_SEARCH_FILES,
        hard_max=MAX_SEARCH_FILES,
    )
    timeout_sec = _bounded_float(
        args.get("timeout_sec"),
        name="search_timeout_sec",
        default=DEFAULT_STEP_TIMEOUT_SEC,
        hard_max=HARD_STEP_TIMEOUT_SEC,
    )
    return {
        "root": _text(args.get("root"), name="root"),
        "query": _text(args.get("query"), name="query"),
        "file_glob": _text(args.get("file_glob", "*"), name="file_glob", nonempty=False),
        "case_sensitive": _bool(args.get("case_sensitive"), name="case_sensitive", default=False),
        "max_results": max_results,
        "timeout_sec": timeout_sec,
        "max_files": max_files,
    }


def _validate_glob_paths(raw: Any) -> dict[str, Any]:
    args = _args(raw, {"pattern", "recursive", "max_results", "timeout_sec"})
    max_results = _bounded_int(
        args.get("max_results"),
        name="max_results",
        default=1,
        hard_max=MAX_GLOB_RESULTS,
    )
    timeout_sec = _bounded_float(
        args.get("timeout_sec"),
        name="glob_timeout_sec",
        default=DEFAULT_STEP_TIMEOUT_SEC,
        hard_max=HARD_STEP_TIMEOUT_SEC,
    )
    return {
        "pattern": _text(args.get("pattern"), name="pattern"),
        "recursive": _bool(args.get("recursive"), name="recursive", default=True),
        "max_results": max_results,
        "timeout_sec": timeout_sec,
    }


def _validate_get_environment(raw: Any) -> dict[str, Any]:
    args = _args(raw, {"name"})
    name = _text(args.get("name"), name="name", max_chars=256)
    # Empty-name get_environment returns the complete process environment and
    # is unbounded, so a specific variable name is required.  We deliberately
    # do not guess which names are secrets: the ordinary read operation already
    # has the same permission model, and substring heuristics are unreliable.
    return {"name": name}


_VALIDATORS: dict[str, Callable[[Any], dict[str, Any]]] = {
    "path_info": _validate_path_info,
    "read_text": _validate_read_text,
    "search_text": _validate_search_text,
    "glob_paths": _validate_glob_paths,
    "get_environment": _validate_get_environment,
}


def _invoke_path_info(args: dict[str, Any], _timeout_sec: float) -> dict[str, Any]:
    return core.path_info(args["path"])


def _invoke_read_text(args: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    """Read at most ``max_chars + 1`` decoded characters cooperatively.

    ``core.read_text`` historically reads a complete file before applying its
    character limit.  A plan must not inherit that unbounded allocation, so it
    performs the bounded streaming read itself and checks the step deadline at
    every chunk.  The result shape remains compatible with ``core.read_text``.
    """

    path = Path(args["path"]).expanduser()
    max_chars = args["max_chars"]
    deadline = time.monotonic() + timeout_sec
    chunks: list[str] = []
    remaining = max_chars + 1
    # Avoid inheriting an unbounded FIFO/device read.  A regular file check is
    # intentionally part of the bounded variant; paths that are not regular
    # files fail as a normal redacted step error.
    if not path.is_file():
        raise ValueError("read_text_requires_regular_file")
    with path.open("r", encoding=args["encoding"], errors="replace") as stream:
        while remaining > 0:
            if time.monotonic() >= deadline:
                raise LocalReadPlanTimeout()
            chunk = stream.read(min(READ_CHUNK_CHARS, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
            if time.monotonic() >= deadline and remaining > 0:
                raise LocalReadPlanTimeout()
    text = "".join(chunks)
    truncated = len(text) > max_chars
    return {
        "path": str(path),
        "text": text[:max_chars] if truncated else text,
        "truncated": truncated,
    }


def _invoke_search_text(args: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    bounded = dict(args)
    bounded["timeout_sec"] = min(float(args["timeout_sec"]), timeout_sec)
    return core.search_text(**bounded)


def _invoke_glob_paths(args: dict[str, Any], timeout_sec: float) -> dict[str, Any]:
    bounded = dict(args)
    bounded["timeout_sec"] = min(float(args["timeout_sec"]), timeout_sec)
    return core.glob_paths(**bounded)


def _invoke_get_environment(args: dict[str, Any], _timeout_sec: float) -> dict[str, Any]:
    return core.get_environment(args["name"])


# Explicit operation dispatch.  Do not replace this with getattr/importing a
# caller-selected symbol: the allowlist is a security boundary.
_DISPATCH: dict[str, Callable[[dict[str, Any], float], dict[str, Any]]] = {
    "path_info": _invoke_path_info,
    "read_text": _invoke_read_text,
    "search_text": _invoke_search_text,
    "glob_paths": _invoke_glob_paths,
    "get_environment": _invoke_get_environment,
}


def _normalize_step(raw: Any, index: int) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise _invalid(f"step_{index}_invalid")
    allowed_keys = {"operation", "tool", "args", "arguments"}
    if set(raw) - allowed_keys:
        raise _invalid(f"step_{index}_fields_not_allowed")

    operation = raw.get("operation")
    alias = raw.get("tool")
    if operation is None:
        operation = alias
    elif alias is not None and alias != operation:
        raise _invalid(f"step_{index}_operation_conflict")
    if not isinstance(operation, str) or operation not in _ALLOWED_OPERATIONS:
        raise _invalid(f"step_{index}_operation_not_allowed")

    args = raw.get("args")
    alias_args = raw.get("arguments")
    if args is None:
        args = alias_args
    elif alias_args is not None and alias_args != args:
        raise _invalid(f"step_{index}_arguments_conflict")
    validated = _VALIDATORS[operation](args)
    return {"operation": operation, "args": validated}


def validate_plan(
    steps: Any,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> list[dict[str, Any]]:
    """Validate every step and return a detached normalized plan.

    This function performs no filesystem or process operation.  In particular,
    callers can safely validate a plan containing a later dangerous/unknown
    step without accidentally executing earlier entries.
    """

    if type(max_steps) is not int or not 1 <= max_steps <= HARD_MAX_STEPS:
        raise _invalid("max_steps_out_of_range")
    if not isinstance(steps, list) or not 1 <= len(steps) <= max_steps:
        raise _invalid("steps_out_of_range")
    # A separate pass makes the all-input-validation guarantee explicit and
    # avoids any future refactor accidentally interleaving execution.
    return [_normalize_step(raw, index) for index, raw in enumerate(steps, start=1)]


def _serialize_json(value: Any) -> bytes:
    """Serialize with the exact compact UTF-8 encoding used by output caps."""

    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _json_size(value: Any) -> int:
    try:
        encoded = _serialize_json(value)
    except Exception:
        encoded = b"{}"
    return len(encoded)


def _bounded_json_value(
    value: Any,
    budget: int,
    *,
    depth: int = 0,
    seen: set[int] | None = None,
) -> tuple[Any, bool]:
    """Bound a result before serialization, without scanning huge values.

    Normal Gateway operations already enforce their own result limits.  This
    defensive layer also handles a faulty or synthetic dispatcher result: a
    multi-megabyte string is sliced by character count before ``json.dumps``
    sees it, and large collections are visited only up to a small item cap.
    """

    budget = max(32, int(budget))
    if depth > 8:
        return {"truncated": True}, True
    if isinstance(value, str):
        max_chars = max(1, budget // 4)
        if len(value) <= max_chars:
            return value, False
        return value[:max_chars], True
    if value is None or isinstance(value, bool):
        return value, False
    if isinstance(value, int):
        # Avoid asking the JSON encoder to materialize an adversarially large
        # decimal integer; ordinary Gateway integers are far smaller.
        if value.bit_length() > 4096:
            return {"result_type": "int", "truncated": True}, True
        return value, False
    if isinstance(value, float):
        return value, False

    if seen is None:
        seen = set()
    if isinstance(value, (dict, list, tuple, set)):
        identity = id(value)
        if identity in seen:
            return {"result_type": type(value).__name__, "truncated": True}, True
        seen.add(identity)
        try:
            if isinstance(value, dict):
                bounded: dict[str, Any] = {}
                truncated = False
                for index, (key, item) in enumerate(value.items()):
                    if index >= 256:
                        truncated = True
                        break
                    key_text = key if isinstance(key, str) else type(key).__name__
                    key_text = key_text[:256]
                    child, child_truncated = _bounded_json_value(
                        item,
                        max(32, budget // 2),
                        depth=depth + 1,
                        seen=seen,
                    )
                    bounded[key_text] = child
                    truncated = truncated or child_truncated
                    # Serialize only the already-bounded prefix.  This check
                    # keeps aggregate fitting cheap even for a huge source
                    # dictionary.
                    if _json_size(bounded) > budget:
                        bounded.pop(key_text, None)
                        truncated = True
                        break
                return bounded, truncated

            bounded_list: list[Any] = []
            truncated = False
            for index, item in enumerate(value):
                if index >= 256:
                    truncated = True
                    break
                child, child_truncated = _bounded_json_value(
                    item,
                    max(32, budget // 2),
                    depth=depth + 1,
                    seen=seen,
                )
                bounded_list.append(child)
                truncated = truncated or child_truncated
                if _json_size(bounded_list) > budget:
                    bounded_list.pop()
                    truncated = True
                    break
            return bounded_list, truncated
        finally:
            seen.discard(identity)

    # Do not call str(value): custom objects can have an unbounded or secret
    # representation.  The operation result should be JSON-like; expose only
    # its type if it is not.
    return {"result_type": type(value).__name__}, True


def _safe_result(value: Any, budget: int) -> tuple[Any, bool]:
    """Convert a bounded operation result without exposing exception text.

    Result content is intentionally not regex-redacted.  A read plan has the
    same read permission as its individual read tools; only validation errors
    and caught exceptions are kept free of argument values and raw messages.
    """

    bounded, truncated = _bounded_json_value(value, budget)
    try:
        # Only the bounded value reaches the encoder.  Keep the fallback for a
        # malformed cyclic/custom value after defensive normalization.
        _serialize_json(bounded)
        return bounded, truncated
    except Exception:
        return {"result_type": type(value).__name__, "truncated": True}, True


def _minimal_payload() -> dict[str, Any]:
    return {
        "ok": False,
        "status": "output_truncated",
        "steps_total": 0,
        "steps_completed": 0,
        "steps": [],
        "output_truncated": True,
        "output_bytes": 0,
    }


def _sync_steps_completed(payload: dict[str, Any]) -> None:
    """Keep the count consistent with the entries actually returned."""

    steps = payload.get("steps")
    if not isinstance(steps, list):
        payload["steps_completed"] = 0
        return
    payload["steps_completed"] = sum(
        isinstance(step, dict) and step.get("status") == "completed"
        for step in steps
    )


def _truncate_current_result(payload: dict[str, Any], cap: int) -> tuple[dict[str, Any], bool]:
    """Fit a newly-added step while retaining all prior step results.

    Previous results have already been checked against the same cap.  If the
    new result does not fit, replace only that result with a compact marker;
    if even the marker cannot fit, omit the current step and report the cap at
    the top level.  No prior result is discarded.
    """

    if _json_size(payload) <= cap:
        return payload, False
    steps = payload.get("steps")
    if isinstance(steps, list) and steps:
        current = steps[-1]
        if isinstance(current, dict):
            current.pop("result", None)
            current["output_truncated"] = True
            if _json_size(payload) <= cap:
                return payload, True
            # Keep previous completed step results if possible.  The current
            # marker itself is less useful than a compact top-level signal when
            # the caller selected an unusually tiny cap.
            steps.pop()
    payload["output_truncated"] = True
    if _json_size(payload) <= cap:
        return payload, True
    # Shrink only the step list as a last resort, preserving the most useful
    # prefix and the structured status fields.
    if isinstance(steps, list):
        while steps and _json_size(payload) > cap:
            steps.pop()
    if _json_size(payload) <= cap:
        return payload, True
    return _minimal_payload(), True


def _set_output_bytes(payload: dict[str, Any], cap: int) -> dict[str, Any]:
    """Fit output and set ``output_bytes`` to its exact final JSON size.

    The size field is self-referential because its decimal digits contribute to
    the JSON size.  Iterating from a zero placeholder reaches a fixed point in
    a few steps.  If adding the field would cross the cap, the newest result is
    compacted/dropped first; the returned object is always measured with the
    same serializer used for this check.
    """

    payload.pop("output_bytes", None)
    payload, truncated = _truncate_current_result(payload, cap)
    if truncated:
        payload["output_truncated"] = True
    _sync_steps_completed(payload)

    for _ in range(8):
        payload["output_bytes"] = 0
        payload, truncated = _truncate_current_result(payload, cap)
        if truncated:
            payload["output_truncated"] = True
        _sync_steps_completed(payload)
        for _ in range(8):
            final_size = _json_size(payload)
            if final_size > cap:
                break
            if payload.get("output_bytes") == final_size:
                return payload
            payload["output_bytes"] = final_size
        # Remove the field before compacting, then restart with its placeholder
        # present.  This retains the completed prefix wherever possible.
        payload.pop("output_bytes", None)
        payload, truncated = _truncate_current_result(payload, cap)
        if truncated:
            payload["output_truncated"] = True
        _sync_steps_completed(payload)

    # A cap >= MIN_MAX_RESULT_BYTES can always hold this compact fallback.  It
    # also has a stable size field, so callers can rely on the exact invariant.
    fallback = _minimal_payload()
    for _ in range(8):
        size = _json_size(fallback)
        fallback["output_bytes"] = size
        final_size = _json_size(fallback)
        if final_size == size and final_size <= cap:
            return fallback
    # Defensive last resort for unexpected serializer behavior.  The minimum
    # accepted cap makes this branch unreachable with the serializer above.
    fallback.pop("output_bytes", None)
    return fallback


def local_read_plan(
    steps: list[dict[str, Any]],
    total_timeout_sec: float = DEFAULT_TOTAL_TIMEOUT_SEC,
    step_timeout_sec: float = DEFAULT_STEP_TIMEOUT_SEC,
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> dict[str, Any]:
    """Execute a static, bounded sequence of read-only local operations.

    The plan is validated in full before any operation starts.  Operations are
    sequential and cannot refer to previous results.  On a step failure or
    timeout the completed prefix is retained and no later step is attempted.
    """

    total_timeout = _bounded_float(
        total_timeout_sec,
        name="total_timeout_sec",
        default=DEFAULT_TOTAL_TIMEOUT_SEC,
        hard_max=HARD_TOTAL_TIMEOUT_SEC,
    )
    step_timeout = _bounded_float(
        step_timeout_sec,
        name="step_timeout_sec",
        default=DEFAULT_STEP_TIMEOUT_SEC,
        hard_max=HARD_STEP_TIMEOUT_SEC,
    )
    result_cap = _bounded_int(
        max_result_bytes,
        name="max_result_bytes",
        default=DEFAULT_MAX_RESULT_BYTES,
        hard_max=HARD_MAX_RESULT_BYTES,
    )
    if result_cap < MIN_MAX_RESULT_BYTES:
        raise _invalid("max_result_bytes_too_small")
    normalized = validate_plan(steps, max_steps=max_steps)

    started = time.monotonic()
    deadline = started + total_timeout
    output: dict[str, Any] = {
        "ok": True,
        "status": "completed",
        "steps_total": len(normalized),
        "steps_completed": 0,
        "steps": [],
        "output_truncated": False,
    }
    for index, step in enumerate(normalized, start=1):
        operation = step["operation"]
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            output.update({"ok": False, "status": "timed_out", "failed_step": index, "reason": "total_timeout"})
            break
        budget = min(step_timeout, remaining)
        budget_limited_by_total = remaining < step_timeout
        step_started = time.monotonic()
        entry: dict[str, Any] = {"index": index, "operation": operation, "status": "completed"}
        try:
            result = _DISPATCH[operation](step["args"], budget)
            elapsed = time.monotonic() - step_started
            entry["duration_ms"] = round(elapsed * 1000, 3)
            if elapsed > budget or (isinstance(result, dict) and result.get("timed_out") is True):
                reason = "total_timeout" if budget_limited_by_total and elapsed >= budget else "step_timeout"
                entry.update({"status": "timed_out", "error": reason})
                output.update({"ok": False, "status": "timed_out", "failed_step": index, "reason": reason})
            else:
                bounded_result, result_truncated = _safe_result(result, result_cap)
                # Result conversion is bounded, but still belongs to the
                # operation budget.  Do not report success if serialization
                # itself consumed the remaining step/total deadline.
                conversion_elapsed = time.monotonic() - step_started
                if conversion_elapsed > budget or time.monotonic() >= deadline:
                    reason = "total_timeout" if budget_limited_by_total and time.monotonic() >= deadline else "step_timeout"
                    entry.update({"status": "timed_out", "error": reason})
                    output.update({"ok": False, "status": "timed_out", "failed_step": index, "reason": reason})
                else:
                    entry["result"] = bounded_result
                    if result_truncated:
                        entry["result_truncated"] = True
                    output["steps_completed"] = index
        except LocalReadPlanTimeout:
            elapsed = time.monotonic() - step_started
            reason = "total_timeout" if budget_limited_by_total else "step_timeout"
            entry.update({
                "status": "timed_out",
                "duration_ms": round(elapsed * 1000, 3),
                "error": reason,
            })
            output.update({"ok": False, "status": "timed_out", "failed_step": index, "reason": reason})
        except Exception as exc:
            elapsed = time.monotonic() - step_started
            entry.update({
                "status": "failed",
                "duration_ms": round(elapsed * 1000, 3),
                "error": "step_failed",
                "error_type": type(exc).__name__,
            })
            output.update({"ok": False, "status": "failed", "failed_step": index, "reason": "step_failed"})

        output["steps"].append(entry)
        output, truncated = _truncate_current_result(output, result_cap)
        if truncated:
            output["output_truncated"] = True
        _sync_steps_completed(output)
        if output.get("status") != "completed":
            break

    output["duration_sec"] = round(time.monotonic() - started, 6)
    output = _set_output_bytes(output, result_cap)
    return output


TOOLS = [local_read_plan]
