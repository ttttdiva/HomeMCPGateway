"""Finite, explicitly armed work dispatcher (separate from the read-only ``work_supervisor`` observer).

A root writes a finite manifest of already-authorized ``claude-cli`` jobs and explicitly arms it with a
finite lease.  For every job this module creates ONE fresh AoiTalk CLI session and sends ONE immutable
prompt, using only the existing ``aoitalk_cli_session_create`` / ``aoitalk_cli_session_send`` wrappers.
It never derives work from worker output, never reconnects / cancels / approves / retries a provider turn,
never writes the observer ledger and never starts or wakes a root.

Durability: an immutable operation *intent* (stable idempotency key + exact request) is committed BEFORE
any I/O; the receipt (or a safe error code) is committed afterwards.  A request whose outcome is unknown
stays ``uncertain`` and is replayed only with the identical key/payload while the same arm is live and the
finite retry budget remains.  Explicit refusals are sticky.  Disarm/expiry stops every NEW POST but cannot
retract a request that was already initiated, and never cancels accepted work.

Completion = the job's own session+run is terminal ``succeeded`` in the observer ledger (opened ``mode=ro``)
AND a digest-verified complete handoff exists for that exact run.  The sha256 of the manifest pins bytes;
it does not authenticate root authorship.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import contextvars
import functools
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import aoitalk, aoitalk_cli_sessions as cli, work_supervisor as ws
from . import jobs as gateway_jobs, work_dispatcher_codex as codex

ROOT_DIR = aoitalk.GATEWAY_ROOT / ".local" / "work-dispatcher"
OBSERVER_DB = ws.ROOT_DIR / "state.sqlite3"
MANIFEST_VERSION = 1
LEDGER_VERSION = 2
POLL_SECONDS = 10.0
MAX_JOBS = 200
MAX_CONCURRENCY = 8
MAX_V2_CONCURRENCY = 16
MAX_RETRY_BUDGET = 10
BACKOFF_BASE = 30.0
BACKOFF_MAX = 600.0
MAX_MANIFEST_BYTES = 8 << 20
UUID_RE = ws.UUID_RE
ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
OBSERVER_TABLES = {
    "runs": {"run_id", "session_id", "status", "message_id"},
    "handoffs": {"handoff_id", "source_type", "source_id", "run_id", "message_id", "terminal_status", "state", "result_text",
                 "result_digest", "result_source"},
    "sessions": {"session_id", "cursor_stalled", "collect_error"},
}
RECEIPT_KEYS = ("session_id", "run_id", "message_id", "status", "agent_id", "agent_revision_id", "binding_id", "provider",
                "model", "working_directory", "permission_mode", "execution_profile_id", "idempotency_key")
VERIFIED_SOURCES = ("cli_output_chunks", "message_get")      # the observer's only validated result sources
VERIFIED_STATES = ("ready", "acked")
OCCUPYING = ("creating", "created", "sending", "sent", "starting", "running", "stopping", "result_pending")
TOP_KEYS = {"version", "manifest_id", "root_source_id", "root_epoch", "created_at", "expires_at", "max_concurrency",
            "uncertain_retry_budget", "jobs"}
JOB_KEYS = {"job_id", "authorization_ref", "create", "message", "resource_key", "depends_on", "root_review"}
MAX_DEPENDS = 32
CREATE_KEYS = {"agent_id", "provider", "model", "working_directory", "effort", "title", "permission_mode",
               "allowed_capabilities", "execution_profile_id"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS roots(root_source_id TEXT PRIMARY KEY, max_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS manifests(
  manifest_id TEXT PRIMARY KEY, digest TEXT NOT NULL UNIQUE, raw BLOB NOT NULL, root_source_id TEXT NOT NULL,
  root_epoch INTEGER NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL, max_concurrency INTEGER NOT NULL,
  retry_budget INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'imported', generation INTEGER NOT NULL DEFAULT 0,
  arm_expires_at REAL, armed_at REAL, disarmed_at REAL, imported_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  manifest_id TEXT NOT NULL, job_id TEXT NOT NULL, ord INTEGER NOT NULL, auth_ref TEXT NOT NULL, resource_key TEXT,
  create_json TEXT NOT NULL, message TEXT NOT NULL, snapshot_digest TEXT NOT NULL, create_key TEXT NOT NULL UNIQUE,
  send_key TEXT NOT NULL UNIQUE, state TEXT NOT NULL DEFAULT 'queued', session_id TEXT, run_id TEXT, message_id TEXT,
  blocked_code TEXT, maybe_executing INTEGER NOT NULL DEFAULT 0, obs_status TEXT, obs_at REAL, handoff_id TEXT,
  result_digest TEXT, result_chars INTEGER, completed_at REAL, updated_at REAL, route TEXT NOT NULL DEFAULT 'aoitalk-claude',
  PRIMARY KEY(manifest_id, job_id));
CREATE TABLE IF NOT EXISTS releases(
  manifest_id TEXT NOT NULL, job_id TEXT NOT NULL, manifest_digest TEXT NOT NULL, root_epoch INTEGER NOT NULL,
  generation INTEGER NOT NULL, binding_json TEXT NOT NULL, released_at REAL NOT NULL, PRIMARY KEY(manifest_id, job_id, generation));
CREATE TRIGGER IF NOT EXISTS releases_no_update BEFORE UPDATE ON releases BEGIN SELECT RAISE(ABORT, 'release_immutable'); END;
CREATE TRIGGER IF NOT EXISTS releases_no_delete BEFORE DELETE ON releases BEGIN SELECT RAISE(ABORT, 'release_immutable'); END;
CREATE UNIQUE INDEX IF NOT EXISTS jobs_session ON jobs(session_id);
CREATE UNIQUE INDEX IF NOT EXISTS jobs_session_ci ON jobs(lower(session_id));
CREATE TABLE IF NOT EXISTS ops(
  op_key TEXT PRIMARY KEY, manifest_id TEXT NOT NULL, job_id TEXT NOT NULL, op TEXT NOT NULL, generation INTEGER NOT NULL,
  request_json TEXT NOT NULL, request_digest TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
  uncertain_seen INTEGER NOT NULL DEFAULT 0, error_code TEXT, next_attempt_at REAL, intent_at REAL NOT NULL,
  io_at REAL, receipt_at REAL, receipt_json TEXT);
CREATE TABLE IF NOT EXISTS codex_execution(
  manifest_id TEXT NOT NULL, job_id TEXT NOT NULL, route TEXT NOT NULL, gateway_job_id TEXT NOT NULL,
  manifest_digest TEXT NOT NULL, op_key TEXT NOT NULL, request_digest TEXT NOT NULL, request_fingerprint TEXT NOT NULL, generation INTEGER NOT NULL,
  root_source_id TEXT NOT NULL, root_epoch INTEGER NOT NULL, thread_id TEXT, result_digest TEXT, log_digest TEXT, log_bytes INTEGER,
  evidence_digest TEXT, result_chars INTEGER, result_ref TEXT, metadata_ref TEXT, state TEXT NOT NULL DEFAULT 'reserved',
  safe_code TEXT, created_at REAL NOT NULL, updated_at REAL, app_server_evidence_json TEXT,
  PRIMARY KEY(manifest_id, job_id), UNIQUE(gateway_job_id));
CREATE UNIQUE INDEX IF NOT EXISTS codex_gateway_job_ci ON codex_execution(lower(gateway_job_id));
"""


class DispatchError(Exception):
    """Carries only a fixed, safe error code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def safe_code(exc: BaseException) -> str:
    if isinstance(exc, DispatchError):
        return exc.code
    if isinstance(exc, ws.SupervisorError):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "ledger_error"
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return "io_error"
    return "unexpected_error"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(value: Any) -> float:
    """Timezone-aware ISO-8601 only."""
    if not isinstance(value, str):
        raise DispatchError("invalid_manifest")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DispatchError("invalid_manifest") from None
    if parsed.tzinfo is None:
        raise DispatchError("invalid_manifest")
    return parsed.timestamp()


# ------------------------------------------------------------------ manifest

def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DispatchError("invalid_manifest")
        result[key] = value
    return result


def _bad_constant(_name: str) -> Any:
    raise DispatchError("invalid_manifest")


def _int(value: Any, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise DispatchError("invalid_manifest")
    return value


def _text(value: Any, pattern: re.Pattern[str] = ID_RE) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise DispatchError("invalid_manifest")
    return value


def op_key(operation: str, digest: str, job_id: str) -> str:
    return f"wd1:{operation}:{hashlib.sha256((digest + chr(0) + job_id + chr(0) + operation).encode('utf-8')).hexdigest()}"


def parse_manifest(raw: bytes, expected_sha256: str | None = None) -> dict[str, Any]:
    """Strict, all-or-nothing validation.  No network, no ledger."""
    if not isinstance(raw, bytes) or not raw or len(raw) > MAX_MANIFEST_BYTES:
        raise DispatchError("invalid_manifest")
    digest = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and (not isinstance(expected_sha256, str) or expected_sha256.lower() != digest):
        raise DispatchError("digest_mismatch")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_dupes, parse_constant=_bad_constant)
    except DispatchError:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise DispatchError("invalid_manifest") from None
    if (not isinstance(data, dict) or set(data) != TOP_KEYS or type(data["version"]) is not int
            or data["version"] not in (1, 2)):
        raise DispatchError("invalid_manifest")
    version = data["version"]
    created, expires = parse_utc(data["created_at"]), parse_utc(data["expires_at"])
    if expires <= created:
        raise DispatchError("invalid_manifest")
    jobs_raw = data["jobs"]
    if not isinstance(jobs_raw, list) or not 1 <= len(jobs_raw) <= MAX_JOBS:
        raise DispatchError("invalid_manifest")
    manifest = {"manifest_id": _text(data["manifest_id"]), "root_source_id": _text(data["root_source_id"]),
                "root_epoch": _int(data["root_epoch"], 0, 2**53), "created_at": created, "expires_at": expires,
                "max_concurrency": _int(data["max_concurrency"], 1, MAX_CONCURRENCY if version == 1 else MAX_V2_CONCURRENCY),
                "retry_budget": _int(data["uncertain_retry_budget"], 0, MAX_RETRY_BUDGET), "digest": digest, "raw": raw, "jobs": []}
    seen: set[str] = set()
    keys: set[str] = set()
    for index, item in enumerate(jobs_raw):
        job = _compile_job(item, digest, index, version=version)
        if job["job_id"] in seen or {job["create_key"], job["send_key"]} & keys or job["create_key"] == job["send_key"]:
            raise DispatchError("invalid_manifest")
        seen.add(job["job_id"])
        keys.update((job["create_key"], job["send_key"]))
        manifest["jobs"].append(job)
    _check_graph(manifest["jobs"])
    return manifest


def _check_graph(jobs: list[dict[str, Any]]) -> None:
    """Dependencies name only jobs of this same manifest: no unknown ID, self-edge or cycle (Kahn)."""
    graph = {j["job_id"]: j["depends_on"] for j in jobs}
    if any(d not in graph or d == jid for jid, deps in graph.items() for d in deps):
        raise DispatchError("invalid_manifest")
    remaining = {jid: set(deps) for jid, deps in graph.items()}
    while remaining:
        free = [jid for jid, deps in remaining.items() if not deps]
        if not free:
            raise DispatchError("invalid_manifest")
        for jid in free:
            del remaining[jid]
        for deps in remaining.values():
            deps.difference_update(free)


def _compile_job(item: Any, digest: str, index: int, *, version: int = 1) -> dict[str, Any]:
    if version == 2:
        return _compile_v2_job(item, digest, index)
    if not isinstance(item, dict) or not {"job_id", "authorization_ref", "create", "message"} <= set(item) or set(item) - JOB_KEYS:
        raise DispatchError("invalid_manifest")
    job_id = _text(item["job_id"])
    create = item["create"]
    if not isinstance(create, dict) or not {"agent_id", "provider", "model", "working_directory"} <= set(create) or set(create) - CREATE_KEYS:
        raise DispatchError("invalid_manifest")
    resource = item.get("resource_key")
    if "resource_key" in item and resource is not None:
        _text(resource)
    depends: tuple[str, ...] = ()
    if "depends_on" in item:
        raw_deps = item["depends_on"]
        if not isinstance(raw_deps, list) or not 1 <= len(raw_deps) <= MAX_DEPENDS:
            raise DispatchError("invalid_manifest")
        depends = tuple(_text(d) for d in raw_deps)
        if len(set(depends)) != len(depends):
            raise DispatchError("invalid_manifest")
    review = False
    if "root_review" in item:
        if item["root_review"] is not True or not depends:        # a gate binds to explicit predecessors; only `true` is meaningful
            raise DispatchError("invalid_manifest")
        review = True
    key = op_key("create", digest, job_id)
    try:
        request: dict[str, Any] = {"agent_id": create["agent_id"], "provider": create["provider"], "model": cli._bounded_text(create["model"], "model", 160),
                                   "working_directory": cli._absolute_directory(create["working_directory"]),
                                   "idempotency_key": cli._key(key)}
        if not isinstance(create["agent_id"], str) or not UUID_RE.fullmatch(create["agent_id"]):
            raise DispatchError("invalid_manifest")
        if create["provider"] != "claude-cli":
            raise DispatchError("invalid_manifest")
        for name, limit in (("effort", 40), ("title", 200)):
            if name in create:
                request[name] = cli._bounded_text(create[name], name, limit)
        if "permission_mode" in create:
            if create["permission_mode"] not in cli.PERMISSION_MODES:
                raise DispatchError("invalid_manifest")
            request["permission_mode"] = create["permission_mode"]
        if "allowed_capabilities" in create:
            caps = create["allowed_capabilities"]
            if (not isinstance(caps, list) or not 1 <= len(caps) <= 3 or len(set(caps)) != len(caps)
                    or any(not isinstance(c, str) or c not in cli.CLI_CAPABILITIES for c in caps)):
                raise DispatchError("invalid_manifest")
            request["allowed_capabilities"] = list(caps)
        if "execution_profile_id" in create:
            profile = create["execution_profile_id"]
            if not isinstance(profile, str) or profile != profile.strip():
                raise DispatchError("invalid_manifest")
            request["execution_profile_id"] = cli._bounded_text(profile, "execution_profile_id", 128, allow_newlines=False)
        message = cli._bounded_text(item["message"], "message", cli.MAX_MESSAGE_CHARS, allow_newlines=True)
        send_key = cli._key(op_key("send", digest, job_id))
        auth_ref = cli._bounded_text(item["authorization_ref"], "authorization_ref", 256, allow_newlines=False)
        for value in (*[v for v in request.values() if isinstance(v, str)], message, auth_ref):
            value.encode("utf-8")
    except cli.CliSessionError:
        raise DispatchError("invalid_manifest") from None
    except UnicodeEncodeError:
        raise DispatchError("invalid_manifest") from None
    snapshot: dict[str, Any] = {"create": request, "message": message, "send_key": send_key}
    if depends:                                      # v1 jobs without these keys keep their historical snapshot digest
        snapshot["depends_on"] = list(depends)
        snapshot["root_review"] = review
    return {"job_id": job_id, "ord": index, "auth_ref": auth_ref, "resource_key": resource, "create_request": request,
            "route": "aoitalk-claude", "message": message, "create_key": key, "send_key": send_key, "depends_on": depends, "root_review": review,
            "snapshot_digest": sha256_text(canonical(snapshot))}


V2_SHARED_JOB_KEYS = {"route", "job_id", "authorization_ref", "message", "resource_key", "depends_on", "root_review"}
V2_CODEX_KEYS = V2_SHARED_JOB_KEYS | {"model", "effort", "node_executable", "codex_entrypoint", "transport", "app_server"}
V2_CLAUDE_KEYS = V2_SHARED_JOB_KEYS | {"create"}


def _v2_graph_fields(item: dict[str, Any]) -> tuple[str, str | None, tuple[str, ...], bool]:
    job_id = _text(item.get("job_id"))
    resource = item.get("resource_key")
    if "resource_key" in item and resource is not None:
        _text(resource)
    depends: tuple[str, ...] = ()
    if "depends_on" in item:
        raw_deps = item["depends_on"]
        if not isinstance(raw_deps, list) or not 1 <= len(raw_deps) <= MAX_DEPENDS:
            raise DispatchError("invalid_manifest")
        depends = tuple(_text(d) for d in raw_deps)
        if len(set(depends)) != len(depends):
            raise DispatchError("invalid_manifest")
    review = False
    if "root_review" in item:
        if item["root_review"] is not True or not depends:
            raise DispatchError("invalid_manifest")
        review = True
    return job_id, resource, depends, review


def _compile_v2_job(item: Any, digest: str, index: int) -> dict[str, Any]:
    if not isinstance(item, dict) or "route" not in item:
        raise DispatchError("invalid_manifest")
    route = item["route"]
    if route not in ("aoitalk-claude", "gateway-codex"):
        raise DispatchError("invalid_manifest")
    allowed = V2_CLAUDE_KEYS if route == "aoitalk-claude" else V2_CODEX_KEYS
    if not {"job_id", "route", "authorization_ref", "message"} <= set(item) or set(item) - allowed:
        raise DispatchError("invalid_manifest")
    job_id, resource, depends, review = _v2_graph_fields(item)
    try:
        message = cli._bounded_text(item["message"], "message", cli.MAX_MESSAGE_CHARS, allow_newlines=True)
        auth_ref = cli._bounded_text(item["authorization_ref"], "authorization_ref", 256, allow_newlines=False)
        message.encode("utf-8")
        auth_ref.encode("utf-8")
        if route == "aoitalk-claude":
            legacy_item = {key: value for key, value in item.items() if key != "route"}
            compiled = _compile_job(legacy_item, digest, index, version=1)
            snapshot = {"route": route, "legacy_snapshot": compiled["snapshot_digest"]}
            if depends:
                snapshot.update(depends_on=list(depends), root_review=review)
            compiled.update(route=route, auth_ref=auth_ref, resource_key=resource, depends_on=depends, root_review=review,
                            snapshot_digest=sha256_text(canonical(snapshot)))
            return compiled
        model = cli._bounded_text(item["model"], "model", 160, allow_newlines=False)
        effort = item["effort"]
        if not isinstance(effort, str) or effort not in {"low", "medium", "high", "xhigh", "max"}:
            raise DispatchError("invalid_manifest")
        node = codex.canonical_file(item["node_executable"])
        if Path(node).name.casefold() not in {"node", "node.exe"}:
            raise DispatchError("invalid_manifest")
        entrypoint = codex.canonical_file(item["codex_entrypoint"], official_entrypoint=True)
        gateway_root = str(Path(__file__).resolve().parents[1])
        start_key = op_key("codex_start", digest, job_id)
        unused_send_key = op_key("codex_unused", digest, job_id)
        if "transport" in item:
            if item["transport"] != codex.APP_SERVER_TRANSPORT:
                raise DispatchError("invalid_manifest")
            spec = {"model": model, "model_provider": "openai", "effort": effort, "input_text": message,
                    "cwd": gateway_root, "sandbox": "read-only", "approval_policy": "never", "approvals_reviewer": "user"}
            if "app_server" in item:
                codex.validate_app_server_spec(item["app_server"], model=model, effort=effort, input_text=message, cwd=gateway_root)
                if item["app_server"] != spec:
                    raise DispatchError("invalid_manifest")
            argv = codex.make_app_server_argv(node, entrypoint)
            launch_request = {"command": argv, "cwd": gateway_root, "model": model, "effort": effort,
                              "transport": codex.APP_SERVER_TRANSPORT, "app_server": spec}
        else:
            if "app_server" in item:
                raise DispatchError("invalid_manifest")
            argv = codex.make_argv(node, entrypoint, model, effort, message)
            codex.validate_command_line(argv)
            launch_request = {"command": argv, "cwd": gateway_root, "model": model, "effort": effort}
        snapshot = {"route": route, "authorization_ref": auth_ref, "launch": launch_request, "depends_on": list(depends),
                    "root_review": review, "resource_key": resource}
        return {"job_id": job_id, "ord": index, "auth_ref": auth_ref, "resource_key": resource,
                "create_request": launch_request, "message": message, "create_key": start_key, "send_key": unused_send_key,
                "route": route, "depends_on": depends, "root_review": review,
                "snapshot_digest": sha256_text(canonical(snapshot))}
    except cli.CliSessionError:
        raise DispatchError("invalid_manifest") from None
    except (UnicodeEncodeError, OSError, ValueError):
        raise DispatchError("invalid_manifest") from None


# ------------------------------------------------------------------ ledger / lock / log

def connect(directory: Path = ROOT_DIR) -> sqlite3.Connection:
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "state.sqlite3", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    job_columns = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    if "route" not in job_columns:
        conn.execute("ALTER TABLE jobs ADD COLUMN route TEXT NOT NULL DEFAULT 'aoitalk-claude'")
    execution_columns = {r[1] for r in conn.execute("PRAGMA table_info(codex_execution)")}
    if "app_server_evidence_json" not in execution_columns:
        conn.execute("ALTER TABLE codex_execution ADD COLUMN app_server_evidence_json TEXT")
    conn.execute("INSERT INTO meta(key,value) VALUES('ledger_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (str(LEDGER_VERSION),))
    return conn


@contextlib.contextmanager
def txn(conn: sqlite3.Connection):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


class DispatcherLock(ws.SingletonLock):
    """OS singleton for the dispatcher; its own file, never the observer's."""

    def __init__(self, directory: Path = ROOT_DIR):
        super().__init__(directory)
        self.path = directory / "dispatcher.lock"


LOG_NAME = "dispatcher.log"
LOG_MAX_BYTES = 1 << 20
LOG_BACKUPS = 5
_LOG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")


def log_event(directory: Path, event: str, **fields: Any) -> None:
    """Identifier-shaped values only; never prompts, output, paths, secrets or exception text."""
    try:
        parts = [datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), f"pid={os.getpid()}", event]
        for key, value in fields.items():
            text = str(value)
            parts.append(f"{key}={text if _LOG_VALUE_RE.fullmatch(text) else 'redacted'}")
        target = directory / LOG_NAME
        if target.exists() and target.stat().st_size >= LOG_MAX_BYTES:
            for index in range(LOG_BACKUPS, 0, -1):
                older = directory / f"{LOG_NAME}.{index}"
                newer = directory / f"{LOG_NAME}.{index - 1}" if index > 1 else target
                if newer.exists():
                    os.replace(newer, older)
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(" ".join(parts) + "\n")
    except Exception:  # noqa: BLE001
        pass


def write_status(directory: Path, status: dict[str, Any]) -> None:
    tmp = directory / f"status.json.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(status, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, directory / "status.json")


# ------------------------------------------------------------------ arm / disarm

def arm(conn: sqlite3.Connection, raw: bytes, expected_sha256: str, arm_expires_at: float, now: float) -> dict[str, Any]:
    """Import immutable local state and start a finite lease.  Performs no network I/O."""
    m = parse_manifest(raw, expected_sha256)
    if not now < arm_expires_at <= m["expires_at"] or m["expires_at"] <= now:
        raise DispatchError("invalid_expiry")
    with txn(conn):
        existing = conn.execute("SELECT * FROM manifests WHERE manifest_id=?", (m["manifest_id"],)).fetchone()
        if existing is not None and existing["digest"] != m["digest"]:
            raise DispatchError("manifest_id_rebound")
        if conn.execute("SELECT 1 FROM manifests WHERE digest=? AND manifest_id<>?", (m["digest"], m["manifest_id"])).fetchone():
            raise DispatchError("manifest_id_rebound")
        root = conn.execute("SELECT max_epoch FROM roots WHERE root_source_id=?", (m["root_source_id"],)).fetchone()
        if root is not None and m["root_epoch"] < root["max_epoch"]:
            raise DispatchError("epoch_rollback")
        if existing is None:
            conn.execute("INSERT INTO manifests(manifest_id,digest,raw,root_source_id,root_epoch,created_at,expires_at,max_concurrency,"
                         "retry_budget,imported_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (m["manifest_id"], m["digest"], raw, m["root_source_id"], m["root_epoch"], m["created_at"], m["expires_at"],
                          m["max_concurrency"], m["retry_budget"], now))
            for job in m["jobs"]:
                conn.execute("INSERT INTO jobs(manifest_id,job_id,ord,auth_ref,resource_key,create_json,message,snapshot_digest,create_key,"
                             "send_key,updated_at,route) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                             (m["manifest_id"], job["job_id"], job["ord"], job["auth_ref"], job["resource_key"],
                              canonical(job["create_request"]), job["message"], job["snapshot_digest"], job["create_key"], job["send_key"], now,
                              job.get("route", "aoitalk-claude")))
        conn.execute("INSERT INTO roots VALUES(?,?) ON CONFLICT(root_source_id) DO UPDATE SET max_epoch=MAX(max_epoch, excluded.max_epoch)",
                     (m["root_source_id"], m["root_epoch"]))
        row = conn.execute("SELECT * FROM manifests WHERE manifest_id=?", (m["manifest_id"],)).fetchone()
        live = row["state"] == "armed" and (row["arm_expires_at"] or 0) > now
        changed = not live          # a live lease is never extended or reset by re-importing
        if changed:
            conn.execute("UPDATE manifests SET state='armed', generation=generation+1, arm_expires_at=?, armed_at=?, disarmed_at=NULL"
                         " WHERE manifest_id=?", (arm_expires_at, now, m["manifest_id"]))
    row = conn.execute("SELECT generation, arm_expires_at FROM manifests WHERE manifest_id=?", (m["manifest_id"],)).fetchone()
    return {"ok": True, "manifest_id": m["manifest_id"], "digest": m["digest"], "jobs": len(m["jobs"]), "generation": row["generation"],
            "arm_expires_at": iso(row["arm_expires_at"]), "changed": changed}


def disarm(conn: sqlite3.Connection, manifest_id: str, now: float) -> dict[str, Any]:
    """Stops all NEW POSTs.  Does not cancel, retract, or delete anything."""
    with txn(conn):
        count = conn.execute("UPDATE manifests SET state='disarmed', disarmed_at=? WHERE manifest_id=? AND state='armed'",
                             (now, manifest_id)).rowcount
        if conn.execute("SELECT 1 FROM manifests WHERE manifest_id=?", (manifest_id,)).fetchone() is None:
            raise DispatchError("not_found")
    return {"ok": True, "manifest_id": manifest_id, "disarmed": bool(count), "remote_cancel": False,
            "note": "accepted work is not cancelled; an already-initiated request cannot be retracted"}


# ------------------------------------------------------------------ dependencies / root-review release

@functools.lru_cache(maxsize=64)
def _graph_of(raw: bytes, digest: str) -> dict[str, tuple[tuple[str, ...], bool]] | None:
    try:
        return {j["job_id"]: (j["depends_on"], j["root_review"]) for j in parse_manifest(raw, digest)["jobs"]}
    except DispatchError:
        return None


def _binding(rows: dict[str, sqlite3.Row], deps: tuple[str, ...],
             codex_rows: dict[str, sqlite3.Row] | None = None) -> str:
    codex_rows = codex_rows or {}
    binding: dict[str, Any] = {}
    for dep in deps:
        row = rows[dep]
        if row["route"] == "gateway-codex":
            execution = codex_rows.get(dep)
            if execution is None:
                return ""
            binding[dep] = {"route": "gateway-codex", "gateway_job_id": execution["gateway_job_id"],
                            "request_fingerprint": execution["request_fingerprint"], "thread_id": execution["thread_id"],
                            "result_digest": execution["result_digest"], "log_digest": execution["log_digest"]}
        else:
            # Keep the historical v1 Claude-only release bytes exactly stable.
            binding[dep] = {"session_id": str(row["session_id"]).lower(), "run_id": str(row["run_id"]).lower(),
                            "result_digest": row["result_digest"]}
    return canonical(binding)


def dependency_view(m: sqlite3.Row, graph: dict[str, tuple[tuple[str, ...], bool]], rows: dict[str, sqlite3.Row],
                    releases: dict[str, sqlite3.Row], roots_max: int, uncertain: frozenset[str] = frozenset(),
                    codex_rows: dict[str, sqlite3.Row] | None = None) -> dict[str, tuple[str, str]]:
    """job_id -> (ready|waiting|review|failed, safe code) for jobs that declare ``depends_on``; others are absent (= ready).

    A predecessor satisfies a dependency ONLY as ``succeeded`` (own session+run, digest-verified handoff, written by
    ``reconcile``).  That proves execution finished, not that its content is adopted; adoption is the root-review gate."""
    memo: dict[str, tuple[str, str]] = {}
    codex_rows = codex_rows or {}

    def pred(pid: str) -> tuple[str, str]:
        row = rows.get(pid)
        if row is None:
            return "fail", "dep_missing"
        state = row["state"]
        if state == "succeeded":
            if row["route"] == "gateway-codex":
                execution = codex_rows.get(pid)
                ok = (execution is not None and execution["state"] == "succeeded" and execution["gateway_job_id"]
                      and execution["request_fingerprint"] and execution["thread_id"] and execution["result_digest"]
                      and execution["log_digest"] and execution["evidence_digest"])
                return ("ok", "") if ok else ("fail", "dep_unverified")
            ok = all(row[k] for k in ("session_id", "run_id", "handoff_id", "result_digest"))
            return ("ok", "") if ok else ("fail", "dep_unverified")
        if state == "failed":
            return "fail", "dep_cancelled" if row["obs_status"] == "cancelled" else "dep_failed"
        if state == "blocked":
            return "fail", "dep_uncertain" if row["maybe_executing"] else "dep_denied"
        if state == "sent":
            return "wait", "dep_result_pending" if row["obs_status"] == "result_pending" else "dep_running"
        if state == "queued":
            return ("fail", "dep_upstream_failed") if view(pid)[0] == "failed" else ("wait", "dep_pending")
        if jid_uncertain(pid):          # the job's own current create/send op is unresolved-uncertain (lost response / budget spent)
            return "wait", "dep_uncertain"
        return "wait", "dep_pending"

    def jid_uncertain(pid: str) -> bool:
        return pid in uncertain

    def view(jid: str) -> tuple[str, str]:
        if jid not in memo:
            deps, review = graph.get(jid, ((), False))
            outcome = ("ready", "")
            if deps:
                parts = [pred(d) for d in deps]
                fail = next((p for p in parts if p[0] == "fail"), None)
                wait = next((p for p in parts if p[0] == "wait"), None)
                if fail:
                    outcome = ("failed", fail[1])
                elif wait:
                    outcome = ("waiting", wait[1])
                elif review:
                    rel = releases.get(jid)
                    if not (rel is not None and rel["manifest_digest"] == m["digest"] and rel["root_epoch"] == m["root_epoch"]
                            and rel["generation"] == m["generation"] and roots_max <= m["root_epoch"]
                            and rel["binding_json"] == _binding(rows, deps, codex_rows)):
                        outcome = ("review", "root_review_wait")
            memo[jid] = outcome
        return memo[jid]

    return {jid: view(jid) for jid, (deps, _r) in graph.items() if deps}


def load_dependency_view(conn: sqlite3.Connection, m: sqlite3.Row) -> tuple[dict[str, Any] | None, dict[str, tuple[str, str]]]:
    graph = _graph_of(bytes(m["raw"]), m["digest"])
    if graph is None:
        return None, {}
    if not any(deps for deps, _r in graph.values()):
        return graph, {}
    rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM jobs WHERE manifest_id=?", (m["manifest_id"],))}
    releases = {r["job_id"]: r for r in conn.execute("SELECT * FROM releases WHERE manifest_id=? AND generation=?", (m["manifest_id"], m["generation"]))}
    codex_rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM codex_execution WHERE manifest_id=?", (m["manifest_id"],))}
    root = conn.execute("SELECT max_epoch FROM roots WHERE root_source_id=?", (m["root_source_id"],)).fetchone()
    uncertain = set()
    for r in rows.values():
        key = r["create_key"] if r["state"] == "creating" else r["send_key"] if r["state"] == "sending" else None
        if key is not None:
            op = conn.execute("SELECT state FROM ops WHERE op_key=? AND manifest_id=? AND job_id=?", (key, m["manifest_id"], r["job_id"])).fetchone()
            if op is not None and op["state"] == "uncertain":
                uncertain.add(r["job_id"])
    return graph, dependency_view(m, graph, rows, releases, root["max_epoch"] if root else 0, frozenset(uncertain), codex_rows)


def release(conn: sqlite3.Connection, manifest_id: str, job_id: str, manifest_sha256: str, root_epoch: int,
            bindings: dict[str, tuple[str, str, str]], now: float, *, generation: int,
            codex_bindings: dict[str, dict[str, str]] | None = None) -> dict[str, Any]:
    """Trusted-operator record of the root's OWN adoption decision for one ``root_review`` job.

    Binds manifest digest + root epoch + current arm generation + the exact (session, run, result digest) of every
    predecessor.  It sends nothing, arms nothing, extends no lease, and is not an approval of any native/AutoReview
    refusal.  ``generation`` is the arm generation the root confirmed; it is never defaulted from the ledger, so a saved
    command cannot be re-applied to a later arm.  Immutable: an identical repeat is a no-op, anything else for the same job is a conflict."""
    if (not isinstance(manifest_sha256, str) or not SHA_RE.fullmatch(manifest_sha256.lower()) or type(root_epoch) is not int
            or type(generation) is not int or generation < 1):
        raise DispatchError("invalid_release")
    clean: dict[str, tuple[str, str, str]] = {}
    for pid, (session_id, run_id, digest) in bindings.items():
        if not (UUID_RE.fullmatch(session_id) and UUID_RE.fullmatch(run_id) and SHA_RE.fullmatch(digest.lower())):
            raise DispatchError("invalid_release")
        clean[pid] = (session_id.lower(), run_id.lower(), digest.lower())
    clean_codex: dict[str, dict[str, str]] = {}
    for pid, value in (codex_bindings or {}).items():
        if (not isinstance(pid, str) or not isinstance(value, dict)
                or set(value) != {"gateway_job_id", "request_fingerprint", "result_digest", "thread_id", "log_digest"}):
            raise DispatchError("invalid_release")
        try:
            gateway_id = str(uuid.UUID(value["gateway_job_id"])).lower()
            thread_id = str(uuid.UUID(value["thread_id"])).lower()
        except (ValueError, AttributeError, TypeError):
            raise DispatchError("invalid_release") from None
        if any(not isinstance(value.get(k), str) or not SHA_RE.fullmatch(value[k])
               for k in ("request_fingerprint", "result_digest", "log_digest")):
            raise DispatchError("invalid_release")
        clean_codex[pid] = {"gateway_job_id": gateway_id, "request_fingerprint": value["request_fingerprint"],
                            "result_digest": value["result_digest"], "thread_id": thread_id, "log_digest": value["log_digest"]}
    with txn(conn):
        m = conn.execute("SELECT * FROM manifests WHERE manifest_id=?", (manifest_id,)).fetchone()
        if m is None:
            raise DispatchError("not_found")
        if m["digest"] != manifest_sha256.lower() or hashlib.sha256(m["raw"]).hexdigest() != m["digest"]:
            raise DispatchError("digest_mismatch")
        graph = _graph_of(bytes(m["raw"]), m["digest"]) or {}
        deps, review = graph.get(job_id, ((), False))
        if not review:
            raise DispatchError("no_review_gate")
        if root_epoch != m["root_epoch"]:
            raise DispatchError("epoch_mismatch")
        root = conn.execute("SELECT max_epoch FROM roots WHERE root_source_id=?", (m["root_source_id"],)).fetchone()
        if root is not None and root["max_epoch"] > root_epoch:
            raise DispatchError("epoch_rollback")
        if generation != m["generation"]:
            raise DispatchError("generation_mismatch")
        if m["state"] != "armed" or not now < (m["arm_expires_at"] or 0) or not now < m["expires_at"]:
            raise DispatchError("not_live")
        rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM jobs WHERE manifest_id=?", (manifest_id,))}
        codex_rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM codex_execution WHERE manifest_id=?", (manifest_id,))}
        if set(clean) & set(clean_codex) or set(clean) | set(clean_codex) != set(deps):
            raise DispatchError("binding_incomplete")
        for pid in deps:
            r = rows[pid]
            if r["route"] == "gateway-codex":
                supplied = clean_codex.get(pid)
                observed = codex_rows.get(pid)
                if supplied is None or observed is None:
                    raise DispatchError("binding_incomplete")
                expected = {"gateway_job_id": observed["gateway_job_id"], "request_fingerprint": observed["request_fingerprint"],
                            "result_digest": observed["result_digest"], "thread_id": observed["thread_id"],
                            "log_digest": observed["log_digest"]}
                if r["state"] != "succeeded" or observed["state"] != "succeeded" or supplied != expected:
                    raise DispatchError("predecessor_mismatch")
            else:
                supplied = clean.get(pid)
                if supplied is None:
                    raise DispatchError("binding_incomplete")
                session_id, run_id, digest = supplied
                if (r["state"] != "succeeded" or str(r["session_id"]).lower() != session_id or str(r["run_id"]).lower() != run_id
                        or r["result_digest"] != digest or not r["handoff_id"]):
                    raise DispatchError("predecessor_mismatch")
        binding = _binding(rows, deps, codex_rows)
        existing = conn.execute("SELECT * FROM releases WHERE manifest_id=? AND job_id=? AND generation=?", (manifest_id, job_id, m["generation"])).fetchone()
        if existing is not None:
            same = (existing["manifest_digest"] == m["digest"] and existing["root_epoch"] == root_epoch
                    and existing["generation"] == m["generation"] and existing["binding_json"] == binding)
            if not same:
                raise DispatchError("decision_conflict")
            return {"ok": True, "manifest_id": manifest_id, "job_id": job_id, "generation": m["generation"], "changed": False}
        if rows[job_id]["state"] != "queued":
            raise DispatchError("job_not_queued")
        conn.execute("INSERT INTO releases VALUES(?,?,?,?,?,?,?)", (manifest_id, job_id, m["digest"], root_epoch, m["generation"], binding, now))
    return {"ok": True, "manifest_id": manifest_id, "job_id": job_id, "generation": m["generation"], "changed": True,
            "note": "records the root's decision only; nothing is armed, extended or sent"}


# ------------------------------------------------------------------ backend

_GUARD: contextvars.ContextVar[Callable[[], bool] | None] = contextvars.ContextVar("work_dispatcher_guard", default=None)


class _GateClosed(Exception):
    pass


class CliBackend:
    """The only mutating surface: the two existing wrappers, called exactly as authorized.

    The wrappers log in before they POST; a lease can lapse during that wait.  While a wrapper runs, ``cli._session`` is
    wrapped so the dispatcher's guard is evaluated after login and immediately before the wrapper's single request."""

    async def _call(self, wrapper: Callable[..., Any], *args: Any, **kwargs: Any) -> dict[str, Any]:
        original = cli._session

        async def guarded(fn: Callable[[Any], Any]) -> Any:
            async def gated(client: Any) -> Any:
                guard = _GUARD.get()
                if guard is not None and not guard():
                    raise _GateClosed
                return await fn(client)
            return await original(gated)
        cli._session = guarded
        try:
            return await wrapper(*args, **kwargs)
        except _GateClosed:
            return {"ok": False, "error": "gate_closed", "gate_closed": True, "submission_uncertain": False}
        finally:
            cli._session = original

    async def create(self, **request: Any) -> dict[str, Any]:
        return await self._call(cli.aoitalk_cli_session_create, **request)

    async def send(self, session_id: str, message: str, idempotency_key: str) -> dict[str, Any]:
        return await self._call(cli.aoitalk_cli_session_send, session_id, message, idempotency_key)


def _clean_code(value: Any) -> str:
    return value if isinstance(value, str) and CODE_RE.fullmatch(value) else "error"


def classify(result: Any) -> tuple[str, str | None]:
    if isinstance(result, dict) and result.get("ok") is True:
        return "ok", None
    if not isinstance(result, dict):
        return "uncertain", "bad_response"
    code = _clean_code(result.get("error"))
    return ("uncertain" if result.get("submission_uncertain") is True else "denied"), code


def _fields(data: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    nested = data.get("session")
    for source in ((nested,) if isinstance(nested, dict) else ()) + (data,):
        for key in RECEIPT_KEYS:
            if key in source and key not in merged:
                merged[key] = source[key]
    return merged


def _conflicting(data: dict[str, Any]) -> bool:
    """Top-level and nested ``session`` identity fields that are both present must agree."""
    nested = data.get("session")
    if not isinstance(nested, dict):
        return False
    for key in RECEIPT_KEYS:
        if key in nested and key in data:
            a, b = nested[key], data[key]
            if isinstance(a, str) and isinstance(b, str):
                same = _norm_path(a) == _norm_path(b) if key == "working_directory" else a.lower() == b.lower()
            else:
                same = a == b
            if not same:
                return True
    return False


def _norm_path(value: str) -> str:
    return value.replace("/", "\\").rstrip("\\").lower()


def _same(a: Any, b: Any) -> bool:
    return isinstance(a, str) and isinstance(b, str) and a.lower() == b.lower()


def _bounded_receipt(fields: dict[str, Any]) -> str:
    safe = {k: (v[:200] if isinstance(v, str) else v) for k, v in fields.items() if isinstance(v, (str, int, bool)) or v is None}
    return canonical(safe)


def validate_create_receipt(data: dict[str, Any], request: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    fields = _fields(data)
    if _conflicting(data):
        return "binding_mismatch", fields
    sid = fields.get("session_id")
    if not isinstance(sid, str) or not UUID_RE.fullmatch(sid):
        return "binding_missing", fields
    fields["session_id"] = sid.lower()          # one physical session = one canonical key, whatever the case returned
    for name in ("agent_id", "provider", "model"):
        if name in fields and not _same(fields[name], request[name]):
            return "binding_mismatch", fields
    if "working_directory" in fields and not (isinstance(fields["working_directory"], str)
                                              and _norm_path(fields["working_directory"]) == _norm_path(request["working_directory"])):
        return "binding_mismatch", fields
    for name in ("permission_mode", "execution_profile_id"):
        if name in request and name in fields and fields[name] != request[name]:
            return "binding_mismatch", fields
    return None, fields


def validate_send_receipt(data: dict[str, Any], session_id: str, create: dict[str, Any] | None = None) -> tuple[str | None, dict[str, Any]]:
    fields = _fields(data)
    if _conflicting(data):
        return "binding_mismatch", fields
    if "session_id" in fields and not _same(fields["session_id"], session_id):
        return "binding_mismatch", fields
    if create is not None:
        for name in ("agent_id", "provider", "model"):
            if name in fields and not _same(fields[name], create[name]):
                return "binding_mismatch", fields
        if "working_directory" in fields and not (isinstance(fields["working_directory"], str)
                                                  and _norm_path(fields["working_directory"]) == _norm_path(create["working_directory"])):
            return "binding_mismatch", fields
        for name in ("permission_mode", "execution_profile_id"):
            if name in create and name in fields and fields[name] != create[name]:
                return "binding_mismatch", fields
    run = data.get("run_id", data.get("agent_run_id"))
    if run is not None:
        if not isinstance(run, str) or not UUID_RE.fullmatch(run):
            return "binding_invalid", fields
        fields["run_id"] = run.lower()
    message = fields.get("message_id")
    if message is not None and (not isinstance(message, str) or not ID_RE.fullmatch(message)):
        fields.pop("message_id")
    return None, fields


# ------------------------------------------------------------------ observer (read-only)

def read_observer(path: Path, session_id: str, run_id: str | None) -> dict[str, Any]:
    """Local, read-only view of the observer ledger for ONE job.  Never writes or migrates it."""
    if not path.exists():
        return {"state": "observer_unavailable", "code": "observer_missing"}
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return {"state": "observer_unavailable", "code": "observer_busy"}
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        for table, columns in OBSERVER_TABLES.items():
            have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if not columns <= have:
                return {"state": "observer_unavailable", "code": "observer_incompatible"}
        runs = conn.execute("SELECT run_id, status, message_id FROM runs WHERE lower(session_id)=?", (session_id.lower(),)).fetchall()
        if run_id is None:
            # The send receipt gave no run ID and the observer holds no trigger-message/idempotency binding, so no
            # run of this session can be shown to be THIS send's run.  Never infer it from "the only run".
            return {"state": "unknown", "code": "run_unbound"}
        match = [r for r in runs if r["run_id"].lower() == run_id]
        if not match:
            return {"state": "unknown", "code": "run_not_observed", "run_id": run_id}
        status = match[0]["status"]
        if status in ("failed", "cancelled"):
            return {"state": "failed", "status": status, "run_id": run_id}
        if status != "succeeded":
            return {"state": "waiting" if status == "awaiting_approval" else "running" if status in ("queued", "running") else "unknown",
                    "status": status, "run_id": run_id}
        handoffs = [h for h in conn.execute(
            "SELECT handoff_id, message_id, result_source, terminal_status, state, result_text, result_digest FROM handoffs WHERE source_type='cli_session'"
            " AND lower(source_id)=? AND lower(run_id)=?", (session_id.lower(), run_id)) if h["state"] != "superseded_v1"]
        if len(handoffs) != 1:
            return {"state": "result_pending", "code": "handoff_missing" if not handoffs else "handoff_conflict", "run_id": run_id}
        h = handoffs[0]
        text = h["result_text"]
        session = conn.execute("SELECT cursor_stalled, collect_error FROM sessions WHERE lower(session_id)=?", (session_id.lower(),)).fetchone()
        expected_message = match[0]["message_id"]
        if (not isinstance(text, str) or not text or sha256_text(text) != h["result_digest"] or h["terminal_status"] in ("failed", "cancelled")
                or h["state"] not in VERIFIED_STATES or h["result_source"] not in VERIFIED_SOURCES or not h["message_id"]
                or (expected_message and str(expected_message).lower() != str(h["message_id"]).lower())
                or session is None or session["cursor_stalled"] or session["collect_error"]):
            return {"state": "result_pending", "code": "handoff_unverified", "run_id": run_id}
        return {"state": "succeeded", "run_id": run_id, "handoff_id": h["handoff_id"], "digest": h["result_digest"], "chars": len(text)}
    except sqlite3.Error:
        return {"state": "observer_unavailable", "code": "observer_busy"}
    finally:
        conn.close()


# ------------------------------------------------------------------ dispatcher

class Dispatcher:
    def __init__(self, conn: sqlite3.Connection, backend: Any | None = None, *, now: Callable[[], float] = time.time,
                 observer_path: Path = OBSERVER_DB, fault: Callable[[str], None] | None = None,
                 codex_backend: Any | None = None):
        self.conn = conn
        self.backend = backend or CliBackend()
        self.codex_backend = codex_backend or codex.GatewayJobAdapter()
        self.now = now
        self.observer_path = observer_path
        self.fault = fault or (lambda _name: None)
        self._compiled: dict[tuple[str, str, str], dict[str, Any]] = {}

    # -- gates
    def _gate(self, manifest_id: str, generation: int) -> sqlite3.Row | None:
        """Live arm, same generation, unexpired, byte-identical snapshot.  Called inside the txn that precedes every POST."""
        m = self.conn.execute("SELECT * FROM manifests WHERE manifest_id=?", (manifest_id,)).fetchone()
        now = self.now()
        if (m is None or m["state"] != "armed" or m["generation"] != generation or not now < (m["arm_expires_at"] or 0)
                or not now < m["expires_at"] or hashlib.sha256(m["raw"]).hexdigest() != m["digest"]):
            return None
        return m

    def _live_manifests(self) -> list[sqlite3.Row]:
        now = self.now()
        return [m for m in self.conn.execute("SELECT * FROM manifests WHERE state='armed' ORDER BY imported_at, manifest_id")
                if now < (m["arm_expires_at"] or 0) and now < m["expires_at"]]

    def _block(self, manifest_id: str, job_id: str, code: str, *, executing: bool = False) -> None:
        self.conn.execute("UPDATE jobs SET state='blocked', blocked_code=?, maybe_executing=MAX(maybe_executing, ?), updated_at=?"
                          " WHERE manifest_id=? AND job_id=?", (code, int(executing), self.now(), manifest_id, job_id))

    def _pinned(self, job: sqlite3.Row) -> dict[str, Any] | None:
        try:
            m = self.conn.execute("SELECT raw, digest FROM manifests WHERE manifest_id=?", (job["manifest_id"],)).fetchone()
            key = (job["manifest_id"], m["digest"], hashlib.sha256(m["raw"]).hexdigest())
            if key not in self._compiled:       # compiled from the byte-verified manifest; the key re-hashes the stored bytes each time
                self._compiled[key] = {j["job_id"]: j for j in parse_manifest(m["raw"], m["digest"])["jobs"]}
            return self._compiled[key].get(job["job_id"])
        except (DispatchError, ValueError, TypeError):
            return None

    def _snapshot_ok(self, job: sqlite3.Row) -> bool:
        """Job row must equal the compile of the digest-pinned manifest bytes (not merely its own stored hash)."""
        pinned = self._pinned(job)
        try:
            create = json.loads(job["create_json"])
        except (ValueError, TypeError):
            return False
        return (pinned is not None and pinned["snapshot_digest"] == job["snapshot_digest"] and canonical(create) == canonical(pinned["create_request"])
                and job["message"] == pinned["message"] and job["send_key"] == pinned["send_key"] and job["create_key"] == pinned["create_key"]
                and job["route"] == pinned.get("route", "aoitalk-claude"))

    def _codex_binding(self, execution: sqlite3.Row) -> dict[str, Any]:
        database = next((row[2] for row in self.conn.execute("PRAGMA database_list") if row[1] == "main"), "")
        if not database:
            raise DispatchError("dispatcher_binding_unavailable")
        return {"db_path": str(Path(database).resolve()), "manifest_id": execution["manifest_id"],
                "manifest_digest": execution["manifest_digest"], "root_source_id": execution["root_source_id"],
                "root_epoch": execution["root_epoch"], "generation": execution["generation"],
                "job_id": execution["job_id"], "op_key": execution["op_key"],
                "request_digest": execution["request_digest"], "gateway_job_id": execution["gateway_job_id"],
                "request_fingerprint": execution["request_fingerprint"]}

    def _prefire(self, op: sqlite3.Row) -> bool:
        """Last check before the backend call: live lease/clock, pinned snapshot, op payload unchanged."""
        op = self.conn.execute("SELECT * FROM ops WHERE op_key=?", (op["op_key"],)).fetchone()       # re-read: never trust the earlier copy
        if op is None or op["state"] not in ("intent", "uncertain"):
            return False
        if self._gate(op["manifest_id"], op["generation"]) is None:
            if op["op"] == "codex_start":
                with txn(self.conn):
                    self.conn.execute("UPDATE ops SET state='denied', error_code='gate_closed', receipt_at=? WHERE op_key=?",
                                      (self.now(), op["op_key"]))
                    self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code='gate_closed', maybe_executing=0, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (self.now(), op["manifest_id"], op["job_id"]))
                    self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code='gate_closed', updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (self.now(), op["manifest_id"], op["job_id"]))
            return False
        job = self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (op["manifest_id"], op["job_id"])).fetchone()
        if op["op"] == "codex_start" and job is not None:
            pinned_launch = self._pinned(job)
            if (pinned_launch is not None and pinned_launch["create_request"].get("transport") == codex.APP_SERVER_TRANSPORT
                    and not codex.app_server_enabled()):
                with txn(self.conn):
                    self.conn.execute("UPDATE ops SET state='denied', error_code='app_server_disabled', receipt_at=? WHERE op_key=?",
                                      (self.now(), op["op_key"]))
                    self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code='app_server_disabled', maybe_executing=0, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (self.now(), op["manifest_id"], op["job_id"]))
                    self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code='app_server_disabled', updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (self.now(), op["manifest_id"], op["job_id"]))
                return False
        ok = job is not None and self._snapshot_ok(job) and sha256_text(op["request_json"]) == op["request_digest"]
        if ok:
            pinned = self._pinned(job)
            if op["op"] == "codex_start":
                expected = canonical(pinned["create_request"])
                execution = self.conn.execute("SELECT * FROM codex_execution WHERE manifest_id=? AND job_id=?",
                                              (op["manifest_id"], op["job_id"])).fetchone()
                try:
                    binding = self._codex_binding(execution) if execution is not None else None
                except (DispatchError, OSError, ValueError):
                    binding = None
                ok = (job["route"] == "gateway-codex" and job["state"] == "starting" and op["op_key"] == pinned["create_key"]
                      and execution is not None and execution["route"] == "gateway-codex"
                      and execution["generation"] == op["generation"] and execution["op_key"] == op["op_key"]
                      and execution["request_digest"] == op["request_digest"]
                      and execution["request_fingerprint"] == op["request_digest"]
                      and execution["manifest_digest"] == self.conn.execute("SELECT digest FROM manifests WHERE manifest_id=?",
                                                                            (op["manifest_id"],)).fetchone()["digest"]
                      and binding is not None and gateway_jobs._dispatcher_binding_valid(binding))
            elif op["op"] == "create":
                expected = canonical(pinned["create_request"])
                ok = op["op_key"] == pinned["create_key"]
            else:
                expected = canonical({"session_id": job["session_id"], "message": pinned["message"], "idempotency_key": pinned["send_key"]})
                ok = bool(job["session_id"]) and op["op_key"] == pinned["send_key"]
            ok = ok and op["request_json"] == expected
        code = "snapshot_corrupt"
        if ok and pinned["depends_on"]:         # predecessor success / root-review release must still hold at the last moment
            live = self._gate(op["manifest_id"], op["generation"])
            if live is None:                    # lease lapsed/disarmed since the first check: no POST, no block, attempt returned by caller
                return False
            _graph, view = load_dependency_view(self.conn, live)
            if view.get(op["job_id"], ("ready", ""))[0] != "ready":
                ok, code = False, "dependency_gate"
        if not ok and op["op"] == "codex_start" and code == "dependency_gate":
            with txn(self.conn):
                self.conn.execute("UPDATE ops SET state='denied', error_code=?, receipt_at=? WHERE op_key=?", (code, self.now(), op["op_key"]))
                self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code=?, maybe_executing=0, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", (code, self.now(), op["manifest_id"], op["job_id"]))
                self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code=?, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", (code, self.now(), op["manifest_id"], op["job_id"]))
            return False
        if not ok:
            with txn(self.conn):
                self.conn.execute("UPDATE ops SET state='denied', error_code=? WHERE op_key=?", (code, op["op_key"]))
                executing = op["op"] == "send" or (op["op"] != "codex_start" and op["attempts"] >= 1)
                self._block(op["manifest_id"], op["job_id"], code, executing=executing)
        return ok

    def _busy_resources(self) -> set[str]:
        """Root-named exclusive keys held by ANY manifest's unresolved job (no new cwd/repo exclusion is invented)."""
        return {r[0] for r in self.conn.execute(
            "SELECT resource_key FROM jobs WHERE resource_key IS NOT NULL AND (state IN ('creating','created','sending','sent')"
            " OR state IN ('starting','running','stopping','result_pending')"
            " OR (state='blocked' AND maybe_executing=1))")}

    # -- intents (committed before any I/O)
    def _reserve_create(self, m: sqlite3.Row) -> str | None:
        with txn(self.conn):
            if self._gate(m["manifest_id"], m["generation"]) is None:
                return None
            rows = self.conn.execute("SELECT job_id, state, resource_key, maybe_executing FROM jobs WHERE manifest_id=? ORDER BY ord",
                                     (m["manifest_id"],)).fetchall()
            held = [r for r in rows if r["state"] in OCCUPYING or (r["state"] == "blocked" and r["maybe_executing"])]
            if len(held) >= m["max_concurrency"]:
                return None
            busy = self._busy_resources()
            graph, view = load_dependency_view(self.conn, m)
            if graph is None:
                return None
            for r in rows:
                if r["state"] != "queued" or (r["resource_key"] and r["resource_key"] in busy):
                    continue
                if view.get(r["job_id"], ("ready", ""))[0] != "ready":      # dependency / root-review gate: no intent at all
                    continue
                job = self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (m["manifest_id"], r["job_id"])).fetchone()
                if not self._snapshot_ok(job):
                    self._block(m["manifest_id"], r["job_id"], "snapshot_corrupt")
                    continue
                is_codex = job["route"] == "gateway-codex"
                if is_codex:
                    pinned = self._pinned(job)
                    if pinned["create_request"].get("transport") == codex.APP_SERVER_TRANSPORT and not codex.app_server_enabled():
                        continue
                request_digest = sha256_text(job["create_json"])
                op_name = "codex_start" if is_codex else "create"
                self.conn.execute("INSERT INTO ops(op_key,manifest_id,job_id,op,generation,request_json,request_digest,state,intent_at)"
                                  " VALUES(?,?,?,?,?,?,?,?,?)", (job["create_key"], m["manifest_id"], r["job_id"], op_name, m["generation"],
                                                                 job["create_json"], request_digest, "intent", self.now()))
                if is_codex:
                    gateway_id = gateway_jobs.stable_job_id(job["create_key"])
                    self.conn.execute("INSERT INTO codex_execution(manifest_id,job_id,route,gateway_job_id,manifest_digest,op_key,"
                                      "request_digest,request_fingerprint,generation,root_source_id,root_epoch,state,created_at)"
                                      " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                      (m["manifest_id"], job["job_id"], "gateway-codex", gateway_id, m["digest"], job["create_key"],
                                       request_digest, request_digest, m["generation"], m["root_source_id"], m["root_epoch"], "reserved", self.now()))
                self.conn.execute("UPDATE jobs SET state=?, updated_at=? WHERE manifest_id=? AND job_id=?",
                                  ("starting" if is_codex else "creating", self.now(), m["manifest_id"], r["job_id"]))
                self.fault("before_intent_commit")
                return job["create_key"]
            return None

    def _begin_send(self, m: sqlite3.Row, job: sqlite3.Row) -> str | None:
        with txn(self.conn):
            if self._gate(m["manifest_id"], m["generation"]) is None:
                return None
            fresh = self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (m["manifest_id"], job["job_id"])).fetchone()
            if fresh["state"] != "created" or not fresh["session_id"]:
                return None
            if not self._snapshot_ok(fresh):
                self._block(m["manifest_id"], job["job_id"], "snapshot_corrupt")
                return None
            request = canonical({"session_id": fresh["session_id"], "message": fresh["message"], "idempotency_key": fresh["send_key"]})
            self.conn.execute("INSERT INTO ops(op_key,manifest_id,job_id,op,generation,request_json,request_digest,state,intent_at)"
                              " VALUES(?,?,?,?,?,?,?,?,?)", (job["send_key"], m["manifest_id"], job["job_id"], "send", m["generation"],
                                                             request, sha256_text(request), "intent", self.now()))
            self.conn.execute("UPDATE jobs SET state='sending', updated_at=? WHERE manifest_id=? AND job_id=?",
                              (self.now(), m["manifest_id"], job["job_id"]))
            self.fault("before_intent_commit")
            return job["send_key"]

    # -- one POST attempt
    async def _execute(self, key: str) -> bool:
        """Returns True when an attempt was made."""
        now = self.now()
        with txn(self.conn):
            op = self.conn.execute("SELECT * FROM ops WHERE op_key=?", (key,)).fetchone()
            if op is None or op["state"] not in ("intent", "uncertain"):
                return False
            m = self._gate(op["manifest_id"], op["generation"])        # same arm only; every POST re-checks
            if m is None:
                if op["op"] == "codex_start" and op["attempts"] == 0:
                    self.conn.execute("UPDATE ops SET state='denied', error_code='gate_closed', receipt_at=? WHERE op_key=?",
                                      (now, op["op_key"]))
                    self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code='gate_closed', maybe_executing=0, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, op["manifest_id"], op["job_id"]))
                    self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code='gate_closed', updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, op["manifest_id"], op["job_id"]))
                return False
            if (op["next_attempt_at"] or 0) > now or op["attempts"] >= 1 + m["retry_budget"]:
                return False
            self.conn.execute("UPDATE ops SET attempts=attempts+1, io_at=?, next_attempt_at=? WHERE op_key=?", (now, now, key))
            self.fault("before_attempt_commit")
        self.fault("after_intent")
        def unsent() -> bool:                           # disarm / expiry / corruption before the request: no POST, attempt not spent
            with txn(self.conn):
                self.conn.execute("UPDATE ops SET attempts=MAX(0, attempts-1) WHERE op_key=? AND state IN ('intent','uncertain')", (key,))
            return False
        if not self._prefire(op):
            return unsent()
        op = self.conn.execute("SELECT * FROM ops WHERE op_key=?", (key,)).fetchone()      # the verified row
        request = json.loads(op["request_json"])
        token = _GUARD.set(lambda: self._prefire(op))
        try:
            if op["op"] == "codex_start":
                execution = self.conn.execute("SELECT * FROM codex_execution WHERE manifest_id=? AND job_id=?",
                                              (op["manifest_id"], op["job_id"])).fetchone()
                if execution is None:
                    raise DispatchError("dispatcher_binding_unavailable")
                result = self.codex_backend.start(request, op["op_key"], execution["request_fingerprint"],
                                                  self._codex_binding(execution))
            elif op["op"] == "create":
                result = await self.backend.create(**request)
            else:
                result = await self.backend.send(request["session_id"], request["message"], request["idempotency_key"])
        except Exception:  # noqa: BLE001 - outcome unknown: never a denial, never success
            result = {"ok": False, "error": "backend_exception", "submission_uncertain": True}
        finally:
            _GUARD.reset(token)
        if isinstance(result, dict) and result.get("gate_closed") is True:
            return unsent()
        self.fault("after_io_before_receipt")
        self._record(op, result)
        self.fault("after_receipt")
        return True

    def _record(self, op: sqlite3.Row, result: Any) -> None:
        if op["op"] == "codex_start":
            self._record_codex(op, result)
            return
        verdict, code = classify(result)
        mid, jid, now = op["manifest_id"], op["job_id"], self.now()
        with txn(self.conn):
            current = self.conn.execute("SELECT * FROM ops WHERE op_key=?", (op["op_key"],)).fetchone()
            if current["state"] not in ("intent", "uncertain"):
                return
            # An earlier attempt whose outcome was never recorded (crash / lost response) may have had an effect.
            prior = bool(current["uncertain_seen"]) or current["attempts"] >= 2
            if verdict == "uncertain":
                delay = min(BACKOFF_BASE * 2 ** max(0, current["attempts"] - 1), BACKOFF_MAX)
                self.conn.execute("UPDATE ops SET state='uncertain', uncertain_seen=1, error_code=?, next_attempt_at=? WHERE op_key=?",
                                  (code, now + delay, op["op_key"]))
                return
            if verdict == "denied":
                self.conn.execute("UPDATE ops SET state='denied', error_code=?, receipt_at=? WHERE op_key=?", (code, now, op["op_key"]))
                self._block(mid, jid, code or "denied", executing=prior)
                return
            request = json.loads(op["request_json"])
            if op["op"] == "create":
                problem, fields = validate_create_receipt(result, request)
                if problem is None and self.conn.execute("SELECT 1 FROM jobs WHERE lower(session_id)=?", (fields["session_id"],)).fetchone():
                    problem = "session_reused"
            else:
                job_row = self.conn.execute("SELECT create_json FROM jobs WHERE manifest_id=? AND job_id=?", (mid, jid)).fetchone()
                problem, fields = validate_send_receipt(result, request["session_id"], json.loads(job_row["create_json"]))
            if problem is not None:
                self.conn.execute("UPDATE ops SET state='denied', error_code=?, receipt_at=?, receipt_json=? WHERE op_key=?",
                                  (problem, now, _bounded_receipt(fields), op["op_key"]))
                self._block(mid, jid, problem, executing=op["op"] == "send" or prior)
                return
            self.conn.execute("UPDATE ops SET state='receipted', error_code=NULL, receipt_at=?, receipt_json=? WHERE op_key=?",
                              (now, _bounded_receipt(fields), op["op_key"]))
            if op["op"] == "create":
                self.conn.execute("UPDATE jobs SET state='created', session_id=?, updated_at=? WHERE manifest_id=? AND job_id=?",
                                  (fields["session_id"], now, mid, jid))
            else:
                self.conn.execute("UPDATE jobs SET state='sent', run_id=?, message_id=?, maybe_executing=1, updated_at=?"
                                  " WHERE manifest_id=? AND job_id=?", (fields.get("run_id"), fields.get("message_id"), now, mid, jid))

    def _record_codex(self, op: sqlite3.Row, result: Any) -> None:
        mid, jid, now = op["manifest_id"], op["job_id"], self.now()
        execution = self.conn.execute("SELECT * FROM codex_execution WHERE manifest_id=? AND job_id=?", (mid, jid)).fetchone()
        job = self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (mid, jid)).fetchone()
        request = json.loads(op["request_json"])
        try:
            binding = self._codex_binding(execution) if execution is not None else None
            expected = gateway_jobs._keyed_fingerprint(request["command"], request["cwd"], {},
                                                       execution["request_fingerprint"], binding) if binding else None
        except (DispatchError, OSError, ValueError, KeyError, TypeError):
            binding, expected = None, None
        result_job_id = result.get("job_id") if isinstance(result, dict) else None
        exact = (isinstance(result, dict) and execution is not None and job is not None and binding is not None
                 and isinstance(result_job_id, str) and result_job_id.lower() == execution["gateway_job_id"].lower()
                 and result.get("idempotency_key") == op["op_key"]
                 and result.get("request_fingerprint") == expected
                 and result.get("dispatcher_request_fingerprint") == execution["request_fingerprint"]
                 and result.get("explicit_env_digest") == gateway_jobs._explicit_env_digest({})
                 and result.get("dispatcher_binding") == binding
                 and result.get("command") == request["command"] and result.get("cwd") == request["cwd"])
        status = result.get("status") if isinstance(result, dict) else None
        if status == "not_launched" and isinstance(result, dict) and result.get("launch_result") == "gate_closed" and exact:
            with txn(self.conn):
                current = self.conn.execute("SELECT state FROM ops WHERE op_key=?", (op["op_key"],)).fetchone()
                if current is None or current["state"] not in ("intent", "uncertain"):
                    return
                self.conn.execute("UPDATE ops SET state='receipted', error_code='gate_closed', receipt_at=?, receipt_json=? WHERE op_key=?",
                                  (now, canonical({"job_id": execution["gateway_job_id"], "status": "not_launched", "reason": "gate_closed"}), op["op_key"]))
                self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code='gate_closed', maybe_executing=0, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code='gate_closed', metadata_ref=?, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?",
                                  (str(Path(result.get("runtime_dir", "")) / "jobs" / execution["gateway_job_id"] / "metadata.json"), now, mid, jid))
            return
        if not exact:
            status = "unknown"
            safe_code = "execution_identity_unknown"
        else:
            raw_code = result.get("error_code") if isinstance(result, dict) else None
            safe_code = raw_code if isinstance(raw_code, str) and CODE_RE.fullmatch(raw_code) else "launch_unknown"
        with txn(self.conn):
            current = self.conn.execute("SELECT state FROM ops WHERE op_key=?", (op["op_key"],)).fetchone()
            if current is None or current["state"] not in ("intent", "uncertain"):
                return
            runtime_value = result.get("runtime_dir", "") if isinstance(result, dict) else ""
            metadata_ref = str(Path(runtime_value) / "jobs" / execution["gateway_job_id"] / "metadata.json") if execution else None
            if status in ("starting", "running", "stopping", "exited", "stopped", "failed") and exact:
                receipt = {"gateway_job_id": execution["gateway_job_id"], "status": status}
                self.conn.execute("UPDATE ops SET state='receipted', error_code=NULL, receipt_at=?, receipt_json=? WHERE op_key=?",
                                  (now, canonical(receipt), op["op_key"]))
                self.conn.execute("UPDATE jobs SET state='running', maybe_executing=1, blocked_code=NULL, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                self.conn.execute("UPDATE codex_execution SET state='running', safe_code=NULL, metadata_ref=?, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", (metadata_ref, now, mid, jid))
            else:
                # The stable reservation exists or cannot be read. Preserve the slot and never mint another key.
                self.conn.execute("UPDATE ops SET state='uncertain', uncertain_seen=1, error_code='job_status_unknown', receipt_at=? WHERE op_key=?",
                                  (now, op["op_key"]))
                self.conn.execute("UPDATE jobs SET state='blocked', blocked_code=?, maybe_executing=1, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", ("execution_identity_unknown" if not exact else "job_status_unknown", now, mid, jid))
                self.conn.execute("UPDATE codex_execution SET state='unknown', safe_code=?, metadata_ref=?, updated_at=? "
                                  "WHERE manifest_id=? AND job_id=?", ("execution_identity_unknown" if not exact else "job_status_unknown",
                                                                       metadata_ref, now, mid, jid))

    # -- result observation (local read of the observer ledger only)
    def reconcile(self) -> None:
        for job in self.conn.execute("SELECT * FROM jobs WHERE state='sent' ORDER BY manifest_id, ord").fetchall():
            seen = read_observer(self.observer_path, job["session_id"], job["run_id"])
            now = self.now()
            with txn(self.conn):
                current = self.conn.execute("SELECT state FROM jobs WHERE manifest_id=? AND job_id=?", (job["manifest_id"], job["job_id"])).fetchone()
                if current["state"] != "sent":
                    continue
                if job["run_id"] is None and seen.get("run_id") and seen["state"] not in ("unknown",):
                    self.conn.execute("UPDATE jobs SET run_id=? WHERE manifest_id=? AND job_id=?", (seen["run_id"], job["manifest_id"], job["job_id"]))
                if seen["state"] == "succeeded":
                    self.conn.execute("UPDATE jobs SET state='succeeded', obs_status='succeeded', obs_at=?, handoff_id=?, result_digest=?,"
                                      " result_chars=?, completed_at=?, updated_at=? WHERE manifest_id=? AND job_id=?",
                                      (now, seen["handoff_id"], seen["digest"], seen["chars"], now, now, job["manifest_id"], job["job_id"]))
                elif seen["state"] == "failed":
                    self.conn.execute("UPDATE jobs SET state='failed', obs_status=?, obs_at=?, completed_at=?, updated_at=? WHERE manifest_id=? AND job_id=?",
                                      (seen["status"], now, now, now, job["manifest_id"], job["job_id"]))
                else:
                    self.conn.execute("UPDATE jobs SET obs_status=?, obs_at=? WHERE manifest_id=? AND job_id=?",
                                      (seen["state"] if seen["state"] != "observer_unavailable" else "result_pending", now, job["manifest_id"], job["job_id"]))
        self._reconcile_codex()

    def _codex_status(self, gateway_job_id: str) -> dict[str, Any]:
        status_fn = getattr(self.codex_backend, "status", None)
        if not callable(status_fn):
            return {"job_id": gateway_job_id, "status": "unknown", "error_code": "status_adapter_unavailable"}
        try:
            result = status_fn(gateway_job_id)
            return result if isinstance(result, dict) else {"job_id": gateway_job_id, "status": "unknown",
                                                             "error_code": "invalid_status_shape"}
        except Exception:
            return {"job_id": gateway_job_id, "status": "unknown", "error_code": "status_unavailable"}

    def _reconcile_codex(self) -> None:
        active_states = ("reserved", "running", "unknown", "result_pending")
        rows = self.conn.execute("SELECT * FROM codex_execution WHERE state IN (?,?,?,?) ORDER BY created_at,manifest_id,job_id",
                                 active_states).fetchall()
        for execution in rows:
            mid, jid = execution["manifest_id"], execution["job_id"]
            gateway_id = execution["gateway_job_id"]
            operation = self.conn.execute("SELECT attempts FROM ops WHERE op_key=?", (execution["op_key"],)).fetchone()
            manifest = self.conn.execute("SELECT generation FROM manifests WHERE manifest_id=?", (mid,)).fetchone()
            if execution["state"] == "reserved" and operation is not None and operation["attempts"] == 0:
                if manifest is not None and self._gate(mid, execution["generation"]) is not None:
                    continue                  # committed pre-I/O intent: step() resumes the same create_key/job UUID
                now = self.now()
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code='gate_closed', updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code='gate_closed', maybe_executing=0, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                    self.conn.execute("UPDATE ops SET state='denied', error_code='gate_closed', receipt_at=? WHERE op_key=?",
                                      (now, execution["op_key"]))
                continue
            status = self._codex_status(gateway_id)
            job = self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND job_id=?", (mid, jid)).fetchone()
            if job is None:
                continue
            try:
                binding = self._codex_binding(execution)
                request = json.loads(job["create_json"])
                pinned = self._pinned(job)
                if pinned is None:
                    raise DispatchError("snapshot_corrupt")
                is_app_server = pinned["create_request"].get("transport") == codex.APP_SERVER_TRANSPORT
                if request.get("transport") != pinned["create_request"].get("transport"):
                    raise DispatchError("snapshot_corrupt")
                if is_app_server:
                    expected_fingerprint = gateway_jobs._keyed_fingerprint(
                        request["command"], request["cwd"], {}, execution["request_fingerprint"], binding,
                        transport=codex.APP_SERVER_TRANSPORT, app_server=request["app_server"])
                else:
                    expected_fingerprint = gateway_jobs._keyed_fingerprint(request["command"], request["cwd"], {},
                                                                           execution["request_fingerprint"], binding)
                job_id_exact = isinstance(status.get("job_id"), str) and status["job_id"].lower() == gateway_id.lower()
                exact = (job_id_exact and status.get("idempotency_key") == execution["op_key"]
                         and status.get("dispatcher_request_fingerprint") == execution["request_fingerprint"]
                         and status.get("explicit_env_digest") == gateway_jobs._explicit_env_digest({})
                         and status.get("request_fingerprint") == expected_fingerprint
                         and status.get("dispatcher_binding") == binding
                         and status.get("command") == request["command"] and status.get("cwd") == request["cwd"])
                runtime_dir = getattr(self.codex_backend, "runtime_dir", None)
                job_directory = gateway_jobs._directory(gateway_id, runtime_dir)
                expected_stdout = job_directory / "stdout.log"
                stdout_path = Path(status["stdout_path"]).resolve() if isinstance(status.get("stdout_path"), str) else None
                exact = exact and stdout_path == expected_stdout.resolve()
                if is_app_server:
                    app_runtime = status.get("app_server_runtime") or {}
                    expected_journal = job_directory / "appserver.rpc.jsonl"
                    journal_path = (Path(app_runtime["journal_path"]).resolve()
                                    if isinstance(app_runtime, dict) and isinstance(app_runtime.get("journal_path"), str) else None)
                    journal_exact = journal_path == expected_journal.resolve()
                    if status.get("status") in ("starting", "running", "stopping") and journal_path is None:
                        journal_exact = isinstance(app_runtime, dict)
                    exact = (exact and self._snapshot_ok(job) and status.get("transport") == codex.APP_SERVER_TRANSPORT
                             and status.get("app_server") == request["app_server"] and journal_exact
                             and codex.steer_guard(binding, status, require_running=False) == "open")
                    expected_stdout = expected_journal
            except (DispatchError, OSError, ValueError, KeyError, TypeError):
                exact = False
                request = {}
                runtime_dir = None
                job_directory = Path(".")
                expected_stdout = job_directory / "stdout.log"
                is_app_server = False
            now = self.now()
            current_status = status.get("status") if isinstance(status, dict) else None
            if not exact:
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='unknown', safe_code='execution_identity_unknown', updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='blocked', blocked_code='execution_identity_unknown', maybe_executing=1, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                continue
            if current_status == "not_launched" and status.get("launch_result") == "gate_closed":
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='not_launched', safe_code='gate_closed', metadata_ref=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (str(job_directory / "metadata.json"), now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='not_launched', blocked_code='gate_closed', maybe_executing=0, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                    self.conn.execute("UPDATE ops SET state='receipted', error_code='gate_closed', receipt_at=? WHERE op_key=?",
                                      (now, execution["op_key"]))
                continue
            if current_status in ("starting", "running", "stopping"):
                process_identity = status.get("process_identity")
                worker_identity = status.get("worker_identity")
                valid_live = ((current_status == "starting" and worker_identity == "alive") or
                              (current_status in ("running", "stopping") and process_identity == "alive" and worker_identity == "alive"))
                if not valid_live:
                    state, safe_code = "unknown", "process_identity_unknown"
                else:
                    state, safe_code = current_status, None
                with txn(self.conn):
                    db_state = "unknown" if state == "unknown" else "running"
                    self.conn.execute("UPDATE codex_execution SET state=?, safe_code=?, metadata_ref=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?",
                                      (db_state, safe_code, str(job_directory / "metadata.json"), now, mid, jid))
                    if state == "unknown":
                        self.conn.execute("UPDATE jobs SET state='blocked', blocked_code=?, maybe_executing=1, updated_at=? "
                                          "WHERE manifest_id=? AND job_id=?", (safe_code, now, mid, jid))
                    else:
                        self.conn.execute("UPDATE jobs SET state='running', blocked_code=NULL, maybe_executing=1, updated_at=? "
                                          "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                    self.conn.execute("UPDATE ops SET state='receipted', error_code=NULL, receipt_at=?, receipt_json=? WHERE op_key=? "
                                      "AND state IN ('intent','uncertain')",
                                      (now, canonical({"gateway_job_id": gateway_id, "status": current_status}), execution["op_key"]))
                continue
            if current_status not in ("exited", "failed", "stopped") or type(status.get("exit_code")) is not int:
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='unknown', safe_code='job_status_unknown', metadata_ref=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (str(job_directory / "metadata.json"), now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='blocked', blocked_code='job_status_unknown', maybe_executing=1, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, mid, jid))
                continue
            if not is_app_server and (current_status == "stopped" or status["exit_code"] != 0):
                code = "cancelled" if current_status == "stopped" else "nonzero_exit"
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='failed', safe_code=?, metadata_ref=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (code, str(job_directory / "metadata.json"), now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='failed', obs_status=?, blocked_code=NULL, maybe_executing=0, completed_at=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (code, now, now, mid, jid))
                    self.conn.execute("UPDATE ops SET state='receipted', error_code=?, receipt_at=? WHERE op_key=?",
                                      (code, now, execution["op_key"]))
                continue
            try:
                before = expected_stdout.stat()
                if before.st_size > codex.MAX_JSONL_BYTES:
                    raise ValueError("result_too_large")
                stream = expected_stdout.read_bytes()
                after = expected_stdout.stat()
                if len(stream) != before.st_size or before.st_size != after.st_size or before.st_mtime_ns != after.st_mtime_ns:
                    raise ValueError("result_changed")
                if is_app_server:
                    from .codex_app_server import parse_appserver_journal
                    parsed = parse_appserver_journal(stream, request["app_server"],
                                                     expected_thread_id=app_runtime.get("thread_id"),
                                                     expected_turn_id=app_runtime.get("turn_id"),
                                                     steer_evidence=status.get("steer_evidence"))
                else:
                    parsed = codex.parse_codex_jsonl(stream)
            except (OSError, ValueError):
                parsed = {"state": "result_pending", "code": "result_unavailable"}
            if is_app_server:
                runtime_errors = [value for value in (app_runtime.get("runtime_error_code"), status.get("error_code"))
                                  if value not in (None, "")]
                if runtime_errors or current_status == "failed":
                    failure = runtime_errors[0] if runtime_errors else "appserver_runtime_failed"
                    failure = failure if isinstance(failure, str) and CODE_RE.fullmatch(failure) else "appserver_runtime_failed"
                    parsed.update(state="failed", code=failure, input_set_uncertain=True)
                safe_evidence = _safe_app_server_evidence(parsed, app_runtime)
                self.conn.execute("UPDATE codex_execution SET app_server_evidence_json=? WHERE manifest_id=? AND job_id=?",
                                  (canonical(safe_evidence), mid, jid))
            if parsed["state"] == "result_pending":
                code = parsed.get("code", "result_pending")
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='result_pending', safe_code=?, metadata_ref=?, result_ref=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?",
                                      (code, str(job_directory / "metadata.json"), str(expected_stdout), now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='result_pending', obs_status='result_pending', blocked_code=?, maybe_executing=1, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (code, now, mid, jid))
                continue
            if parsed["state"] == "failed":
                with txn(self.conn):
                    self.conn.execute("UPDATE codex_execution SET state='failed', safe_code=?, thread_id=?, log_digest=?, metadata_ref=?, result_ref=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?",
                                      (parsed.get("code", "turn_failed"), parsed.get("thread_id"), parsed.get("log_digest"),
                                       str(job_directory / "metadata.json"), str(expected_stdout), now, mid, jid))
                    self.conn.execute("UPDATE jobs SET state='failed', obs_status='failed', blocked_code=NULL, maybe_executing=0, completed_at=?, updated_at=? "
                                      "WHERE manifest_id=? AND job_id=?", (now, now, mid, jid))
                continue
            evidence_payload = {"gateway_job_id": gateway_id, "request_fingerprint": execution["request_fingerprint"],
                                "thread_id": parsed["thread_id"], "result_digest": parsed["result_digest"],
                                "log_digest": parsed["log_digest"], "log_bytes": parsed["log_bytes"]}
            if is_app_server:
                evidence_payload.update(transport=codex.APP_SERVER_TRANSPORT,
                                        app_server=request["app_server"], turn_id=parsed.get("turn_id"),
                                        spec_digest=parsed.get("spec_digest"), native_terminal_digest=parsed.get("native_terminal_digest"),
                                        verifier_evidence_digest=parsed.get("evidence_digest"),
                                        input_set_uncertain=parsed.get("input_set_uncertain"),
                                        steer_evidence_digest=parsed.get("steer_evidence_digest"), native_outcome=parsed.get("native_outcome"))
            evidence_digest = hashlib.sha256(canonical(evidence_payload).encode("utf-8")).hexdigest()
            with txn(self.conn):
                self.conn.execute("UPDATE codex_execution SET state='succeeded', safe_code=NULL, thread_id=?, result_digest=?, log_digest=?, log_bytes=?, "
                                  "evidence_digest=?, result_chars=?, result_ref=?, metadata_ref=?, updated_at=? WHERE manifest_id=? AND job_id=?",
                                  (parsed["thread_id"], parsed["result_digest"], parsed["log_digest"], parsed["log_bytes"], evidence_digest,
                                   parsed["result_chars"], str(expected_stdout), str(job_directory / "metadata.json"), now, mid, jid))
                self.conn.execute("UPDATE jobs SET state='succeeded', obs_status='verified', result_digest=?, result_chars=?, completed_at=?, "
                                  "maybe_executing=0, blocked_code=NULL, updated_at=? WHERE manifest_id=? AND job_id=?",
                                  (parsed["result_digest"], parsed["result_chars"], now, now, mid, jid))
                self.conn.execute("UPDATE ops SET state='receipted', error_code=NULL, receipt_at=?, receipt_json=? WHERE op_key=?",
                                  (now, canonical({"gateway_job_id": gateway_id, "thread_id": parsed["thread_id"],
                                                   "result_digest": parsed["result_digest"], "log_digest": parsed["log_digest"]}), execution["op_key"]))

    # -- one pass
    async def step(self) -> dict[str, Any]:
        self.reconcile()
        attempts = 0
        for _ in range(MAX_JOBS * 4):
            acted = False
            for m in self._live_manifests():
                for job in self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND state IN ('creating','sending','starting') ORDER BY ord",
                                             (m["manifest_id"],)).fetchall():
                    key = job["create_key"] if job["state"] in ("creating", "starting") else job["send_key"]
                    acted |= await self._execute(key)
                for job in self.conn.execute("SELECT * FROM jobs WHERE manifest_id=? AND state='created' ORDER BY ord", (m["manifest_id"],)).fetchall():
                    key = self._begin_send(m, job)
                    if key:
                        acted |= await self._execute(key)
                while (key := self._reserve_create(m)) is not None:
                    acted |= await self._execute(key)
            attempts += int(acted)
            if not acted:
                break
        self.reconcile()
        return {"ok": True, "attempts_made": attempts, **self.progress()}

    def progress(self) -> dict[str, Any]:
        """``done`` = nothing further can happen without a new root action."""
        live = self._live_manifests()
        pending = False
        for m in live:
            rows = self.conn.execute("SELECT job_id, state, resource_key, maybe_executing FROM jobs WHERE manifest_id=? ORDER BY ord",
                                     (m["manifest_id"],)).fetchall()
            states = [r["state"] for r in rows]
            if any(s in states for s in ("sent", "created", "starting", "running", "stopping", "result_pending")):
                pending = True
            ops = self.conn.execute("SELECT attempts FROM ops WHERE manifest_id=? AND generation=? AND state IN ('intent','uncertain')",
                                    (m["manifest_id"], m["generation"])).fetchall()
            if any(o["attempts"] < 1 + m["retry_budget"] for o in ops):
                pending = True
            held = [r for r in rows if r["state"] in OCCUPYING or (r["state"] == "blocked" and r["maybe_executing"])]
            busy = self._busy_resources()
            _graph, view = load_dependency_view(self.conn, m)
            if len(held) < m["max_concurrency"] and any(
                    r["state"] == "queued" and not (r["resource_key"] and r["resource_key"] in busy)
                    and view.get(r["job_id"], ("ready", ""))[0] == "ready" for r in rows):
                pending = True
        return {"live_arms": len(live), "done": not pending}


STOP_NAME = "stop.requested"


def stop_requested(directory: Path) -> bool:
    return (directory / STOP_NAME).exists()


async def run_loop(dispatcher: Dispatcher, directory: Path, interval: float, sleep: Callable[[float], Any] = asyncio.sleep, *,
                   watch: bool = False, stop: Callable[[], bool] | None = None) -> dict[str, Any]:
    """Default: exit once drained.  ``watch``: keep polling the ledger for a NEW explicitly armed manifest; each pass is the
    same ``step`` (live leases only), so an idle / expired / disarmed ledger makes zero requests.  Nothing here arms,
    extends or edits a manifest."""
    while True:
        if stop is not None and stop():
            log_event(directory, "stopped")
            return {"ok": True, "stopped": True, **dispatcher.progress()}
        result = await dispatcher.step()
        write_status(directory, build_status(dispatcher.conn, dispatcher.now()))
        if result["done"] and not watch:
            log_event(directory, "drained", live_arms=result["live_arms"])
            return result
        await sleep(interval)


# ------------------------------------------------------------------ status

def _dep_fields(job_id: str, graph: dict[str, Any], view: dict[str, tuple[str, str]]) -> dict[str, Any]:
    deps, review = graph.get(job_id, ((), False))
    if not deps:
        return {}
    state, code = view.get(job_id, ("ready", ""))
    return {"depends_on": list(deps), "root_review": review, "dependency": {"state": state, "code": code or None}}


def _safe_app_server_evidence(parsed: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    """Persist bounded proof summaries and turn provenance, never RPC payloads or input text."""
    evidence: dict[str, Any] = {"transport": codex.APP_SERVER_TRANSPORT,
                                "input_set_uncertain": parsed.get("input_set_uncertain") is not False}
    for key in ("spec_digest", "native_terminal_digest", "steer_evidence_digest", "evidence_digest"):
        value = parsed.get(key)
        evidence[key] = value if isinstance(value, str) and SHA_RE.fullmatch(value) else None
    outcome = parsed.get("native_outcome")
    evidence["native_outcome"] = outcome if outcome in ("completed", "failed", "interrupted") else None
    turn_id = parsed.get("turn_id")
    evidence["turn_id"] = (turn_id if isinstance(turn_id, str) and 0 < len(turn_id) <= 512
                           and all(ord(char) >= 32 for char in turn_id) else None)
    shutdown = runtime.get("shutdown_code")
    evidence["shutdown_code"] = shutdown if shutdown in ("appserver_shutdown_forced", "appserver_shutdown_incomplete") else None
    return evidence


def build_status(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    manifests = []
    for m in conn.execute("SELECT * FROM manifests ORDER BY imported_at, manifest_id").fetchall():
        try:
            manifest_version = json.loads(bytes(m["raw"]).decode("utf-8")).get("version")
        except (ValueError, UnicodeDecodeError, AttributeError):
            manifest_version = None
        is_v2 = manifest_version == 2
        live = m["state"] == "armed" and now < (m["arm_expires_at"] or 0) and now < m["expires_at"]
        arm_state = "live" if live else ("disarmed" if m["state"] == "disarmed" else "expired" if m["state"] == "armed" else "imported")
        counts = dict.fromkeys(("queued", "accepted", "active", "uncertain", "blocked", "expired", "succeeded", "failed", "result_pending",
                                "dependency_wait", "root_review_wait", "dependency_failed"), 0)
        if is_v2:
            counts.update(dict.fromkeys(("running", "denied", "unknown", "verified_success"), 0))
        route_counts = {"aoitalk-claude": 0, "gateway-codex": 0}
        inflight = 0
        jobs = conn.execute("SELECT * FROM jobs WHERE manifest_id=? ORDER BY ord", (m["manifest_id"],)).fetchall()
        graph, view = load_dependency_view(conn, m)
        graph = graph or {}
        for j in jobs:
            route_counts[j["route"] if j["route"] in route_counts else "aoitalk-claude"] += 1
            op = conn.execute("SELECT state, uncertain_seen FROM ops WHERE op_key=?",
                              (j["create_key"] if j["state"] in ("creating", "starting") else j["send_key"],)).fetchone() if j["state"] in ("creating", "sending", "starting") else None
            if j["state"] == "queued":
                dep = view.get(j["job_id"], ("ready", ""))[0] if live else "ready"
                counts[{"ready": "queued" if live else "expired", "waiting": "dependency_wait", "review": "root_review_wait",
                        "failed": "dependency_failed"}[dep]] += 1
            elif j["state"] == "created":
                counts["accepted"] += 1
            elif j["state"] in ("creating", "sending"):
                if op is not None and op["state"] == "uncertain":
                    counts["uncertain"] += 1
                else:
                    counts["active"] += 1
                inflight += 1
            elif j["state"] in ("starting", "running", "stopping"):
                counts["active"] += 1
                if is_v2:
                    counts["running"] += int(j["state"] != "starting")
                inflight += 1
            elif j["state"] == "result_pending":
                counts["result_pending"] += 1
                inflight += 1
            elif j["state"] == "sent":
                counts["result_pending" if j["obs_status"] == "result_pending" else "active"] += 1
            elif j["state"] == "blocked":
                counts["blocked"] += 1
                counts["uncertain"] += int(bool(j["maybe_executing"]))
                if is_v2:
                    counts["unknown"] += int(bool(j["maybe_executing"]))
                    counts["denied"] += int(not bool(j["maybe_executing"]))
            elif j["state"] in ("succeeded", "failed"):
                counts[j["state"]] += 1
                if is_v2:
                    counts["verified_success"] += int(j["state"] == "succeeded")
            elif j["state"] == "not_launched" and is_v2:
                counts["denied"] += 1
        execution_rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM codex_execution WHERE manifest_id=?", (m["manifest_id"],))}
        root_review_jobs = [j for j in jobs if graph.get(j["job_id"], ((), False))[1]]
        released = conn.execute("SELECT COUNT(*) FROM releases WHERE manifest_id=? AND generation=?", (m["manifest_id"], m["generation"])).fetchone()[0]
        manifest_status = {"manifest_id": m["manifest_id"], "digest": m["digest"], "arm_state": arm_state, "generation": m["generation"],
                          "arm_expires_at": iso(m["arm_expires_at"]), "expires_at": iso(m["expires_at"]), "max_concurrency": m["max_concurrency"],
                          "total_jobs": len(jobs), "counts": counts, "initiated_requests_unresolved": inflight,
                          "fully_verified_succeeded": all(j["state"] == "succeeded" for j in jobs),
                          "integrity_ok": hashlib.sha256(m["raw"]).hexdigest() == m["digest"],
                          "jobs": [{"job_id": j["job_id"], "state": j["state"], "session_id": j["session_id"], "run_id": j["run_id"],
                                    "blocked_code": j["blocked_code"], "maybe_executing": bool(j["maybe_executing"]),
                                    "observation": j["obs_status"], "handoff_id": j["handoff_id"], "result_digest": j["result_digest"],
                                    **({"route": j["route"]} if is_v2 else {}),
                                    **({"execution": {"gateway_job_id": execution_rows[j["job_id"]]["gateway_job_id"],
                                                       "request_fingerprint": execution_rows[j["job_id"]]["request_fingerprint"],
                                                       "state": execution_rows[j["job_id"]]["state"],
                                                       "thread_id": execution_rows[j["job_id"]]["thread_id"],
                                                       "result_digest": execution_rows[j["job_id"]]["result_digest"],
                                                       "log_digest": execution_rows[j["job_id"]]["log_digest"],
                                                       "log_bytes": execution_rows[j["job_id"]]["log_bytes"],
                                                       "evidence_digest": execution_rows[j["job_id"]]["evidence_digest"],
                                                       "result_ref": execution_rows[j["job_id"]]["result_ref"],
                                                       "metadata_ref": execution_rows[j["job_id"]]["metadata_ref"],
                                                       "safe_code": execution_rows[j["job_id"]]["safe_code"]}}
                                       if is_v2 and j["route"] == "gateway-codex" and j["job_id"] in execution_rows else {}),
                                    **_dep_fields(j["job_id"], graph, view)}
                                   for j in jobs]}
        if is_v2:
            for job_row, job_status in zip(jobs, manifest_status["jobs"]):
                execution = execution_rows.get(job_row["job_id"])
                if execution is None or job_row["route"] != "gateway-codex":
                    continue
                try:
                    request = json.loads(job_row["create_json"])
                    if request.get("transport") != codex.APP_SERVER_TRANSPORT:
                        continue
                    evidence = json.loads(execution["app_server_evidence_json"] or "{}")
                    evidence = _safe_app_server_evidence(evidence, evidence)
                except (ValueError, TypeError, AttributeError):
                    evidence = _safe_app_server_evidence({}, {})
                job_status["execution"].update(transport=codex.APP_SERVER_TRANSPORT, app_server_evidence=evidence)
            manifest_status.update(owned_route_counts=route_counts,
                                   execution_complete=all(j["state"] in ("succeeded", "failed", "not_launched") for j in jobs),
                                   source_adoption=("not_required" if not root_review_jobs else
                                                    "recorded" if released == len(root_review_jobs) else "pending"))
        manifests.append(manifest_status)
    return {"schema": 1, "generated_at": iso(now), "manifests": manifests,
            "note": "counts come from the local dispatch ledger; no root liveness or running-worker count is implied"}


# ------------------------------------------------------------------ CLI

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="work_dispatcher", description="Finite, explicitly armed work dispatcher.")
    parser.add_argument("--dir", type=Path, default=ROOT_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="strictly validate a manifest offline (no network, no ledger)")
    val.add_argument("--manifest", type=Path, required=True)
    val.add_argument("--sha256")
    arm_p = sub.add_parser("arm", help="root-only: import an immutable manifest and start a finite lease (sends nothing)")
    arm_p.add_argument("--manifest", type=Path, required=True)
    arm_p.add_argument("--sha256", required=True)
    arm_p.add_argument("--expires-at", required=True, help="timezone-aware ISO time, not after the manifest expiry")
    rel = sub.add_parser("release", help="root-only: record the root's own adoption decision for one root_review job "
                         "(binds manifest digest, epoch, generation and exact route-aware predecessor results; sends nothing)")
    rel.add_argument("manifest_id")
    rel.add_argument("job_id")
    rel.add_argument("--manifest-sha256", required=True)
    rel.add_argument("--root-epoch", type=int, required=True)
    rel.add_argument("--generation", type=int, required=True, help="the arm generation the root confirmed (see `show`); never defaulted")
    rel.add_argument("--predecessor", action="append", metavar="JOB=SESSION_ID:RUN_ID:RESULT_SHA256")
    rel.add_argument("--codex-predecessor", action="append",
                     metavar="JOB=GATEWAY_JOB_UUID:REQUEST_SHA256:RESULT_SHA256:THREAD_ID:LOG_SHA256")
    dis = sub.add_parser("disarm", help="stop NEW requests for a manifest (no remote cancel)")
    dis.add_argument("manifest_id")
    run = sub.add_parser("run", help="dispatch the armed finite queue until it is drained or nothing can progress")
    run.add_argument("--interval", type=float, default=POLL_SECONDS)
    run.add_argument("--watch", action="store_true", help="opt-in: stay idle and wait for a new explicitly armed manifest "
                     "(still never arms, extends or sends outside a live lease)")
    sub.add_parser("stop", help="ask a running `run --watch` to exit after its current pass (local file only)")
    sub.add_parser("once", help="one dispatch pass, then exit")
    sub.add_parser("status", help="local-only status from the dispatch ledger")
    show = sub.add_parser("show", help="local-only job detail for a manifest")
    show.add_argument("manifest_id")
    args = parser.parse_args(argv)

    def emit(payload: dict[str, Any]) -> None:
        if sys.stdout is not None:
            print(json.dumps(payload, ensure_ascii=False, indent=2))

    conn = None
    try:
        if args.command == "validate":
            m = parse_manifest(args.manifest.read_bytes(), args.sha256)
            emit({"ok": True, "manifest_id": m["manifest_id"], "digest": m["digest"], "jobs": len(m["jobs"]), "armed": False})
            return 0
        with contextlib.ExitStack() as stack:
            if args.command == "stop":
                args.dir.mkdir(parents=True, exist_ok=True)
                (args.dir / STOP_NAME).write_text("stop", encoding="ascii")
                emit({"ok": True, "stop_requested": True})
                return 0
            if args.command in ("run", "once"):
                stack.enter_context(DispatcherLock(args.dir))
                if args.command == "run":
                    with contextlib.suppress(OSError):
                        (args.dir / STOP_NAME).unlink()          # a stale request must not block this start      # before the ledger is opened or migrated
            conn = connect(args.dir)
            now = time.time()
            if args.command == "arm":
                emit(arm(conn, args.manifest.read_bytes(), args.sha256, parse_utc(args.expires_at), now))
            elif args.command == "release":
                bindings: dict[str, tuple[str, str, str]] = {}
                for spec in args.predecessor or []:
                    pid, _, rest = spec.partition("=")
                    parts = rest.split(":")
                    if not pid or len(parts) != 3 or pid in bindings:
                        raise DispatchError("invalid_release")
                    bindings[pid] = (parts[0], parts[1], parts[2])
                codex_bindings: dict[str, dict[str, str]] = {}
                for spec in args.codex_predecessor or []:
                    pid, _, rest = spec.partition("=")
                    parts = rest.split(":")
                    if not pid or len(parts) != 5 or pid in bindings or pid in codex_bindings:
                        raise DispatchError("invalid_release")
                    codex_bindings[pid] = {"gateway_job_id": parts[0], "request_fingerprint": parts[1],
                                           "result_digest": parts[2], "thread_id": parts[3], "log_digest": parts[4]}
                if not bindings and not codex_bindings:
                    raise DispatchError("invalid_release")
                emit(release(conn, args.manifest_id, args.job_id, args.manifest_sha256, args.root_epoch, bindings, now,
                             generation=args.generation, codex_bindings=codex_bindings))
            elif args.command == "disarm":
                emit(disarm(conn, args.manifest_id, now))
            elif args.command in ("once", "run"):
                dispatcher = Dispatcher(conn)
                log_event(args.dir, "start", command=args.command)
                try:
                    if args.command == "once":
                        emit(asyncio.run(dispatcher.step()))
                    else:
                        emit(asyncio.run(run_loop(dispatcher, args.dir, max(1.0, args.interval), watch=args.watch,
                                                  stop=lambda: stop_requested(args.dir))))
                finally:
                    log_event(args.dir, "stop")
                write_status(args.dir, build_status(conn, time.time()))
            else:
                status = build_status(conn, now)
                if args.command == "status":
                    write_status(args.dir, status)
                    emit({**status, "manifests": [{k: v for k, v in m.items() if k != "jobs"} for m in status["manifests"]]})
                else:
                    found = [m for m in status["manifests"] if m["manifest_id"] == args.manifest_id]
                    if not found:
                        raise DispatchError("not_found")
                    emit(found[0])
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        code = safe_code(exc)
        emit({"ok": False, "error": code})
        if args.command in ("run", "once"):
            log_event(args.dir, "exit", code=code)
            if code == "already_running":
                return 0
        return 1
    finally:
        if conn is not None:
            conn.close()
