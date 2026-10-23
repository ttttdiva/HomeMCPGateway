"""Durable per-job steering mailbox; the supervisor is the sole state writer.

The public gateway publishes immutable requests. Worker state and native transport
handles never appear in public receipts.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Iterator
import uuid

MAX_TEXT_BYTES = 32 * 1024
MAX_PENDING = 8
TERMINAL = {"accepted_by_runtime", "rejected"}
STATES = {"queued", "sending", *TERMINAL}
REASONS = {"stopping", "job_stopping", "job_finished", "worker_stopped", "no_active_turn", "stale_turn",
           "runtime_rejected", "runtime_error", "runtime_unavailable", "connection_lost",
           "guard_closed", "guard_mismatch", "guard_unavailable", "request_invalid",
           "mailbox_closed", "not_delivered", "delivery_uncertain", "worker_gone"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def validate_key(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 255 or "\x00" in value:
        raise ValueError("invalid_idempotency_key")
    value.encode("utf-8", errors="strict")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def validate_text(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8", errors="strict")) > MAX_TEXT_BYTES:
        raise ValueError("invalid_steer_text")
    return value


def validate_turn(value: Any) -> str:
    if not isinstance(value, str) or not value or "\x00" in value or len(value.encode("utf-8", errors="strict")) > 1024:
        raise ValueError("invalid_expected_turn_id")
    return value


def _mailbox(directory: Path) -> Path:
    return Path(directory) / "steering"


def _sync_directory(directory: Path) -> None:
    if os.name != "nt":
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def publish_json(path: Path, value: dict[str, Any]) -> bool:
    """Publish a complete fsynced object exclusively, never replace a winner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            if os.name == "nt":
                os.rename(temp, path)  # Windows rename fails when destination exists.
            else:
                os.link(temp, path)  # Atomic exclusive publication of complete data.
        except FileExistsError:
            return False
        _sync_directory(path.parent)
        return True
    finally:
        temp.unlink(missing_ok=True)


def _replace_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("xb") as stream:
            stream.write(_canonical(value))
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.025)
        _sync_directory(path.parent)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    for attempt in range(20):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError("invalid_mailbox_record")
            return value
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.025)
    raise AssertionError("unreachable")


@contextmanager
def admission_lock(directory: Path, timeout: float = 2.0) -> Iterator[None]:
    """OS released cross-process lock; a crashed publisher cannot strand it."""
    mailbox = _mailbox(directory)
    mailbox.mkdir(parents=True, exist_ok=True)
    with (mailbox / "admission.lock").open("a+b") as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b"\0")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("steer_mailbox_busy")
                time.sleep(.01)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def mailbox_closed(directory: Path) -> bool:
    return (Path(directory) / "mailbox.closed").exists()


def close_mailbox(directory: Path, reason: str = "stopping") -> dict[str, Any]:
    if reason not in REASONS:
        raise ValueError("invalid_steer_reason")
    path = Path(directory) / "mailbox.closed"
    # Closing cannot wait on a publisher's admission lock: the fence must be
    # present before cleanup even if that publisher stalls. Publishers recheck
    # this fence after their complete exclusive publication.
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _read_json(path)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_canonical({"version": 1, "closed_at": time.time(), "reason": reason}))
        stream.flush()
        os.fsync(stream.fileno())
    _sync_directory(path.parent)
    return _read_json(path)


def request_digest(job_id: str, expected_turn_id: str, text: str) -> str:
    return hashlib.sha256(_canonical({"job_id": job_id, "expected_turn_id": expected_turn_id, "text": text})).hexdigest()


def client_user_message_id(job_id: str, request_id: str, digest: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"home-mcp-steer:{job_id}:{request_id}:{digest}"))


def make_request(job_id: str, expected_turn_id: str, text: str, idempotency_key: str) -> dict[str, Any]:
    if not isinstance(job_id, str) or str(uuid.UUID(job_id)) != job_id:
        raise ValueError("invalid_job_id")
    request_id = validate_key(idempotency_key)
    validate_turn(expected_turn_id)
    validate_text(text)
    digest = request_digest(job_id, expected_turn_id, text)
    return {"version": 1, "request_id": request_id, "key_hash": request_id, "job_id": job_id,
            "expected_turn_id": expected_turn_id, "text": text, "request_digest": digest,
            "client_user_message_id": client_user_message_id(job_id, request_id, digest), "created_at": time.time()}


def request_path(directory: Path, request_id: str) -> Path:
    if not _sha(request_id):
        raise ValueError("invalid_request_id")
    return _mailbox(directory) / "requests" / f"{request_id}.json"


def validate_request(value: Any, *, job_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"version", "request_id", "key_hash", "job_id", "expected_turn_id", "text", "request_digest", "client_user_message_id", "created_at"}:
        raise ValueError("invalid_mailbox_request")
    if value["version"] != 1 or not _sha(value["request_id"]) or value["key_hash"] != value["request_id"]:
        raise ValueError("invalid_mailbox_request")
    if str(uuid.UUID(value["job_id"])) != value["job_id"] or (job_id is not None and value["job_id"] != job_id):
        raise ValueError("invalid_mailbox_request")
    validate_turn(value["expected_turn_id"])
    validate_text(value["text"])
    digest = request_digest(value["job_id"], value["expected_turn_id"], value["text"])
    if (value["request_digest"] != digest or value["client_user_message_id"] != client_user_message_id(value["job_id"], value["request_id"], digest)
            or type(value["created_at"]) not in (int, float) or not math.isfinite(value["created_at"])):
        raise ValueError("invalid_mailbox_request")
    return value


def read_request(directory: Path, request_id: str) -> dict[str, Any] | None:
    try:
        value = validate_request(_read_json(request_path(directory, request_id)), job_id=Path(directory).name)
    except FileNotFoundError:
        return None
    if value["request_id"] != request_id:
        raise ValueError("invalid_mailbox_request")
    return value


def enumerate_requests(directory: Path, *, strict: bool = False) -> list[dict[str, Any]]:
    """Ignore incomplete temporary files and malformed records; never execute them."""
    result = []
    for path in (_mailbox(directory) / "requests").glob("*.json"):
        try:
            value = read_request(directory, path.stem)
            if value is not None:
                result.append(value)
        except (ValueError, OSError, UnicodeError, TypeError):
            if strict:
                raise ValueError("invalid_mailbox_request")
            continue
    return sorted(result, key=lambda value: (value["created_at"], value["request_id"]))


def read_state(directory: Path, request_id: str) -> dict[str, Any] | None:
    if not _sha(request_id):
        raise ValueError("invalid_request_id")
    try:
        value = _read_json(_mailbox(directory) / "states" / f"{request_id}.json")
    except FileNotFoundError:
        return None
    if value.get("request_id") != request_id or value.get("state") not in STATES or not _sha(value.get("request_digest")):
        raise ValueError("invalid_mailbox_state")
    if value["state"] in {"sending", "accepted_by_runtime"} and (
            not isinstance(value.get("connection_id"), str) or not value["connection_id"]
            or type(value.get("rpc_id")) not in (str, int)
            or not isinstance(value.get("client_user_message_id"), str)
            or type(value.get("sending_at")) not in (int, float)):
        raise ValueError("invalid_mailbox_state")
    return value


def write_state(directory: Path, request: dict[str, Any], state: str, **fields: Any) -> dict[str, Any]:
    """Worker only: persist sending before RPC; terminal results cannot be changed."""
    validate_request(request, job_id=Path(directory).name)
    if set(fields) - {"connection_id", "rpc_id", "request_digest", "client_user_message_id", "reason"}:
        raise ValueError("invalid_steer_state_fields")
    if fields.get("request_digest", request["request_digest"]) != request["request_digest"]:
        raise ValueError("steer_request_mismatch")
    if fields.get("client_user_message_id", request["client_user_message_id"]) != request["client_user_message_id"]:
        raise ValueError("steer_request_mismatch")
    old = read_state(directory, request["request_id"])
    previous = old["state"] if old else "queued"
    if state not in STATES or (previous in TERMINAL and state != previous) or (previous == "sending" and state == "queued"):
        raise ValueError("invalid_steer_transition")
    if old and old["request_digest"] != request["request_digest"]:
        raise ValueError("steer_request_mismatch")
    if previous in TERMINAL:
        return old
    if state in TERMINAL and previous == "sending" and any(
            key in fields and fields[key] != old.get(key) for key in ("connection_id", "rpc_id", "client_user_message_id")):
        raise ValueError("steer_request_mismatch")
    reason = fields.get("reason")
    if reason is not None and reason not in REASONS:
        raise ValueError("invalid_steer_reason")
    if state == "sending":
        if previous != "queued":
            raise ValueError("steer_already_sending")
        if (not isinstance(fields.get("connection_id"), str) or not fields["connection_id"]
                or type(fields.get("rpc_id")) not in (str, int)
                or fields.get("request_digest", request["request_digest"]) != request["request_digest"]
                or fields.get("client_user_message_id", request["client_user_message_id"]) != request["client_user_message_id"]):
            raise ValueError("missing_steer_send_evidence")
    if state == "accepted_by_runtime" and previous != "sending":
        raise ValueError("invalid_steer_transition")
    now = time.time()
    result = {**(old or {}), "version": 1, "request_id": request["request_id"], "state": state,
              "request_digest": request["request_digest"], "created_at": request["created_at"], "updated_at": now}
    result.update(fields)
    if state == "sending":
        result.update(sending_at=now, client_user_message_id=request["client_user_message_id"], request_digest=request["request_digest"])
    if state in TERMINAL:
        result["finished_at"] = now
    _replace_json(_mailbox(directory) / "states" / f"{request['request_id']}.json", result)
    return result


def pending_count(directory: Path) -> int:
    count = 0
    for request in enumerate_requests(directory, strict=True):
        state = read_state(directory, request["request_id"])
        if state is None or state["state"] not in TERMINAL:
            count += 1
    return count


def steer_evidence(directory: Path) -> list[dict[str, Any]]:
    result = []
    for request in enumerate_requests(directory, strict=True):
        state = read_state(directory, request["request_id"])
        value = {"request_id": request["request_id"], "request_digest": request["request_digest"],
                 "job_id": request["job_id"], "expected_turn_id": request["expected_turn_id"],
                 "created_at": request["created_at"], "state": state["state"] if state else "queued"}
        if state and state.get("sending_at") is not None:
            if state.get("client_user_message_id") != request["client_user_message_id"]:
                raise ValueError("steer_evidence_mismatch")
            value.update({key: state[key] for key in ("client_user_message_id", "sending_at", "connection_id", "rpc_id", "reason", "recorded", "recorded_turn_id") if key in state})
        result.append(value)
    return sorted(result, key=lambda value: (value.get("sending_at", float("inf")), value["created_at"], value["request_id"]))


def record_native_item(directory: Path, request: dict[str, Any], *, turn_id: str, client_id: str) -> dict[str, Any]:
    """Worker only: record an exact native client ID, never infer recording from RPC success."""
    validate_request(request, job_id=Path(directory).name)
    state = read_state(directory, request["request_id"])
    if (not state or state["state"] != "accepted_by_runtime" or client_id != request["client_user_message_id"]
            or state.get("client_user_message_id") != client_id or turn_id != request["expected_turn_id"]):
        raise ValueError("steer_recording_mismatch")
    if state.get("recorded") and state.get("recorded_turn_id") != turn_id:
        raise ValueError("steer_recording_mismatch")
    state.update(recorded=True, recorded_turn_id=turn_id)
    _replace_json(_mailbox(directory) / "states" / f"{request['request_id']}.json", state)
    return state


def public_receipt(request: dict[str, Any] | None, state: dict[str, Any] | None, *, closed: bool,
                   worker_alive: bool, connection_id: str | None, connection_alive: bool) -> dict[str, Any]:
    if request is None:
        return {"ok": True, "state": "not_delivered" if closed or not worker_alive else "not_found"}
    if state and state.get("request_digest") != request["request_digest"]:
        raise ValueError("steer_receipt_mismatch")
    effective = state["state"] if state else "queued"
    reason = state.get("reason") if state else None
    if effective == "sending" and (not worker_alive or not connection_alive or state.get("connection_id") != connection_id):
        effective, reason = "delivery_uncertain", "delivery_uncertain"
    elif effective == "queued" and (closed or not worker_alive):
        effective, reason = "not_delivered", "not_delivered"
    result = {"ok": True, "state": effective, "request_digest": request["request_digest"], "created_at": request["created_at"]}
    if state:
        for key in ("updated_at", "sending_at", "finished_at"):
            if type(state.get(key)) in (int, float):
                result[key] = state[key]
    if reason in REASONS:
        result["reason"] = reason
    return result
