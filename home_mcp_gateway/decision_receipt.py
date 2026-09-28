"""Bounded, privacy-safe telemetry for semantic decisions and Host outcomes."""
from __future__ import annotations

from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
import hashlib
import json
import math
import os
import re
from threading import RLock
from typing import Any, Iterable, Mapping
from uuid import uuid4

DECISION_CONTRACT_VERSION = "1"
MAX_RECENT_RECEIPTS = 2048
_RECENT_RECEIPTS: OrderedDict[str, "DecisionReceipt"] = OrderedDict()
_RECEIPT_LOCK = RLock()


def decision_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sensitive_values(values: Iterable[str] = ()) -> tuple[str, ...]:
    environment = (
        value for key, value in os.environ.items()
        if any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"))
    )
    return tuple({value for value in (*environment, *values) if isinstance(value, str) and value})


def _code(value: Any, secrets: tuple[str, ...] = ()) -> str:
    return value if (
        isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_.:-]{1,80}", value)
        and not any(secret in value for secret in secrets)
    ) else ""


def _candidate_id(value: str, secrets: tuple[str, ...] = ()) -> str:
    return _code(value, secrets) or "id_" + decision_digest(value)


def redact_decision_metadata(value: Any, *, sensitive_values: Iterable[str] = ()) -> Any:
    """Remove known credentials from the existing decision trace, including keys."""
    secrets = _sensitive_values(sensitive_values)

    def redact(item: Any) -> Any:
        if isinstance(item, str):
            return "[redacted]" if any(secret in item for secret in secrets) else item
        if isinstance(item, Mapping):
            return {redact(key): redact(child) for key, child in item.items()}
        if isinstance(item, (list, tuple)):
            return [redact(child) for child in item]
        return item

    return redact(value)


def _probability(value: Any) -> float | None:
    if type(value) in {int, float} and math.isfinite(value) and 0 <= value <= 1:
        return float(value)
    return None


@dataclass(frozen=True)
class DecisionReceipt:
    decision_id: str
    created_at: str
    decision_kind: str
    contract_version: str
    state_digest: str
    candidate_set_digest: str
    candidate_ids: tuple[str, ...]
    selected_candidate: str = ""
    probabilities: Mapping[str, float] = field(default_factory=dict)
    confidence: float | None = None
    threshold: float | None = None
    engine: str = ""
    fallback_reason: str = ""
    latency_ms: float = 0.0
    validation_result: str = "decision_only"
    host_action: str = ""
    host_candidate: str = ""
    outcome: str = "pending"

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["candidate_ids"] = list(self.candidate_ids)
        return result


def create_decision_receipt(
    *, decision_kind: str, state: Any, candidates: Mapping[str, Any],
    selected_candidate: str = "", probabilities: Mapping[str, Any] | None = None,
    confidence: float | None = None, threshold: float | None = None,
    engine: str = "jev", fallback_reason: str = "", latency_ms: float = 0.0,
    validation_result: str = "decision_only",
    sensitive_values: Iterable[str] = (),
) -> DecisionReceipt:
    secrets = _sensitive_values(sensitive_values)
    raw_ids = tuple(candidates)[:200]
    allowed = set(raw_ids) | {"none"}
    safe_probabilities = {}
    for key, value in (probabilities or {}).items():
        probability = _probability(value)
        if key in allowed and probability is not None:
            safe_probabilities[_candidate_id(key, secrets)] = probability
    return DecisionReceipt(
        decision_id=uuid4().hex,
        created_at=datetime.now(timezone.utc).isoformat(),
        decision_kind=_code(decision_kind, secrets), contract_version=DECISION_CONTRACT_VERSION,
        state_digest=decision_digest(state), candidate_set_digest=decision_digest(candidates),
        candidate_ids=tuple(_candidate_id(key, secrets) for key in raw_ids),
        selected_candidate=_candidate_id(selected_candidate, secrets) if selected_candidate in allowed else "",
        probabilities=safe_probabilities, confidence=_probability(confidence),
        threshold=_probability(threshold), engine=_code(engine, secrets),
        fallback_reason=_code(fallback_reason, secrets),
        latency_ms=max(0.0, float(latency_ms)) if type(latency_ms) in {int, float} and math.isfinite(latency_ms) else 0.0,
        validation_result=_code(validation_result, secrets),
    )


def record_decision_receipt(receipt: DecisionReceipt) -> DecisionReceipt:
    with _RECEIPT_LOCK:
        _RECENT_RECEIPTS[receipt.decision_id] = receipt
        _RECENT_RECEIPTS.move_to_end(receipt.decision_id)
        while len(_RECENT_RECEIPTS) > MAX_RECENT_RECEIPTS:
            _RECENT_RECEIPTS.popitem(last=False)
    return receipt


def finalize_decision_receipt(
    receipt: DecisionReceipt, *, outcome: str,
    validation_result: str | None = None, host_action: str | None = None,
    host_candidate: str | None = None, threshold: float | None = None,
    sensitive_values: Iterable[str] = (),
) -> DecisionReceipt:
    secrets = _sensitive_values(sensitive_values)
    candidate = _candidate_id(host_candidate, secrets) if host_candidate else ""
    updated = replace(
        receipt, outcome=_code(outcome, secrets),
        validation_result=_code(validation_result, secrets) if validation_result is not None else receipt.validation_result,
        host_action=_code(host_action, secrets) if host_action is not None else receipt.host_action,
        host_candidate=(candidate if candidate in receipt.candidate_ids else "") if host_candidate is not None else receipt.host_candidate,
        threshold=_probability(threshold) if threshold is not None else receipt.threshold,
    )
    return record_decision_receipt(updated)


def recent_decision_receipts(limit: int = 100) -> list[dict[str, Any]]:
    with _RECEIPT_LOCK:
        count = max(0, min(int(limit), len(_RECENT_RECEIPTS)))
        return [row.to_dict() for row in list(_RECENT_RECEIPTS.values())[-count:]] if count else []


def summarize_decision_receipts(
    receipts: Iterable[DecisionReceipt] | None = None,
) -> dict[str, Any]:
    with _RECEIPT_LOCK:
        rows = list({row.decision_id: row for row in (receipts if receipts is not None else _RECENT_RECEIPTS.values())}.values())
    return {
        "count": len(rows),
        "by_kind": dict(Counter(row.decision_kind for row in rows)),
        "outcomes": dict(Counter(row.outcome for row in rows)),
        "validation_results": dict(Counter(row.validation_result for row in rows)),
    }
