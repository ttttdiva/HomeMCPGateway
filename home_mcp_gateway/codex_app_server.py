"""Offline verification of one dedicated Codex app-server protocol journal.

This module never starts a process or contacts a provider.  Human stdout is not
protocol evidence.  Native identifiers are returned only for private callers;
public projections must use the digests and fixed codes instead.
"""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
import os
from typing import Any

MAX_JOURNAL_BYTES = 64 << 20
MAX_FRAME_BYTES = 1 << 20
TRANSPORT = "codex_app_server"
_BENIGN_ACCOUNT_NOTIFICATIONS = {"account/updated", "account/rateLimits/updated"}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8", errors="strict")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512 and "\x00" not in value


def _rpc_id(value: Any) -> bool:
    return (type(value) is int and 0 <= value < 2 ** 63) or _identifier(value)


def _same_path(left: Any, right: Any) -> bool:
    if not isinstance(left, str) or not isinstance(right, str) or "\x00" in left or "\x00" in right:
        return False
    try:
        return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))
    except (OSError, ValueError):
        return False


def parse_appserver_journal(data: bytes, spec: dict[str, Any], expected_thread_id: str | None = None,
                            expected_turn_id: str | None = None, steer_evidence: Any = None) -> dict[str, Any]:
    """Verify native start/policy/terminal proof and the complete accepted input set.

    Journal envelopes are exactly ``{connection_id,timestamp,direction,frame}``,
    with directions ``send`` / ``receive``.  Mailbox evidence is the private list
    returned by ``codex_steer_mailbox.steer_evidence``; only hashes and counts are
    exposed in the returned input provenance.
    """
    base: dict[str, Any] = {"state": "result_pending", "transport": TRANSPORT,
                            "input_set_uncertain": False}
    if not isinstance(data, bytes) or len(data) > MAX_JOURNAL_BYTES:
        return {**base, "code": "result_too_large"}
    base.update(log_digest=hashlib.sha256(data).hexdigest(), log_bytes=len(data))

    def pending(code: str) -> dict[str, Any]:
        return {**base, "code": code}

    try:
        # Reuse the admission validator: the verifier must never broaden policy.
        from .work_dispatcher_codex import validate_app_server_spec
        pinned = validate_app_server_spec(spec)
        base["spec_digest"] = _digest(pinned)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return pending("app_server_spec_invalid")
    if not data or not data.endswith(b"\n"):
        return pending("partial_rpc_journal")
    records: list[dict[str, Any]] = []
    try:
        for line in data.splitlines():
            if not line.strip():
                continue
            # Bound before decoding as well as checking the native frame itself.
            if len(line) > MAX_FRAME_BYTES + 4096:
                return pending("rpc_frame_too_large")
            record = json.loads(line.decode("utf-8", errors="strict"), object_pairs_hook=_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite")))
            if not isinstance(record, dict) or set(record) != {"connection_id", "timestamp", "direction", "frame"}:
                return pending("rpc_journal_invalid")
            if (not _identifier(record["connection_id"]) or record["direction"] not in {"send", "receive"}
                    or not isinstance(record["timestamp"], str)):
                return pending("rpc_journal_invalid")
            stamp = datetime.fromisoformat(record["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                return pending("rpc_journal_invalid")
            frame = record["frame"]
            if not isinstance(frame, dict) or len(_canonical(frame)) > MAX_FRAME_BYTES:
                return pending("rpc_frame_too_large" if isinstance(frame, dict) else "rpc_journal_invalid")
            if frame.get("redacted") is True:
                benign_notification = (record["direction"] == "receive" and set(frame) == {"method", "redacted"}
                                       and isinstance(frame.get("method"), str)
                                       and frame["method"] in _BENIGN_ACCOUNT_NOTIFICATIONS)
                if not benign_notification and (not _rpc_id(frame.get("id")) or set(frame) - {"id", "method", "redacted"}):
                    return pending("rpc_journal_invalid")
                if "method" in frame and not _identifier(frame["method"]):
                    return pending("rpc_journal_invalid")
            elif "method" in frame:
                if not _identifier(frame["method"]) or ("id" in frame and not _rpc_id(frame["id"])):
                    return pending("rpc_journal_invalid")
                if "result" in frame or "error" in frame or ("params" in frame and not isinstance(frame["params"], dict)):
                    return pending("rpc_journal_invalid")
            elif not (_rpc_id(frame.get("id")) and (("result" in frame) != ("error" in frame))):
                return pending("rpc_journal_invalid")
            elif "error" in frame and (not isinstance(frame["error"], dict)
                    or type(frame["error"].get("code")) is not int or not isinstance(frame["error"].get("message"), str)):
                return pending("rpc_journal_invalid")
            records.append(record)
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        return pending("rpc_journal_invalid")
    if not records or len({r["connection_id"] for r in records}) != 1:
        return pending("rpc_connection_invalid")
    connection_id = records[0]["connection_id"]
    sends: dict[Any, tuple[int, dict[str, Any]]] = {}
    responses: dict[Any, tuple[int, dict[str, Any]]] = {}
    server_requests: dict[Any, tuple[int, dict[str, Any]]] = {}
    server_responses: set[Any] = set()
    last_client_id = -1
    for index, record in enumerate(records):
        frame = record["frame"]
        method = frame.get("method")
        benign_notification = (record["direction"] == "receive" and "id" not in frame
                               and frame.get("redacted") is True and method in _BENIGN_ACCOUNT_NOTIFICATIONS)
        if not benign_notification and isinstance(method, str) and (method.startswith(("account/", "auth/")) or method == "attestation/generate"):
            return {**base, "state": "failed", "code": "appserver_auth_required"}
        if "id" not in frame:
            if record["direction"] == "send" and frame != {"method": "initialized"}:
                return pending("rpc_protocol_invalid")
            continue
        if record["direction"] == "send" and "method" in frame:
            if (type(frame["id"]) is not int or frame["id"] <= last_client_id or frame["id"] in sends
                    or method not in {"initialize", "thread/start", "turn/start", "turn/steer", "turn/interrupt"}):
                return pending("rpc_request_identity_invalid")
            last_client_id = frame["id"]
            sends[frame["id"]] = (index, frame)
        elif record["direction"] == "receive" and "method" not in frame:
            if frame["id"] not in sends or frame["id"] in responses or frame.get("redacted") is True:
                return pending("rpc_response_identity_invalid")
            responses[frame["id"]] = (index, frame)
        elif record["direction"] == "receive" and "method" in frame:
            if frame["id"] in server_requests:
                return pending("rpc_request_identity_invalid")
            server_requests[frame["id"]] = (index, frame)
        elif record["direction"] == "send" and "method" not in frame:
            if frame["id"] not in server_requests or frame["id"] in server_responses:
                return pending("rpc_response_identity_invalid")
            server_responses.add(frame["id"])
    if set(server_requests) != server_responses:
        return pending("rpc_server_response_missing")

    def exchange(method: str) -> tuple[int, dict[str, Any], int, dict[str, Any]] | None:
        requests = [(index, frame) for index, frame in sends.values() if frame.get("method") == method]
        if len(requests) != 1:
            return None
        index, request = requests[0]
        response = responses.get(request["id"])
        if (response is None or response[0] <= index or "error" in response[1]
                or not isinstance(response[1].get("result"), dict)):
            return None
        return index, request.get("params", {}), response[0], response[1]["result"]

    initialized = exchange("initialize")
    started = exchange("thread/start")
    turn_started = exchange("turn/start")
    if initialized is None:
        return pending("rpc_initialize_missing")
    if (not isinstance(initialized[1], dict) or set(initialized[1]) != {"clientInfo"}
            or not isinstance(initialized[1]["clientInfo"], dict)):
        return pending("rpc_initialize_invalid")
    if started is None or started[0] <= initialized[2]:
        return pending("native_thread_start_missing")
    if not any(r["direction"] == "send" and r["frame"] == {"method": "initialized"}
               and initialized[2] < i < started[0] for i, r in enumerate(records)):
        return pending("rpc_initialize_missing")
    request, response = started[1], started[3]
    if not isinstance(request, dict):
        return pending("native_policy_mismatch")
    expected = {"model": pinned["model"], "modelProvider": pinned["model_provider"],
                "sandbox": pinned["sandbox"], "approvalPolicy": pinned["approval_policy"],
                "approvalsReviewer": pinned["approvals_reviewer"]}
    if (any(request.get(key) != value for key, value in expected.items())
            or set(request) != set(expected) | {"cwd", "config"}
            or not _same_path(request.get("cwd"), pinned["cwd"])
            or not isinstance(request.get("config"), dict)
            or set(request["config"]) != {"model_reasoning_effort"}
            or request["config"].get("model_reasoning_effort") != pinned["effort"]):
        return pending("native_policy_mismatch")
    if (any(response.get(key) != value for key, value in expected.items() if key != "sandbox")
            or response.get("reasoningEffort") != pinned["effort"]
            or not _same_path(response.get("cwd"), pinned["cwd"])
            or not isinstance(response.get("sandbox"), dict)
            or set(response["sandbox"]) - {"type", "networkAccess"}
            or response["sandbox"].get("type") != "readOnly"
            or response["sandbox"].get("networkAccess", False) is not False):
        return pending("native_policy_mismatch")
    thread = response.get("thread")
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not _identifier(thread_id):
        return pending("thread_identity_invalid")
    if expected_thread_id is not None and thread_id != expected_thread_id:
        return pending("thread_identity_mismatch")
    if turn_started is None or turn_started[0] <= started[2]:
        return pending("native_turn_start_missing")
    turn_request, turn_response = turn_started[1], turn_started[3]
    if (not isinstance(turn_request, dict) or turn_request.get("threadId") != thread_id
            or turn_request.get("input") != [{"type": "text", "text": pinned["input_text"]}]
            or turn_request.get("effort") != pinned["effort"]):
        return pending("native_turn_spec_mismatch")
    # Overrides beyond the pinned start are not allowed to silently change policy.
    turn_allowed = {"threadId", "input", "effort"}
    if set(turn_request) != turn_allowed:
        return pending("native_turn_spec_mismatch")
    native_turn = turn_response.get("turn")
    turn_id = native_turn.get("id") if isinstance(native_turn, dict) else None
    if not _identifier(turn_id):
        return pending("turn_identity_invalid")
    if expected_turn_id is not None and turn_id != expected_turn_id:
        return pending("turn_identity_mismatch")
    notifications = [(i, r["frame"].get("method"), r["frame"].get("params"))
                     for i, r in enumerate(records) if r["direction"] == "receive" and "method" in r["frame"]]
    turn_starts = [i for i, method, params in notifications if method == "turn/started" and isinstance(params, dict)
                   and params.get("threadId") == thread_id and isinstance(params.get("turn"), dict)
                   and params["turn"].get("id") == turn_id and i > turn_started[0]]
    if len(turn_starts) != 1:
        return pending("native_turn_start_missing")
    terminals = [(i, params) for i, method, params in notifications if method == "turn/completed"
                 and isinstance(params, dict) and params.get("threadId") == thread_id
                 and isinstance(params.get("turn"), dict) and params["turn"].get("id") == turn_id]
    if len(terminals) != 1 or terminals[0][0] <= max(turn_starts[0], turn_started[2]):
        return pending("native_terminal_missing")
    terminal_index, terminal = terminals[0]
    if not any(method == "thread/started" and isinstance(params, dict)
               and isinstance(params.get("thread"), dict) and params["thread"].get("id") == thread_id
               and started[0] < i < terminal_index for i, method, params in notifications):
        return pending("native_thread_start_missing")
    outcome = terminal["turn"].get("status")
    if not isinstance(outcome, str) or outcome not in {"completed", "failed", "interrupted"}:
        return pending("native_terminal_invalid")
    base.update(thread_id=thread_id, turn_id=turn_id, turn_count=1, native_outcome=outcome,
                native_terminal_digest=_digest(records[terminal_index]))
    approval_methods = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval",
                        "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval"}
    unexpected_requests = sum(1 for r in records if r["direction"] == "receive"
                              and r["frame"].get("method") in approval_methods and "id" in r["frame"])
    base["approval_request_count"] = unexpected_requests
    if unexpected_requests:
        base["policy_code"] = "policy_unexpected_request"

    # Every durable send contributes state and exact independently checked proof.
    steer_rows = [] if steer_evidence is None else steer_evidence
    if not isinstance(steer_rows, list):
        base["input_set_uncertain"] = True
        return pending("steer_evidence_invalid")
    provenance: list[dict[str, Any]] = []
    seen_rpc: set[Any] = set()
    seen_requests: set[str] = set()
    uncertain = False
    from .job_steering import REASONS as steer_reasons
    writer_unsent_reasons = {"stopping", "job_finished", "guard_closed", "guard_mismatch", "guard_unavailable"}
    for row in steer_rows:
        if not isinstance(row, dict):
            base["input_set_uncertain"] = True
            return pending("steer_evidence_invalid")
        request_id, digest, state = row.get("request_id"), row.get("request_digest"), row.get("state")
        rpc_id, client_id = row.get("rpc_id"), row.get("client_user_message_id")
        if (not _identifier(request_id) or request_id in seen_requests or not isinstance(digest, str)
                or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest)
                or not isinstance(state, str)
                or state not in {"queued", "sending", "accepted_by_runtime", "rejected", "not_delivered", "delivery_uncertain"}):
            base["input_set_uncertain"] = True
            return pending("steer_evidence_invalid")
        seen_requests.add(request_id)
        entry: dict[str, Any] = {"request_digest": digest, "state": state, "recorded_proof": None}
        reason = row.get("reason")
        if isinstance(reason, str) and reason in steer_reasons:
            entry["reason"] = reason
        native = sends.get(rpc_id) if _rpc_id(rpc_id) else None
        # The writer persists sending intent before its final gate.  A refusal
        # at that gate is durable known nondelivery even though sending_at and
        # the reserved RPC ID already exist; an actual native send still needs
        # a correlated rejection response below.
        writer_refused = (state == "rejected" and native is None
                          and isinstance(reason, str) and reason in writer_unsent_reasons)
        if state == "queued" or writer_refused or (state in {"rejected", "not_delivered"}
                and row.get("sending_at") is None and native is None):
            if not writer_refused and (rpc_id is not None or row.get("sending_at") is not None):
                uncertain = True
            entry["_order"] = (len(records), str(row.get("created_at", "")), request_id)
            provenance.append(entry)
            continue
        matched = bool(native and native[1].get("method") == "turn/steer" and row.get("connection_id") == connection_id
                       and isinstance(native[1].get("params"), dict) and _identifier(client_id))
        if matched:
            index, frame = native
            params = frame["params"]
            matched = (turn_started[0] < index < terminal_index and params.get("threadId") == thread_id
                       and params.get("expectedTurnId") == turn_id and params.get("clientUserMessageId") == client_id
                       and set(params) == {"threadId", "expectedTurnId", "clientUserMessageId", "input"}
                       and isinstance(params.get("input"), list) and len(params["input"]) == 1
                       and isinstance(params["input"][0], dict) and set(params["input"][0]) == {"type", "text"}
                       and params["input"][0].get("type") == "text" and isinstance(params["input"][0].get("text"), str))
            if matched and "job_id" in row:
                content = params["input"]
                matched = (len(content) == 1 and isinstance(content[0], dict) and content[0].get("type") == "text"
                    and isinstance(content[0].get("text"), str) and row.get("expected_turn_id") == turn_id
                    and _digest({"job_id": row["job_id"], "expected_turn_id": turn_id, "text": content[0]["text"]}) == digest)
        if matched:
            seen_rpc.add(rpc_id)
            entry["send_digest"] = _digest(records[index])
            entry["_order"] = (index, "", request_id)
            reply = responses.get(rpc_id)
            accepted = bool(reply and index < reply[0] and isinstance(reply[1].get("result"), dict)
                            and reply[1]["result"].get("turnId") == turn_id and "error" not in reply[1])
            rejected = bool(reply and index < reply[0] and "error" in reply[1])
            entry["accepted_proof"] = _digest(records[reply[0]]) if accepted else None
            entry["rejected_proof"] = _digest(records[reply[0]]) if rejected else None
            proof = [(i, records[i]) for i, method, notification in notifications
                     if method in {"item/started", "item/completed"} and index < i and isinstance(notification, dict)
                     and notification.get("threadId") == thread_id and notification.get("turnId") == turn_id
                     and isinstance(notification.get("item"), dict) and notification["item"].get("type") == "userMessage"
                     and notification["item"].get("clientId") == client_id]
            proof.extend((i, records[i]) for i, method, notification in notifications
                if method in {"turn/started", "turn/completed"} and index < i and isinstance(notification, dict)
                and notification.get("threadId") == thread_id and isinstance(notification.get("turn"), dict)
                and notification["turn"].get("id") == turn_id and isinstance(notification["turn"].get("items"), list)
                and any(isinstance(item, dict) and item.get("type") == "userMessage" and item.get("clientId") == client_id
                        for item in notification["turn"]["items"]))
            proof.sort(key=lambda value: value[0])
            if proof:
                entry["recorded_proof"] = _digest([record for _, record in proof])
            if row.get("recorded") is True and (entry["recorded_proof"] is None or row.get("recorded_turn_id") != turn_id):
                uncertain = True
            if state in {"rejected", "not_delivered"} and not rejected:
                uncertain = True
            elif state in {"accepted_by_runtime", "sending"} and not accepted and not rejected:
                uncertain = True
            elif state == "accepted_by_runtime" and not accepted:
                uncertain = True
        else:
            uncertain = True
            entry["_order"] = (len(records), str(row.get("sending_at", "")), request_id)
        if state == "delivery_uncertain" or row.get("uncertain") is True:
            uncertain = True
        provenance.append(entry)
    target_steers = {rpc for rpc, (i, f) in sends.items() if f.get("method") == "turn/steer"
                     and isinstance(f.get("params"), dict) and f["params"].get("threadId") == thread_id
                     and f["params"].get("expectedTurnId") == turn_id and i < terminal_index}
    if target_steers != seen_rpc:
        uncertain = True
    provenance.sort(key=lambda entry: entry["_order"])
    for entry in provenance:
        del entry["_order"]
    base.update(input_set_uncertain=uncertain, steer_count=len(provenance), steer_evidence_digest=_digest(provenance))
    base["recorded_steer_count"] = sum(entry["recorded_proof"] is not None for entry in provenance)
    messages = [(params["item"]["text"], params["item"]["id"])
                for i, method, params in notifications if method == "item/completed" and turn_starts[0] < i < terminal_index
                and isinstance(params, dict) and params.get("threadId") == thread_id and params.get("turnId") == turn_id
                and isinstance(params.get("item"), dict) and params["item"].get("type") == "agentMessage"
                and params["item"].get("delivery") != "async" and isinstance(params["item"].get("text"), str)
                and _identifier(params["item"].get("id"))]
    if outcome == "completed" and not messages:
        return pending("final_message_missing")
    if messages:
        text, item_id = messages[-1]
        try:
            answer_digest = hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()
        except UnicodeError:
            return pending("agent_message_invalid")
        base.update(result_text=text, result_digest=answer_digest, result_chars=len(text), final_item_id=item_id)
    base["evidence_digest"] = _digest({"spec_digest": base["spec_digest"],
        "native_terminal_digest": base["native_terminal_digest"], "result_digest": base.get("result_digest"),
        "steer_evidence_digest": base["steer_evidence_digest"], "input_set_uncertain": uncertain})
    if outcome != "completed":
        return {**base, "state": "failed", "code": "turn_interrupted" if outcome == "interrupted" else "turn_failed"}
    return {**base, "state": "succeeded", **({"code": "input_set_uncertain"} if uncertain else {})}
