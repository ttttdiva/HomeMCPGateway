"""Read-only work supervisor: observes AoiTalk CLI sessions / tasks / Gateway jobs.

Observer only.  It performs GET list/status/events, the read-only ``collect`` POST, the
ACL'd read-only run / run-list / message GETs used to reconcile KNOWN run IDs, and local job listing.
It never creates, sends to, reconnects, cancels or retries an agent and never launches follow-up work; every HTTP call goes through :func:`observe`, which
rejects anything outside a fixed read-only allow-list.

State lives in a private SQLite WAL ledger.  One poll = fetch (no DB writes) followed by
ONE transaction that persists events, the next cursor (only when the page was complete)
and the snapshot.  A crash before the commit loses nothing; replaying the same page is
absorbed by ``UNIQUE(session_id, event_id)``.  That gives exactly-once inbox *insertion*,
not exactly-once human processing: items stay visible until explicitly acknowledged.

Authentication and sanitization reuse :class:`aoitalk_tasks.TaskClient` and
:class:`aoitalk_cli_sessions.CliSessionClient`; credentials are never read or printed
here.  A task/session is "completed" only with a confirmed terminal state AND a persisted
result; silence or a vanished process is never completion.  Root/aux heartbeats are
written only by the explicit ``heartbeat`` command, never by the monitor.

Results are kept per (session, run, assistant message) in ``handoffs`` and, in the same transaction,
in a durable ``outbox``: a root that was absent drains them later with ``pending`` (read-only), records
``receive`` (delivery) and then ``ack`` (processed).  The daemon never starts or wakes a root.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from . import aoitalk, aoitalk_cli_sessions as cli, aoitalk_tasks as tasks, work_dispatcher_codex, codex_app_server

SCHEMA_VERSION = 3          # status.json schema
LEDGER_VERSION = 5          # v5: additive standalone Gateway Codex observation ledger
POLL_SECONDS = 10.0
MIN_CLAUDE = 8
AUX_TARGET = 6
STALE_SECONDS = 900.0
LIST_LIMIT = 200
TERMINAL_LIST_LIMIT = 100       # terminal sessions are ~14 KB each; 200 exceeds the 2 MB response cap (server_error)
TASK_LIST_LIMIT = 100
STATUS_CHECKS_PER_POLL = 20
PAGE_LIMITS = (100, 25, 5, 1)
CLI_ACTIVE = ("running", "queued", "waiting_input", "waiting_approval", "disconnected", "ready", "cancelling")
CLI_TERMINAL = ("completed", "failed", "interrupted", "cancelled")
TASK_ACTIVE = ("queued", "waiting", "running", "waiting_approval")
TASK_TERMINAL = tuple(sorted(tasks.TERMINAL))
ROOT_DIR = aoitalk.GATEWAY_ROOT / ".local" / "work-supervisor"
SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
TERMINAL_EVENT_RE = re.compile(r"(?i)(?:cli|run)\.(?:completed|failed|cancelled|canceled|interrupted|terminal|finished)")
RESULT_EVENT_RE = re.compile(r"(?i)(?:cli|run)\.(?:result|final)")
MAX_CHUNKS = 1000
COLLECT_GROUP = 10
TERMINAL_RECHECKS_PER_POLL = 3   # known terminal+drained sessions re-collected per poll (fair rotation): a follow-up that started and ended between polls is never observed active
TERMINAL_RECHECK_SECONDS = 120.0   # minimum age of the last collection before a drained terminal session is rechecked without a change hint
BACKLOG_PER_POLL = 10           # terminal, not-yet-drained sessions collected per poll (fair rotation) so a first catch-up cannot stall the loop
MAX_HEARTBEAT_TTL = 86_400.0
RUN_STATUSES = ("queued", "running", "awaiting_approval", "uncertain", "succeeded", "failed", "cancelled")
RUN_TERMINAL = ("succeeded", "failed", "cancelled")
RUN_ACTIVE = ("queued", "running", "awaiting_approval", "uncertain")
RUN_LIST_LIMIT = 200
RUN_FRESH_SECONDS = 120.0       # a run state older than this is UNKNOWN, never counted as running
RUN_RECHECK_SECONDS = 30.0
RUN_CHECKS_PER_POLL = 20
SESSION_RUN_LISTS_PER_POLL = 10
RECOVERIES_PER_POLL = 5
RECOVERY_GRACE_SECONDS = 30.0   # let the cli.output events arrive before falling back to the message GET
RECOVERY_MAX_ATTEMPTS = 30
MESSAGE_LIMITS = (20, 5, 1)
MESSAGE_PAGES = 3
MESSAGE_CLIP_CHARS = 12_000     # cli._sanitize clips every string field here
GATEWAY_METADATA_MAX_BYTES = 64 << 10
GATEWAY_MAX_JOBS_PER_POLL = 12
GATEWAY_MAX_BYTES_PER_POLL = 64 << 20
GATEWAY_MAX_FILE_BYTES = work_dispatcher_codex.MAX_JSONL_BYTES
GATEWAY_PRIORITY_SHARE = 0.5
GATEWAY_STDOUT_CHANGE_RETRY_CODES = frozenset({
    "partial_jsonl", "invalid_jsonl", "empty_or_invalid_jsonl", "terminal_record_invalid",
    "events_after_terminal", "agent_message_invalid", "final_message_missing",
})
GATEWAY_BACKOFF_SECONDS = 30.0
BACKOFF_DENIED = 3600.0
BACKOFF_RETRY = 30.0
UUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
UUID_RE = re.compile(rf"^{UUID}$")
RUN_GET_PARAMS = {"include_events": "false", "include_tool_calls": "false", "include_edges": "false", "include_timeline": "false"}
ITEM_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,47}$")

# (method, path regex): the complete set of HTTP calls this module may issue.
READ_ONLY_ROUTES = (
    ("GET", re.compile(r"^/api/cli-sessions$")),
    ("GET", re.compile(r"^/api/cli-sessions/[0-9a-fA-F-]{36}$")),
    ("GET", re.compile(r"^/api/cli-sessions/[0-9a-fA-F-]{36}/events$")),
    ("POST", re.compile(r"^/api/cli-sessions/collect$")),  # read-only batch event read
    ("GET", re.compile(r"^/api/local-agent/tasks$")),
    ("GET", re.compile(r"^/api/local-agent/tasks/[a-f0-9]{32}$")),
    ("GET", re.compile(r"^/api/agent-tasks/[a-f0-9]{32}$")),
    # Known-run reconciliation / result recovery: strict UUID read-only routes with ACL on the server.
    ("GET", re.compile(rf"^/api/agent-runs/{UUID}$")),
    ("GET", re.compile(rf"^/api/conversations/{UUID}/agent-runs$")),
    ("GET", re.compile(rf"^/api/conversations/{UUID}/messages$")),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, provider TEXT NOT NULL, status TEXT NOT NULL,
  cursor TEXT, first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_progress_at REAL NOT NULL,
  terminal_confirmed_at REAL, retired INTEGER NOT NULL DEFAULT 0, cursor_stalled INTEGER NOT NULL DEFAULT 0,
  drained INTEGER NOT NULL DEFAULT 0, collect_error TEXT, collected_at REAL);
CREATE TABLE IF NOT EXISTS tasks(
  task_id TEXT PRIMARY KEY, status TEXT NOT NULL, executor TEXT, goal TEXT,
  first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_progress_at REAL NOT NULL,
  stalled INTEGER NOT NULL DEFAULT 0, terminal_confirmed_at REAL, result_json TEXT);
CREATE TABLE IF NOT EXISTS inbox(
  id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_id TEXT NOT NULL,
  event_type TEXT, status TEXT, kind TEXT NOT NULL, summary TEXT, body_json TEXT NOT NULL,
  received_at REAL NOT NULL, acked_at REAL, run_id TEXT, message_id TEXT, handoff_id TEXT,
  UNIQUE(session_id, event_id));
CREATE TABLE IF NOT EXISTS handoffs(
  handoff_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT,
  terminal_status TEXT NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL, acked_at REAL,
  run_id TEXT, message_id TEXT, result_text TEXT, result_source TEXT, result_digest TEXT);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'unknown', observed_at REAL,
  source TEXT NOT NULL, first_seen REAL NOT NULL, remote_created_at REAL, remote_updated_at REAL,
  message_id TEXT, output_persisted INTEGER NOT NULL DEFAULT 0, checked_at REAL, check_error TEXT, next_check_at REAL,
  terminal_at REAL, recover_attempts INTEGER NOT NULL DEFAULT 0, recover_at REAL, recover_reason TEXT,
  settled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS run_lists(
  session_id TEXT NOT NULL, status TEXT NOT NULL, observed_at REAL NOT NULL, complete INTEGER NOT NULL,
  run_count INTEGER NOT NULL, error TEXT, PRIMARY KEY(session_id, status));
CREATE TABLE IF NOT EXISTS recovered_messages(
  run_id TEXT NOT NULL, message_id TEXT NOT NULL, session_id TEXT NOT NULL, digest TEXT NOT NULL, text TEXT NOT NULL,
  verified_at REAL NOT NULL, PRIMARY KEY(run_id, message_id));
CREATE TABLE IF NOT EXISTS outbox(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, handoff_id TEXT NOT NULL UNIQUE, source_type TEXT NOT NULL, source_id TEXT NOT NULL,
  run_id TEXT, message_id TEXT, result_digest TEXT NOT NULL, result_chars INTEGER NOT NULL, created_at REAL NOT NULL,
  delivery_state TEXT NOT NULL DEFAULT 'pending', received_at REAL, root_source_id TEXT, root_epoch INTEGER, receipt_note TEXT);
CREATE TABLE IF NOT EXISTS source_heartbeats(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, source_type TEXT NOT NULL,
  observed_at REAL NOT NULL, expires_at REAL, root_epoch INTEGER, aux_count INTEGER,
  via TEXT NOT NULL, error_code TEXT);
CREATE TABLE IF NOT EXISTS acknowledgments(
  item_kind TEXT NOT NULL, item_id TEXT NOT NULL, acked_at REAL NOT NULL, note TEXT,
  PRIMARY KEY(item_kind, item_id));
CREATE TABLE IF NOT EXISTS snapshots(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL, data_json TEXT NOT NULL);
"""


class SupervisorError(Exception):
    """Carries only a fixed, safe error code."""

    def __init__(self, code: str, *, bytes_read: int = 0):
        super().__init__(code)
        self.code = code
        self.bytes_read = bytes_read


def safe_code(exc: BaseException) -> str:
    if isinstance(exc, SupervisorError):
        return exc.code
    if isinstance(exc, (cli.CliSessionError, tasks.TaskError)):
        return exc.code
    if isinstance(exc, sqlite3.Error):
        return "ledger_error"
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return "io_error"
    return "unexpected_error"


def iso(ts: float | None) -> str | None:
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


# ---------------------------------------------------------------- ledger / lock

def connect(directory: Path = ROOT_DIR) -> sqlite3.Connection:
    directory.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(directory / "state.sqlite3", timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.executescript(SCHEMA)
    migrate(conn, directory)
    if "app_server_evidence_json" not in _columns(conn, "gateway_jobs"):
        conn.execute("ALTER TABLE gateway_jobs ADD COLUMN app_server_evidence_json TEXT")
    if "collected_at" not in _columns(conn, "sessions"):     # additive, idempotent (v4 ledgers created before this column)
        conn.execute("ALTER TABLE sessions ADD COLUMN collected_at REAL")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def migrate(conn: sqlite3.Connection, directory: Path) -> None:
    """Migrate old ledgers with a backup before schema changes.  Original observations are preserved: a backup copy is taken first and no row
    is deleted.  v1 session-keyed handoffs (which could mix turns) are only marked superseded; stored
    events are re-classified under the current rules; every existing handoff gets an outbox row
    (ready -> pending, already acked -> received by legacy ack, superseded -> superseded).  v4 -> v5 is strictly additive."""
    row = conn.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()
    version = int(row[0]) if row is not None else 0
    if version >= LEDGER_VERSION:
        return
    had_data = any(conn.execute(f'SELECT 1 FROM "{name}" LIMIT 1').fetchone() is not None
                   for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall())
    if had_data:
        backup = directory / f"state.sqlite3.pre-v{LEDGER_VERSION}-{time.time_ns()}.bak"
        with contextlib.closing(sqlite3.connect(backup)) as target:
            conn.backup(target)
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS gateway_jobs(
          job_id TEXT PRIMARY KEY, launch_identity TEXT, thread_id TEXT, final_item_id TEXT,
          result_digest TEXT, log_digest TEXT, log_bytes INTEGER, stdout_ref TEXT,
          stdout_size INTEGER, stdout_mtime_ns INTEGER, stdout_file_identity TEXT,
          process_status TEXT NOT NULL DEFAULT 'unknown', exit_code INTEGER,
          turn_status TEXT NOT NULL DEFAULT 'unknown', execution_status TEXT NOT NULL DEFAULT 'unknown',
          result_state TEXT NOT NULL DEFAULT 'pending', error_code TEXT,
          error_attempts INTEGER NOT NULL DEFAULT 0, next_check_at REAL,
          observed_at REAL NOT NULL, handoff_id TEXT UNIQUE)""")
        if version == 4:
            # The previous schema already performed legacy repair. Do not rewrite any v4 handoff,
            # outbox, receipt, or acknowledgement rows during this additive migration.
            conn.execute("INSERT OR REPLACE INTO meta VALUES('ledger_version', ?)", (str(LEDGER_VERSION),))
            conn.execute("COMMIT")
            return
        for table, column, kind in (("sessions", "drained", "INTEGER NOT NULL DEFAULT 0"), ("sessions", "collect_error", "TEXT"), ("sessions", "collected_at", "REAL"),
                                    ("inbox", "run_id", "TEXT"), ("inbox", "message_id", "TEXT"), ("inbox", "handoff_id", "TEXT"),
                                    ("handoffs", "run_id", "TEXT"), ("handoffs", "message_id", "TEXT"), ("handoffs", "result_text", "TEXT"),
                                    ("handoffs", "result_source", "TEXT"), ("handoffs", "result_digest", "TEXT")):
            if column not in _columns(conn, table):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")
        for item in conn.execute("SELECT id, session_id, received_at, body_json FROM inbox").fetchall():
            try:
                body = json.loads(item["body_json"])
            except ValueError:
                continue
            if not isinstance(body, dict):
                continue
            payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
            run_id = _run_id(body.get("run_id"))
            kind = classify_event(body)[1]
            conn.execute("UPDATE inbox SET run_id=?, message_id=?, kind=? WHERE id=?",
                         (run_id, _str_or_none(payload.get("assistant_message_id")), kind, item["id"]))
            if run_id:
                conn.execute("INSERT OR IGNORE INTO runs(run_id,session_id,status,source,first_seen) VALUES(?,?,?,?,?)",
                             (run_id, item["session_id"], "unknown", "event", item["received_at"]))
                if is_terminal_evidence(kind, body.get("status")):
                    conn.execute("UPDATE runs SET terminal_at=COALESCE(terminal_at, ?) WHERE run_id=?", (item["received_at"], run_id))
        conn.execute("UPDATE handoffs SET state='superseded_v1' WHERE source_type='cli_session' AND run_id IS NULL AND state='ready'")
        conn.execute("UPDATE sessions SET retired=0")   # v1 ack retired whole sessions; v2+ never retires
        for h in conn.execute("SELECT * FROM handoffs").fetchall():
            text = h["result_text"] or ""
            digest = h["result_digest"] or _digest(text)
            conn.execute("UPDATE handoffs SET result_digest=? WHERE handoff_id=?", (digest, h["handoff_id"]))
            state = {"ready": "pending", "acked": "received"}.get(h["state"], "superseded")
            conn.execute("INSERT OR IGNORE INTO outbox(handoff_id,source_type,source_id,run_id,message_id,result_digest,result_chars,"
                         "created_at,delivery_state,received_at,receipt_note) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                         (h["handoff_id"], h["source_type"], h["source_id"], h["run_id"], h["message_id"], digest, len(text),
                          h["created_at"], state, h["acked_at"] if state == "received" else None,
                          "legacy_ack_implies_receipt" if state == "received" else None))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('ledger_version', ?)", (str(LEDGER_VERSION),))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


def _run_id(value: Any) -> str | None:
    return value.lower() if isinstance(value, str) and UUID_RE.fullmatch(value) else None


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


class SingletonLock:
    """OS-level exclusive lock (released by the OS even after a crash)."""

    def __init__(self, directory: Path = ROOT_DIR):
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "supervisor.lock"
        self.handle = None

    def __enter__(self) -> "SingletonLock":
        handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise SupervisorError("already_running") from None
        self.handle = handle
        return self

    def __exit__(self, *exc: Any) -> None:
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()
            self.handle = None


def recover(conn: sqlite3.Connection, directory: Path = ROOT_DIR) -> None:
    """Crash recovery: drop half-written exports, verify the ledger, re-export status."""
    for leftover in directory.glob("status.json.*.tmp"):
        try:
            leftover.unlink()
        except OSError:
            pass
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise SupervisorError("ledger_corrupt")
    conn.execute("INSERT OR REPLACE INTO meta VALUES('recovered_at', ?)", (str(time.time()),))
    write_status(directory, build_status(conn, time.time()))


def write_status(directory: Path, status: dict[str, Any]) -> None:
    target = directory / "status.json"
    tmp = directory / f"status.json.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(status, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, target)


# ------------------------------------------------------------------------- safe log

LOG_NAME = "supervisor.log"
LOG_MAX_BYTES = 1 << 20         # 1 MiB x (current + 5 rotated)
LOG_BACKUPS = 5
_LOG_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:-]{0,64}$")


def log_event(directory: Path, event: str, **fields: Any) -> None:
    """Append one line of start/stop/safe-code/freshness facts.  Only identifier-shaped values are written (never
    text, paths, credentials, exception messages or HTTP bodies); a logging failure never affects observation."""
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


# ------------------------------------------------------------------ read-only gate

def _check_params(method: str, path: str, params: Any) -> None:
    """Parameter shape of the reconciliation routes is fixed too, not only the path."""
    ok = True
    if method == "GET" and path.startswith("/api/agent-runs/"):
        ok = params == RUN_GET_PARAMS
    elif method == "GET" and path.endswith("/agent-runs"):
        ok = (isinstance(params, dict) and set(params) == {"limit", "status"} and isinstance(params["limit"], int)
              and 1 <= params["limit"] <= RUN_LIST_LIMIT and params["status"] in RUN_STATUSES)
    elif method == "GET" and path.startswith("/api/conversations/") and path.endswith("/messages"):
        ok = (isinstance(params, dict) and set(params) == {"limit", "since"} and isinstance(params["limit"], int)
              and 1 <= params["limit"] <= max(MESSAGE_LIMITS) and parse_time(params["since"]) is not None)
    if not ok:
        raise SupervisorError("route_not_allowed")


LISTING_KEYS = (("/api/cli-sessions", "sessions"), ("/api/local-agent/tasks", "tasks"))


def _listing_key(method: str, path: str) -> str | None:
    if method != "GET":
        return None
    if path.endswith("/agent-runs"):
        return "agent_runs"
    return next((key for prefix, key in LISTING_KEYS if path == prefix), None)


def _sanitize_listing(data: dict[str, Any], key: str, settings: Any) -> dict[str, Any]:
    """Sanitize a list response item by item.  The shared 64 KB budget of ``cli._sanitize`` would otherwise cut a
    list after ~4 sessions (each ~14 KB) and make every count a lower bound; redaction is unchanged, a clipped
    item still sets ``output_truncated``."""
    items = data.get(key)
    if not isinstance(items, list):
        return cli._sanitize(data, settings)
    result = cli._sanitize({k: v for k, v in data.items() if k != key}, settings)
    truncated = bool(result.get("output_truncated"))
    clean: list[Any] = []
    for item in items:
        wrapped = cli._sanitize({"item": item}, settings)
        truncated = truncated or bool(wrapped.get("output_truncated"))
        if "item" in wrapped:
            clean.append(wrapped["item"])
        else:
            truncated = True
    result[key] = clean
    if truncated:
        result["output_truncated"] = True
    return result


async def observe(client: Any, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
    """The only HTTP entry point: rejects every route that could mutate agent state."""
    if not any(method == m and rx.fullmatch(path) for m, rx in READ_ONLY_ROUTES):
        raise SupervisorError("route_not_allowed")
    _check_params(method, path, kwargs.get("params"))
    _, data = await client.call(method, path, **kwargs)
    key = _listing_key(method, path)
    return _sanitize_listing(data, key, client.settings) if key else cli._sanitize(data, client.settings)


# ------------------------------------------------------------------------- fetch

class Fetched:
    def __init__(self, observed_at: float):
        self.observed_at = observed_at
        self.sources: dict[str, dict[str, Any]] = {}
        self.cli_lists: dict[str, list[dict[str, Any]]] = {}   # status -> sessions (ok lists only)
        self.cli_status: dict[str, str] = {}                   # sid -> confirmed status
        self.pages: dict[str, dict[str, Any]] = {}              # sid -> accepted page
        self.stalled_pages: dict[str, str] = {}                 # sid -> reason (cursor must not move)
        self.item_errors: dict[str, str] = {}                   # sid -> safe code (page missing / item error / unreachable)
        self.collected: set[str] = set()                        # sessions a collect was attempted for (backlog rotation)
        self.run_lists: dict[tuple[str, str], dict[str, Any]] = {}
        self.run_checks: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, dict[str, Any]] = {}
        self.task_lists: dict[str, list[dict[str, Any]]] = {}
        self.task_details: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, Any] = {}
        self.gateway_observations: list[dict[str, Any]] = []
        self.gateway_cursor_next: str | None = None
        self.gateway_previous: dict[str, dict[str, Any]] = {}
        self.gateway_summary: dict[str, Any] = {"discovered": 0, "observed": 0, "pending": 0, "conflicts": 0,
                                                "handoffs": 0, "errors": []}
        self.gateway_cursor = ""

    def source(self, name: str, *, ok: bool, lower_bound: bool = False, error: str | None = None) -> None:
        previous = self.sources.get(name, {"complete": True, "lower_bound": False, "error": None})
        self.sources[name] = {"complete": previous["complete"] and ok and not lower_bound,
                              "lower_bound": previous["lower_bound"] or lower_bound,
                              "error": previous["error"] or error, "observed_at": self.observed_at}


def make_plan(conn: sqlite3.Connection, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    sessions = {row["session_id"]: dict(row) for row in conn.execute("SELECT * FROM sessions")}
    pending = {row["task_id"]: dict(row) for row in conn.execute(
        "SELECT task_id, status, result_json FROM tasks WHERE terminal_confirmed_at IS NULL OR result_json IS NULL")}
    gateway_rows = {row["job_id"]: dict(row) for row in conn.execute("SELECT * FROM gateway_jobs")}
    cursor_row = conn.execute("SELECT value FROM meta WHERE key='gateway_cursor'").fetchone()
    run_lists = {(r["session_id"], r["status"]): r["observed_at"] for r in conn.execute("SELECT * FROM run_lists")}
    # Known run IDs are persisted; checks rotate fairly (never-checked / least recently checked first) and are capped.
    checks = [(r["run_id"], r["session_id"]) for r in conn.execute(
        "SELECT run_id, session_id FROM runs WHERE settled=0 AND status<>'succeeded' AND (next_check_at IS NULL OR next_check_at<=?)"
        " AND (observed_at IS NULL OR status NOT IN ('queued','running','awaiting_approval','uncertain') OR observed_at<?)"
        " ORDER BY COALESCE(checked_at,0), first_seen LIMIT ?", (now, now - RUN_RECHECK_SECONDS, RUN_CHECKS_PER_POLL))]
    recoveries = []
    for r in conn.execute(
            "SELECT * FROM runs WHERE settled=0 AND status='succeeded' AND output_persisted=1 AND message_id IS NOT NULL"
            " AND recover_attempts<? AND terminal_at<=? AND (next_check_at IS NULL OR next_check_at<=?)"
            " AND NOT EXISTS(SELECT 1 FROM recovered_messages m WHERE m.run_id=runs.run_id AND m.message_id=runs.message_id)"
            " ORDER BY COALESCE(recover_at,0) LIMIT ?", (RECOVERY_MAX_ATTEMPTS, now - RECOVERY_GRACE_SECONDS, now, RECOVERIES_PER_POLL)):
        # The messages route is ascending by time with a small page: start at this run's own start so the run's
        # assistant message is among the first rows, however long the run took.
        base = r["remote_created_at"] or ((r["terminal_at"] or now) - 3600.0)
        recoveries.append({"run_id": r["run_id"], "session_id": r["session_id"], "message_id": r["message_id"],
                           "since": iso(max(0.0, base - 60.0))})
    return {"sessions": sessions, "tasks": pending, "run_lists": run_lists, "run_checks": checks,
            "recoveries": recoveries, "gateway_jobs": gateway_rows,
            "gateway_cursor": cursor_row[0] if cursor_row else ""}


def _session_id(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("session_id") or item.get("id")
    return value if isinstance(value, str) and cli.SESSION_ID_RE.fullmatch(value) else None


async def _fetch_cli_lists(client: Any, out: Fetched) -> None:
    for status in CLI_ACTIVE + CLI_TERMINAL:
        try:
            limit = TERMINAL_LIST_LIMIT if status in CLI_TERMINAL else LIST_LIMIT
            data = await observe(client, "GET", "/api/cli-sessions", params={"limit": limit, "status": status})
            items = data.get("sessions")
            if not isinstance(items, list):
                raise SupervisorError("invalid_response")
            valid = [i for i in items if _session_id(i)]
            out.cli_lists[status] = valid
            # No pagination/offset exists: a full page (or clipped output) is only a lower bound.
            bound = len(items) >= limit or bool(data.get("output_truncated"))
            out.source("cli_list", ok=True, lower_bound=bound)
            out.source(f"cli_list:{status}", ok=True, lower_bound=bound)
        except (cli.CliSessionError, SupervisorError) as exc:
            out.source("cli_list", ok=False, error=safe_code(exc))
            out.source(f"cli_list:{status}", ok=False, error=safe_code(exc))


async def _fetch_cli_status(client: Any, out: Fetched, plan: dict[str, Any]) -> None:
    listed: dict[str, str] = {}
    for status, items in out.cli_lists.items():
        for item in items:
            listed[_session_id(item)] = status
    wanted = [sid for sid, row in plan["sessions"].items()
              if row["status"] not in CLI_TERMINAL and sid not in listed]
    wanted += [sid for sid, status in listed.items() if status in CLI_TERMINAL
               and (sid not in plan["sessions"] or plan["sessions"][sid]["status"] not in CLI_TERMINAL)]
    for sid in list(dict.fromkeys(wanted))[:STATUS_CHECKS_PER_POLL]:
        try:
            data = await observe(client, "GET", f"/api/cli-sessions/{sid}")
            nested = data.get("session") if isinstance(data.get("session"), dict) else {}
            status = data.get("status") or nested.get("status")
            if isinstance(status, str) and status in cli.SESSION_STATUSES:
                out.cli_status[sid] = status
            out.source("cli_status", ok=True)
        except (cli.CliSessionError, SupervisorError) as exc:
            out.source("cli_status", ok=False, error=safe_code(exc))
    if len(set(wanted)) > STATUS_CHECKS_PER_POLL:
        out.source("cli_status", ok=True, lower_bound=True)


def _page_verdict(page: Any) -> str:
    """ok | blocked (truncated: retry smaller) | malformed (cursor must not move)."""
    if not isinstance(page, dict):
        return "malformed"
    events = page.get("events")
    if not isinstance(events, list):
        return "malformed"
    if page.get("cursor_advance_blocked"):
        return "blocked"
    for event in events:
        if not isinstance(event, dict) or not event_identity(event):
            return "malformed"
        if event.get("event_type") is not None and not isinstance(event.get("event_type"), str):
            return "malformed"
    cursor = page.get("next_cursor")
    if events and not (isinstance(cursor, str) and cli.CURSOR_RE.fullmatch(cursor)):
        return "malformed"
    return "ok"


def _item_error_code(page: dict[str, Any]) -> str | None:
    error = page.get("error")
    if error is None:
        return None
    code = error.get("code") if isinstance(error, dict) else error
    return code if isinstance(code, str) and ITEM_CODE_RE.fullmatch(code) else "item_error"


async def _collect(client: Any, out: Fetched, plan: dict[str, Any], discovered: set[str],
                   revisit: set[str] | None = None, rechecks: list[str] | None = None) -> None:
    active_listed = {_session_id(i) for status, items in out.cli_lists.items() if status not in CLI_TERMINAL for i in items}
    candidates = {sid for sid, row in plan["sessions"].items()
                  if row["status"] not in CLI_TERMINAL or not row["drained"] or row["cursor_stalled"]
                  or row.get("collect_error")} | discovered | (revisit or set())
    # Active / failing sessions are always collected; terminal sessions that still hold unread events rotate through
    # a bounded budget (least recently collected first), so the first catch-up of a large history cannot stall the loop.
    must = sorted(sid for sid in candidates if sid in active_listed or (sid in plan["sessions"] and (
        plan["sessions"][sid]["status"] not in CLI_TERMINAL or plan["sessions"][sid]["cursor_stalled"]
        or plan["sessions"][sid].get("collect_error"))) or sid in (revisit or ()))
    backlog = sorted((sid for sid in candidates if sid not in must),
                     key=lambda sid: ((plan["sessions"].get(sid) or {}).get("collected_at") or 0.0, sid))
    ids = sorted(set(must + backlog[:BACKLOG_PER_POLL] + list(rechecks or ())))
    if len(backlog) > BACKLOG_PER_POLL:
        out.source("cli_collect", ok=True, lower_bound=True)    # more unread history than this poll's budget
    out.collected.update(ids)
    if not ids:
        out.source("cli_collect", ok=True)
        return
    suspect = list(ids)
    reasons: dict[str, str] = {}

    async def fetch_group(group: list[str], limit: int) -> None:
        if not group:
            return
        cursors = {sid: plan["sessions"][sid]["cursor"] for sid in group
                   if sid in plan["sessions"] and plan["sessions"][sid]["cursor"]}
        body = {"cursors": cursors, "session_ids": group, "limit_per_session": limit}
        try:
            data = await observe(client, "POST", "/api/cli-sessions/collect", json=body)
        except (cli.CliSessionError, SupervisorError) as exc:
            if len(group) > 1:                  # one bad session must not hide the others: bisect
                middle = len(group) // 2
                await fetch_group(group[:middle], limit)
                await fetch_group(group[middle:], limit)
            else:
                out.source("cli_collect", ok=False, error=safe_code(exc))
                out.item_errors[group[0]] = safe_code(exc)
            return
        clipped = bool(data.get("output_truncated"))
        pages = data.get("sessions")
        if not isinstance(pages, list):
            for sid in group:
                out.item_errors[sid] = "malformed_response"
            out.source("cli_collect", ok=False, error="malformed_response")
            return
        returned: dict[str, Any] = {}
        for item in pages:
            if _session_id(item):
                returned.setdefault(_session_id(item), item)
        retry: list[str] = []
        for sid in group:
            page = returned.get(sid)
            if page is None:
                if clipped:                     # the response budget dropped it: retry smaller, do not call it an error yet
                    reasons[sid] = "page_truncated"
                    retry.append(sid)
                else:
                    out.item_errors[sid] = "page_missing"
                    out.source("cli_collect", ok=False, error="page_missing")
                continue
            code = _item_error_code(page) if isinstance(page, dict) else None
            if code:                            # a per-session error is never a successful page
                out.item_errors[sid] = code
                out.source("cli_collect", ok=False, error=code)
                continue
            verdict = _page_verdict(page)
            if verdict == "ok" and not (clipped and page.get("events")):
                out.pages[sid] = page
                out.item_errors.pop(sid, None)
            else:
                reasons[sid] = "page_malformed" if verdict == "malformed" else "page_truncated"
                retry.append(sid)
        if len(group) > 1 and retry:
            # A combined response that clips says nothing about any one session's page: split the affected sessions
            # at the SAME limit and only shrink the limit for a session whose own isolated response still fails.
            middle = len(retry) // 2 if len(retry) > 1 else 0
            if middle:
                await fetch_group(retry[:middle], limit)
            await fetch_group(retry[middle:], limit)
        else:
            suspect.extend(retry)
        out.source("cli_collect", ok=True)

    for step, limit in enumerate(PAGE_LIMITS):
        if not suspect:
            break
        size = COLLECT_GROUP if step == 0 else 1     # later steps only hold sessions whose isolated page already failed
        groups = [suspect[i:i + size] for i in range(0, len(suspect), size)]
        suspect = []
        for group in groups:
            await fetch_group(group, limit)
    for sid in suspect:
        out.stalled_pages[sid] = reasons.get(sid, "page_truncated")
        out.source("cli_collect", ok=False, error=reasons.get(sid, "page_truncated"))


def _parse_run(item: Any, *, session_id: str | None = None, status: str | None = None, run_id: str | None = None) -> dict[str, Any] | None:
    """Validate one run record from the agent-run API; any binding or shape mismatch rejects it."""
    if not isinstance(item, dict):
        return None
    rid, sid = _run_id(item.get("id")), item.get("session_id")
    st = item.get("status")
    if rid is None or st not in RUN_STATUSES or not isinstance(sid, str) or not UUID_RE.fullmatch(sid):
        return None
    if (run_id and rid != run_id) or (session_id and sid.lower() != session_id.lower()) or (status and st != status):
        return None
    result = item.get("result") if isinstance(item.get("result"), dict) else {}
    validation = item.get("validation") if isinstance(item.get("validation"), dict) else {}
    message = result.get("assistant_message_id")
    return {"run_id": rid, "session_id": sid.lower(), "status": st,
            "message_id": message.lower() if isinstance(message, str) and UUID_RE.fullmatch(message) else None,
            "output_persisted": validation.get("provider_output_persisted") is True,
            "created_at": parse_time(item.get("created_at")), "updated_at": parse_time(item.get("updated_at"))}


async def _fetch_run_list(client: Any, out: Fetched, sid: str, status: str) -> None:
    key = (sid, status)
    try:
        data = await observe(client, "GET", f"/api/conversations/{sid}/agent-runs", params={"limit": RUN_LIST_LIMIT, "status": status})
        items = data.get("agent_runs")
        if data.get("success") is not True or not isinstance(items, list) or str(data.get("session_id", "")).lower() != sid.lower():
            raise SupervisorError("invalid_response")
    except (cli.CliSessionError, SupervisorError) as exc:
        out.run_lists[key] = {"complete": False, "runs": [], "error": safe_code(exc)}
        out.source("cli_run_lists", ok=False, error=safe_code(exc))
        return
    runs = [obs for obs in (_parse_run(i, session_id=sid, status=status) for i in items) if obs]
    # A bounded list (limit reached / clipped / any invalid item) is a lower bound, never an exact set.
    complete = len(runs) == len(items) and len(items) < RUN_LIST_LIMIT and not data.get("output_truncated")
    out.run_lists[key] = {"complete": complete, "runs": runs, "error": None if complete else "list_bounded"}
    out.source("cli_run_lists", ok=True, lower_bound=not complete)


async def _check_run(client: Any, out: Fetched, run_id: str, session_id: str) -> None:
    try:
        data = await observe(client, "GET", f"/api/agent-runs/{run_id}", params=dict(RUN_GET_PARAMS))
    except (cli.CliSessionError, SupervisorError) as exc:
        out.run_checks[run_id] = {"ok": False, "error": safe_code(exc)}
        out.source("cli_runs", ok=False, error=safe_code(exc))
        return
    item = data.get("agent_run") if data.get("success") is True else None
    obs = _parse_run(item, run_id=run_id, session_id=session_id)
    if obs is None:
        mismatch = isinstance(item, dict) and _run_id(item.get("id")) == run_id and isinstance(item.get("session_id"), str)
        code = "binding_mismatch" if mismatch else "invalid_response"
        out.run_checks[run_id] = {"ok": False, "error": code}
        out.source("cli_runs", ok=False, error=code)
        return
    out.run_checks[run_id] = {"ok": True, "obs": obs}
    out.source("cli_runs", ok=True)


async def _recover_message(client: Any, out: Fetched, rec: dict[str, Any]) -> None:
    """Fetch one assistant message through the session-ACL'd read-only messages route and verify it belongs
    to exactly this session / run / message id.  Anything short of a full, unclipped, bound match is a failure."""
    result: dict[str, Any] = {"ok": False, "reason": "message_not_found", "message_id": rec["message_id"]}
    since = rec["since"]
    try:
        for _page in range(MESSAGE_PAGES):
            data = None
            for limit in MESSAGE_LIMITS:
                data = await observe(client, "GET", f"/api/conversations/{rec['session_id']}/messages",
                                     params={"limit": limit, "since": since})
                if not data.get("output_truncated"):
                    break
                data = None                       # clipped response: retry with a smaller page
            if data is None:
                result["reason"] = "message_response_truncated"
                break
            messages = data.get("messages")
            if data.get("success") is not True or not isinstance(messages, list):
                result["reason"] = "invalid_response"
                break
            hits = [m for m in messages if isinstance(m, dict) and str(m.get("id", "")).lower() == rec["message_id"]]
            if len(hits) > 1:
                result["reason"] = "message_conflict"
                break
            if hits:
                result = _verify_message(hits[0], rec)
                break
            if len(messages) < limit:
                result["reason"] = "message_not_found"
                break
            result["reason"] = "message_not_in_bounded_page"
            # The route orders by created_at but filters on max(updated_at, created_at) with a 5s overlap, and its
            # server_time is the page's latest updated_at: following it can jump over unread rows.  Only the last row's
            # created_at is a proven cursor (every unread row has created_at >= it, hence matches the next filter).
            stamps = [parse_time(m.get("created_at")) if isinstance(m, dict) else None for m in messages]
            if any(t is None for t in stamps) or stamps != sorted(stamps) or stamps[-1] <= (parse_time(since) or 0):
                break                                    # no proven progress (ties / unordered / missing): stay result-pending
            if iso(stamps[-1]) == since:
                break
            since = iso(stamps[-1])
        out.source("cli_message_recovery", ok=True)
    except (cli.CliSessionError, SupervisorError) as exc:
        result = {"ok": False, "reason": safe_code(exc), "message_id": rec["message_id"]}
        out.source("cli_message_recovery", ok=False, error=safe_code(exc))
    out.messages[rec["run_id"]] = result


def _verify_message(message: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    def fail(reason: str) -> dict[str, Any]:
        return {"ok": False, "reason": reason, "message_id": rec["message_id"]}
    metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
    content = message.get("content")
    if str(message.get("session_id", "")).lower() != rec["session_id"].lower() or message.get("role") != "assistant":
        return fail("message_binding_mismatch")
    if str(metadata.get("run_id", "")).lower() != rec["run_id"]:
        return fail("message_binding_mismatch")
    if message.get("deleted_at") or not isinstance(content, str):
        return fail("message_unreadable")
    if len(content) >= MESSAGE_CLIP_CHARS:
        return fail("message_text_clipped")
    return {"ok": True, "reason": None, "message_id": rec["message_id"], "text": content}


async def _fetch_runs(client: Any, out: Fetched, plan: dict[str, Any]) -> None:
    """Known-run reconciliation with the same authenticated client; failures leave the state UNKNOWN."""
    scope = sorted({_session_id(i) for status in CLI_ACTIVE for i in out.cli_lists.get(status, [])},
                   key=lambda sid: min(plan["run_lists"].get((sid, st), 0.0) for st in ("running", "queued")))
    for sid in scope[:SESSION_RUN_LISTS_PER_POLL]:
        for status in ("running", "queued"):
            await _fetch_run_list(client, out, sid, status)
    for run_id, session_id in plan["run_checks"][:RUN_CHECKS_PER_POLL]:
        await _check_run(client, out, run_id, session_id)
    for rec in plan["recoveries"][:RECOVERIES_PER_POLL]:
        await _recover_message(client, out, rec)


async def _fetch_tasks(client: Any, out: Fetched, plan: dict[str, Any]) -> None:
    for status in TASK_ACTIVE + TASK_TERMINAL:
        try:
            data = await observe(client, "GET", "/api/local-agent/tasks", params={"limit": TASK_LIST_LIMIT, "status": status})
            items = data.get("tasks")
            if not isinstance(items, list):
                raise SupervisorError("invalid_response")
            valid = []
            for item in items:
                try:
                    valid.append(tasks._compact(tasks._task({"task": item})))
                except tasks.TaskError:
                    continue
            out.task_lists[status] = valid
            out.source("tasks", ok=True, lower_bound=len(items) >= TASK_LIST_LIMIT or bool(data.get("output_truncated")))
        except (tasks.TaskError, SupervisorError) as exc:
            out.source("tasks", ok=False, error=safe_code(exc))
    need = [t["task_id"] for status in TASK_TERMINAL for t in out.task_lists.get(status, [])
            if not (plan["tasks"].get(t["task_id"]) or {}).get("result_json") and not _has_result(t)]
    for task_id in list(dict.fromkeys(need))[:STATUS_CHECKS_PER_POLL]:
        try:
            canonical, data = await _task_get(client, task_id)
            task = tasks._canonical_task(data) if canonical else tasks._task(data)
            out.task_details[task_id] = tasks._compact(task)
        except (tasks.TaskError, SupervisorError) as exc:
            out.source("tasks_detail", ok=False, error=safe_code(exc))


async def _task_get(client: Any, task_id: str) -> tuple[bool, dict[str, Any]]:
    try:
        return False, await observe(client, "GET", f"/api/local-agent/tasks/{task_id}")
    except tasks.TaskError as exc:
        if exc.code != "not_found":
            raise
    return True, await observe(client, "GET", f"/api/agent-tasks/{task_id}")


async def _fetch_jobs(out: Fetched) -> None:
    """Observe local Gateway jobs without consulting metadata-provided log paths."""
    try:
        result = await asyncio.to_thread(_scan_gateway_jobs, out.gateway_cursor, out.gateway_previous, out.observed_at)
        out.gateway_observations = result["observations"]
        out.gateway_cursor_next = result["cursor"]
        out.gateway_summary = result["summary"]
        out.jobs = {"active": result["active"], "active_lower_bound": bool(result["lower_bound"]),
                    "errors": len(result["summary"]["errors"])}
        out.source("jobs", ok=True, lower_bound=result["lower_bound"])
        incomplete = bool(result["summary"]["errors"])
        out.source("gateway_codex", ok=not incomplete, lower_bound=result["lower_bound"],
                   error="gateway_observation_incomplete" if incomplete else None)
    except Exception as exc:  # noqa: BLE001 - only the safe code is kept
        code = safe_code(exc)
        out.source("jobs", ok=False, error=code)
        out.source("gateway_codex", ok=False, error=code)


def _same_stat(left: Any, right: Any) -> bool:
    return bool(left and right and left.st_size == right.st_size and left.st_mtime_ns == right.st_mtime_ns
                and left.st_ino == right.st_ino and left.st_dev == right.st_dev)


def _safe_stat(path: Path) -> os.stat_result:
    try:
        stat = path.lstat()
    except FileNotFoundError:
        raise SupervisorError("file_missing") from None
    except OSError:
        raise SupervisorError("file_unreadable") from None
    if os.path.islink(path) or not os.path.isfile(path):
        raise SupervisorError("path_not_regular")
    return stat


def _read_stable_file(path: Path, max_bytes: int) -> tuple[bytes, os.stat_result]:
    size = 0
    before = _safe_stat(path)
    if before.st_size > max_bytes:
        raise SupervisorError("file_too_large")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not _same_stat(before, opened):
                raise SupervisorError("file_changed")
            chunks: list[bytes] = []
            while size < max_bytes:
                chunk = stream.read(min(1 << 20, max_bytes - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            after_fd = os.fstat(stream.fileno())
        after_path = _safe_stat(path)
    except SupervisorError as exc:
        exc.bytes_read = size
        raise
    except FileNotFoundError:
        raise SupervisorError("file_missing") from None
    except OSError:
        raise SupervisorError("file_unreadable", bytes_read=size) from None
    if not _same_stat(before, after_fd) or not _same_stat(before, after_path) or size != before.st_size:
        raise SupervisorError("file_changed", bytes_read=size)
    return b"".join(chunks), after_path


def _inside(path: Path, root: Path) -> bool:
    try:
        return os.path.commonpath((os.path.normcase(str(path)), os.path.normcase(str(root)))) == os.path.normcase(str(root))
    except (OSError, ValueError):
        return False


def _validate_gateway_job_directory(directory: Path, resolved_jobs: Path) -> Path:
    try:
        resolved = directory.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SupervisorError("job_path_invalid") from None
    if resolved != directory or not _inside(resolved, resolved_jobs):
        raise SupervisorError("job_path_invalid")
    return resolved


def _looks_like_codex_exec(command: Any) -> bool:
    if not isinstance(command, list) or len(command) < 2 or not all(isinstance(value, str) for value in command):
        return False
    entry = command[1].replace("/", "\\").casefold()
    official = "\\node_modules\\@openai\\codex\\bin\\codex.js"
    return entry.endswith(official) and ("exec" in command[2:] or "--json" in command[2:])


def _pinned_appserver_transport(metadata: dict[str, Any]) -> bool:
    """Select transport from the digest-verified manifest, never mutable ops."""
    from .jobs import _dispatcher_binding_valid, stable_job_id
    from .work_dispatcher import DispatchError, canonical, parse_manifest
    from urllib.parse import quote
    binding = metadata.get("dispatcher_binding")
    if binding is None:
        return False
    if not _dispatcher_binding_valid(binding):
        raise SupervisorError("binding_mismatch")
    try:
        path = Path(binding["db_path"]).resolve(strict=True)
        uri = "file:" + quote(str(path).replace("\\", "/"), safe="/:\\") + "?mode=ro"
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=0.2)) as conn:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("SELECT digest,raw,root_source_id,root_epoch FROM manifests WHERE manifest_id=?",
                               (binding["manifest_id"],)).fetchone()
        if row is None:
            raise SupervisorError("binding_unavailable")
        manifest = parse_manifest(bytes(row[1]), binding["manifest_digest"])
        if (row[0] != binding["manifest_digest"] or manifest["manifest_id"] != binding["manifest_id"]
                or manifest["root_source_id"] != binding["root_source_id"] or row[2] != binding["root_source_id"]
                or manifest["root_epoch"] != binding["root_epoch"] or row[3] != binding["root_epoch"]
                or metadata.get("job_id") != binding["gateway_job_id"]
                or stable_job_id(binding["op_key"]) != binding["gateway_job_id"]):
            raise SupervisorError("binding_mismatch")
        pinned = next((job for job in manifest["jobs"] if job["job_id"] == binding["job_id"]), None)
        if (pinned is None or pinned.get("route") != "gateway-codex" or pinned["create_key"] != binding["op_key"]
                or hashlib.sha256(canonical(pinned["create_request"]).encode("utf-8")).hexdigest() != binding["request_digest"]
                or binding["request_digest"] != binding["request_fingerprint"]):
            raise SupervisorError("binding_mismatch")
        return pinned["create_request"].get("transport") == codex_app_server.TRANSPORT
    except DispatchError:
        raise SupervisorError("binding_mismatch") from None
    except (OSError, ValueError, TypeError, sqlite3.Error, RecursionError):
        raise SupervisorError("binding_unavailable") from None


def _canonical_executable(value: str, *, official_entrypoint: bool = False) -> str:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise SupervisorError("launch_path_invalid")
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SupervisorError("launch_path_invalid") from None
    if not resolved.is_file() or os.path.normcase(os.path.normpath(str(resolved))) != os.path.normcase(os.path.normpath(value)):
        raise SupervisorError("launch_path_invalid")
    if official_entrypoint:
        suffix = tuple(part.casefold() for part in resolved.parts[-len(work_dispatcher_codex.OFFICIAL_ENTRYPOINT_SUFFIX):])
        if suffix != tuple(part.casefold() for part in work_dispatcher_codex.OFFICIAL_ENTRYPOINT_SUFFIX):
            raise SupervisorError("entrypoint_untrusted")
    elif resolved.name.casefold() not in {"node.exe", "node"}:
        raise SupervisorError("node_untrusted")
    return str(resolved)


def _validated_gateway_metadata(metadata: Any, job_id: str, directory: Path, runtime_root: Path) -> tuple[str, list[str]]:
    if not isinstance(metadata, dict):
        raise SupervisorError("metadata_invalid")
    if metadata.get("job_id") != job_id:
        raise SupervisorError("metadata_job_id_mismatch")
    if metadata.get("transport") == codex_app_server.TRANSPORT:
        return _validated_appserver_metadata(metadata, job_id, directory, runtime_root)
    if "app_server" in metadata or _pinned_appserver_transport(metadata):
        raise SupervisorError("app_server_transport_mismatch")
    command = metadata.get("command")
    if not _looks_like_codex_exec(command):
        raise SupervisorError("launch_not_gateway_codex")
    argv = command
    if (len(argv) < 5 or argv[2] != "exec" or argv.count("--json") != 1
            or "--json" not in argv[3:-1] or any("\x00" in value for value in argv)):
        raise SupervisorError("launch_shape_invalid")
    _canonical_executable(argv[0])
    _canonical_executable(argv[1], official_entrypoint=True)
    expected_stdout = directory / "stdout.log"
    raw_stdout = metadata.get("stdout_path")
    if not isinstance(raw_stdout, str) or not Path(raw_stdout).is_absolute():
        raise SupervisorError("stdout_reference_invalid")
    if os.path.normcase(os.path.normpath(raw_stdout)) != os.path.normcase(os.path.normpath(str(expected_stdout))):
        raise SupervisorError("stdout_reference_mismatch")
    if "runtime_dir" in metadata:
        raw_runtime = metadata.get("runtime_dir")
        if not isinstance(raw_runtime, str) or os.path.normcase(os.path.normpath(raw_runtime)) != os.path.normcase(os.path.normpath(str(runtime_root))):
            raise SupervisorError("runtime_reference_mismatch")
    if not isinstance(metadata.get("cwd"), str) or not metadata["cwd"]:
        raise SupervisorError("launch_identity_invalid")
    started_at = metadata.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise SupervisorError("launch_identity_invalid")
    stable = {"job_id": job_id, "command": argv, "cwd": metadata["cwd"], "started_at": started_at,
              "runtime_dir": metadata.get("runtime_dir", str(runtime_root)), "stdout_ref": f"jobs/{job_id}/stdout.log"}
    for key in ("idempotency_key", "request_fingerprint", "dispatcher_request_fingerprint", "explicit_env_digest", "dispatcher_binding"):
        if key in metadata:
            stable[key] = metadata[key]
    identity = hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
    return identity, argv


def _validated_appserver_metadata(metadata: dict[str, Any], job_id: str, directory: Path,
                                 runtime_root: Path) -> tuple[str, list[str]]:
    """Select the separate transport only from its immutable metadata discriminator."""
    if metadata.get("transport") != codex_app_server.TRANSPORT:
        raise SupervisorError("app_server_transport_mismatch")
    guard = work_dispatcher_codex.steer_guard(metadata.get("dispatcher_binding"), metadata, require_running=False)
    if guard != "open":
        raise SupervisorError("binding_mismatch" if guard == "mismatch" else "binding_unavailable" if guard == "unavailable" else "binding_closed")
    try:
        argv = work_dispatcher_codex.validate_app_server_command(metadata.get("command"))
        if not isinstance(metadata.get("cwd"), str):
            raise ValueError("invalid_cwd")
        spec = work_dispatcher_codex.validate_app_server_spec(metadata.get("app_server"), cwd=metadata["cwd"])
    except (ValueError, TypeError, OSError, RuntimeError):
        raise SupervisorError("app_server_metadata_invalid") from None
    if metadata.get("job_id") != job_id:
        raise SupervisorError("metadata_job_id_mismatch")
    started_at = metadata.get("started_at")
    if isinstance(started_at, bool) or not isinstance(started_at, (int, float)):
        raise SupervisorError("launch_identity_invalid")
    if metadata.get("runtime_dir", str(runtime_root)) != str(runtime_root):
        raise SupervisorError("runtime_reference_mismatch")
    runtime = metadata.get("app_server_runtime")
    if runtime is not None and not isinstance(runtime, dict):
        raise SupervisorError("app_server_metadata_invalid")
    expected_journal = directory / "appserver.rpc.jsonl"
    journal_ref = runtime.get("journal_path") if isinstance(runtime, dict) else None
    if journal_ref is not None and journal_ref != str(expected_journal):
        raise SupervisorError("rpc_journal_reference_mismatch")
    stable = {"job_id": job_id, "command": argv, "cwd": spec["cwd"], "started_at": started_at,
              "transport": codex_app_server.TRANSPORT, "app_server": spec,
              "runtime_dir": str(runtime_root), "journal_ref": f"jobs/{job_id}/appserver.rpc.jsonl"}
    for key in ("idempotency_key", "request_fingerprint", "dispatcher_request_fingerprint", "explicit_env_digest", "dispatcher_binding"):
        if key in metadata:
            stable[key] = metadata[key]
    return hashlib.sha256(json.dumps(stable, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest(), argv


_APP_SERVER_DIGESTS = {"spec_digest", "native_terminal_digest", "steer_evidence_digest", "evidence_digest"}
_APP_SERVER_SHUTDOWN_CODES = {"control_lost", "unknown", "appserver_shutdown_forced", "appserver_shutdown_incomplete"}
_APP_SERVER_RUNTIME_ERRORS = {"appserver_auth_required", "appserver_connection_lost", "appserver_disabled",
    "appserver_evidence_limit", "appserver_mailbox_finalize_failed", "appserver_pipe_lost", "appserver_policy_mismatch",
    "appserver_protocol_invalid", "appserver_protocol_io_failed", "appserver_response_write_failed",
    "appserver_rpc_rejected", "appserver_runtime_failed", "appserver_shutdown_incomplete"}


def _safe_appserver_evidence(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict) or value.get("transport") != codex_app_server.TRANSPORT:
        return None
    safe: dict[str, Any] = {"transport": codex_app_server.TRANSPORT}
    for key in _APP_SERVER_DIGESTS:
        item = value.get(key)
        if isinstance(item, str) and re.fullmatch(r"[a-f0-9]{64}", item):
            safe[key] = item
    if type(value.get("input_set_uncertain")) is bool:
        safe["input_set_uncertain"] = value["input_set_uncertain"]
    for key in ("steer_count", "recorded_steer_count", "approval_request_count"):
        if type(value.get(key)) is int and 0 <= value[key] <= 1_000_000:
            safe[key] = value[key]
    if isinstance(value.get("native_outcome"), str) and value["native_outcome"] in {"completed", "failed", "interrupted"}:
        safe["native_outcome"] = value["native_outcome"]
    if isinstance(value.get("shutdown_code"), str) and value["shutdown_code"] in _APP_SERVER_SHUTDOWN_CODES:
        safe["shutdown_code"] = value["shutdown_code"]
    elif "shutdown_code" in value and value["shutdown_code"] is None:
        safe["shutdown_code"] = None
    if value.get("policy_code") == "policy_unexpected_request":
        safe["policy_code"] = "policy_unexpected_request"
    if isinstance(value.get("turn_id"), str) and SOURCE_ID_RE.fullmatch(value["turn_id"]):
        safe["turn_id"] = value["turn_id"]
    return safe


def _decode_appserver_evidence(raw: Any) -> dict[str, Any] | None:
    try:
        return _safe_appserver_evidence(json.loads(raw or "null"))
    except (ValueError, TypeError, RecursionError):
        return None


def _appserver_process_facts(metadata: dict[str, Any], state: str) -> tuple[str, int | None, str]:
    status, exit_code, _ = _process_facts(metadata, "result_pending")
    runtime = metadata.get("app_server_runtime") or {}
    if isinstance(runtime.get("shutdown_code"), str) and runtime["shutdown_code"] in {"worker_lost", "control_lost", "child_alive"}:
        status = "unknown"
    if status in {"starting", "running", "stopping"}:
        from .jobs import identity
        if identity(metadata.get("worker_pid"), metadata.get("worker_create_time")) != "alive":
            status = "unknown"
    return status, exit_code, "succeeded" if state == "succeeded" else "failed" if state == "failed" else "unknown"


def _observe_appserver_job(metadata: dict[str, Any], job_id: str, directory: Path, runtime_root: Path,
                           previous: dict[str, Any] | None, now: float, byte_limit: int) -> tuple[dict[str, Any], int]:
    """The app-server journal alone proves the turn; process shutdown is separate."""
    from . import job_steering
    identity, _ = _validated_appserver_metadata(metadata, job_id, directory, runtime_root)
    base: dict[str, Any] = {"job_id": job_id, "observed_at": now, "launch_identity": identity,
        "stdout_ref": f"jobs/{job_id}/appserver.rpc.jsonl", "result_state": "pending", "turn_status": "unknown",
        "execution_status": "unknown", "error_code": None}
    base["process_status"], base["exit_code"], _ = _appserver_process_facts(metadata, "result_pending")
    if previous and previous.get("launch_identity") and previous["launch_identity"] != identity:
        return {**base, "result_state": "conflict", "error_code": "launch_identity_conflict", "reuse": True}, 0
    path = directory / "appserver.rpc.jsonl"
    try:
        data, stat = _read_stable_file(path, min(codex_app_server.MAX_JOURNAL_BYTES, max(0, byte_limit)))
    except SupervisorError as exc:
        code = ("poll_byte_budget" if exc.code == "file_too_large" and byte_limit < codex_app_server.MAX_JOURNAL_BYTES
                else "rpc_journal_missing" if exc.code == "file_missing" else exc.code)
        return {**base, "error_code": code}, exc.bytes_read
    runtime = metadata.get("app_server_runtime") or {}
    try:
        steering = job_steering.steer_evidence(directory)
    except (OSError, ValueError, TypeError, RecursionError):
        steering = {"unavailable": True}
    parsed = codex_app_server.parse_appserver_journal(data, metadata["app_server"],
        expected_thread_id=runtime.get("thread_id"), expected_turn_id=runtime.get("turn_id"), steer_evidence=steering)
    # Some malformed wire frames cannot themselves be journaled.  A runtime
    # failure remains authoritative even if earlier native turn proof was valid.
    if runtime.get("runtime_error_code"):
        code = runtime["runtime_error_code"]
        parsed.update(state="failed", code=code if isinstance(code, str) and code in _APP_SERVER_RUNTIME_ERRORS else "appserver_runtime_failed")
    if (parsed.get("state") == "succeeded" and (metadata.get("status") not in {"exited", "failed", "stopped"}
            or runtime.get("connection_state") in {"connecting", "connected"})):
        # The reader can still discover a fatal post-terminal frame.  Only the
        # worker's final close makes the journal suitable for an outbox handoff.
        parsed.update(state="result_pending", code="appserver_journal_open")
    evidence = _safe_appserver_evidence({**parsed, "shutdown_code": runtime.get("shutdown_code", "unknown")})
    state = parsed.get("state")
    base["process_status"], base["exit_code"], base["execution_status"] = _appserver_process_facts(metadata, state)
    base.update(thread_id=parsed.get("thread_id"), final_item_id=parsed.get("final_item_id"),
        result_digest=parsed.get("result_digest"), result_text=parsed.get("result_text"),
        log_digest=hashlib.sha256(data).hexdigest(), log_bytes=len(data),
        result_state="succeeded" if state == "succeeded" else "failed" if state == "failed" else "pending",
        turn_status="turn_completed" if parsed.get("native_outcome") == "completed" else
                    "turn_failed" if parsed.get("native_outcome") in {"failed", "interrupted"} else "unknown",
        error_code=parsed.get("code"), stdout_size=stat.st_size, stdout_mtime_ns=stat.st_mtime_ns,
        stdout_file_identity=f"{stat.st_dev}:{stat.st_ino}", app_server_evidence=evidence)
    if (previous and previous.get("result_state") in {"succeeded", "failed"}
            and (previous.get("result_digest") != base.get("result_digest")
                 or previous.get("thread_id") != base.get("thread_id")
                 or previous.get("final_item_id") != base.get("final_item_id"))):
        base.update(result_state="conflict", error_code="result_identity_conflict", reuse=True)
    return base, len(data)


def _process_facts(metadata: dict[str, Any], parsed_state: str) -> tuple[str, int | None, str]:
    raw_status = metadata.get("status")
    process_status = raw_status if isinstance(raw_status, str) and raw_status in {
        "starting", "running", "stopping", "exited", "failed", "stopped", "lost", "unknown", "not_launched"
    } else "unknown"
    raw_exit = metadata.get("exit_code")
    exit_code = raw_exit if type(raw_exit) is int else None
    if parsed_state == "failed":
        execution = "failed"
    elif parsed_state != "succeeded":
        execution = "unknown"
    elif process_status == "exited" and exit_code == 0:
        execution = "succeeded"
    elif process_status in {"failed", "stopped", "not_launched"} or (process_status == "exited" and exit_code is not None and exit_code != 0):
        execution = "failed"
    else:
        execution = "unknown"
    return process_status, exit_code, execution


_GATEWAY_TERMINAL_PROCESS = frozenset({"exited", "failed", "stopped"})
_GATEWAY_TERMINAL_HINT_BOOST = 1 << 62


def _gateway_priority_hint(job_id: str, directory: Path, previous: dict[str, Any] | None,
                           resolved_jobs: Path) -> int | None:
    """Return a recency hint for new/changed verified candidates; stat never proves a result."""
    try:
        _validate_gateway_job_directory(directory, resolved_jobs)
    except SupervisorError:
        return None
    try:
        metadata_stat = _safe_stat(directory / "metadata.json")
    except SupervisorError:
        return None
    # The discriminator chooses a separate journal; stdout never selects a transport.
    try:
        raw_candidate, _ = _read_stable_file(directory / "metadata.json", GATEWAY_METADATA_MAX_BYTES)
        candidate = json.loads(raw_candidate.decode("utf-8", errors="strict"))
        if isinstance(candidate, dict) and candidate.get("transport") == codex_app_server.TRANSPORT:
            identity, _ = _validated_appserver_metadata(candidate, job_id, directory, directory.parent.parent)
            if previous and previous.get("result_state") == "conflict":
                return None
            try:
                journal_stat = _safe_stat(directory / "appserver.rpc.jsonl")
            except SupervisorError:
                journal_stat = None
            changed = bool(previous is None or identity != previous.get("launch_identity")
                or (journal_stat and (journal_stat.st_size != previous.get("stdout_size")
                    or journal_stat.st_mtime_ns != previous.get("stdout_mtime_ns"))))
            status, exit_code, _ = _appserver_process_facts(candidate, "result_pending")
            changed = changed or bool(previous and (previous.get("process_status") != status or previous.get("exit_code") != exit_code))
            return max(metadata_stat.st_mtime_ns, journal_stat.st_mtime_ns if journal_stat else 0) if changed else None
    except (SupervisorError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    try:
        stdout_stat = _safe_stat(directory / "stdout.log")
    except SupervisorError:
        stdout_stat = None

    if previous is None:
        try:
            raw, _ = _read_stable_file(directory / "metadata.json", GATEWAY_METADATA_MAX_BYTES)
            metadata = json.loads(raw.decode("utf-8", errors="strict"))
        except (SupervisorError, UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(metadata, dict) or metadata.get("job_id") != job_id or not _looks_like_codex_exec(metadata.get("command")):
            return None
        return max(metadata_stat.st_mtime_ns, stdout_stat.st_mtime_ns if stdout_stat else 0)

    # A changed stat must not reopen a pinned conflict or an authorization/binding denial.
    if (previous.get("result_state") == "conflict" or not previous.get("launch_identity")
            or previous.get("error_code") in {
                "permission_denied", "denied", "not_found", "binding_mismatch",
                "launch_identity_conflict", "result_identity_conflict",
            }):
        return None
    stdout_changed = bool(stdout_stat and (
        previous.get("stdout_size") != stdout_stat.st_size
        or previous.get("stdout_mtime_ns") != stdout_stat.st_mtime_ns
        or previous.get("stdout_file_identity") != f"{stdout_stat.st_dev}:{stdout_stat.st_ino}"
    ))
    try:
        raw, _ = _read_stable_file(directory / "metadata.json", GATEWAY_METADATA_MAX_BYTES)
        metadata = json.loads(raw.decode("utf-8", errors="strict"))
        if not isinstance(metadata, dict):
            return None
        identity, _ = _validated_gateway_metadata(metadata, job_id, directory, directory.parent.parent)
        process_status, exit_code, _ = _process_facts(metadata, "unknown")
    except (SupervisorError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    metadata_changed = (identity != previous.get("launch_identity")
                        or process_status != previous.get("process_status")
                        or exit_code != previous.get("exit_code"))
    if not stdout_changed and not metadata_changed:
        return None
    hint = max(metadata_stat.st_mtime_ns, stdout_stat.st_mtime_ns if stdout_stat else 0)
    # A running->terminal transition outranks streaming jobs, whose stdout mtime is always newer.
    if (process_status in _GATEWAY_TERMINAL_PROCESS and process_status != previous.get("process_status")
            and previous.get("process_status") not in _GATEWAY_TERMINAL_PROCESS):
        hint += _GATEWAY_TERMINAL_HINT_BOOST
    return hint


def _scan_gateway_jobs(cursor: str, previous_rows: dict[str, dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """Read a fair, bounded slice of official standalone Codex jobs from the D80 runtime."""
    from . import jobs

    now = time.time() if now is None else now
    runtime = jobs.runtime_root()
    jobs_root = runtime / "jobs"
    if not jobs_root.exists():
        return {"observations": [], "cursor": cursor, "summary": {"discovered": 0, "observed": 0, "pending": 0,
                "conflicts": 0, "handoffs": 0, "errors": [{"job_id": "runtime", "code": "jobs_path_missing"}]},
                "active": 0, "lower_bound": False}
    if jobs_root.is_symlink():
        raise SupervisorError("jobs_path_invalid")
    try:
        resolved_jobs = jobs_root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise SupervisorError("jobs_path_invalid") from None
    if not _inside(resolved_jobs, runtime) or resolved_jobs != runtime / "jobs":
        raise SupervisorError("jobs_path_invalid")
    ids = []
    for entry in jobs_root.iterdir():
        if not entry.is_dir() or entry.is_symlink():
            continue
        try:
            canonical = str(__import__("uuid").UUID(entry.name)).lower()
        except (ValueError, AttributeError):
            continue
        if canonical == entry.name:
            ids.append(canonical)
    ids.sort()
    discovered = len(ids)
    if ids and cursor in ids:
        offset = (ids.index(cursor) + 1) % len(ids)
        ordered = ids[offset:] + ids[:offset]
    else:
        ordered = ids
    job_limit = max(0, GATEWAY_MAX_JOBS_PER_POLL)
    priority_limit = min(len(ids), job_limit // 2)
    if job_limit == 1:
        # A one-job test/deployment slice remains history-first so its cursor always moves.
        priority_limit = 0
    priority_hints: list[tuple[int, str]] = []
    for job_id in ids:
        hint = _gateway_priority_hint(job_id, jobs_root / job_id, previous_rows.get(job_id), resolved_jobs)
        if hint is not None:
            priority_hints.append((hint, job_id))
    priority_hints.sort(key=lambda item: (item[0], item[1]), reverse=True)
    priority_ids = [job_id for _, job_id in priority_hints[:priority_limit]]
    priority_set = set(priority_ids)
    history_limit = max(0, job_limit - len(priority_ids))
    history_ids = [job_id for job_id in ordered if job_id not in priority_set][:history_limit]
    selected = priority_ids + history_ids
    selected_lane = {job_id: "priority" for job_id in priority_ids}
    selected_lane.update({job_id: "history" for job_id in history_ids})
    lower_bound = len(ordered) > len(selected)
    summary = {"discovered": discovered, "observed": 0, "pending": 0, "conflicts": 0, "handoffs": 0, "errors": []}
    observations: list[dict[str, Any]] = []
    total_bytes = 0
    priority_bytes = 0
    priority_byte_budget = int(GATEWAY_MAX_BYTES_PER_POLL * GATEWAY_PRIORITY_SHARE)
    active = 0
    last_history_consumed_id: str | None = None
    history_byte_defer_cursor: str | None = None
    for job_id in selected:
        lane = selected_lane[job_id]
        summary["observed"] += 1
        directory = jobs_root / job_id
        previous = previous_rows.get(job_id)
        base: dict[str, Any] = {"job_id": job_id, "observed_at": now, "stdout_ref": f"jobs/{job_id}/stdout.log"}
        try:
            _validate_gateway_job_directory(directory, resolved_jobs)
            metadata_path = directory / "metadata.json"
            meta_stat = _safe_stat(metadata_path)
            raw_metadata, after_metadata = _read_stable_file(metadata_path, GATEWAY_METADATA_MAX_BYTES)
            if not _same_stat(meta_stat, after_metadata):
                raise SupervisorError("metadata_changed")
            try:
                metadata = json.loads(raw_metadata.decode("utf-8", errors="strict"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                raise SupervisorError("metadata_invalid") from None
            if not isinstance(metadata, dict):
                continue
            if metadata.get("transport") != codex_app_server.TRANSPORT and ("app_server" in metadata or _pinned_appserver_transport(metadata)):
                raise SupervisorError("app_server_transport_mismatch")
            if metadata.get("transport") == codex_app_server.TRANSPORT:
                base["stdout_ref"] = f"jobs/{job_id}/appserver.rpc.jsonl"
                available = (min(GATEWAY_MAX_BYTES_PER_POLL - total_bytes, priority_byte_budget - priority_bytes)
                             if lane == "priority" else GATEWAY_MAX_BYTES_PER_POLL - total_bytes)
                observation, consumed = _observe_appserver_job(metadata, job_id, directory, runtime, previous, now, available)
                total_bytes += consumed
                if lane == "priority":
                    priority_bytes += consumed
                else:
                    last_history_consumed_id = job_id
                if observation.get("process_status") in {"starting", "running", "stopping"}:
                    active += 1
                if observation.get("result_state") == "pending":
                    summary["pending"] += 1
                elif observation.get("result_state") == "conflict":
                    summary["conflicts"] += 1
                if observation.get("error_code"):
                    summary["errors"].append({"job_id": job_id, "code": observation["error_code"]})
                observations.append(observation)
                continue
            if metadata.get("status") in {"starting", "running", "stopping"}:
                active += 1
            if not _looks_like_codex_exec(metadata.get("command")):
                continue
            identity, _argv = _validated_gateway_metadata(metadata, job_id, directory, runtime)
            base.update(launch_identity=identity, process_status="unknown", exit_code=None,
                        turn_status="unknown", execution_status="unknown", result_state="pending", error_code=None)
            base["process_status"], base["exit_code"], _ = _process_facts(metadata, "unknown")
            if previous and previous.get("launch_identity") and previous["launch_identity"] != identity:
                base.update(result_state="conflict", error_code="launch_identity_conflict", reuse=True)
                summary["conflicts"] += 1
                summary["errors"].append({"job_id": job_id, "code": "launch_identity_conflict"})
                observations.append(base)
                continue
            stdout_path = directory / "stdout.log"
            try:
                stdout_stat = _safe_stat(stdout_path)
            except SupervisorError as exc:
                base["error_code"] = "stdout_missing" if exc.code == "file_missing" else exc.code
                base["stdout_size"] = None
                base["stdout_mtime_ns"] = None
                summary["pending"] += 1
                summary["errors"].append({"job_id": job_id, "code": base["error_code"]})
                observations.append(base)
                continue
            file_identity = f"{stdout_stat.st_dev}:{stdout_stat.st_ino}"
            base.update(stdout_size=stdout_stat.st_size, stdout_mtime_ns=stdout_stat.st_mtime_ns,
                        stdout_file_identity=file_identity)
            same_log = bool(previous and previous.get("stdout_size") == stdout_stat.st_size
                            and previous.get("stdout_mtime_ns") == stdout_stat.st_mtime_ns
                            and previous.get("stdout_file_identity") == file_identity)
            same_launch = bool(previous and previous.get("launch_identity") == identity)
            status_changed = bool(previous and previous.get("process_status") != base["process_status"])
            if same_log and same_launch and previous.get("result_state") in {"succeeded", "failed", "conflict"}:
                base.update({k: previous.get(k) for k in ("thread_id", "final_item_id", "result_digest", "log_digest", "log_bytes",
                              "turn_status", "execution_status", "result_state", "error_code", "handoff_id")})
                base["process_status"], base["exit_code"], base["execution_status"] = _process_facts(
                    metadata, "failed" if previous.get("turn_status") == "turn_failed" else
                    ("succeeded" if previous.get("turn_status") == "turn_completed" else "result_pending"))
                if base["execution_status"] == "failed":
                    base["error_code"] = "nonzero_exit" if base["exit_code"] not in (None, 0) else "process_failed"
                base["reuse"] = True
                observations.append(base)
                continue
            stdout_changed = not same_log
            changed_stdout_is_retryable = bool(
                previous and previous.get("log_digest") and previous.get("log_bytes") is not None
                and previous.get("error_code") in GATEWAY_STDOUT_CHANGE_RETRY_CODES
            )
            if (same_launch and previous and previous.get("result_state") == "pending" and previous.get("next_check_at")
                    and previous["next_check_at"] > now and not status_changed
                    and not (stdout_changed and changed_stdout_is_retryable)):
                base.update({k: previous.get(k) for k in ("thread_id", "final_item_id", "result_digest", "log_digest", "log_bytes",
                              "turn_status", "execution_status", "result_state", "error_code", "handoff_id")})
                base["reuse"] = True
                summary["pending"] += 1
                summary["errors"].append({"job_id": job_id, "code": previous.get("error_code") or "result_pending"})
                observations.append(base)
                continue
            if stdout_stat.st_size > GATEWAY_MAX_FILE_BYTES:
                base.update(error_code="result_too_large", result_state="pending", turn_status="unknown", execution_status="unknown",
                            log_digest=None, log_bytes=stdout_stat.st_size)
                summary["pending"] += 1
                summary["errors"].append({"job_id": job_id, "code": "result_too_large"})
                if previous and previous.get("result_state") in {"succeeded", "failed"}:
                    base.update(result_state="conflict", error_code="result_identity_conflict", reuse=True)
                    summary["conflicts"] += 1
                observations.append(base)
                continue
            available_bytes = (min(GATEWAY_MAX_BYTES_PER_POLL - total_bytes,
                                   priority_byte_budget - priority_bytes)
                               if lane == "priority" else GATEWAY_MAX_BYTES_PER_POLL - total_bytes)
            if stdout_stat.st_size > available_bytes:
                base.update(error_code="poll_byte_budget", result_state="pending", turn_status="unknown", execution_status="unknown",
                            reuse=True)
                if previous and previous.get("result_state") in {"succeeded", "failed"}:
                    base.update(result_state="conflict", error_code="result_identity_conflict")
                    summary["conflicts"] += 1
                if lane == "history" and history_byte_defer_cursor is None:
                    history_byte_defer_cursor = last_history_consumed_id or job_id
                summary["pending"] += 1
                summary["errors"].append({"job_id": job_id, "code": "poll_byte_budget"})
                if previous and previous.get("result_state") in {"succeeded", "failed"}:
                    base.update(result_state="conflict", error_code="result_identity_conflict")
                    summary["conflicts"] += 1
                observations.append(base)
                continue
            available_bytes = (min(GATEWAY_MAX_BYTES_PER_POLL - total_bytes,
                                   priority_byte_budget - priority_bytes)
                               if lane == "priority" else GATEWAY_MAX_BYTES_PER_POLL - total_bytes)
            read_limit = min(GATEWAY_MAX_FILE_BYTES, max(0, available_bytes))
            try:
                data, read_stat = _read_stable_file(stdout_path, read_limit)
            except SupervisorError as exc:
                total_bytes += exc.bytes_read
                if lane == "priority":
                    priority_bytes += exc.bytes_read
                elif exc.bytes_read:
                    last_history_consumed_id = job_id
                budget_overrun = ((exc.code == "file_too_large" and read_limit < GATEWAY_MAX_FILE_BYTES)
                                  or (exc.code == "file_changed" and read_limit < GATEWAY_MAX_FILE_BYTES
                                      and exc.bytes_read >= read_limit))
                code = "poll_byte_budget" if budget_overrun else exc.code
                base.update(error_code=code, result_state="pending", turn_status="unknown", execution_status="unknown",
                            reuse=budget_overrun)
                if budget_overrun and lane == "history" and history_byte_defer_cursor is None:
                    history_byte_defer_cursor = last_history_consumed_id or job_id
                if previous and previous.get("result_state") in {"succeeded", "failed"} and not same_log:
                    base.update(result_state="conflict", error_code="result_identity_conflict", reuse=True)
                    summary["conflicts"] += 1
                    code = "result_identity_conflict"
                summary["pending"] += 1
                summary["errors"].append({"job_id": job_id, "code": code})
                observations.append(base)
                continue
            total_bytes += len(data)
            if lane == "priority":
                priority_bytes += len(data)
            else:
                last_history_consumed_id = job_id
            raw_digest = hashlib.sha256(data).hexdigest()
            if previous and previous.get("result_state") in {"succeeded", "failed"} and previous.get("log_digest") != raw_digest:
                base.update(result_state="conflict", error_code="result_identity_conflict", log_digest=raw_digest,
                            log_bytes=len(data), reuse=True)
                summary["conflicts"] += 1
                summary["errors"].append({"job_id": job_id, "code": "result_identity_conflict"})
                observations.append(base)
                continue
            if not data.endswith(b"\n"):
                parsed = {"state": "result_pending", "code": "partial_jsonl"}
            else:
                parsed = work_dispatcher_codex.parse_codex_jsonl(data)
            if parsed.get("state") == "succeeded" and not (isinstance(parsed.get("final_item_id"), str) and parsed["final_item_id"]):
                parsed = {"state": "result_pending", "code": "final_item_identity_missing"}
            state = parsed.get("state")
            turn = "turn_completed" if state == "succeeded" else "turn_failed" if state == "failed" else "unknown"
            process_status, exit_code, execution_status = _process_facts(metadata, state)
            base.update(thread_id=parsed.get("thread_id"), final_item_id=parsed.get("final_item_id"),
                        result_digest=parsed.get("result_digest"), log_digest=raw_digest, log_bytes=len(data),
                        turn_status=turn, process_status=process_status, exit_code=exit_code,
                        execution_status=execution_status,
                        result_state="succeeded" if state == "succeeded" else "failed" if state == "failed" else "pending",
                        result_text=parsed.get("result_text"), error_code=None if state == "succeeded" else parsed.get("code") or "result_pending",
                        stdout_size=read_stat.st_size, stdout_mtime_ns=read_stat.st_mtime_ns,
                        stdout_file_identity=f"{read_stat.st_dev}:{read_stat.st_ino}")
            if state == "succeeded" and execution_status == "failed":
                base["error_code"] = "nonzero_exit" if exit_code not in (None, 0) else "process_failed"
            if state != "succeeded":
                if state == "result_pending":
                    summary["pending"] += 1
                    summary["errors"].append({"job_id": job_id, "code": base["error_code"]})
            observations.append(base)
        except SupervisorError as exc:
            code = exc.code
            base.update(result_state="pending", turn_status="unknown", execution_status="unknown", error_code=code)
            if previous and previous.get("result_state") in {"succeeded", "failed"}:
                base.update(result_state="conflict", error_code="result_identity_conflict", reuse=True)
                summary["conflicts"] += 1
                code = "result_identity_conflict"
            summary["pending"] += 1
            summary["errors"].append({"job_id": job_id, "code": code})
            observations.append(base)
        except (OSError, ValueError, TypeError, RecursionError):
            base.update(result_state="pending", turn_status="unknown", execution_status="unknown", error_code="observation_invalid")
            summary["pending"] += 1
            summary["errors"].append({"job_id": job_id, "code": "observation_invalid"})
            observations.append(base)
    next_cursor = (history_byte_defer_cursor if history_byte_defer_cursor is not None else
                   history_ids[-1] if history_ids else cursor)
    return {"observations": observations, "cursor": next_cursor, "summary": summary,
            "active": active, "lower_bound": lower_bound}


def _terminal_revisits(out: Fetched, plan: dict[str, Any], now: float) -> tuple[set[str], list[str]]:
    """Known terminal+drained sessions can continue and finish between two polls, so they are never observed active.
    A listed change hint (updated_at-like, newer than the last progress we recorded) triggers collection; every other
    drained session is rechecked in bounded, least-recently-collected-first rotation."""
    changed: set[str] = set()
    quiet: list[tuple[float, str]] = []
    for status in CLI_TERMINAL:
        for item in out.cli_lists.get(status, []):
            sid = _session_id(item)
            row = plan["sessions"].get(sid)
            if row is None or row["status"] not in CLI_TERMINAL or not row["drained"] or row["cursor_stalled"]:
                continue                                     # unknown / undrained / failing sessions are handled by _collect
            hint = next((t for t in (parse_time(item.get(k)) for k in ("updated_at", "last_activity_at", "last_event_at")) if t), None)
            if hint is not None and hint > (row["last_progress_at"] or 0.0):
                changed.add(sid)
            elif now - (row.get("collected_at") or 0.0) >= TERMINAL_RECHECK_SECONDS:
                quiet.append((row.get("collected_at") or 0.0, sid))
    return changed, [sid for _, sid in sorted(quiet)[:TERMINAL_RECHECKS_PER_POLL] if sid not in changed]


async def fetch_all(plan: dict[str, Any], cli_client: Any, task_client: Any, now: float) -> Fetched:
    out = Fetched(now)
    out.gateway_cursor = plan.get("gateway_cursor", "")
    out.gateway_previous = plan.get("gateway_jobs", {})
    await _fetch_cli_lists(cli_client, out)
    await _fetch_cli_status(cli_client, out, plan)
    # A continued durable session usually reappears in an active list; short follow-ups that finish between polls are
    # caught by _terminal_revisits instead.
    discovered = {_session_id(i) for status, items in out.cli_lists.items() for i in items
                  if status not in CLI_TERMINAL or _session_id(i) not in plan["sessions"]}
    revisit, rechecks = _terminal_revisits(out, plan, now)
    await _collect(cli_client, out, plan, discovered, revisit, rechecks)
    await _fetch_runs(cli_client, out, plan)
    await _fetch_tasks(task_client, out, plan)
    await _fetch_jobs(out)
    out.source("aoitalk", ok=True)
    return out


async def default_fetch(plan: dict[str, Any]) -> Fetched:
    observed_at = time.time()
    def local_only() -> Fetched:
        out = Fetched(observed_at)
        out.gateway_cursor = plan.get("gateway_cursor", "")
        out.gateway_previous = plan.get("gateway_jobs", {})
        return out

    try:
        settings = aoitalk.load_settings()
    except aoitalk.ClipError:
        out = local_only()
        await _fetch_jobs(out)
        out.source("aoitalk", ok=False, error="configuration_error")
        return out
    cli_client, task_client = cli.CliSessionClient(settings), tasks.TaskClient(settings)
    try:
        try:
            await cli_client.login_cli_session()
            await task_client.login_task()
        except Exception as exc:  # noqa: BLE001 - Gateway local observation remains independent
            out = local_only()
            await _fetch_jobs(out)
            out.source("aoitalk", ok=False, error=safe_code(exc))
            return out
        out = await fetch_all(plan, cli_client, task_client, observed_at)
        return out
    finally:
        await cli_client.http.aclose()
        await task_client.http.aclose()


# ------------------------------------------------------------------------ commit

def _has_result(task: dict[str, Any]) -> bool:
    if any(task.get(k) for k in ("result", "summary_text", "completion_summary")):
        return True
    # A failed/cancelled/interrupted task's persisted outcome is its failure record;
    # "completed" is never inferred without a result or summary.
    return task.get("status") in {"failed", "cancelled", "interrupted"} and bool(
        task.get("failure_reason") or task.get("failure_detail"))


def _result_json(task: dict[str, Any]) -> str | None:
    if not _has_result(task):
        return None
    keep = {k: task.get(k) for k in ("result", "summary_text", "completion_summary", "failure_reason", "failure_detail", "changed_files") if task.get(k)}
    text = json.dumps(keep, ensure_ascii=False)
    if len(text) > 20_000:
        text = json.dumps({"truncated": True, "summary_text": str(task.get("summary_text") or "")[:8000]}, ensure_ascii=False)
    return text


def event_identity(event: dict[str, Any]) -> str | None:
    raw = event.get("event_id")
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str) and raw:
        return raw[:512]
    if isinstance(raw, int):
        return str(raw)
    return None


def classify_event(event: dict[str, Any]) -> tuple[str, str]:
    """Return (event_id, kind) where kind is terminal | result | output | info.

    Only the backend's ``cli.output`` event carries assistant text (as message-bound chunks).  Every other
    ``cli.*`` event (assistant_text, tool_start/tool_end, invocation, status_update, ...) is diagnostic
    telemetry: it is stored as ``info`` and is never mistaken for output or for output corruption."""
    event_id = event_identity(event)
    if event_id is None:    # only stored legacy rows reach here; fresh pages with such events are rejected as malformed
        event_id = "h:" + hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    label = str(event.get("event_type") or event.get("type") or "")
    if label == "cli.output":
        return event_id, "output"       # may carry a run status but is not a terminal marker by itself
    if event.get("terminal") is True or TERMINAL_EVENT_RE.fullmatch(label):
        return event_id, "terminal"
    if RESULT_EVENT_RE.fullmatch(label):
        return event_id, "result"
    return event_id, "info"


def is_terminal_evidence(kind: str, status: Any) -> bool:
    """A run's own terminal evidence: a terminal event, or the backend's cli.output (written with the terminal status)."""
    return kind == "terminal" or (kind == "output" and status in CLI_TERMINAL)


def event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    for value in (event.get("message"), event.get("text"), payload.get("text"), payload.get("message")):
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def validate_chunks(rows: list[sqlite3.Row]) -> tuple[bool, str | None, str]:
    """Hydrate one assistant message from its ``cli.output`` chunks.

    Backend contract (``_bounded_text_chunk``): chunk i covers ``[start, end)`` of the message in Python code
    points, ``text`` is that slice, ``text_end`` is ``start + len(text)`` OF THAT CHUNK (not of the message) and
    ``text_truncated`` is false when the slice was not clipped.  The message is complete only when every
    index 0..count-1 is present, the ranges tile it from 0 without gap/overlap/reversal and every chunk is
    unclipped; any other combination is UNKNOWN."""
    chunks: dict[int, tuple[int, int, str]] = {}
    count = None
    for row in rows:
        try:
            body = json.loads(row["body_json"])
            payload = body.get("payload")
        except (ValueError, AttributeError):
            return False, "chunk_unreadable", ""
        if not isinstance(payload, dict):
            return False, "chunk_payload_missing", ""
        index, this_count = _int(payload.get("chunk_index")), _int(payload.get("chunk_count"))
        start, end, text_end = _int(payload.get("start")), _int(payload.get("end")), _int(payload.get("text_end"))
        text = payload.get("text")
        if None in (index, this_count, start, end, text_end) or not isinstance(text, str):
            return False, "chunk_fields_missing", ""
        if payload.get("text_truncated") is not False:
            return False, "chunk_text_truncated", ""
        if not 1 <= this_count <= MAX_CHUNKS or count not in (None, this_count):
            return False, "chunk_count_invalid", ""
        if not 0 <= index < this_count:
            return False, "chunk_index_invalid", ""
        count = this_count
        if not 0 <= start <= end or len(text) != end - start:
            # cli._sanitize replaces credential-shaped text with "[redacted]", so the text no longer matches its range
            return False, ("chunk_text_redacted" if "[redacted]" in text and 0 <= start <= end else "chunk_range_invalid"), ""
        if text_end != end:
            return False, "text_end_mismatch", ""
        record = (start, end, text)
        if index in chunks and chunks[index] != record:
            return False, "chunk_conflict", ""      # same index, different range/text: never overwrite
        chunks[index] = record
    if count is None or set(chunks) != set(range(count)):
        return False, "chunks_missing", ""
    position = 0
    for index in range(count):
        start, end, _ = chunks[index]
        if start != position:
            return False, "chunk_gap", ""           # gap, overlap or reversed order
        position = end
    return True, None, "".join(chunks[i][2] for i in range(count))


def analyze_session(conn: sqlite3.Connection, sid: str) -> dict[str, dict[str, Any]]:
    """Per-run view of one durable session.

    state: terminal | running | queued.  A run is terminal by its own authority: the run GET
    (succeeded/failed/cancelled) or its own terminal event; never by the session's latest status.
    messages: assistant message id -> {ok, reason, text, source, handed_off, event_ids}."""
    run_rows = {r["run_id"]: dict(r) for r in conn.execute("SELECT * FROM runs WHERE session_id=?", (sid,))}
    recovered = {(r["run_id"], r["message_id"]): r["text"] for r in conn.execute(
        "SELECT run_id, message_id, text FROM recovered_messages WHERE session_id=?", (sid,))}
    handed = {(r["run_id"], r["message_id"]) for r in conn.execute(
        "SELECT run_id, message_id FROM handoffs WHERE source_id=? AND run_id IS NOT NULL", (sid,))}
    runs: dict[str, dict[str, Any]] = {}
    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}

    def entry(run_id: str) -> dict[str, Any]:
        return runs.setdefault(run_id, {"event_terminal": False, "event_status": None, "event_terminal_at": 0.0, "active": False,
                                        "pointers": set(), "messages": {}, "conflict": False})

    for row in conn.execute("SELECT id, run_id, event_type, status, kind, message_id, received_at, body_json, handoff_id"
                            " FROM inbox WHERE session_id=? AND run_id IS NOT NULL ORDER BY id", (sid,)):
        run = entry(row["run_id"])
        if is_terminal_evidence(row["kind"], row["status"]):
            run["event_terminal"] = True
            run["event_status"] = run["event_status"] or (row["status"] if row["status"] in CLI_TERMINAL else None) \
                or (row["event_type"] or "").rpartition(".")[2] or "terminal"
            run["event_terminal_at"] = max(run["event_terminal_at"], row["received_at"])
        if row["kind"] == "terminal" and row["message_id"]:
            run["pointers"].add(row["message_id"])
        if row["kind"] == "output":
            groups.setdefault((row["run_id"], row["message_id"] or ""), []).append(row)
        elif row["kind"] != "terminal" and not (row["event_type"] or "").startswith("run.queued") and row["status"] != "queued":
            run["active"] = True
    for run_id in run_rows:
        entry(run_id)
    for run_id, run in runs.items():
        row = run_rows.get(run_id)
        remote = row["status"] if row else None
        if remote in RUN_TERMINAL:
            run.update(state="terminal", terminal_status=remote, authority="run_get")
        elif run["event_terminal"] and not (remote in RUN_ACTIVE and row["observed_at"] is not None
                                            and row["observed_at"] >= run["event_terminal_at"]):
            run.update(state="terminal", terminal_status=run["event_status"] or "completed", authority="events")
        else:
            run["conflict"] = run["event_terminal"]     # a later read says the run is still active: hold
            if remote in ("running", "awaiting_approval", "uncertain") or (remote != "queued" and run["active"]):
                run["state"] = "running"
            else:
                run["state"] = "queued"
            run["terminal_status"], run["authority"] = None, None
        expected = row["message_id"] if row else None
        ids = {m for (rid, m) in groups if rid == run_id} | run["pointers"] | ({expected} if expected else set())
        for message_id in ids:
            rows = groups.get((run_id, message_id), [])
            ok, reason, text, source = False, None, "", "cli_output_chunks"
            if not message_id:
                reason = "no_message_provenance"
            elif expected and expected != message_id.lower():
                reason = "message_id_mismatch"
            elif rows and all(r["handoff_id"] for r in rows):
                ok = True                                # already handed off; the text lives in the handoff
            elif rows:
                ok, reason, text = validate_chunks(rows)
            else:
                reason = "output_pointer_unrecovered"    # a pointer without any chunk is not a result
            if not ok and message_id and (run_id, message_id.lower()) in recovered and reason != "message_id_mismatch":
                ok, reason, text, source = True, None, recovered[(run_id, message_id.lower())], "message_get"
            run["messages"][message_id] = {
                "ok": ok, "reason": reason, "text": text, "message_id": message_id, "source": source,
                "event_ids": [r["id"] for r in rows],
                "handed_off": (run_id, message_id) in handed or (bool(rows) and all(r["handoff_id"] for r in rows))}
    return runs


def commit(conn: sqlite3.Connection, fetched: Fetched, plan: dict[str, Any]) -> dict[str, Any]:
    """Persist events + cursors + run states + handoffs/outbox + snapshot atomically."""
    now = fetched.observed_at
    summary = {"new_events": 0}
    conn.execute("BEGIN IMMEDIATE")
    try:
        _commit_cli(conn, fetched, now, summary)
        _commit_runs(conn, fetched, now)
        _commit_tasks(conn, fetched, now)
        _create_handoffs(conn, fetched, now)
        _commit_gateway(conn, fetched, now, summary)
        fetched.gateway_summary["handoffs"] = summary.get("gateway_handoffs", 0)
        _correct_handoff_status(conn)
        _settle_runs(conn)
        for name, src in fetched.sources.items():
            if ":" in name:
                continue                    # per-state sources live in the snapshot only
            conn.execute("INSERT INTO source_heartbeats(source_id,source_type,observed_at,via,error_code)"
                         " VALUES(?,?,?,?,?)", (name, "observer_source", now, "monitor", src["error"]))
        snapshot = _snapshot(fetched)
        conn.execute("INSERT INTO snapshots(observed_at,data_json) VALUES(?,?)", (now, json.dumps(snapshot)))
        conn.execute("INSERT OR REPLACE INTO meta VALUES('last_commit_at', ?)", (str(now),))
        if fetched.gateway_cursor_next is not None:
            conn.execute("INSERT OR REPLACE INTO meta VALUES('gateway_cursor', ?)", (fetched.gateway_cursor_next,))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return summary


def _commit_cli(conn: sqlite3.Connection, f: Fetched, now: float, summary: dict[str, Any]) -> None:
    seen: dict[str, tuple[str, dict[str, Any]]] = {}
    for status, items in f.cli_lists.items():
        for item in items:
            seen[_session_id(item)] = (status, item)
    for sid, (status, item) in seen.items():
        provider = item.get("provider") if item.get("provider") in cli.PROVIDERS else "unknown"
        hint = next((t for t in (parse_time(item.get(k)) for k in ("updated_at", "last_activity_at", "last_event_at")) if t), None)
        conn.execute(
            "INSERT INTO sessions(session_id,provider,status,first_seen,last_seen,last_progress_at) VALUES(?,?,?,?,?,?)"
            " ON CONFLICT(session_id) DO UPDATE SET provider=excluded.provider, status=excluded.status,"
            " last_seen=excluded.last_seen, last_progress_at=MAX(last_progress_at, ?),"
            " drained=CASE WHEN ? IN ('completed','failed','interrupted','cancelled') THEN drained ELSE 0 END",
            (sid, provider, status, now, now, hint or now, hint or 0, status))
    for sid, status in f.cli_status.items():
        # The per-session status read is the only session-level terminal confirmation, valid for this poll only.
        conn.execute("UPDATE sessions SET status=?, terminal_confirmed_at=? WHERE session_id=?",
                     (status, now if status in CLI_TERMINAL else None, sid))
    for sid, page in f.pages.items():
        row = conn.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO sessions(session_id,provider,status,first_seen,last_seen,last_progress_at)"
                         " VALUES(?,?,?,?,?,?)", (sid, "unknown", "ready", now, now, now))
        inserted = 0
        for event in page.get("events", []):
            if not isinstance(event, dict):
                continue
            event_id, kind = classify_event(event)
            text = event_text(event)
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            run_id = _run_id(event.get("run_id"))
            inserted += conn.execute(
                "INSERT OR IGNORE INTO inbox(session_id,event_id,event_type,status,kind,summary,body_json,received_at,run_id,message_id)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (sid, event_id, str(event.get("event_type") or event.get("type") or "")[:128],
                 str(event.get("status") or "")[:64], kind, str(text)[:2000],
                 json.dumps(event, ensure_ascii=False), now, run_id, _str_or_none(payload.get("assistant_message_id")))).rowcount
            if run_id:
                conn.execute("INSERT OR IGNORE INTO runs(run_id,session_id,status,source,first_seen) VALUES(?,?,?,?,?)",
                             (run_id, sid, "unknown", "event", now))
                if is_terminal_evidence(kind, event.get("status")):
                    conn.execute("UPDATE runs SET terminal_at=COALESCE(terminal_at, ?) WHERE run_id=? AND session_id=?", (now, run_id, sid))
        summary["new_events"] += inserted
        conn.execute("UPDATE sessions SET collect_error=NULL WHERE session_id=?", (sid,))
        cursor = page.get("next_cursor")
        if isinstance(cursor, str) and cli.CURSOR_RE.fullmatch(cursor) and not page.get("cursor_advance_blocked"):
            drained = int(page.get("has_more") is False or (page.get("has_more") is None and not page.get("events")))
            conn.execute("UPDATE sessions SET cursor=?, cursor_stalled=0, drained=? WHERE session_id=?", (cursor, drained, sid))
        elif not page.get("events") and page.get("has_more") is not True:
            conn.execute("UPDATE sessions SET drained=1 WHERE session_id=? AND cursor_stalled=0", (sid,))
        if inserted:
            conn.execute("UPDATE sessions SET last_progress_at=? WHERE session_id=?", (now, sid))
    conn.executemany("UPDATE sessions SET collected_at=? WHERE session_id=?", [(now, sid) for sid in f.collected])
    for sid, reason in f.stalled_pages.items():
        conn.execute("UPDATE sessions SET cursor_stalled=1, drained=0, collect_error=? WHERE session_id=?", (reason, sid))
    for sid, code in f.item_errors.items():     # no cursor movement, never drained
        conn.execute("UPDATE sessions SET collect_error=?, drained=0 WHERE session_id=?", (code, sid))


_UPSERT_RUN = (
    "INSERT INTO runs(run_id,session_id,status,observed_at,source,first_seen,remote_created_at,remote_updated_at,message_id,"
    "output_persisted,terminal_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)"
    " ON CONFLICT(run_id) DO UPDATE SET status=excluded.status, observed_at=excluded.observed_at, source=excluded.source,"
    " remote_created_at=COALESCE(excluded.remote_created_at, remote_created_at),"
    " remote_updated_at=COALESCE(excluded.remote_updated_at, remote_updated_at),"
    " message_id=COALESCE(excluded.message_id, message_id), output_persisted=MAX(output_persisted, excluded.output_persisted),"
    " terminal_at=COALESCE(terminal_at, excluded.terminal_at), check_error=NULL"
    " WHERE session_id=excluded.session_id"
    " AND (status NOT IN ('succeeded','failed','cancelled') OR excluded.status IN ('succeeded','failed','cancelled'))")


def _upsert_run(conn: sqlite3.Connection, obs: dict[str, Any], now: float, source: str) -> None:
    conn.execute(_UPSERT_RUN, (obs["run_id"], obs["session_id"], obs["status"], now, source, now, obs["created_at"], obs["updated_at"],
                               obs["message_id"], int(obs["output_persisted"]), now if obs["status"] in RUN_TERMINAL else None))


def _commit_runs(conn: sqlite3.Connection, f: Fetched, now: float) -> None:
    """Persist the reconciled run states.  Lists first, then the later per-run reads (which win)."""
    for (sid, status), listing in f.run_lists.items():
        conn.execute("INSERT OR REPLACE INTO run_lists VALUES(?,?,?,?,?,?)",
                     (sid, status, now, int(listing["complete"]), len(listing["runs"]), listing["error"]))
        for obs in listing["runs"]:
            _upsert_run(conn, obs, now, "run_list")
        if listing["complete"]:
            listed = {obs["run_id"] for obs in listing["runs"]}
            for row in conn.execute("SELECT run_id FROM runs WHERE session_id=? AND status=?", (sid, status)).fetchall():
                if row["run_id"] not in listed:     # left the list: its state is unknown until the run GET says otherwise
                    conn.execute("UPDATE runs SET status='unknown', next_check_at=NULL WHERE run_id=?", (row["run_id"],))
    for run_id, check in f.run_checks.items():
        if check["ok"]:
            _upsert_run(conn, check["obs"], now, "run_get")
            conn.execute("UPDATE runs SET checked_at=?, next_check_at=NULL WHERE run_id=?", (now, run_id))
        else:
            wait = BACKOFF_DENIED if check["error"] in ("permission_denied", "not_found", "binding_mismatch") else BACKOFF_RETRY
            # A rejected / failed read proves nothing: a previously observed state is demoted to unknown, never kept fresh.
            conn.execute("UPDATE runs SET checked_at=?, check_error=?, next_check_at=?,"
                         " status=CASE WHEN status IN ('succeeded','failed','cancelled') THEN status ELSE 'unknown' END WHERE run_id=?",
                         (now, check["error"], now + wait, run_id))
    for run_id, result in f.messages.items():
        row = conn.execute("SELECT session_id, message_id, recover_attempts FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None or result.get("message_id") != row["message_id"]:
            continue
        if result["ok"]:
            conn.execute("INSERT OR IGNORE INTO recovered_messages VALUES(?,?,?,?,?,?)",
                         (run_id, row["message_id"], row["session_id"], _digest(result["text"]), result["text"], now))
            conn.execute("UPDATE runs SET recover_at=?, recover_reason=NULL WHERE run_id=?", (now, run_id))
        else:
            wait = min(3600.0, BACKOFF_RETRY * 2 ** row["recover_attempts"])
            conn.execute("UPDATE runs SET recover_attempts=recover_attempts+1, recover_at=?, recover_reason=?, next_check_at=? WHERE run_id=?",
                         (now, result["reason"], now + wait, run_id))


def _correct_handoff_status(conn: sqlite3.Connection) -> None:
    """A handoff's terminal_status is its own run's authoritative status, never the session's (older ledgers
    stored the session-level status).  Only a reconciled run GET/list may correct it."""
    conn.execute(
        "UPDATE handoffs SET terminal_status=(SELECT r.status FROM runs r WHERE r.run_id=handoffs.run_id)"
        " WHERE run_id IS NOT NULL AND EXISTS(SELECT 1 FROM runs r WHERE r.run_id=handoffs.run_id AND r.source<>'event'"
        " AND r.status IN ('succeeded','failed','cancelled')"
        " AND CASE r.status WHEN 'succeeded' THEN 'completed' ELSE r.status END<>handoffs.terminal_status)")


def _settle_runs(conn: sqlite3.Connection) -> None:
    """Runs whose state and result need no further reads are settled (not polled again)."""
    conn.execute("UPDATE runs SET settled=1 WHERE settled=0 AND status IN ('failed','cancelled')")
    conn.execute("UPDATE runs SET settled=1 WHERE settled=0 AND status='succeeded' AND (output_persisted=0 OR message_id IS NULL)")
    conn.execute("UPDATE runs SET settled=1 WHERE settled=0 AND status='succeeded' AND EXISTS("
                 "SELECT 1 FROM handoffs h WHERE h.run_id=runs.run_id AND h.source_id=runs.session_id)")


def _commit_tasks(conn: sqlite3.Connection, f: Fetched, now: float) -> None:
    for status, items in f.task_lists.items():
        for item in items:
            detail = f.task_details.get(item["task_id"], item)
            terminal = status in tasks.TERMINAL
            hint = parse_time(item.get("last_progress_at"))
            conn.execute(
                "INSERT INTO tasks(task_id,status,executor,goal,first_seen,last_seen,last_progress_at,stalled,terminal_confirmed_at,result_json)"
                " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,"
                " last_seen=excluded.last_seen, last_progress_at=MAX(last_progress_at, excluded.last_progress_at),"
                " stalled=excluded.stalled, terminal_confirmed_at=COALESCE(terminal_confirmed_at, excluded.terminal_confirmed_at),"
                " result_json=COALESCE(result_json, excluded.result_json)",
                (item["task_id"], status, str(item.get("executor") or "")[:64], str(item.get("goal") or "")[:200],
                 now, now, hint or now, int(bool(item.get("stalled")) or item.get("worker_alive") is False and status == "running"),
                 now if terminal else None, _result_json(detail) if terminal else None))


def _insert_handoff(conn: sqlite3.Connection, *, handoff_id: str, source_type: str, source_id: str, provider: str | None,
                    status: str, now: float, run_id: str | None = None, message_id: str | None = None,
                    text: str = "", source: str | None = None) -> bool:
    """Ready-ing a result and its outbox row happen in the caller's transaction; replays add nothing."""
    digest = _digest(text)
    created = conn.execute(
        "INSERT OR IGNORE INTO handoffs(handoff_id,source_type,source_id,provider,terminal_status,created_at,state,run_id,message_id,"
        "result_text,result_source,result_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (handoff_id, source_type, source_id, provider, status, now, "ready", run_id, message_id, text, source, digest)).rowcount
    if created:
        conn.execute("INSERT OR IGNORE INTO outbox(handoff_id,source_type,source_id,run_id,message_id,result_digest,result_chars,created_at)"
                     " VALUES(?,?,?,?,?,?,?,?)", (handoff_id, source_type, source_id, run_id, message_id, digest, len(text), now))
    return bool(created)


def _commit_gateway(conn: sqlite3.Connection, f: Fetched, now: float, summary: dict[str, Any]) -> None:
    """Atomically pin Gateway observation identity and create its ordinary outbox handoff."""
    for obs in f.gateway_observations:
        job_id = obs["job_id"]
        existing = conn.execute("SELECT * FROM gateway_jobs WHERE job_id=?", (job_id,)).fetchone()
        identity = obs.get("launch_identity")
        code = obs.get("error_code")
        incoming_state = obs.get("result_state", "pending")
        app_evidence = _safe_appserver_evidence(obs.get("app_server_evidence"))
        if existing is not None and existing["result_state"] == "conflict":
            code = existing["error_code"] or "result_identity_conflict"
            conn.execute("UPDATE gateway_jobs SET process_status=?, exit_code=?, observed_at=?, error_code=?,"
                         " stdout_size=COALESCE(?,stdout_size),stdout_mtime_ns=COALESCE(?,stdout_mtime_ns),"
                         " stdout_file_identity=COALESCE(?,stdout_file_identity) WHERE job_id=?",
                         (obs.get("process_status", existing["process_status"]), obs.get("exit_code", existing["exit_code"]), now, code,
                          obs.get("stdout_size"), obs.get("stdout_mtime_ns"), obs.get("stdout_file_identity"), job_id))
            continue
        if existing is not None and existing["launch_identity"] and identity and existing["launch_identity"] != identity:
            code = "launch_identity_conflict"
            conn.execute("UPDATE gateway_jobs SET result_state='conflict', process_status=?, exit_code=?, observed_at=?, error_code=?, next_check_at=NULL,"
                         " stdout_size=COALESCE(?,stdout_size),stdout_mtime_ns=COALESCE(?,stdout_mtime_ns)"
                         " WHERE job_id=?", (obs.get("process_status", existing["process_status"]), obs.get("exit_code", existing["exit_code"]), now,
                                             code, obs.get("stdout_size"), obs.get("stdout_mtime_ns"), job_id))
            summary["gateway_conflicts"] = summary.get("gateway_conflicts", 0) + 1
            continue
        if (existing is not None and existing["result_state"] in {"succeeded", "failed"}
                and incoming_state in {"succeeded", "failed"}
                and ((app_evidence is None and existing["log_digest"] and obs.get("log_digest") != existing["log_digest"])
                     or (existing["result_digest"] and obs.get("result_digest") != existing["result_digest"])
                     or (existing["thread_id"] and obs.get("thread_id") != existing["thread_id"])
                     or (existing["final_item_id"] and obs.get("final_item_id") != existing["final_item_id"]))):
            code = "result_identity_conflict"
            conn.execute("UPDATE gateway_jobs SET result_state='conflict', process_status=?, exit_code=?, observed_at=?, error_code=?, next_check_at=NULL,"
                         " stdout_size=COALESCE(?,stdout_size),stdout_mtime_ns=COALESCE(?,stdout_mtime_ns),"
                         " stdout_file_identity=COALESCE(?,stdout_file_identity)"
                         " WHERE job_id=?", (obs.get("process_status", existing["process_status"]), obs.get("exit_code", existing["exit_code"]), now,
                                             code, obs.get("stdout_size"), obs.get("stdout_mtime_ns"), obs.get("stdout_file_identity"), job_id))
            summary["gateway_conflicts"] = summary.get("gateway_conflicts", 0) + 1
            continue
        if existing is not None and existing["result_state"] in {"succeeded", "failed"} and incoming_state not in {"conflict"}:
            # Terminal result identity is immutable. New process metadata may update independently.
            conn.execute("UPDATE gateway_jobs SET process_status=?, exit_code=?, execution_status=?, observed_at=?, error_code=?,"
                         " stdout_size=COALESCE(?,stdout_size),stdout_mtime_ns=COALESCE(?,stdout_mtime_ns),"
                         " stdout_file_identity=COALESCE(?,stdout_file_identity) WHERE job_id=?",
                         (obs.get("process_status", existing["process_status"]), obs.get("exit_code", existing["exit_code"]),
                          obs.get("execution_status", existing["execution_status"]), now,
                          obs.get("error_code") if obs.get("error_code") else existing["error_code"],
                          obs.get("stdout_size"), obs.get("stdout_mtime_ns"), obs.get("stdout_file_identity"), job_id))
            if app_evidence is not None:
                conn.execute("UPDATE gateway_jobs SET app_server_evidence_json=?,log_digest=?,log_bytes=? WHERE job_id=?",
                             (json.dumps(app_evidence, sort_keys=True), obs.get("log_digest"), obs.get("log_bytes"), job_id))
            continue
        if incoming_state == "conflict":
            code = code or "result_identity_conflict"
            conn.execute("""INSERT INTO gateway_jobs(job_id,launch_identity,process_status,exit_code,turn_status,execution_status,result_state,
                         error_code,error_attempts,next_check_at,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(job_id) DO UPDATE SET result_state='conflict',process_status=excluded.process_status,
                         exit_code=excluded.exit_code,error_code=excluded.error_code,observed_at=excluded.observed_at,next_check_at=NULL,
                         stdout_size=COALESCE(?,gateway_jobs.stdout_size),stdout_mtime_ns=COALESCE(?,gateway_jobs.stdout_mtime_ns),
                         stdout_file_identity=COALESCE(?,gateway_jobs.stdout_file_identity)""",
                         (job_id, identity, obs.get("process_status", "unknown"), obs.get("exit_code"), "unknown", "unknown",
                          "conflict", code, 0, None, now, obs.get("stdout_size"), obs.get("stdout_mtime_ns"),
                          obs.get("stdout_file_identity")))
            summary["gateway_conflicts"] = summary.get("gateway_conflicts", 0) + 1
            continue
        text = obs.get("result_text")
        if incoming_state == "succeeded":
            if not isinstance(text, str):
                incoming_state, code = "pending", "result_text_missing"
            else:
                try:
                    calculated = hashlib.sha256(text.encode("utf-8", errors="strict")).hexdigest()
                except UnicodeEncodeError:
                    calculated = ""
                if not calculated or calculated != obs.get("result_digest"):
                    incoming_state, code = "pending", "result_digest_mismatch"
        if existing is None:
            attempts = 0
        else:
            attempts = existing["error_attempts"]
        if incoming_state == "pending" and not obs.get("reuse"):
            attempts += 1
        wait = min(3600.0, GATEWAY_BACKOFF_SECONDS * 2 ** min(max(0, attempts - 1), 7)) if incoming_state == "pending" else None
        next_check_at = (existing["next_check_at"] if obs.get("reuse") and existing is not None
                         else now + wait if wait else None)
        handoff_id = f"gateway-codex:{job_id}:{obs['thread_id']}" if incoming_state == "succeeded" else None
        values = (job_id, identity or (existing["launch_identity"] if existing else None), obs.get("thread_id"), obs.get("final_item_id"),
                  obs.get("result_digest"), obs.get("log_digest"), obs.get("log_bytes"), obs.get("stdout_ref"),
                  obs.get("stdout_size"), obs.get("stdout_mtime_ns"), obs.get("stdout_file_identity"),
                  obs.get("process_status", "unknown"), obs.get("exit_code"),
                  obs.get("turn_status", "unknown"), obs.get("execution_status", "unknown"), incoming_state, code,
                  attempts, next_check_at, now, handoff_id)
        conn.execute("""INSERT INTO gateway_jobs(job_id,launch_identity,thread_id,final_item_id,result_digest,log_digest,log_bytes,stdout_ref,
                     stdout_size,stdout_mtime_ns,stdout_file_identity,process_status,exit_code,turn_status,execution_status,result_state,error_code,error_attempts,
                     next_check_at,observed_at,handoff_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(job_id) DO UPDATE SET launch_identity=COALESCE(gateway_jobs.launch_identity,excluded.launch_identity),
                     thread_id=excluded.thread_id,final_item_id=excluded.final_item_id,result_digest=excluded.result_digest,
                     log_digest=excluded.log_digest,log_bytes=excluded.log_bytes,stdout_ref=excluded.stdout_ref,
                     stdout_size=excluded.stdout_size,stdout_mtime_ns=excluded.stdout_mtime_ns,
                     stdout_file_identity=excluded.stdout_file_identity,process_status=excluded.process_status,
                     exit_code=excluded.exit_code,turn_status=excluded.turn_status,execution_status=excluded.execution_status,
                     result_state=excluded.result_state,error_code=excluded.error_code,error_attempts=excluded.error_attempts,
                     next_check_at=excluded.next_check_at,observed_at=excluded.observed_at,handoff_id=COALESCE(gateway_jobs.handoff_id,excluded.handoff_id)""", values)
        if app_evidence is not None:
            conn.execute("UPDATE gateway_jobs SET app_server_evidence_json=? WHERE job_id=?",
                         (json.dumps(app_evidence, sort_keys=True), job_id))
        if incoming_state == "succeeded" and handoff_id:
            created = _insert_handoff(conn, handoff_id=handoff_id, source_type="gateway_codex", source_id=job_id,
                                      provider="codex-cli", status="turn_completed", now=now,
                                      text=text, source="gateway_codex_appserver" if app_evidence is not None else "gateway_codex_jsonl")
            if created:
                summary["gateway_handoffs"] = summary.get("gateway_handoffs", 0) + 1


def _create_handoffs(conn: sqlite3.Connection, f: Fetched, now: float) -> None:
    """A handoff exists per (session, run, assistant message): the run's own terminal authority AND a fully
    validated message.  Other runs of the same session (queued/running) neither block nor lend their state."""
    for session in conn.execute("SELECT session_id, provider FROM sessions").fetchall():
        sid = session["session_id"]
        for run_id, run in analyze_session(conn, sid).items():
            if run["state"] != "terminal":
                continue
            for message_id, message in run["messages"].items():
                if not message_id or not message["ok"] or message["handed_off"]:
                    continue
                handoff_id = f"cli:{sid}:{run_id}:{message_id}"
                _insert_handoff(conn, handoff_id=handoff_id, source_type="cli_session", source_id=sid, provider=session["provider"],
                                status=run["terminal_status"] or "completed", now=now, run_id=run_id, message_id=message_id,
                                text=message["text"], source=message["source"])
                conn.executemany("UPDATE inbox SET handoff_id=? WHERE id=? AND handoff_id IS NULL",
                                 [(handoff_id, i) for i in message["event_ids"]])
    for row in conn.execute("SELECT task_id, status, result_json FROM tasks WHERE terminal_confirmed_at IS NOT NULL AND result_json IS NOT NULL").fetchall():
        _insert_handoff(conn, handoff_id=f"task:{row['task_id']}", source_type="task", source_id=row["task_id"], provider=None,
                        status=row["status"], now=now, text=row["result_json"], source="task_result")
        # Handoffs written by older ledgers kept no body for tasks; backfill it from the stored task result while
        # nothing has been delivered yet (a received digest must never change underneath the root).
        legacy = conn.execute("SELECT 1 FROM handoffs h JOIN outbox o USING(handoff_id) WHERE h.handoff_id=? AND h.result_text IS NULL"
                              " AND o.delivery_state='pending'", (f"task:{row['task_id']}",)).fetchone()
        if legacy:
            digest = _digest(row["result_json"])
            conn.execute("UPDATE handoffs SET result_text=?, result_digest=?, result_source='task_result' WHERE handoff_id=?",
                         (row["result_json"], digest, f"task:{row['task_id']}"))
            conn.execute("UPDATE outbox SET result_digest=?, result_chars=? WHERE handoff_id=?",
                         (digest, len(row["result_json"]), f"task:{row['task_id']}"))


def _snapshot(f: Fetched) -> dict[str, Any]:
    """Session-list PROJECTION counts only (the session shows its latest run).  Reconciled run counts are derived
    from the runs table in build_status and are kept separate."""
    providers: dict[str, dict[str, Any]] = {p: {"session_projection": {}} for p in sorted(cli.PROVIDERS)}
    for state in ("running", "queued"):
        listed = f.cli_lists.get(state)
        bound = f.sources.get(f"cli_list:{state}", {}).get("lower_bound", False)
        for name, entry in providers.items():
            entry["session_projection"][state] = None if listed is None else sum(1 for i in listed if i.get("provider") == name)
            entry["session_projection"][f"{state}_lower_bound"] = bool(bound and listed is not None)
    task_counts = {s: (len(f.task_lists[s]) if s in f.task_lists else None) for s in TASK_ACTIVE}
    return {"providers": providers, "tasks": task_counts, "sources": f.sources, "jobs": f.jobs,
            "gateway_codex": f.gateway_summary}


def _gateway_provenance(row: Any) -> dict[str, Any] | None:
    if row is None or row["gateway_job_id"] is None:
        return None
    result = {"job_id": row["gateway_job_id"], "thread_id": row["thread_id"], "final_item_id": row["final_item_id"],
            "process_status": row["process_status"], "exit_code": row["exit_code"],
            "turn_status": row["turn_status"], "execution_status": row["execution_status"],
            "result_state": row["result_state"], "result_digest": row["gateway_result_digest"],
            "log_digest": row["gateway_log_digest"],
            "log_bytes": row["log_bytes"], "stdout_ref": row["stdout_ref"],
            "observed_at": iso(row["gateway_observed_at"]), "error_code": row["gateway_error_code"]}
    if "app_server_evidence_json" in row.keys():
        try:
            safe = _safe_appserver_evidence(json.loads(row["app_server_evidence_json"] or "null"))
        except (ValueError, TypeError, RecursionError):
            safe = None
        if safe is not None:
            result["app_server"] = safe
    return result


# ------------------------------------------------------------------- user commands

def record_heartbeat(conn: sqlite3.Connection, *, source_id: str, source_type: str, root_epoch: int | None,
                     observed_at: float | None = None, expires_at: float | None = None, ttl: float | None = None,
                     aux_count: int | None = None, now: float | None = None) -> dict[str, Any]:
    """Explicit heartbeat.  Only the CLI ``heartbeat`` command reaches this; the monitor never does."""
    now = time.time() if now is None else now
    observed = now if observed_at is None else observed_at
    if not SOURCE_ID_RE.fullmatch(source_id or "") or source_type not in {"root", "aux"}:
        raise SupervisorError("invalid_input")
    if (expires_at is None) == (ttl is None):
        raise SupervisorError("invalid_input")
    expires = expires_at if expires_at is not None else observed + float(ttl)
    if observed > now + 60 or not observed < expires <= observed + MAX_HEARTBEAT_TTL:
        raise SupervisorError("invalid_input")
    if aux_count is not None and not 0 <= aux_count <= 1000:
        raise SupervisorError("invalid_input")
    if source_type == "root" and (root_epoch is None or root_epoch < 0):
        raise SupervisorError("invalid_input")
    conn.execute("BEGIN IMMEDIATE")
    try:
        if source_type == "root":
            newest = conn.execute("SELECT MAX(root_epoch) FROM source_heartbeats WHERE source_type='root'").fetchone()[0]
            if newest is not None and root_epoch < newest:
                raise SupervisorError("root_epoch_regressed")
        conn.execute("INSERT INTO source_heartbeats(source_id,source_type,observed_at,expires_at,root_epoch,aux_count,via)"
                     " VALUES(?,?,?,?,?,?,'cli_heartbeat')",
                     (source_id, source_type, observed, expires, root_epoch, aux_count))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "source_id": source_id, "source_type": source_type, "observed_at": iso(observed),
            "expires_at": iso(expires), "root_epoch": root_epoch}


def record_receipt(conn: sqlite3.Connection, items: list[str], *, root_source_id: str, root_epoch: int,
                   note: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Delivery receipt, written ONLY when the root explicitly states it received a handoff and proves it with the
    result digest (``<handoff_id>=<sha256>``).  Reading (pending/show/status), daemon heartbeats or a guessed root
    liveness never reach this.  All items or none; a receipt is not an ack (processing)."""
    now = time.time() if now is None else now
    if not SOURCE_ID_RE.fullmatch(root_source_id or "") or not isinstance(root_epoch, int) or root_epoch < 0 or not items:
        raise SupervisorError("invalid_input")
    parsed = []
    for item in items:
        handoff_id, sep, digest = item.rpartition("=")
        if not sep or not handoff_id or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise SupervisorError("invalid_input")
        parsed.append((handoff_id, digest))
    received: list[str] = []
    already: list[str] = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        newest = conn.execute("SELECT MAX(root_epoch) FROM source_heartbeats WHERE source_type='root'").fetchone()[0]
        if newest is not None and root_epoch < newest:
            raise SupervisorError("root_epoch_regressed")
        for handoff_id, digest in parsed:
            row = conn.execute("SELECT o.delivery_state, o.result_digest, h.result_text FROM outbox o JOIN handoffs h USING(handoff_id)"
                               " WHERE o.handoff_id=?", (handoff_id,)).fetchone()
            if row is None:
                raise SupervisorError("not_found")
            if row["delivery_state"] == "superseded":
                raise SupervisorError("not_deliverable")
            if digest != row["result_digest"] or _digest(row["result_text"] or "") != row["result_digest"]:
                raise SupervisorError("digest_mismatch")
            if row["delivery_state"] == "received":
                already.append(handoff_id)      # the first receipt stands
                continue
            conn.execute("UPDATE outbox SET delivery_state='received', received_at=?, root_source_id=?, root_epoch=?, receipt_note=?"
                         " WHERE handoff_id=? AND delivery_state='pending'", (now, root_source_id, root_epoch, (note or "")[:200], handoff_id))
            received.append(handoff_id)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "received": received, "already_received": already, "root_source_id": root_source_id,
            "root_epoch": root_epoch, "received_at": iso(now)}


def pending_view(conn: sqlite3.Connection, now: float, *, cursor: int = 0, limit: int = 20,
                 interval: float = POLL_SECONDS) -> dict[str, Any]:
    """Read-only drain page of handoffs not yet received by a root.  Nothing is marked received or processed;
    ``next_cursor`` is a stable outbox sequence, so paging is repeatable and an omitted page is never "done"."""
    limit = max(1, min(int(limit), 100))
    cursor = max(0, int(cursor))
    rows = conn.execute("SELECT o.*, h.state AS handoff_state, h.terminal_status, h.provider, h.result_source, h.result_text,"
                        " g.job_id AS gateway_job_id, g.thread_id, g.final_item_id, g.process_status, g.exit_code, g.turn_status,"
                        " g.execution_status, g.result_state, g.result_digest AS gateway_result_digest,"
                        " g.log_digest AS gateway_log_digest, g.log_bytes, g.stdout_ref,g.app_server_evidence_json,"
                        " g.observed_at AS gateway_observed_at, g.error_code AS gateway_error_code"
                        " FROM outbox o JOIN handoffs h USING(handoff_id) LEFT JOIN gateway_jobs g USING(handoff_id)"
                        " WHERE o.delivery_state='pending' AND o.seq>? ORDER BY o.seq LIMIT ?", (cursor, limit + 1)).fetchall()
    page = rows[:limit]
    total = conn.execute("SELECT COUNT(*) FROM outbox WHERE delivery_state='pending'").fetchone()[0]
    remaining = conn.execute("SELECT COUNT(*) FROM outbox WHERE delivery_state='pending' AND seq>?", (cursor,)).fetchone()[0]
    status = build_status(conn, now, interval=interval)
    return {
        "ok": True, "read_only": True, "total_pending": total, "remaining_after_cursor": remaining, "returned": len(page),
        "has_more": len(rows) > limit, "next_cursor": page[-1]["seq"] if page else cursor,
        "items": [{"seq": r["seq"], "handoff_id": r["handoff_id"], "source_type": r["source_type"], "source_id": r["source_id"],
                   "run_id": r["run_id"], "message_id": r["message_id"], "provider": r["provider"],
                   "terminal_status": r["terminal_status"], "result_source": r["result_source"], "result_digest": r["result_digest"],
                   "result_chars": r["result_chars"], "created_at": iso(r["created_at"]), "delivery_state": r["delivery_state"],
                   "result_preview": None if r["source_type"] == "gateway_codex" else (r["result_text"] or "")[:200],
                   "gateway_provenance": _gateway_provenance(r),
                   "fetch": f"show {r['handoff_id']}"} for r in page],
        "received_not_acked": conn.execute("SELECT COUNT(*) FROM outbox o JOIN handoffs h USING(handoff_id)"
                                           " WHERE o.delivery_state='received' AND h.state='ready'").fetchone()[0],
        "blockers": [{k: a[k] for k in ("code", "subject", "severity", "count", "items") if k in a} for a in status["alerts"]][:50],
        "source_freshness": {"observed_at": status["observed_at"], "snapshot_age_seconds": status["snapshot_age_seconds"],
                             "snapshot_fresh": status["snapshot_fresh"], "poll_cycle": status["poll_cycle"],
                             "root_heartbeat": status["root_heartbeat"]["state"]},
    }


def acknowledge(conn: sqlite3.Connection, items: list[str], note: str | None = None, now: float | None = None) -> dict[str, Any]:
    """Explicitly acknowledge ``handoff:<id>`` (consumed rows only; requires a recorded delivery receipt) or ``inbox:<n>``."""
    now = time.time() if now is None else now
    acked = []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for item in items:
            kind, _, ident = item.partition(":")
            if kind == "handoff":
                row = conn.execute("SELECT source_type, source_id, run_id FROM handoffs WHERE handoff_id=?", (ident,)).fetchone()
                if row is None:
                    raise SupervisorError("not_found")
                box = conn.execute("SELECT delivery_state FROM outbox WHERE handoff_id=?", (ident,)).fetchone()
                if box is None or box["delivery_state"] != "received":
                    raise SupervisorError("delivery_not_received")   # processing cannot precede a recorded receipt
                conn.execute("UPDATE handoffs SET state='acked', acked_at=? WHERE handoff_id=? AND state<>'acked'", (now, ident))
                # Only the rows this handoff consumed are acknowledged.  The durable session is never retired:
                # it may be continued, and the new turn must be observed and handed off on its own.
                conn.execute("UPDATE inbox SET acked_at=? WHERE handoff_id=? AND acked_at IS NULL", (now, ident))
                if row["source_type"] == "cli_session" and row["run_id"] and not conn.execute(
                        "SELECT 1 FROM handoffs WHERE source_id=? AND run_id=? AND state='ready'",
                        (row["source_id"], row["run_id"])).fetchone():
                    conn.execute("UPDATE inbox SET acked_at=? WHERE session_id=? AND run_id=? AND kind='terminal' AND acked_at IS NULL",
                                 (now, row["source_id"], row["run_id"]))
            elif kind == "inbox" and ident.isdigit():
                if conn.execute("UPDATE inbox SET acked_at=COALESCE(acked_at, ?) WHERE id=?", (now, int(ident))).rowcount == 0:
                    raise SupervisorError("not_found")
            else:
                raise SupervisorError("invalid_input")
            conn.execute("INSERT OR IGNORE INTO acknowledgments VALUES(?,?,?,?)", (kind, ident, now, (note or "")[:200]))
            acked.append(item)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"ok": True, "acked": acked}


# ------------------------------------------------------------------------- status

def _deficit(target: int, running: int | None, *, lower_bound: bool) -> dict[str, Any]:
    if running is None:
        return {"target": target, "running": None, "deficit": "UNKNOWN", "exact": False}
    deficit = max(0, target - running)
    # With a lower-bound count the true deficit can only be smaller or equal.
    return {"target": target, "running": running, "deficit": deficit, "exact": not lower_bound,
            **({"deficit_is_upper_bound": True, "running_is_lower_bound": True} if lower_bound else {})}


def _reconciled_runs(conn: sqlite3.Connection, now: float) -> tuple[dict[str, dict[str, int]], int, list[dict[str, Any]], set[str]]:
    """Run states per provider from the runs table.  Only a state observed within RUN_FRESH_SECONDS counts;
    anything older / never confirmed is UNKNOWN, never assumed running or finished."""
    cutoff = now - RUN_FRESH_SECONDS
    per = {p: {"running": 0, "queued": 0, "other_active": 0, "unknown": 0} for p in sorted(cli.PROVIDERS)}
    unattributed = 0
    fresh_by_session: dict[str, dict[str, list[str]]] = {}
    providers_of: dict[str, str] = {}
    for r in conn.execute("SELECT r.run_id, r.session_id, r.status, r.observed_at, s.provider FROM runs r"
                          " LEFT JOIN sessions s ON s.session_id=r.session_id"
                          " WHERE r.status NOT IN ('succeeded','failed','cancelled') AND r.terminal_at IS NULL"):
        provider = r["provider"] if r["provider"] in per else None
        fresh = r["status"] != "unknown" and r["observed_at"] is not None and r["observed_at"] >= cutoff
        if provider is None:
            unattributed += 1                    # provider not known: cannot be ruled out for any provider
            continue
        providers_of[r["session_id"]] = provider
        if not fresh:
            per[provider]["unknown"] += 1
        elif r["status"] == "running":
            per[provider]["running"] += 1
            fresh_by_session.setdefault(r["session_id"], {"running": [], "queued": []})["running"].append(r["run_id"])
        elif r["status"] == "queued":
            per[provider]["queued"] += 1
            fresh_by_session.setdefault(r["session_id"], {"running": [], "queued": []})["queued"].append(r["run_id"])
        else:
            per[provider]["other_active"] += 1
    followups = [{"session_id": sid, "provider": providers_of[sid], "queued_run_id": queued, "behind_run_id": group["running"][0]}
                 for sid, group in sorted(fresh_by_session.items()) if group["running"] for queued in group["queued"]]
    running_sessions = {sid for sid, group in fresh_by_session.items() if group["running"]}
    return per, unattributed, followups, running_sessions


def _cycle_view(conn: sqlite3.Connection, now: float) -> dict[str, Any]:
    ok_row = conn.execute("SELECT value FROM meta WHERE key='last_commit_at'").fetchone()
    err_row = conn.execute("SELECT value FROM meta WHERE key='last_cycle_error'").fetchone()
    last_ok = float(ok_row[0]) if ok_row else None
    failure = None
    if err_row:
        try:
            failure = json.loads(err_row[0])
        except ValueError:
            failure = None
    failing = bool(failure) and (last_ok is None or failure["at"] > last_ok)
    return {"state": "failing" if failing else ("ok" if last_ok else "unknown"), "last_success_at": iso(last_ok),
            "last_failure": {"code": failure["code"], "at": iso(failure["at"])} if failure else None}


def build_status(conn: sqlite3.Connection, now: float, *, interval: float = POLL_SECONDS,
                 stale_seconds: float = STALE_SECONDS) -> dict[str, Any]:
    snap_row = conn.execute("SELECT observed_at, data_json FROM snapshots ORDER BY seq DESC LIMIT 1").fetchone()
    snap = json.loads(snap_row["data_json"]) if snap_row else None
    observed_at = snap_row["observed_at"] if snap_row else None
    window = max(3 * interval, 90)
    snapshot_age = None if observed_at is None else max(0.0, now - observed_at)
    snapshot_fresh = snapshot_age is not None and snapshot_age <= window
    alerts: list[dict[str, Any]] = []

    def alert(code: str, subject: str | None = None, severity: str = "warning") -> None:
        alerts.append({"code": code, "subject": subject, "severity": severity})

    cycle = _cycle_view(conn, now)
    sources: dict[str, Any] = {}
    if snap is None:
        alert("no_observation_yet", severity="error")
    else:
        for name, src in snap.get("sources", {}).items():
            seen = src.get("observed_at")
            sources[name] = {**src, "age_seconds": None if seen is None else round(max(0.0, now - seen), 1),
                             "fresh": seen is not None and now - seen <= window
                             and not (name in {"jobs", "gateway_codex", "aoitalk"}
                                      and (src.get("error") or not src.get("complete")))}
        if not snapshot_fresh:
            alert("observer_stale", severity="error")
    if cycle["state"] == "failing":
        alert(f"poll_cycle_failed:{cycle['last_failure']['code']}", "poll_cycle", "error")
    for name, src in sources.items():
        if ":" in name:
            continue                    # state-specific sources are folded into the aggregate alert
        if src["error"]:
            alert(f"source_error:{src['error']}", name, "error")
        elif not src["complete"]:
            alert("source_incomplete", name)

    providers = snap.get("providers") if snap and snap.get("providers") else {p: {"session_projection": {}} for p in sorted(cli.PROVIDERS)}
    reconciled, unattributed, followups, running_sessions = _reconciled_runs(conn, now)
    for name, entry in providers.items():
        entry["reconciled_runs"] = reconciled.get(name, {})

    # minClaude counts RECONCILED running runs only.  Exact only when every list source is complete, error-free,
    # unbounded and fresh, every scoped session has a fresh complete run list and no run state is unknown.
    claude = reconciled["claude-cli"]
    unknown_runs = claude["unknown"] + unattributed
    reasons: list[str] = []
    if snap is None:
        reasons.append("no_observation")
    else:
        if not snapshot_fresh:
            reasons.append("snapshot_stale")
        for state in CLI_ACTIVE:
            src = sources.get(f"cli_list:{state}")
            if src is None or src["error"] or not src["complete"] or src["lower_bound"] or not src["fresh"]:
                reasons.append(f"cli_list:{state}")
    if cycle["state"] == "failing":
        reasons.append("poll_cycle_failed")
    # The count is scoped (active sessions + known runs), but a failed global session listing (any state) means the
    # session universe itself was not read cleanly this poll, so the figure is not exact.
    if snap is not None and (sources.get("cli_list") or {}).get("error"):
        reasons.append("cli_list_error")
    if unknown_runs:
        reasons.append("unreconciled_runs")
    cutoff = now - RUN_FRESH_SECONDS
    listed = {(r["session_id"], r["status"]) for r in conn.execute(
        "SELECT session_id, status FROM run_lists WHERE complete=1 AND error IS NULL AND observed_at>=?", (cutoff,))}
    unlisted = [r["session_id"] for r in conn.execute(
        "SELECT session_id FROM sessions WHERE provider='claude-cli' AND status IN (%s)" % ",".join("?" * len(CLI_ACTIVE)), CLI_ACTIVE)
        if (r["session_id"], "running") not in listed or (r["session_id"], "queued") not in listed]
    if unlisted:
        reasons.append("run_lists_missing")
    run_list_source = sources.get("cli_run_lists")
    if run_list_source and (run_list_source["error"] or not run_list_source["complete"]):
        reasons.append("run_list_source_incomplete")
    # A session projected as running with no reconciled running run is an unexplained difference: not exact.
    if any(r["session_id"] not in running_sessions for r in conn.execute(
            "SELECT session_id FROM sessions WHERE provider='claude-cli' AND status='running'")):
        reasons.append("projection_without_running_run")
    exact = not reasons
    min_claude = _deficit(MIN_CLAUDE, None if snap is None else claude["running"], lower_bound=not exact)
    min_claude.update({
        "basis": "reconciled_runs", "queued_not_counted": claude["queued"], "unknown_runs": unknown_runs,
        "sessions_without_fresh_run_list": len(unlisted), "not_exact_reasons": sorted(set(reasons)),
        "session_projection": {k: (providers.get("claude-cli", {}).get("session_projection") or {}).get(k) for k in ("running", "queued")},
        "scope": "sessions projected active (list) plus all known run IDs; sessions projected terminal are not enumerated",
        "observed_at": iso(observed_at)})

    root = conn.execute("SELECT * FROM source_heartbeats WHERE source_type='root' ORDER BY root_epoch DESC, observed_at DESC LIMIT 1").fetchone()
    root_view: dict[str, Any] = {"state": "missing"}
    if root is None:
        alert("root_heartbeat_missing", severity="error")
    else:
        fresh = root["expires_at"] > now
        root_view = {"state": "live" if fresh else "expired", "source_id": root["source_id"],
                     "source_type": "root", "root_epoch": root["root_epoch"],
                     "observed_at": iso(root["observed_at"]), "expires_at": iso(root["expires_at"])}
        if not fresh:
            alert("root_heartbeat_expired", root["source_id"], "error")
    aux_row = conn.execute("SELECT * FROM source_heartbeats WHERE via='cli_heartbeat' AND aux_count IS NOT NULL"
                           " AND expires_at>? ORDER BY observed_at DESC, seq DESC LIMIT 1", (now,)).fetchone()
    aux = _deficit(AUX_TARGET, aux_row["aux_count"] if aux_row else None, lower_bound=False)
    aux["provenance"] = ({"source_id": aux_row["source_id"], "source_type": aux_row["source_type"],
                          "observed_at": iso(aux_row["observed_at"]), "expires_at": iso(aux_row["expires_at"]),
                          "root_epoch": aux_row["root_epoch"]} if aux_row else None)
    if aux_row is None:
        alert("aux_visibility_unknown")

    for row in conn.execute("SELECT session_id, provider, last_progress_at FROM sessions WHERE status='running'"):
        if now - row["last_progress_at"] > stale_seconds:
            alert("stale_worker", f"cli:{row['session_id']}")
    for row in conn.execute("SELECT task_id, last_progress_at, stalled FROM tasks WHERE status='running'"):
        if row["stalled"] or now - row["last_progress_at"] > stale_seconds:
            alert("stale_worker", f"task:{row['task_id']}")
    for row in conn.execute("SELECT session_id, cursor_stalled, collect_error FROM sessions WHERE cursor_stalled=1 OR collect_error IS NOT NULL"):
        if row["cursor_stalled"]:
            alerts.append({"code": "cursor_stalled_truncated_page", "subject": f"cli:{row['session_id']}", "severity": "warning",
                           "reason": row["collect_error"]})
        else:
            alert(f"collect_error:{row['collect_error']}", f"cli:{row['session_id']}")
    missing: list[str] = []
    incomplete: list[dict[str, Any]] = []
    for row in conn.execute("SELECT session_id, status, drained FROM sessions").fetchall():
        runs = analyze_session(conn, row["session_id"])
        if row["status"] in CLI_TERMINAL and not runs and row["drained"]:
            missing.append(f"cli:{row['session_id']}")
        for run_id, run in runs.items():
            subject = f"cli:{row['session_id']}:{run_id}"
            if run["conflict"]:
                incomplete.append({"subject": subject, "reason": "run_state_conflict"})
            if run["state"] != "terminal":
                continue
            if not run["messages"]:
                missing.append(subject)
            for message_id, message in run["messages"].items():
                if not message["ok"]:
                    incomplete.append({"subject": subject, "reason": message["reason"]})
    missing += [f"task:{r['task_id']}" for r in conn.execute(
        "SELECT task_id FROM tasks WHERE terminal_confirmed_at IS NOT NULL AND result_json IS NULL")]
    if missing:
        # One aggregated alert: historic backlog must not drown the live alerts.
        alerts.append({"code": "terminal_without_result", "subject": None, "severity": "warning",
                       "count": len(missing), "items": missing[:20]})
    if incomplete:
        # Provenance is incomplete or oversized: the result is UNKNOWN, never silently complete.
        alerts.append({"code": "result_provenance_incomplete", "subject": None, "severity": "warning",
                       "count": len(incomplete), "items": incomplete[:20]})
    if unknown_runs:
        alerts.append({"code": "runs_unreconciled", "subject": None, "severity": "warning", "count": unknown_runs,
                       "items": [r["run_id"] for r in conn.execute(
                           "SELECT run_id FROM runs WHERE settled=0 AND terminal_at IS NULL AND status NOT IN ('succeeded','failed','cancelled')"
                           " ORDER BY COALESCE(checked_at,0) LIMIT 20")]})
    for row in conn.execute("SELECT session_id FROM sessions WHERE status='running'"
                            " AND last_seen<?", (observed_at or now,)):
        alert("state_unconfirmed_absent_from_lists", f"cli:{row['session_id']}")

    ready = [{"handoff_id": r["handoff_id"], "source_type": r["source_type"], "source_id": r["source_id"],
              "provider": r["provider"], "terminal_status": r["terminal_status"], "created_at": iso(r["created_at"]),
              "run_id": r["run_id"], "message_id": r["message_id"], "result_source": r["result_source"],
              "result_digest": r["result_digest"], "delivery_state": r["delivery_state"],
              "result_chars": len(r["result_text"] or ""),
              "result_preview": None if r["source_type"] == "gateway_codex" else (r["result_text"] or "")[:300],
              "gateway_provenance": _gateway_provenance(r)}
             for r in conn.execute("""SELECT h.*, o.delivery_state, g.job_id AS gateway_job_id, g.thread_id, g.final_item_id,
                                   g.process_status, g.exit_code, g.turn_status, g.execution_status, g.result_state,
                                   g.result_digest AS gateway_result_digest, g.log_digest AS gateway_log_digest, g.log_bytes, g.stdout_ref,g.app_server_evidence_json,
                                   g.observed_at AS gateway_observed_at, g.error_code AS gateway_error_code
                                   FROM handoffs h LEFT JOIN outbox o USING(handoff_id) LEFT JOIN gateway_jobs g USING(handoff_id)
                                   WHERE h.state='ready' ORDER BY h.created_at LIMIT 100""")]
    delivery = {state: conn.execute("SELECT COUNT(*) FROM outbox WHERE delivery_state=?", (state,)).fetchone()[0]
                for state in ("pending", "received", "superseded")}
    gateway_rows = conn.execute("""SELECT job_id,thread_id,final_item_id,process_status,exit_code,turn_status,execution_status,
                                result_state,result_digest,log_digest,log_bytes,stdout_ref,observed_at,error_code,handoff_id,app_server_evidence_json
                                FROM gateway_jobs ORDER BY observed_at DESC LIMIT 100""").fetchall()
    gateway_counts = {state: conn.execute("SELECT COUNT(*) FROM gateway_jobs WHERE result_state=?", (state,)).fetchone()[0]
                      for state in ("pending", "succeeded", "failed", "conflict")}
    gateway_view = {**(snap.get("gateway_codex", {}) if snap else {}), "source_type": "gateway_codex", "provider": "codex-cli",
                    "result_counts": gateway_counts,
                    "jobs": [{"job_id": r["job_id"], "thread_id": r["thread_id"], "final_item_id": r["final_item_id"],
                              "process_status": r["process_status"], "exit_code": r["exit_code"], "turn_status": r["turn_status"],
                              "execution_status": r["execution_status"], "result_state": r["result_state"],
                              "result_digest": r["result_digest"], "log_digest": r["log_digest"], "log_bytes": r["log_bytes"],
                              "stdout_ref": r["stdout_ref"], "observed_at": iso(r["observed_at"]),
                              "error_code": r["error_code"], "handoff_id": r["handoff_id"],
                              **({"app_server": _decode_appserver_evidence(r["app_server_evidence_json"])}
                                 if _decode_appserver_evidence(r["app_server_evidence_json"]) is not None else {})} for r in gateway_rows]}
    unacked = [{"inbox_id": r["id"], "session_id": r["session_id"], "kind": r["kind"], "event_type": r["event_type"],
                "summary": (r["summary"] or "")[:300], "received_at": iso(r["received_at"])}
               for r in conn.execute("SELECT * FROM inbox WHERE acked_at IS NULL AND kind IN ('terminal','result') ORDER BY id LIMIT 100")]
    return {
        "schema_version": SCHEMA_VERSION, "observed_at": iso(observed_at), "generated_at": iso(now),
        "snapshot_age_seconds": None if snapshot_age is None else round(snapshot_age, 1), "snapshot_fresh": snapshot_fresh,
        "poll_cycle": cycle, "observer_only": True, "poll_interval_seconds": interval,
        "providers": providers, "tasks_by_state": snap.get("tasks", {}) if snap else {}, "jobs": snap.get("jobs", {}) if snap else {},
        "gateway_codex": gateway_view,
        "source_completeness": sources, "minClaude": min_claude, "aux": aux, "root_heartbeat": root_view,
        "delivery": delivery, "ready_handoffs": ready, "unacked_terminal_or_result": unacked,
        "queued_followups": followups, "alerts": alerts,
    }


# --------------------------------------------------------------------------- run

Fetcher = Callable[[dict[str, Any]], Awaitable[Fetched]]


async def poll_once(conn: sqlite3.Connection, directory: Path = ROOT_DIR, fetch: Fetcher = default_fetch) -> dict[str, Any]:
    plan = make_plan(conn)
    fetched = await fetch(plan)          # network only; nothing is written yet
    commit(conn, fetched, plan)          # single transaction
    status = build_status(conn, time.time())
    write_status(directory, status)
    return status


def record_cycle_error(conn: sqlite3.Connection, code: str) -> None:
    now = time.time()
    conn.execute("INSERT INTO source_heartbeats(source_id,source_type,observed_at,via,error_code) VALUES('poll_cycle','observer_source',?,'monitor',?)",
                 (now, code))
    conn.execute("INSERT OR REPLACE INTO meta VALUES('last_cycle_error', ?)", (json.dumps({"code": code, "at": now}),))


async def run_loop(conn: sqlite3.Connection, directory: Path, interval: float, fetch: Fetcher = default_fetch,
                   cycles: int | None = None) -> None:
    count = 0
    failing: str | None = None
    log_event(directory, "loop_start", ledger=LEDGER_VERSION)
    while cycles is None or count < cycles:
        started = time.monotonic()
        try:
            status = await poll_once(conn, directory, fetch)
            if failing is not None or count % 30 == 0:
                log_event(directory, "poll_ok" if failing is None else "cycle_recovered", fresh=status["snapshot_fresh"],
                          pending=status["delivery"]["pending"])
            failing = None
        except Exception as exc:  # noqa: BLE001 - persist the safe code only, keep observing
            code = safe_code(exc)
            if code != failing:
                log_event(directory, "cycle_failed", code=code)
            failing = code
            try:
                record_cycle_error(conn, code)
                write_status(directory, build_status(conn, time.time()))   # make the failure visible, not only the ledger
            except (sqlite3.Error, OSError):
                pass
        count += 1
        if cycles is None or count < cycles:
            await asyncio.sleep(max(0.0, interval - (time.monotonic() - started)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="work_supervisor", description="Read-only work supervisor (observer only).")
    parser.add_argument("--dir", type=Path, default=ROOT_DIR, help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="poll forever (default every 10s)")
    run.add_argument("--interval", type=float, default=POLL_SECONDS)
    sub.add_parser("once", help="one poll, then exit")
    sub.add_parser("status", help="rebuild and print status.json from the ledger (no network)")
    pend = sub.add_parser("pending", help="read-only drain page of handoffs not yet received by a root")
    pend.add_argument("--cursor", type=int, default=0, help="outbox seq to continue after (next_cursor of the previous page)")
    pend.add_argument("--limit", type=int, default=20)
    recv = sub.add_parser("receive", help="root-only: record that the root received handoffs (ID=sha256 pairs)")
    recv.add_argument("items", nargs="+", help="<handoff_id>=<result_digest>")
    recv.add_argument("--root-source-id", required=True)
    recv.add_argument("--root-epoch", type=int, required=True)
    recv.add_argument("--note")
    ack = sub.add_parser("ack", help="acknowledge handoff:<id> / inbox:<n> items (handoffs need a receipt first)")
    ack.add_argument("items", nargs="+")
    ack.add_argument("--note")
    show = sub.add_parser("show", help="print the stored result text of a handoff (read-only)")
    show.add_argument("handoff_id")
    hb = sub.add_parser("heartbeat", help="explicit root/aux heartbeat (the monitor never writes these)")
    hb.add_argument("--source-id", required=True)
    hb.add_argument("--source-type", required=True, choices=["root", "aux"])
    hb.add_argument("--root-epoch", type=int)
    hb.add_argument("--observed-at")
    hb.add_argument("--expires-at")
    hb.add_argument("--ttl", type=float, help="seconds until expiry (alternative to --expires-at)")
    hb.add_argument("--aux-count", type=int, help="native aux workers the caller explicitly reports")
    args = parser.parse_args(argv)

    def emit(payload: dict[str, Any]) -> None:
        if sys.stdout is not None:      # pythonw (scheduled task) has no stdout
            print(json.dumps(payload, ensure_ascii=False, indent=2))

    conn = None
    try:
        with contextlib.ExitStack() as stack:
            if args.command in ("run", "once"):
                # The singleton lock comes BEFORE connect(): a duplicate start never touches (or migrates) the ledger.
                stack.enter_context(SingletonLock(args.dir))
            conn = connect(args.dir)
            if args.command == "run":
                recover(conn, args.dir)
                log_event(args.dir, "start", command="run")
                try:
                    asyncio.run(run_loop(conn, args.dir, max(1.0, args.interval)))
                finally:
                    log_event(args.dir, "stop")
            elif args.command == "once":
                recover(conn, args.dir)
                emit(asyncio.run(poll_once(conn, args.dir)))
            elif args.command == "status":
                status = build_status(conn, time.time())
                write_status(args.dir, status)
                emit(status)
            elif args.command == "pending":
                emit(pending_view(conn, time.time(), cursor=args.cursor, limit=args.limit))
            elif args.command == "receive":
                emit(record_receipt(conn, args.items, root_source_id=args.root_source_id, root_epoch=args.root_epoch, note=args.note))
            elif args.command == "show":
                row = conn.execute("""SELECT h.*, o.delivery_state, o.received_at, o.root_source_id, o.root_epoch,
                                      g.job_id AS gateway_job_id, g.thread_id, g.final_item_id, g.process_status, g.exit_code,
                                      g.turn_status, g.execution_status, g.result_state, g.result_digest AS gateway_result_digest,
                                      g.log_digest AS gateway_log_digest,
                                      g.log_bytes, g.stdout_ref,g.app_server_evidence_json, g.observed_at AS gateway_observed_at, g.error_code AS gateway_error_code
                                      FROM handoffs h LEFT JOIN outbox o USING(handoff_id)
                                      LEFT JOIN gateway_jobs g USING(handoff_id) WHERE h.handoff_id=?""",
                                      (args.handoff_id,)).fetchone()
                if row is None:
                    raise SupervisorError("not_found")
                text = row["result_text"]
                emit({"ok": True, "handoff_id": row["handoff_id"], "state": row["state"], "run_id": row["run_id"],
                      "message_id": row["message_id"], "terminal_status": row["terminal_status"], "result_source": row["result_source"],
                      "result_digest": row["result_digest"], "result_chars": len(text or ""),
                      "digest_verified": _digest(text or "") == row["result_digest"], "delivery_state": row["delivery_state"],
                      "received_at": iso(row["received_at"]), "root_source_id": row["root_source_id"], "root_epoch": row["root_epoch"],
                      "gateway_provenance": _gateway_provenance(row),
                      "result_text": text})
            elif args.command == "ack":
                emit(acknowledge(conn, args.items, args.note))
            else:
                observed, expires = parse_time(args.observed_at), parse_time(args.expires_at)
                if (args.observed_at and observed is None) or (args.expires_at and expires is None):
                    raise SupervisorError("invalid_input")
                emit(record_heartbeat(conn, source_id=args.source_id, source_type=args.source_type,
                                      root_epoch=args.root_epoch, observed_at=observed, expires_at=expires,
                                      ttl=args.ttl, aux_count=args.aux_count))
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # noqa: BLE001
        code = safe_code(exc)
        emit({"ok": False, "error": code})
        if args.command == "run":
            log_event(args.dir, "exit", code=code)
            if code == "already_running":
                return 0                # another observer owns the lock: a clean no-op, so the scheduler does not restart-loop
        return 1
    finally:
        if conn is not None:
            conn.close()
