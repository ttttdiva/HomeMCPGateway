"""Narrow helpers for the v2 standalone Gateway Codex job route.

This module does not schedule work. It validates the pinned launch shape, reads an
exact dispatcher gate in read-only mode at worker admission, and verifies one job's
complete official CLI JSONL result.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Any
from urllib.parse import quote
import uuid

MAX_JSONL_BYTES = 64 << 20
WINDOWS_COMMAND_LIMIT = 32767
OFFICIAL_ENTRYPOINT_SUFFIX = ("node_modules", "@openai", "codex", "bin", "codex.js")
APP_SERVER_TRANSPORT = "codex_app_server"
APP_SERVER_SPEC_KEYS = {"model", "model_provider", "effort", "input_text", "cwd", "sandbox", "approval_policy", "approvals_reviewer"}


def app_server_enabled() -> bool:
    """The separate transport is opt-in; ambient Codex config cannot enable it."""
    return os.environ.get("HOME_MCP_CODEX_APP_SERVER_ENABLED") == "1"


def validate_app_server_spec(spec: Any, *, model: str | None = None, effort: str | None = None,
                             input_text: str | None = None, cwd: str | None = None) -> dict[str, Any]:
    """Validate the complete immutable spec, including all restrictive policy values."""
    from . import aoitalk_cli_sessions as cli
    if (not isinstance(spec, dict) or set(spec) != APP_SERVER_SPEC_KEYS
            or not all(isinstance(value, str) for value in spec.values())):
        raise ValueError("invalid_app_server_spec")
    try:
        cli._bounded_text(spec["model"], "model", 160)
        cli._bounded_text(spec["input_text"], "message", cli.MAX_MESSAGE_CHARS, allow_newlines=True)
        spec["model"].encode("utf-8")
        spec["input_text"].encode("utf-8")
    except (cli.CliSessionError, UnicodeEncodeError):
        raise ValueError("invalid_app_server_spec") from None
    if (spec["model_provider"] != "openai" or spec["effort"] not in {"low", "medium", "high", "xhigh", "max"}
            or spec["sandbox"] != "read-only" or spec["approval_policy"] != "never" or spec["approvals_reviewer"] != "user"
            or not isinstance(spec["cwd"], str) or not spec["cwd"] or "\x00" in spec["cwd"]
            or not Path(spec["cwd"]).is_absolute()):
        raise ValueError("invalid_app_server_spec")
    expected = {"model": model, "effort": effort, "input_text": input_text, "cwd": cwd}
    if any(value is not None and spec[key] != value for key, value in expected.items()):
        raise ValueError("invalid_app_server_spec")
    return dict(spec)


def _is_sha(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def canonical_file(value: Any, *, official_entrypoint: bool = False) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid_path")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise ValueError("invalid_path")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("invalid_path")
    if os.path.normcase(os.path.normpath(str(resolved))) != os.path.normcase(os.path.normpath(value)):
        raise ValueError("invalid_path")
    if official_entrypoint:
        parts = tuple(part.casefold() for part in resolved.parts[-len(OFFICIAL_ENTRYPOINT_SUFFIX):])
        if parts != tuple(part.casefold() for part in OFFICIAL_ENTRYPOINT_SUFFIX):
            raise ValueError("invalid_path")
    return str(resolved)


def make_argv(node_executable: str, entrypoint: str, model: str, effort: str, message: str) -> list[str]:
    return [node_executable, entrypoint, "exec", "-m", model, "-c", f'model_reasoning_effort="{effort}"', "--json", message]


def validate_app_server_command(command: Any) -> list[str]:
    """Only the existing official Node entrypoint, in its default stdio mode."""
    if (not isinstance(command, list) or len(command) != 3
            or not all(isinstance(arg, str) for arg in command) or command[2] != "app-server"):
        raise ValueError("invalid_app_server_command")
    node = canonical_file(command[0])
    if Path(node).name.casefold() not in {"node", "node.exe"}:
        raise ValueError("invalid_app_server_command")
    entrypoint = canonical_file(command[1], official_entrypoint=True)
    normalized = [node, entrypoint, "app-server"]
    if command != normalized:
        raise ValueError("invalid_app_server_command")
    validate_command_line(normalized)
    return normalized


def make_app_server_argv(node_executable: str, entrypoint: str) -> list[str]:
    return validate_app_server_command([node_executable, entrypoint, "app-server"])


def command_line_chars(argv: list[str]) -> int:
    """Windows CreateProcess command line size, including its terminating NUL."""
    import subprocess
    return len(subprocess.list2cmdline(argv).encode("utf-16-le")) // 2 + 1


def validate_command_line(argv: list[str]) -> None:
    if command_line_chars(argv) > WINDOWS_COMMAND_LIMIT:
        raise ValueError("command_line_too_long")


class GatewayJobAdapter:
    """Launch exactly one stable keyed job through the existing D80 jobs primitive."""

    def __init__(self, runtime_dir: str | None = None):
        self.runtime_dir = runtime_dir

    def start(self, request: dict[str, Any], idempotency_key: str, request_fingerprint: str,
              dispatcher_binding: dict[str, Any]) -> dict[str, Any]:
        from .jobs import job_start
        if request.get("transport") == APP_SERVER_TRANSPORT:
            return job_start(request["command"], cwd=request["cwd"], runtime_dir=self.runtime_dir,
                             idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
                             dispatcher_binding=dispatcher_binding, transport=APP_SERVER_TRANSPORT,
                             app_server=request["app_server"])
        return job_start(request["command"], cwd=request["cwd"], runtime_dir=self.runtime_dir,
                         idempotency_key=idempotency_key, request_fingerprint=request_fingerprint,
                         dispatcher_binding=dispatcher_binding)

    def status(self, gateway_job_id: str) -> dict[str, Any]:
        from .jobs import job_status, _directory, _read
        status = job_status(gateway_job_id, self.runtime_dir)
        # Public job status redacts native handles. The verifier uses only this
        # exact job's local metadata and mailbox evidence through the private adapter.
        directory = _directory(gateway_job_id, self.runtime_dir)
        try:
            metadata = _read(directory / "metadata.json")
            if metadata.get("transport") == APP_SERVER_TRANSPORT:
                from .job_steering import steer_evidence
                status.update(transport=metadata.get("transport"), app_server=metadata.get("app_server"),
                              app_server_runtime=metadata.get("app_server_runtime"))
                try:
                    status["steer_evidence"] = steer_evidence(directory)
                except (OSError, ValueError, TypeError, RecursionError):
                    status["steer_evidence"] = []
                    status["error_code"] = "appserver_steer_evidence_unavailable"
        except (OSError, ValueError, TypeError):
            if status.get("transport") == APP_SERVER_TRANSPORT:
                status["error_code"] = "appserver_metadata_unavailable"
        return status


def worker_launch_guard(binding: dict[str, Any], launch_data: dict[str, Any]) -> str:
    """Verify actual Popen data, keyed metadata identity, and the exact pinned op read-only."""
    from .jobs import _dispatcher_binding_valid, _explicit_env_digest, _keyed_fingerprint_from_env_digest
    if not _dispatcher_binding_valid(binding):
        return "unavailable"
    launch_keys = {"command", "cwd", "idempotency_key", "request_fingerprint", "dispatcher_request_fingerprint", "explicit_env_digest"}
    is_app_server = isinstance(launch_data, dict) and launch_data.get("transport") == APP_SERVER_TRANSPORT
    if is_app_server:
        launch_keys |= {"transport", "app_server"}
    if (not isinstance(launch_data, dict) or set(launch_data) != launch_keys
            or not isinstance(launch_data.get("command"), list)
            or not all(isinstance(arg, str) for arg in launch_data["command"])
            or not isinstance(launch_data.get("cwd"), str)
            or launch_data.get("idempotency_key") != binding["op_key"]
            or launch_data.get("dispatcher_request_fingerprint") != binding["request_fingerprint"]):
        return "mismatch"
    if is_app_server and not app_server_enabled():
        return "closed"
    try:
        if launch_data["explicit_env_digest"] != _explicit_env_digest({}):
            return "mismatch"
        if is_app_server:
            validate_app_server_command(launch_data["command"])
            validate_app_server_spec(launch_data["app_server"], cwd=launch_data["cwd"])
            expected_keyed_fingerprint = _keyed_fingerprint_from_env_digest(
                launch_data["command"], launch_data["cwd"], launch_data["explicit_env_digest"],
                launch_data["dispatcher_request_fingerprint"], binding, transport=APP_SERVER_TRANSPORT,
                app_server=launch_data["app_server"])
        else:
            expected_keyed_fingerprint = _keyed_fingerprint_from_env_digest(
                launch_data["command"], launch_data["cwd"], launch_data["explicit_env_digest"],
                launch_data["dispatcher_request_fingerprint"], binding)
        if launch_data["request_fingerprint"] != expected_keyed_fingerprint:
            return "mismatch"
        db_path = Path(binding["db_path"]).resolve(strict=True)
        uri = "file:" + quote(str(db_path).replace("\\", "/"), safe="/:\\") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        try:
            manifest = conn.execute("SELECT * FROM manifests WHERE manifest_id=?", (binding["manifest_id"],)).fetchone()
            root = conn.execute("SELECT max_epoch FROM roots WHERE root_source_id=?", (binding["root_source_id"],)).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (binding["manifest_id"], binding["job_id"])).fetchone()
            operation = conn.execute("SELECT * FROM ops WHERE op_key=?", (binding["op_key"],)).fetchone()
            execution = conn.execute("SELECT * FROM codex_execution WHERE manifest_id=? AND job_id=?",
                                      (binding["manifest_id"], binding["job_id"])).fetchone()
            if None in (manifest, root, job, operation, execution):
                return "unavailable"
            raw = bytes(manifest["raw"])
            if hashlib.sha256(raw).hexdigest() != binding["manifest_digest"] or manifest["digest"] != binding["manifest_digest"]:
                return "unavailable"
            if (manifest["root_source_id"] != binding["root_source_id"] or manifest["root_epoch"] != binding["root_epoch"]
                    or root["max_epoch"] > binding["root_epoch"]):
                return "unavailable"
            if manifest["generation"] != binding["generation"]:
                return "closed"
            now = time.time()
            if (manifest["state"] != "armed" or not now < (manifest["arm_expires_at"] or 0)
                    or not now < manifest["expires_at"]):
                return "closed"
            if (job["route"] != "gateway-codex" or job["create_key"] != binding["op_key"]
                    or job["state"] not in ("starting", "running", "blocked") or operation["op"] != "codex_start"
                    or operation["manifest_id"] != binding["manifest_id"] or operation["job_id"] != binding["job_id"]
                    or operation["generation"] != binding["generation"]):
                return "closed"
            if operation["state"] not in ("intent", "uncertain", "receipted"):
                return "closed"
            if job["state"] == "blocked" and not job["maybe_executing"]:
                return "closed"
            if operation["request_digest"] != binding["request_digest"]:
                return "mismatch"
            actual_op_digest = hashlib.sha256(operation["request_json"].encode("utf-8")).hexdigest()
            if actual_op_digest != binding["request_digest"] or actual_op_digest != operation["request_digest"]:
                return "mismatch"
            try:
                op_request = json.loads(operation["request_json"])
                stored_request = json.loads(job["create_json"])
            except (ValueError, TypeError):
                return "mismatch"
            from .work_dispatcher import canonical, parse_manifest
            try:
                pinned_manifest = parse_manifest(raw, binding["manifest_digest"])
            except Exception:
                return "unavailable"
            pinned = next((entry for entry in pinned_manifest["jobs"] if entry["job_id"] == binding["job_id"]), None)
            if (pinned is None or pinned.get("route") != "gateway-codex" or pinned["create_key"] != binding["op_key"]
                    or pinned["snapshot_digest"] != job["snapshot_digest"]
                    or canonical(stored_request) != canonical(pinned["create_request"])
                    or operation["request_json"] != job["create_json"]
                    or canonical(op_request) != canonical(pinned["create_request"])):
                return "mismatch"
            if (launch_data["command"] != pinned["create_request"].get("command")
                    or launch_data["cwd"] != pinned["create_request"].get("cwd")):
                return "mismatch"
            if is_app_server:
                if (pinned["create_request"].get("transport") != APP_SERVER_TRANSPORT
                        or launch_data["app_server"] != pinned["create_request"].get("app_server")
                        or operation["request_json"] != canonical(pinned["create_request"])
                        or binding["request_fingerprint"] != binding["request_digest"]):
                    return "mismatch"
            elif "transport" in pinned["create_request"] or "app_server" in pinned["create_request"]:
                return "mismatch"
            if operation["state"] == "receipted":
                try:
                    receipt = json.loads(operation["receipt_json"] or "null")
                except ValueError:
                    return "mismatch"
                if (not isinstance(receipt, dict) or receipt.get("gateway_job_id") != binding["gateway_job_id"]
                        or receipt.get("status") not in ("starting", "running")):
                    return "closed"
            if (execution["route"] != "gateway-codex" or execution["gateway_job_id"].lower() != binding["gateway_job_id"].lower()
                    or execution["manifest_digest"] != binding["manifest_digest"]
                    or execution["request_fingerprint"] != binding["request_fingerprint"]
                    or execution["op_key"] != binding["op_key"] or execution["request_digest"] != binding["request_digest"]
                    or execution["generation"] != binding["generation"]
                    or execution["state"] not in ("reserved", "running", "unknown")):
                return "mismatch"
            # The dependency helper is a read-only view over this exact database connection.
            from .work_dispatcher import load_dependency_view
            graph, view = load_dependency_view(conn, manifest)
            if graph is None:
                return "unavailable"
            if view.get(binding["job_id"], ("ready", ""))[0] != "ready":
                return "closed"
            return "open"
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return "unavailable"


def steer_guard(binding: dict[str, Any], metadata: dict[str, Any], *, require_running: bool = True) -> str:
    """Read-only steer admission, or immutable receipt integrity after terminal state.

    The launch arm lease authorizes only a new child. An accepted running turn can
    receive a steer after that lease ends. Receipt reads also survive disarm and
    manifest expiry, but never bypass pinned identity, generation or root epoch.
    """
    from .jobs import (_dispatcher_binding_valid, _explicit_env_digest,
                       _keyed_fingerprint_from_env_digest, stable_job_id)
    if not _dispatcher_binding_valid(binding):
        return "unavailable"
    if not isinstance(metadata, dict):
        return "mismatch"
    if require_running and not app_server_enabled():
        return "closed"
    try:
        if (metadata.get("job_id") != binding["gateway_job_id"]
                or stable_job_id(binding["op_key"]) != binding["gateway_job_id"]
                or metadata.get("dispatcher_binding") != binding
                or metadata.get("idempotency_key") != binding["op_key"]
                or metadata.get("dispatcher_request_fingerprint") != binding["request_fingerprint"]
                or metadata.get("explicit_env_digest") != _explicit_env_digest({})
                or metadata.get("transport") != APP_SERVER_TRANSPORT):
            return "mismatch"
        try:
            command = validate_app_server_command(metadata.get("command"))
            spec = validate_app_server_spec(metadata.get("app_server"), cwd=metadata.get("cwd"))
        except (OSError, ValueError, TypeError):
            return "mismatch"
        fingerprint = _keyed_fingerprint_from_env_digest(
            command, metadata["cwd"], metadata["explicit_env_digest"], binding["request_fingerprint"], binding,
            transport=APP_SERVER_TRANSPORT, app_server=spec)
        if metadata.get("request_fingerprint") != fingerprint:
            return "mismatch"
        db_path = Path(binding["db_path"]).resolve(strict=True)
        uri = "file:" + quote(str(db_path).replace("\\", "/"), safe="/:\\") + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=0.2)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        try:
            manifest = conn.execute("SELECT * FROM manifests WHERE manifest_id=?", (binding["manifest_id"],)).fetchone()
            root = conn.execute("SELECT max_epoch FROM roots WHERE root_source_id=?", (binding["root_source_id"],)).fetchone()
            job = conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (binding["manifest_id"], binding["job_id"])).fetchone()
            operation = conn.execute("SELECT * FROM ops WHERE op_key=?", (binding["op_key"],)).fetchone()
            execution = conn.execute("SELECT * FROM codex_execution WHERE manifest_id=? AND job_id=?",
                                     (binding["manifest_id"], binding["job_id"])).fetchone()
            if any(row is None for row in (manifest, root, job, operation, execution)):
                return "unavailable"
            raw = bytes(manifest["raw"])
            if (hashlib.sha256(raw).hexdigest() != binding["manifest_digest"]
                    or manifest["digest"] != binding["manifest_digest"]):
                return "mismatch"
            if (manifest["root_source_id"] != binding["root_source_id"] or manifest["root_epoch"] != binding["root_epoch"]
                    or root["max_epoch"] > binding["root_epoch"]):
                return "closed"
            if manifest["generation"] != binding["generation"]:
                return "closed"
            if require_running and (manifest["state"] != "armed" or not time.time() < manifest["expires_at"]):
                return "closed"
            if (job["route"] != "gateway-codex" or job["create_key"] != binding["op_key"]
                    or operation["op"] != "codex_start" or operation["manifest_id"] != binding["manifest_id"]
                    or operation["job_id"] != binding["job_id"] or operation["generation"] != binding["generation"]
                    or execution["route"] != "gateway-codex" or execution["gateway_job_id"] != binding["gateway_job_id"]
                    or execution["manifest_digest"] != binding["manifest_digest"]
                    or execution["request_fingerprint"] != binding["request_fingerprint"]
                    or execution["op_key"] != binding["op_key"] or execution["request_digest"] != binding["request_digest"]
                    or execution["generation"] != binding["generation"]
                    or execution["root_source_id"] != binding["root_source_id"] or execution["root_epoch"] != binding["root_epoch"]):
                return "mismatch"
            if require_running and (job["state"] != "running" or execution["state"] != "running"
                                    or metadata.get("status") != "running"):
                return "closed"
            if operation["state"] not in ("intent", "uncertain", "receipted"):
                return "closed"
            request_digest = hashlib.sha256(operation["request_json"].encode("utf-8")).hexdigest()
            if (request_digest != binding["request_digest"] or request_digest != operation["request_digest"]
                    or request_digest != binding["request_fingerprint"] or operation["request_json"] != job["create_json"]):
                return "mismatch"
            from .work_dispatcher import canonical, parse_manifest
            try:
                pinned_manifest = parse_manifest(raw, binding["manifest_digest"])
                pinned = next((entry for entry in pinned_manifest["jobs"] if entry["job_id"] == binding["job_id"]), None)
                op_request = json.loads(operation["request_json"])
                stored_request = json.loads(job["create_json"])
            except Exception:
                return "mismatch"
            if (pinned is None or pinned.get("route") != "gateway-codex" or pinned["create_key"] != binding["op_key"]
                    or pinned["snapshot_digest"] != job["snapshot_digest"] or job["message"] != pinned["message"]
                    or job["send_key"] != pinned["send_key"]
                    or pinned_manifest["root_source_id"] != manifest["root_source_id"]
                    or pinned_manifest["root_epoch"] != manifest["root_epoch"]
                    or pinned_manifest["expires_at"] != manifest["expires_at"]
                    or operation["request_json"] != canonical(pinned["create_request"])
                    or canonical(stored_request) != canonical(pinned["create_request"])
                    or canonical(op_request) != canonical(pinned["create_request"])):
                return "mismatch"
            request = pinned["create_request"]
            if (request.get("transport") != APP_SERVER_TRANSPORT or request.get("app_server") != spec
                    or request.get("command") != command or request.get("cwd") != metadata.get("cwd")):
                return "mismatch"
            if operation["state"] == "receipted":
                try:
                    receipt = json.loads(operation["receipt_json"] or "null")
                except ValueError:
                    return "mismatch"
                if not isinstance(receipt, dict) or receipt.get("gateway_job_id") != binding["gateway_job_id"]:
                    return "mismatch"
            return "open"
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError, KeyError):
        return "unavailable"


def parse_codex_jsonl(data: bytes, expected_thread_id: str | None = None, max_bytes: int = MAX_JSONL_BYTES) -> dict[str, Any]:
    """Verify a complete own --json stream and select its final agent_message."""
    if not isinstance(data, bytes) or len(data) > max_bytes:
        return {"state": "result_pending", "code": "result_too_large"}
    log_digest = hashlib.sha256(data).hexdigest()
    try:
        text = data.decode("utf-8", errors="strict")
        events = [json.loads(line) for line in text.splitlines() if line.strip()]
    except (UnicodeDecodeError, ValueError, RecursionError):
        return {"state": "result_pending", "code": "invalid_jsonl"}
    if not events or any(not isinstance(event, dict) for event in events):
        return {"state": "result_pending", "code": "empty_or_invalid_jsonl"}
    starts = [event for event in events if event.get("type") == "thread.started"]
    if len(starts) != 1 or not isinstance(starts[0].get("thread_id"), str):
        return {"state": "result_pending", "code": "thread_identity_invalid"}
    thread_id = starts[0]["thread_id"]
    try:
        thread_id = str(uuid.UUID(thread_id)).lower()
    except (ValueError, AttributeError):
        return {"state": "result_pending", "code": "thread_identity_invalid"}
    if expected_thread_id is not None and thread_id != expected_thread_id.lower():
        return {"state": "result_pending", "code": "thread_identity_mismatch"}
    for event in events:
        event_thread = event.get("thread_id")
        if event_thread is not None and event_thread != thread_id:
            return {"state": "result_pending", "code": "thread_identity_conflict"}
    terminals = [(index, event) for index, event in enumerate(events)
                 if event.get("type") in ("turn.completed", "turn.failed", "error")]
    if len(terminals) != 1:
        return {"state": "result_pending", "code": "terminal_record_invalid"}
    terminal_index, terminal = terminals[0]
    if terminal_index != len(events) - 1:
        return {"state": "result_pending", "code": "events_after_terminal"}
    if terminal.get("type") != "turn.completed":
        return {"state": "failed", "code": "turn_failed", "thread_id": thread_id,
                "log_digest": log_digest, "log_bytes": len(data)}
    thread_start_index = next(index for index, event in enumerate(events) if event.get("type") == "thread.started")
    messages: list[tuple[str, Any]] = []
    for index, event in enumerate(events):
        if event.get("type") == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                if index <= thread_start_index or index >= terminal_index or not isinstance(item.get("text"), str):
                    if index <= thread_start_index:
                        continue
                    return {"state": "result_pending", "code": "agent_message_invalid"}
                messages.append((item["text"], item.get("id")))
    if not messages:
        return {"state": "result_pending", "code": "final_message_missing"}
    result_text, final_item_id = messages[-1]
    result_digest = hashlib.sha256(result_text.encode("utf-8")).hexdigest()
    evidence = {"thread_id": thread_id, "result_digest": result_digest, "log_digest": log_digest, "log_bytes": len(data)}
    evidence_digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    result = {"state": "succeeded", "thread_id": thread_id, "result_text": result_text,
              "result_digest": result_digest, "result_chars": len(result_text), "log_digest": log_digest,
              "log_bytes": len(data), "evidence_digest": evidence_digest}
    if isinstance(final_item_id, str):
        result["final_item_id"] = final_item_id
    return result
