"""Safe, bounded diagnostic projections shared by both AoiTalk task status tools."""
from __future__ import annotations

import re
from itertools import islice
from typing import Any

MAX_DIAGNOSTIC_ITEMS = 10
MAX_DIAGNOSTIC_COUNT = 2_147_483_647
_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$")
_RECEIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_COUNT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}_count$")
_COUNT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
_ENTRY_FIELDS = {"tool", "reason", "denied", "receipt_id"}
_ENTRY_GROUPS = {
    "diagnostic", "recovered", "refused", "unresolved", "recovered_tools", "unresolved_tools",
    "tools", "failures", "items",
}


def _safe_code(value: Any, limit: int = 160, *, receipt: bool = False) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    pattern = _RECEIPT_RE if receipt else _CODE_RE
    return value if pattern.fullmatch(value) else None


def _count(value: Any) -> int | None:
    if (isinstance(value, bool) or not isinstance(value, int)
            or value < 0 or value > MAX_DIAGNOSTIC_COUNT):
        return None
    return value


def _tool_entry(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    if "tool" in value:
        result["tool"] = _safe_code(value.get("tool"), 80)
    if "reason" in value:
        result["reason"] = _safe_code(value.get("reason"))
    if isinstance(value.get("denied"), bool):
        result["denied"] = value["denied"]
    if "receipt_id" in value:
        result["receipt_id"] = _safe_code(value.get("receipt_id"), 128, receipt=True)
    return result or None


def _tool_entries(value: Any) -> list[Any]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value[:MAX_DIAGNOSTIC_ITEMS]:
        if isinstance(item, str):
            name = _safe_code(item, 80)
            if name is not None:
                result.append(name)
        else:
            entry = _tool_entry(item)
            if entry is not None:
                result.append(entry)
    return result


def project_failure(value: Any) -> dict[str, Any] | None:
    """Keep framework failure codes and tool names; never pass arguments or output."""
    if not isinstance(value, dict):
        return None
    unresolved = _tool_entries(value.get("unresolved_tools"))
    # Failure's unresolved_tools historically contains entry objects. Drop bare
    # names there so its output shape remains stable for existing consumers.
    unresolved = [entry for entry in unresolved if isinstance(entry, dict)]
    successful = [name for name in _tool_entries(value.get("successful_tools"))
                  if isinstance(name, str)]
    successful_count = _count(value.get("successful_tool_count"))
    return {
        "reason": _safe_code(value.get("reason"), 96),
        "unresolved_tools": unresolved,
        "successful_tool_count": successful_count if successful_count is not None else 0,
        "successful_tools": successful,
    }


def _project_tool_failure_map(value: dict[str, Any], depth: int = 0) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, child in islice(value.items(), 100):
        if not isinstance(key, str) or len(key) > 48:
            continue
        if key in _ENTRY_FIELDS:
            entry = _tool_entry({key: child})
            if entry is not None and key in entry:
                result[key] = entry[key]
        elif _COUNT_KEY_RE.fullmatch(key):
            count = _count(child)
            if count is not None:
                result[key] = count
        elif key == "counts" and depth < 2 and isinstance(child, dict):
            counts = {name: count for name, raw in islice(child.items(), 50)
                      if isinstance(name, str) and _COUNT_NAME_RE.fullmatch(name)
                      and (count := _count(raw)) is not None}
            if counts:
                result[key] = counts
        elif key in _ENTRY_GROUPS:
            if isinstance(child, list):
                entries = _tool_entries(child)
                if entries:
                    result[key] = entries
            elif isinstance(child, dict) and depth == 0:
                nested = _project_tool_failure_map(child, depth + 1)
                if nested:
                    result[key] = nested
            elif isinstance(child, int) and not isinstance(child, bool):
                count = _count(child)
                if count is not None:
                    result[key] = count
    return result


def project_tool_failures(value: Any) -> dict[str, Any] | None:
    """Keep bounded counts and allowlisted tool failure receipts only."""
    if not isinstance(value, dict):
        return None
    result = _project_tool_failure_map(value)
    return result or None


def project_diagnostics(task: dict[str, Any]) -> dict[str, Any]:
    """Return only safe optional diagnostics; malformed detail never rejects status."""
    result: dict[str, Any] = {}
    if "failure" in task and task.get("failure") is not None:
        failure = project_failure(task.get("failure"))
        if failure is not None:
            result["failure"] = failure
    if "tool_failures" in task and task.get("tool_failures") is not None:
        tool_failures = project_tool_failures(task.get("tool_failures"))
        if tool_failures is not None:
            result["tool_failures"] = tool_failures
    return result
