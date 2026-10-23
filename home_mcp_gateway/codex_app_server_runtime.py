"""One worker-owned, fail-closed Codex app-server stdio connection.

The private RPC journal is the evidence; stdout is only a bounded status projection.
This adapter never authenticates, resumes a thread, or retries a steering request.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time
import uuid

import psutil

from home_mcp_gateway.jobs import _read, _terminate_tree, identity, process_create_time

MAX_FRAME_BYTES = 1024 * 1024
MAX_JOURNAL_BYTES = 64 * 1024 * 1024
MAX_PROJECTION_BYTES = 64 * 1024
_AUTH_METHODS = {"account/chatgptAuthTokens/refresh", "attestation/generate"}
_BENIGN_ACCOUNT_NOTIFICATIONS = {"account/updated", "account/rateLimits/updated"}
_APPROVAL_METHODS = {
    "item/commandExecution/requestApproval", "item/fileChange/requestApproval",
    "item/permissions/requestApproval", "applyPatchApproval", "execCommandApproval",
}


class RuntimeFailure(Exception):
    """Only fixed, non-sensitive codes cross the worker boundary."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _json_bytes(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _canonical_cwd(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return os.path.normcase(os.path.realpath(value))


def _valid_id(value: object) -> bool:
    return isinstance(value, (int, str)) and not isinstance(value, bool)


def _decode_frame(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise RuntimeFailure("appserver_protocol_invalid") from None
    if not isinstance(value, dict):
        raise RuntimeFailure("appserver_protocol_invalid")
    if "method" in value:
        if not isinstance(value["method"], str) or not value["method"] or "result" in value or "error" in value:
            raise RuntimeFailure("appserver_protocol_invalid")
        if "id" in value and not _valid_id(value["id"]):
            raise RuntimeFailure("appserver_protocol_invalid")
        if "params" in value and not isinstance(value["params"], dict):
            raise RuntimeFailure("appserver_protocol_invalid")
    elif not _valid_id(value.get("id")) or (("result" in value) == ("error" in value)):
        raise RuntimeFailure("appserver_protocol_invalid")
    elif "error" in value:
        error = value["error"]
        if (not isinstance(error, dict) or not isinstance(error.get("code"), int)
                or isinstance(error["code"], bool) or not isinstance(error.get("message"), str)):
            raise RuntimeFailure("appserver_protocol_invalid")
    return value


def server_request_response(frame: dict) -> tuple[dict, str | None]:
    """Schema-correct denials, independent of untrusted request parameters."""
    method, rpc_id = frame["method"], frame["id"]
    if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
        return {"id": rpc_id, "result": {"decision": "decline"}}, None
    if method == "item/permissions/requestApproval":
        return {"id": rpc_id, "result": {"permissions": {}, "scope": "turn"}}, None
    if method in {"applyPatchApproval", "execCommandApproval"}:
        return {"id": rpc_id, "result": {"decision": {"denied": {"rejection": "Gateway read-only policy"}}}}, None
    if method == "mcpServer/elicitation/request":
        return {"id": rpc_id, "result": {"action": "decline"}}, None
    if method in _AUTH_METHODS:
        return {"id": rpc_id, "error": {"code": -32000, "message": "Gateway authentication is unavailable"}}, "appserver_auth_required"
    if method in {"item/tool/requestUserInput", "item/tool/call"}:
        return {"id": rpc_id, "error": {"code": -32601, "message": "Gateway does not provide interactive tools"}}, None
    return {"id": rpc_id, "error": {"code": -32601, "message": "Method not found"}}, None


class _Runtime:
    def __init__(self, directory: Path, data: dict, save):
        self.directory, self.data, self.save = directory, data, save
        self.spec = data["app_server"]
        self.connection_id = uuid.uuid4().hex
        self.info = {"connection_id": self.connection_id,
                     "journal_path": str(directory / "appserver.rpc.jsonl"),
                     "connection_state": "connecting", "thread_id": None,
                     "turn_id": None, "current_turn_id": None, "turn_outcome": None,
                     "shutdown_code": None, "approval_request_count": 0}
        data["app_server_runtime"] = self.info
        self.proc = None
        self.create_time = None
        self.writer_lock = threading.Lock()
        self.write_queue = queue.Queue()
        self.writer_done = threading.Event()
        self.write_active_since = None
        self.writer_thread = None
        self.journal_lock = threading.RLock()
        self.id_lock = threading.Lock()
        self.events = queue.Queue()
        self.responses = {}
        self.pending = {}
        self.seen_response_ids = set()
        self.next_id = 0
        self.journal_bytes = 0
        self.projection_bytes = 0
        self.failure = None
        self.failure_lock = threading.Lock()
        self.auth_ids = set()
        self.reader_done = threading.Event()
        self.terminal_observed = threading.Event()
        self.stopping = False
        self.stop_deadline = None
        self.interrupt_deadline = None
        self.interrupt_id = None
        self.inflight = None
        self.recorded_client_ids = set()
        self.observed_turn_ids = set()
        self.journal = None
        self.projection = None
        self.descendants = {}

    def _persist(self):
        self.save(self.data)

    def _fail(self, code):
        with self.failure_lock:
            if self.failure is None:
                self.failure = code

    def _redacted(self, frame):
        method = frame.get("method", "")
        if method in _AUTH_METHODS or method.startswith("account/") or method.startswith("auth/"):
            if "id" in frame:
                self.auth_ids.add(frame["id"])
            return {key: frame[key] for key in ("method", "id") if key in frame} | {"redacted": True}
        if frame.get("id") in self.auth_ids and "method" not in frame:
            return {"id": frame["id"], "redacted": True}
        # Authentication errors can include tokens in data or their message.
        if "error" in frame:
            error = frame["error"]
            message = str(error.get("message", "")).lower()
            if any(word in message for word in ("auth", "token", "credential", "api key", "api_key", "bearer")):
                return {"id": frame["id"], "redacted": True}
        return frame

    def _journal(self, direction, frame):
        # Bound the native frame before redaction as well as the persisted envelope.
        if len(_json_bytes(frame)) > MAX_FRAME_BYTES:
            raise RuntimeFailure("appserver_evidence_limit")
        envelope = {"connection_id": self.connection_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "direction": direction, "frame": self._redacted(frame)}
        raw = _json_bytes(envelope) + b"\n"
        with self.journal_lock:
            if self.journal_bytes + len(raw) > MAX_JOURNAL_BYTES:
                raise RuntimeFailure("appserver_evidence_limit")
            self.journal.write(raw)
            self.journal.flush()
            os.fsync(self.journal.fileno())
            self.journal_bytes += len(raw)

    def _write(self, frame, *, steer_request=None):
        raw = _json_bytes(frame) + b"\n"
        if len(raw) > MAX_FRAME_BYTES:
            raise RuntimeFailure("appserver_evidence_limit")
        # A reader must never wait for pipe backpressure. One writer preserves
        # both journal and actual-send order for requests and server replies.
        self.write_queue.put((time.monotonic(), frame, raw, steer_request))

    def _final_steer_guard(self, request):
        from home_mcp_gateway import job_steering as mailbox
        if self.failure:
            return "runtime_error"
        if self.stopping or (self.directory / "stop.json").exists() or mailbox.mailbox_closed(self.directory):
            return "stopping"
        if (self.terminal_observed.is_set() or self.info["turn_outcome"] is not None
                or self.info["current_turn_id"] != request["expected_turn_id"]):
            return "job_finished"
        if not self._steer_identity_matches():
            return "guard_mismatch"
        from home_mcp_gateway.work_dispatcher_codex import steer_guard
        guard = steer_guard(self.data.get("dispatcher_binding"), self.data, require_running=True)
        if guard != "open":
            return {"closed": "guard_closed", "mismatch": "guard_mismatch"}.get(guard, "guard_unavailable")
        # Guard I/O may have overlapped a stop or a native terminal notification.
        if self.stopping or (self.directory / "stop.json").exists() or mailbox.mailbox_closed(self.directory):
            return "stopping"
        if self.terminal_observed.is_set() or self.info["current_turn_id"] != request["expected_turn_id"]:
            return "job_finished"
        if not self._steer_identity_matches():
            return "guard_mismatch"
        return None

    def _writer(self):
        try:
            while True:
                item = self.write_queue.get()
                try:
                    if item is None:
                        return
                    queued_at, frame, raw, steer_request = item
                    self.write_active_since = queued_at
                    if time.monotonic() - queued_at >= 5:
                        raise RuntimeFailure("appserver_response_write_failed")
                    # Serialize native send/receive journal ordering across the
                    # final guard and wire write. No fsync occurs between the
                    # final guard and wire; crash before send evidence stays
                    # uncertain in the durable mailbox.
                    with self.journal_lock:
                        if steer_request is not None:
                            reason = self._final_steer_guard(steer_request)
                            if reason:
                                self.events.put(("unsent", (steer_request, frame["id"], reason)))
                                self.write_active_since = None
                                continue
                        elif self.failure and "method" in frame:
                            raise RuntimeFailure(self.failure)
                        # Only actual pipe writes hold the writer lock.
                        with self.writer_lock:
                            remaining = memoryview(raw)
                            while remaining:
                                count = self.proc.stdin.write(remaining)
                                if not count:
                                    raise RuntimeFailure("appserver_pipe_lost")
                                remaining = remaining[count:]
                            self.proc.stdin.flush()
                        self._journal("send", frame)
                    self.write_active_since = None
                finally:
                    self.write_queue.task_done()
        except RuntimeFailure as exc:
            self._fail(exc.code)
        except (OSError, ValueError):
            self._fail("appserver_pipe_lost")
        except Exception:
            self._fail("appserver_protocol_io_failed")
        finally:
            self.writer_done.set()

    def _reserve(self, method):
        with self.id_lock:
            self.next_id += 1
            rpc_id = self.next_id
            self.pending[rpc_id] = method
            return rpc_id

    def _request(self, method, params):
        rpc_id = self._reserve(method)
        self._write({"id": rpc_id, "method": method, "params": params})
        return rpc_id

    def _reader(self):
        try:
            while True:
                raw = self.proc.stdout.readline(MAX_FRAME_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_FRAME_BYTES or not raw.endswith(b"\n"):
                    raise RuntimeFailure("appserver_evidence_limit" if len(raw) > MAX_FRAME_BYTES else "appserver_protocol_invalid")
                frame = _decode_frame(raw)
                if frame.get("method") == "turn/completed":
                    self.terminal_observed.set()
                self._journal("receive", frame)
                if "method" in frame and "id" in frame:
                    reply, failure = server_request_response(frame)
                    if frame["method"] in _APPROVAL_METHODS:
                        self.events.put(("approval", None))
                    self._write(reply)
                    if failure:
                        self._fail(failure)
                        break
                else:
                    self.events.put(("frame", frame))
        except RuntimeFailure as exc:
            self._fail(exc.code)
        except Exception:
            self._fail("appserver_protocol_io_failed")
        finally:
            self.reader_done.set()

    def _stderr_reader(self):
        # stderr is untrusted and may contain credentials. Never copy it to logs.
        try:
            while self.proc.stderr.read(8192):
                pass
        except (OSError, ValueError):
            pass

    def _project(self, text):
        raw = (text + "\n").encode("ascii")
        if self.projection_bytes + len(raw) <= MAX_PROJECTION_BYTES:
            self.projection.write(raw)
            self.projection_bytes += len(raw)

    def _native_items(self, items):
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("type") == "userMessage" and isinstance(item.get("clientId"), str):
                self.recorded_client_ids.add(item["clientId"])

    def _steer_identity_matches(self):
        """Re-read the exact worker/job/child binding immediately before dispatch."""
        try:
            if (self.data.get("job_id") != self.directory.name
                    or self.data.get("dispatcher_binding", {}).get("gateway_job_id") != self.directory.name
                    or self.data.get("worker_pid") != os.getpid()
                    or identity(self.data.get("worker_pid"), self.data.get("worker_create_time")) != "alive"
                    or self.data.get("pid") != self.proc.pid
                    or self.data.get("process_create_time") != self.create_time
                    or identity(self.proc.pid, self.create_time) != "alive"):
                return False
            disk = _read(self.directory / "metadata.json")
            keys = ("job_id", "command", "cwd", "idempotency_key", "request_fingerprint",
                    "dispatcher_request_fingerprint", "explicit_env_digest", "dispatcher_binding",
                    "transport", "app_server", "worker_pid", "worker_create_time", "pid", "process_create_time")
            if disk.get("status") != "running" or any(disk.get(key) != self.data.get(key) for key in keys):
                return False
            runtime = disk.get("app_server_runtime", {})
            return all(runtime.get(key) == self.info.get(key) for key in
                       ("connection_id", "thread_id", "turn_id", "current_turn_id", "connection_state"))
        except (OSError, ValueError, TypeError, AttributeError):
            return False

    def _snapshot_descendants(self):
        if self.proc is None or identity(self.proc.pid, self.create_time) != "alive":
            return
        try:
            parent = psutil.Process(self.proc.pid)
            if abs(parent.create_time() - self.create_time) > .001:
                return
            for child in parent.children(recursive=True):
                try:
                    self.descendants[child.pid] = child.create_time()
                except psutil.NoSuchProcess:
                    pass
        except psutil.Error:
            self._fail("appserver_process_identity_unavailable")

    def _frame(self, frame):
        if "method" not in frame:
            rpc_id = frame["id"]
            if rpc_id not in self.pending or rpc_id in self.seen_response_ids:
                raise RuntimeFailure("appserver_protocol_invalid")
            self.seen_response_ids.add(rpc_id)
            self.responses[rpc_id] = frame
            return
        method, params = frame["method"], frame.get("params", {})
        if "id" not in frame and method in _BENIGN_ACCOUNT_NOTIFICATIONS:
            # These installed-schema account status notifications are harmless.
            # _redacted still excludes all their private parameters from evidence.
            return
        if method.startswith("account/") or method.startswith("auth/"):
            raise RuntimeFailure("appserver_auth_required")
        thread_id, turn_id = self.info["thread_id"], self.info["turn_id"]
        if method in {"turn/started", "turn/completed", "item/started", "item/completed"}:
            # Notifications before turn/start's response remain in the journal, but
            # only the response can bind the worker's turn identity.
            if thread_id is not None and params.get("threadId") != thread_id:
                raise RuntimeFailure("appserver_protocol_invalid")
            if method.startswith("item/"):
                if not isinstance(params.get("turnId"), str) or not params["turnId"]:
                    raise RuntimeFailure("appserver_protocol_invalid")
                self.observed_turn_ids.add(params["turnId"])
                if turn_id is not None and params.get("turnId") != turn_id:
                    raise RuntimeFailure("appserver_protocol_invalid")
                self._native_items([params.get("item")])
            else:
                turn = params.get("turn")
                if not isinstance(turn, dict) or not isinstance(turn.get("id"), str):
                    raise RuntimeFailure("appserver_protocol_invalid")
                self.observed_turn_ids.add(turn["id"])
                if turn_id is not None and turn["id"] != turn_id:
                    raise RuntimeFailure("appserver_protocol_invalid")
                self._native_items(turn.get("items"))
                if method == "turn/completed":
                    if turn.get("status") not in {"completed", "failed", "interrupted"}:
                        raise RuntimeFailure("appserver_protocol_invalid")
                    self.info["turn_outcome"] = turn["status"]
                    self.info["terminal_turn_id"] = turn["id"]
                    self.info["current_turn_id"] = None
                    self._persist()

    def _drain(self):
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "approval":
                self.info["approval_request_count"] += 1
                self.info["policy_code"] = "policy_unexpected_request"
                self._persist()
            elif kind == "unsent":
                request, rpc_id, reason = value
                from home_mcp_gateway import job_steering as mailbox
                mailbox.write_state(self.directory, request, "rejected", reason=reason)
                if self.inflight is not None and self.inflight[1] == rpc_id:
                    self.inflight = None
            else:
                self._frame(value)
        if self.failure:
            raise RuntimeFailure(self.failure)

    def _mailbox(self):
        from home_mcp_gateway import job_steering as mailbox
        if self.inflight is not None:
            request, rpc_id = self.inflight
            if rpc_id in self.responses:
                frame = self.responses.pop(rpc_id)
                if "error" in frame:
                    mailbox.write_state(self.directory, request, "rejected", reason="runtime_rejected")
                elif (isinstance(frame.get("result"), dict)
                      and frame["result"].get("turnId") == request["expected_turn_id"] == self.info["turn_id"]):
                    mailbox.write_state(self.directory, request, "accepted_by_runtime")
                else:
                    # A mismatched success is not proof of rejection or acceptance.
                    raise RuntimeFailure("appserver_protocol_invalid")
                self.inflight = None
        # Recording is a separate fact, proven only by the native clientId.
        for request in mailbox.enumerate_requests(self.directory):
            state = mailbox.read_state(self.directory, request["request_id"])
            if state and state.get("state") == "accepted_by_runtime" and request["client_user_message_id"] in self.recorded_client_ids:
                if not state.get("recorded"):
                    mailbox.record_native_item(self.directory, request, turn_id=self.info["turn_id"],
                                               client_id=request["client_user_message_id"])
        closed = self.stopping or self.info["turn_outcome"] is not None or mailbox.mailbox_closed(self.directory)
        for request in mailbox.enumerate_requests(self.directory):
            state = mailbox.read_state(self.directory, request["request_id"])
            if state is not None:
                continue
            if closed:
                mailbox.write_state(self.directory, request, "rejected", reason="stopping" if self.stopping else "job_finished")
                continue
            if self.inflight is not None or self.info["current_turn_id"] is None:
                continue
            if request["expected_turn_id"] != self.info["current_turn_id"]:
                mailbox.write_state(self.directory, request, "rejected", reason="stale_turn")
                continue
            if not self._steer_identity_matches():
                mailbox.write_state(self.directory, request, "rejected", reason="guard_mismatch")
                continue
            from home_mcp_gateway.work_dispatcher_codex import steer_guard
            guard = steer_guard(self.data.get("dispatcher_binding"), self.data, require_running=True)
            if guard != "open":
                mailbox.write_state(self.directory, request, "rejected", reason={"closed": "guard_closed", "mismatch": "guard_mismatch"}.get(guard, "guard_unavailable"))
                continue
            # Recheck after guard I/O. Stop/terminal observations prohibit dispatch.
            self._drain()
            if ((self.directory / "stop.json").exists() or self.info["turn_outcome"] is not None
                    or mailbox.mailbox_closed(self.directory)):
                continue
            if not self._steer_identity_matches():
                mailbox.write_state(self.directory, request, "rejected", reason="guard_mismatch")
                continue
            rpc_id = self._reserve("turn/steer")
            mailbox.write_state(self.directory, request, "sending", connection_id=self.connection_id,
                                rpc_id=rpc_id, request_digest=request["request_digest"],
                                client_user_message_id=request["client_user_message_id"])
            self.inflight = (request, rpc_id)
            self._write({"id": rpc_id, "method": "turn/steer", "params": {
                "threadId": self.info["thread_id"], "input": [{"type": "text", "text": request["text"]}],
                "expectedTurnId": request["expected_turn_id"],
                "clientUserMessageId": request["client_user_message_id"]}}, steer_request=request)
            break

    def _stop_request(self):
        request_path = self.directory / "stop.json"
        if self.stopping or not request_path.exists():
            return
        stop = _read(request_path)
        from home_mcp_gateway import job_steering as mailbox
        mailbox.close_mailbox(self.directory, reason="stopping")
        self.stopping = True
        now = time.monotonic()
        deadline = stop.get("deadline_monotonic")
        self.stop_deadline = deadline if isinstance(deadline, (int, float)) else now + 5
        self.interrupt_deadline = max(now, self.stop_deadline - 2)
        self.data["status"] = "stopping"
        self._persist()
        request_path.unlink(missing_ok=True)
        self._snapshot_descendants()
        if self.info["current_turn_id"] is not None:
            self.interrupt_id = self._request("turn/interrupt", {
                "threadId": self.info["thread_id"], "turnId": self.info["current_turn_id"]})
            self.info["interrupt_rpc_id"] = self.interrupt_id
            self._persist()

    def _tick(self):
        if self.write_active_since is not None and time.monotonic() - self.write_active_since >= 5:
            self._fail("appserver_response_write_failed")
        self._drain()
        self._stop_request()
        self._mailbox()
        if self.interrupt_id is not None and self.interrupt_id in self.responses:
            frame = self.responses.pop(self.interrupt_id)
            self.info["interrupt_response"] = "error" if "error" in frame else "acknowledged"
            self.interrupt_id = None
            self._persist()
        if self.stopping and (self.info["turn_outcome"] is not None or time.monotonic() >= self.interrupt_deadline):
            raise RuntimeFailure("appserver_stop_requested")
        if self.reader_done.is_set() and self.events.empty():
            if self.info["turn_outcome"] is None:
                raise RuntimeFailure("appserver_connection_lost")

    def _wait_rpc(self, rpc_id):
        # No arbitrary response timeout: a pending steering write stays uncertain
        # until its same RPC response arrives, or this exact connection is lost.
        while True:
            self._tick()
            if rpc_id in self.responses:
                response = self.responses.pop(rpc_id)
                if "error" in response:
                    raise RuntimeFailure("appserver_rpc_rejected")
                if not isinstance(response.get("result"), dict):
                    raise RuntimeFailure("appserver_protocol_invalid")
                return response["result"]
            time.sleep(.02)

    def _handshake(self):
        self._wait_rpc(self._request("initialize", {"clientInfo": {
            "name": "home_mcp_gateway", "version": "1.0"}}))
        self._write({"method": "initialized"})
        spec = self.spec
        result = self._wait_rpc(self._request("thread/start", {
            "model": spec["model"], "modelProvider": spec["model_provider"], "cwd": spec["cwd"],
            "sandbox": "read-only", "approvalPolicy": "never", "approvalsReviewer": "user",
            "config": {"model_reasoning_effort": spec["effort"]}}))
        sandbox = result.get("sandbox")
        thread = result.get("thread")
        if (result.get("model") != spec["model"] or result.get("modelProvider") != spec["model_provider"]
                or _canonical_cwd(result.get("cwd")) != _canonical_cwd(spec["cwd"])
                or result.get("approvalPolicy") != "never" or result.get("approvalsReviewer") != "user"
                or not isinstance(sandbox, dict) or sandbox.get("type") != "readOnly"
                or set(sandbox) - {"type", "networkAccess"}
                or sandbox.get("networkAccess", False) is not False
                or result.get("reasoningEffort") != spec["effort"]
                or not isinstance(thread, dict) or not isinstance(thread.get("id"), str) or not thread["id"]):
            raise RuntimeFailure("appserver_policy_mismatch")
        self.info["thread_id"] = thread["id"]
        self._persist()
        result = self._wait_rpc(self._request("turn/start", {
            "threadId": thread["id"], "input": [{"type": "text", "text": spec["input_text"]}],
            "effort": spec["effort"]}))
        turn = result.get("turn")
        if (not isinstance(turn, dict) or not isinstance(turn.get("id"), str) or not turn["id"]
                or not isinstance(turn.get("items"), list)
                or turn.get("status") not in {"completed", "interrupted", "failed", "inProgress"}):
            raise RuntimeFailure("appserver_protocol_invalid")
        if self.info.get("terminal_turn_id") not in (None, turn["id"]):
            raise RuntimeFailure("appserver_protocol_invalid")
        if self.observed_turn_ids - {turn["id"]}:
            raise RuntimeFailure("appserver_protocol_invalid")
        self.info["turn_id"] = turn["id"]
        self.info["current_turn_id"] = turn["id"] if self.info["turn_outcome"] is None else None
        self._persist()
        self._project("Codex app-server turn started.")

    def _close_mailbox(self, reason):
        from home_mcp_gateway import job_steering as mailbox
        mailbox.close_mailbox(self.directory, reason=reason)
        # Finalize durable replies already read before closing the connection.
        try:
            self._drain()
        finally:
            self._mailbox()

    def _cleanup(self):
        # Fence must exist before pipe close / process-tree cleanup.
        try:
            self._close_mailbox("stopping" if self.stopping else "worker_stopped")
        except RuntimeFailure as exc:
            self._fail(exc.code)
        except Exception:
            self._fail("appserver_mailbox_finalize_failed")
        if self.proc is None:
            return
        self._snapshot_descendants()
        if self.writer_thread is not None:
            pending_writes = self.write_queue.unfinished_tasks > 0 or self.write_active_since is not None
            self.write_queue.put(None)
            writer_budget = .5 if self.stop_deadline is None else max(0, min(.5, self.stop_deadline - time.monotonic() - 2))
            self.writer_thread.join(timeout=writer_budget)
            if not self.writer_done.is_set() and pending_writes:
                self._fail("appserver_response_write_failed")
                self.info["shutdown_code"] = "appserver_shutdown_forced"
                self.data["stop_result"] = _terminate_tree(self.proc.pid, self.create_time, True,
                                                         deadline_monotonic=self.stop_deadline)
        try:
            self.proc.stdin.close()
        except (OSError, ValueError):
            pass
        budget = 2.0
        if self.stop_deadline is not None:
            budget = max(0.0, min(budget, self.stop_deadline - time.monotonic() - 2))
        try:
            self.proc.wait(timeout=budget)
        except subprocess.TimeoutExpired:
            self.info["shutdown_code"] = "appserver_shutdown_forced"
        # Exact pid/create-time identity is checked even after graceful exit.
        self.data["stop_result"] = _terminate_tree(self.proc.pid, self.create_time, True,
                                                 deadline_monotonic=self.stop_deadline)
        descendant_results = []
        for pid, create_time in self.descendants.items():
            result = _terminate_tree(pid, create_time, True, deadline_monotonic=self.stop_deadline)
            descendant_results.append({"pid": pid, "process_create_time": create_time, **result})
        self.info["descendant_stop_results"] = descendant_results
        if self.data["stop_result"].get("remaining_pids") or any(result.get("remaining_pids") for result in descendant_results):
            self.info["shutdown_code"] = "appserver_shutdown_incomplete"
            self._fail("appserver_shutdown_incomplete")
        self.info["connection_state"] = "lost"
        self.info["current_turn_id"] = None
        self.info["journal_bytes"] = self.journal_bytes

    def run(self):
        code = None
        try:
            with Path(self.info["journal_path"]).open("xb", buffering=0) as journal, Path(self.data["stdout_path"]).open("ab", buffering=0) as projection:
                self.journal, self.projection = journal, projection
                try:
                    self.proc = subprocess.Popen(self.data["command"], cwd=self.spec["cwd"], shell=False,
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                        **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
                    self.create_time = process_create_time(self.proc)
                    self.data.update(pid=self.proc.pid, process_create_time=self.create_time, status="running")
                    self.info["connection_state"] = "connected"
                    self._persist()
                    reader = threading.Thread(target=self._reader, daemon=True, name="appserver-stdout")
                    stderr = threading.Thread(target=self._stderr_reader, daemon=True, name="appserver-stderr")
                    self.writer_thread = threading.Thread(target=self._writer, daemon=True, name="appserver-stdin")
                    self.writer_thread.start()
                    reader.start()
                    stderr.start()
                    self._handshake()
                    while self.info["turn_outcome"] is None:
                        self._tick()
                        time.sleep(.02)
                    self._project("Codex app-server turn " + self.info["turn_outcome"] + ".")
                except RuntimeFailure as exc:
                    code = exc.code
                except Exception:
                    code = "appserver_runtime_failed"
                finally:
                    self._cleanup()
                    if self.proc:
                        reader = locals().get("reader")
                        if reader:
                            reader.join(timeout=.5 if self.stop_deadline is None else
                                        max(0, min(.5, self.stop_deadline - time.monotonic())))
                        stderr = locals().get("stderr")
                        if stderr:
                            stderr.join(timeout=.5 if self.stop_deadline is None else
                                        max(0, min(.5, self.stop_deadline - time.monotonic())))
                        try:
                            self._drain()
                            self._mailbox()
                        except RuntimeFailure as exc:
                            self._fail(exc.code)
                        except Exception:
                            self._fail("appserver_protocol_io_failed")
                        for stream in (self.proc.stdout, self.proc.stderr):
                            try:
                                stream.close()
                            except (OSError, ValueError):
                                pass
        except Exception:
            code = "appserver_runtime_failed"
            try:
                self._cleanup()
            except Exception:
                pass
        if code == "appserver_stop_requested":
            code = None
        code = code or self.failure
        outcome = self.info["turn_outcome"]
        self.data.update(status="stopped" if self.stopping else "exited" if outcome is not None and code is None else "failed",
                         exit_code=self.proc.poll() if self.proc else None, finished_at=time.time())
        if code:
            self.data["error_code"] = code
        self.info["runtime_error_code"] = code
        self.info["journal_bytes"] = self.journal_bytes
        self._persist()


def run_app_server(directory: Path, data: dict, save) -> None:
    """Called only after the worker's launch guard has matched the pinned job."""
    from home_mcp_gateway.work_dispatcher_codex import app_server_enabled, validate_app_server_spec
    if not app_server_enabled():
        raise RuntimeFailure("appserver_disabled")
    data["app_server"] = validate_app_server_spec(data.get("app_server"), cwd=data.get("cwd"))
    _Runtime(directory, data, save).run()
