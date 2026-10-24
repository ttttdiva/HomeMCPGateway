"""Durable, single-controller resource queue.

The resource queue is deliberately small and local.  It is a ledger for one
operation at a time on each registered phone or GPU; it does not start a
daemon, poll a provider token, or attempt to recover an unknown process by
guessing.  ``Controller.pump`` is the only code path that dispatches runner work.

Two invariants are worth calling out here:

* A lease which expires is still a lease.  Expiry is a signal for the next
  controller to reconcile the old owner/generation, never permission to use
  the resource for another operation.
* A runner's terminal status is not a release proof.  A release receipt must
  bind the immutable operation, resource, stable job, thread, controller
  owner/generation, and payload hash, and must contain independently verified
  process and device cleanup evidence.  The queue cannot attest a real phone
  or GPU itself; that boundary belongs to the injected runner.

The module intentionally has no dependency on a particular device adapter.
Adapters may use the supplied task snapshot to invoke a dedicated thread or
the per-operation Gateway job identified by ``job_id``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import copy
import errno
import hashlib
import inspect
import json
import math
import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Protocol

try:  # pragma: no cover - only imported on Windows
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - exercised on POSIX CI
    msvcrt = None  # type: ignore

if msvcrt is None:  # pragma: no cover - exercised on POSIX CI
    import fcntl  # type: ignore
else:  # pragma: no cover - Windows does not provide fcntl
    fcntl = None  # type: ignore

try:  # psutil is a project dependency and supplies exact PID/create-time pairs.
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - defensive fallback for minimal installs
    psutil = None  # type: ignore

from .jobs import stable_job_id
from .resource_queue_claude import CLAUDE_PROVIDER, CLAUDE_ROUTE, ClaudeExecutorError, canonical_session_id, normalize_profile


__all__ = [
    "Controller",
    "Queue",
    "ResourceQueue",
    "QueueError",
    "OperationConflict",
    "QueueNotFound",
    "Runner",
]


_QUEUE_STATES = {"blocked", "ready", "running", "terminal"}
_OUTCOMES = {"succeeded", "failed", "cancelled"}
_KINDS = {"phone", "gpu"}
_TERMINAL_STATUS = {"succeeded", "failed", "cancelled", "completed", "exited", "terminated"}
_RUNNING_STATUS = {"running", "starting", "started", "accepted", "pending", "active"}
_UNKNOWN_STATUS = {"unknown", "lost", "not_found", "missing", "indeterminate"}
# Executor providers a resource can be bound to.  ``codex`` is the legacy
# dedicated-thread executor recorded for existing ledgers (``none`` when the
# resource has no thread). Both providers can be selected by an idle rebind.
_LEGACY_PROVIDER = "codex"
_NO_PROVIDER = "none"
_REBIND_TARGETS = {_LEGACY_PROVIDER, CLAUDE_PROVIDER}
_STALE_BINDING_CODE = "executor_rebind_required"


class QueueError(RuntimeError):
    """Base error raised for invalid queue operations."""


class OperationConflict(QueueError):
    """The operation id already names a different immutable request."""


class QueueNotFound(QueueError):
    """A requested resource or operation does not exist."""


class Runner(Protocol):
    """The intentionally narrow adapter contract used by :class:`Controller`."""

    def start(self, task: Mapping[str, Any]) -> Any: ...

    def lookup(self, task: Mapping[str, Any]) -> Any: ...

    def request_cancel(self, task: Mapping[str, Any]) -> Any: ...

    # An adapter must expose either verify_release or inspect.  The methods
    # remain optional at runtime so old adapters fail closed as unknown rather
    # than accidentally releasing a resource.
    def verify_release(self, task: Mapping[str, Any], receipt: Mapping[str, Any]) -> Any: ...

    def inspect(self, task: Mapping[str, Any]) -> Any: ...


class _JSONError(ValueError):
    pass


def _json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"__bytes_b64__": base64.b64encode(value).decode("ascii")}
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"value is not JSON serialisable: {type(value).__name__}")


def _canonical(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            default=_json_default,
        )
    except (TypeError, ValueError, OverflowError) as exc:
        raise _JSONError(str(exc)) from exc


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _decode(raw: str | None, default: Any = None) -> Any:
    if raw is None:
        return copy.deepcopy(default)
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def _safe_int(value: Any, *, name: str, minimum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name}_must_be_integer")
    if minimum is not None and value < minimum:
        raise ValueError(f"{name}_below_minimum")
    return value


def _now_value(now: Callable[[], float] | float) -> float:
    value = now() if callable(now) else now
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_clock") from exc
    if not math.isfinite(number):
        raise ValueError("invalid_clock")
    return number


def _operation_key(operation_id: str) -> str:
    """Return the immutable queue key used by Gateway's stable UUID codec."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        raise ValueError("invalid_operation_id")
    # The operation id, and only the operation id, chooses the key.  Payload
    # changes therefore cannot silently create a second Gateway job.
    return "rq1:" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()


def _uuid() -> str:
    return str(uuid.uuid4())


def _current_process_create_time() -> float | None:
    if psutil is None:
        return None
    try:
        value = float(psutil.Process(os.getpid()).create_time())
    except (OSError, ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def _as_json_object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name}_must_be_object")
    try:
        # Normalise foreign mapping implementations before retaining them.
        return json.loads(_canonical(dict(value)))
    except _JSONError as exc:
        raise ValueError(f"invalid_{name}") from exc


def _normalise_dependencies(value: Iterable[str] | None, operation_id: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        raise ValueError("dependencies_must_be_list")
    try:
        deps = list(value)
    except TypeError as exc:
        raise ValueError("dependencies_must_be_list") from exc
    if any(not isinstance(item, str) or not item.strip() for item in deps):
        raise ValueError("invalid_dependency")
    if len(set(deps)) != len(deps):
        raise ValueError("duplicate_dependency")
    if operation_id in deps:
        raise ValueError("self_dependency")
    return deps


def _pick(mapping: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _truth(value: Any) -> bool:
    return value is True or value == 1 or value == "true" or value == "verified"


def _status_of(value: Any) -> str:
    if not isinstance(value, Mapping):
        return "unknown"
    status = _pick(value, "status", "state", "phase", "result")
    if isinstance(status, Mapping):
        status = _pick(status, "status", "state")
    if not isinstance(status, str):
        if _truth(value.get("terminal")):
            return "terminal"
        return "unknown"
    return status.strip().lower()


def _outcome_of(value: Mapping[str, Any], *, cancellation_requested: bool = False) -> str | None:
    raw = _pick(value, "outcome", "terminal_outcome", "result")
    if isinstance(raw, Mapping):
        raw = _pick(raw, "outcome", "status", "state")
    if isinstance(raw, str):
        raw = raw.strip().lower()
        if raw in _OUTCOMES:
            return raw
        if raw in {"complete", "completed", "success", "ok", "exited", "terminated"}:
            return "cancelled" if cancellation_requested and raw in {"exited", "terminated"} else "succeeded"
        if raw in {"error", "failure", "failed"}:
            return "failed"
    status = _status_of(value)
    if status in _OUTCOMES:
        return status
    if status in {"completed", "complete", "exited", "terminated"}:
        return "cancelled" if cancellation_requested and status in {"exited", "terminated"} else "succeeded"
    return None


def _strip_response(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return copy.deepcopy(dict(value))
    return None


def _extract_release_evidence(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    for name in ("release_evidence", "release_receipt", "release", "evidence"):
        candidate = value.get(name)
        if isinstance(candidate, Mapping):
            return copy.deepcopy(dict(candidate))
    # A runner may return the evidence itself rather than wrapping it.
    binding_names = {"operation_id", "resource_id", "job_id", "thread_id", "owner", "generation", "payload_hash"}
    if binding_names.intersection(value):
        return copy.deepcopy(dict(value))
    return None


def _error_text(exc: BaseException) -> str:
    # Exception text belongs in a durable receipt for diagnosis, but cap it so
    # an adapter cannot turn the ledger into an unbounded log sink.
    text = f"{type(exc).__name__}: {exc}"
    return text[:1000]


class _ExclusiveLock:
    """Small cross-process exclusive lock for the controller's entire pump."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path = path
        self.timeout = max(0.0, float(timeout))
        self.handle: Any = None

    def __enter__(self) -> "_ExclusiveLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+b")
        if self.handle.seek(0, os.SEEK_END) == 0:
            self.handle.write(b"0")
            self.handle.flush()
        self.handle.seek(0)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if msvcrt is not None and os.name == "nt":
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:  # pragma: no cover - Windows is the production target
                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (OSError, IOError) as exc:
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise TimeoutError("resource_queue_controller_lock_timeout") from exc
                time.sleep(0.01)

    def __exit__(self, *_args: Any) -> None:
        if self.handle is None:
            return
        try:
            self.handle.seek(0)
            if msvcrt is not None and os.name == "nt":
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - Windows is the production target
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


class Queue:
    """SQLite-backed operation ledger for independently leased resources."""

    _process_locks: dict[str, threading.RLock] = {}
    _process_locks_guard = threading.Lock()

    def __init__(self, directory: str | os.PathLike[str], now: Callable[[], float] | float = time.time):
        target = Path(directory).expanduser()
        if target.suffix.lower() in {".sqlite", ".sqlite3", ".db"}:
            self.directory = target.parent.resolve()
            self.db_path = target.resolve()
        else:
            self.directory = target.resolve()
            self.db_path = self.directory / "state.sqlite3"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.directory / "resource_queue.lock"
        self.now = now
        self._init_db()

        with self._process_locks_guard:
            key = str(self.db_path)
            self._local_lock = self._process_locks.setdefault(key, threading.RLock())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS resources (
                    resource_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    dedicated_thread_id TEXT,
                    attestation_json TEXT NOT NULL,
                    attestation_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    operation_id TEXT PRIMARY KEY,
                    operation_key TEXT NOT NULL UNIQUE,
                    job_id TEXT NOT NULL UNIQUE,
                    resource_id TEXT NOT NULL REFERENCES resources(resource_id),
                    project TEXT NOT NULL,
                    input_json TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    procedure_json TEXT NOT NULL,
                    procedure_hash TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    resource_attestation_hash TEXT NOT NULL,
                    device_identity TEXT NOT NULL,
                    dependencies_json TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    enqueue_seq INTEGER NOT NULL UNIQUE,
                    review_ref TEXT,
                    reviewed INTEGER NOT NULL DEFAULT 0,
                    reviewed_input_hash TEXT,
                    reviewed_procedure_hash TEXT,
                    queue_state TEXT NOT NULL,
                    substate TEXT,
                    blocker_code TEXT,
                    blocker_json TEXT,
                    owner TEXT,
                    generation INTEGER,
                    owner_pid INTEGER,
                    owner_create_time REAL,
                    lease_until REAL,
                    last_heartbeat_at REAL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    cancel_reason TEXT,
                    cancel_sent INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    outcome TEXT,
                    terminal_at REAL,
                    last_receipt_id TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS leases (
                    resource_id TEXT PRIMARY KEY REFERENCES resources(resource_id),
                    operation_id TEXT NOT NULL UNIQUE REFERENCES tasks(operation_id),
                    owner TEXT NOT NULL,
                    generation INTEGER NOT NULL,
                    owner_pid INTEGER,
                    owner_create_time REAL,
                    acquired_at REAL NOT NULL,
                    lease_until REAL NOT NULL,
                    last_heartbeat_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    receipt_id TEXT PRIMARY KEY,
                    operation_id TEXT NOT NULL REFERENCES tasks(operation_id),
                    kind TEXT NOT NULL,
                    attempt INTEGER,
                    created_at REAL NOT NULL,
                    owner TEXT,
                    generation INTEGER,
                    job_id TEXT,
                    payload_hash TEXT,
                    outcome TEXT,
                    data_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS receipts_operation_idx
                    ON receipts(operation_id, created_at, receipt_id);
                CREATE INDEX IF NOT EXISTS tasks_ready_idx
                    ON tasks(queue_state, priority DESC, enqueue_seq ASC);
                CREATE UNIQUE INDEX IF NOT EXISTS resources_dedicated_thread_unique
                    ON resources(dedicated_thread_id)
                    WHERE dedicated_thread_id IS NOT NULL;
                """
            )
            # Lightweight forward migration for ledgers created by an earlier
            # development revision.  SQLite has no IF NOT EXISTS form for
            # ALTER TABLE, so inspect the columns before adding each one.
            for table, column, sql in (
                ("tasks", "owner_pid", "ALTER TABLE tasks ADD COLUMN owner_pid INTEGER"),
                ("tasks", "owner_create_time", "ALTER TABLE tasks ADD COLUMN owner_create_time REAL"),
                ("tasks", "last_heartbeat_at", "ALTER TABLE tasks ADD COLUMN last_heartbeat_at REAL"),
                ("tasks", "resource_attestation_hash", "ALTER TABLE tasks ADD COLUMN resource_attestation_hash TEXT"),
                ("tasks", "device_identity", "ALTER TABLE tasks ADD COLUMN device_identity TEXT"),
                ("leases", "owner_pid", "ALTER TABLE leases ADD COLUMN owner_pid INTEGER"),
                ("leases", "owner_create_time", "ALTER TABLE leases ADD COLUMN owner_create_time REAL"),
                ("leases", "last_heartbeat_at", "ALTER TABLE leases ADD COLUMN last_heartbeat_at REAL"),
            ):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                if column not in columns:
                    conn.execute(sql)
            conn.executescript(
                """
                CREATE TRIGGER IF NOT EXISTS receipts_append_only_update
                BEFORE UPDATE ON receipts BEGIN
                    SELECT RAISE(ABORT, 'receipts_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS receipts_append_only_delete
                BEFORE DELETE ON receipts BEGIN
                    SELECT RAISE(ABORT, 'receipts_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS tasks_immutable_snapshot
                BEFORE UPDATE OF operation_id,operation_key,job_id,resource_id,project,input_json,input_hash,
                    procedure_json,procedure_hash,payload_json,payload_hash,resource_attestation_hash,device_identity,
                    dependencies_json,review_ref,created_at
                ON tasks BEGIN
                    SELECT RAISE(ABORT, 'task_snapshot_immutable');
                END;
                CREATE VIEW IF NOT EXISTS attempt_receipts AS SELECT * FROM receipts;
                CREATE TRIGGER IF NOT EXISTS attempt_receipts_update_block
                INSTEAD OF UPDATE ON attempt_receipts BEGIN
                    SELECT RAISE(ABORT, 'receipts_append_only');
                END;
                CREATE TRIGGER IF NOT EXISTS attempt_receipts_delete_block
                INSTEAD OF DELETE ON attempt_receipts BEGIN
                    SELECT RAISE(ABORT, 'receipts_append_only');
                END;
                """
            )
            self._migrate_executor_bindings(conn)
        finally:
            conn.close()

    def _migrate_executor_bindings(self, conn: sqlite3.Connection) -> None:
        """Add executor bindings and per-task snapshots without touching history.

        Everything runs in one ``BEGIN IMMEDIATE`` transaction, re-checked under
        the write lock, so a failure leaves the old schema intact and two
        processes cannot both migrate.  Existing resources receive a generation-1
        binding that records their current dedicated thread as the legacy
        ``codex`` executor, and existing tasks snapshot that binding; no receipt
        or task payload is rewritten.
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            if "executor_generation" not in {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS executor_bindings ("
                    "binding_id TEXT PRIMARY KEY,"
                    "resource_id TEXT NOT NULL REFERENCES resources(resource_id),"
                    "generation INTEGER NOT NULL,"
                    "provider TEXT NOT NULL,"
                    "session_id TEXT,"
                    "profile_json TEXT NOT NULL,"
                    "kind TEXT NOT NULL,"
                    "previous_generation INTEGER,"
                    "previous_provider TEXT,"
                    "previous_session_id TEXT,"
                    "reason TEXT NOT NULL,"
                    "requested_by TEXT,"
                    "created_at REAL NOT NULL,"
                    "created_at_utc TEXT NOT NULL,"
                    "receipt_json TEXT NOT NULL,"
                    "UNIQUE(resource_id, generation))"
                )
                for event in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS executor_bindings_append_only_{event.lower()} "
                        f"BEFORE {event} ON executor_bindings BEGIN SELECT RAISE(ABORT, 'executor_bindings_append_only'); END"
                    )
                for column in ("executor_provider TEXT", "executor_session_id TEXT", "executor_generation INTEGER"):
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {column}")
                # The old immutable-snapshot trigger does not list the new columns,
                # and the backfill below must be allowed to set them once.
                conn.execute("DROP TRIGGER IF EXISTS tasks_immutable_snapshot")
                for resource in conn.execute("SELECT * FROM resources").fetchall():
                    thread = resource["dedicated_thread_id"]
                    self._insert_binding_tx(
                        conn, resource["resource_id"], 1, _LEGACY_PROVIDER if thread else _NO_PROVIDER, thread, {},
                        kind="legacy_backfill", stamp=resource["created_at"],
                        reason="schema migration: recorded the existing dedicated thread as the generation-1 executor",
                    )
                conn.execute(
                    "UPDATE tasks SET "
                    "executor_provider=(SELECT provider FROM executor_bindings b WHERE b.resource_id=tasks.resource_id AND b.generation=1),"
                    "executor_session_id=(SELECT session_id FROM executor_bindings b WHERE b.resource_id=tasks.resource_id AND b.generation=1),"
                    "executor_generation=1"
                )
                conn.execute(
                    "CREATE TRIGGER tasks_immutable_snapshot "
                    "BEFORE UPDATE OF operation_id,operation_key,job_id,resource_id,project,input_json,input_hash,"
                    "procedure_json,procedure_hash,payload_json,payload_hash,resource_attestation_hash,device_identity,"
                    "dependencies_json,review_ref,created_at,executor_provider,executor_session_id,executor_generation "
                    "ON tasks BEGIN SELECT RAISE(ABORT, 'task_snapshot_immutable'); END"
                )
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

    @staticmethod
    def _insert_binding_tx(
        conn: sqlite3.Connection,
        resource_id: str,
        generation: int,
        provider: str,
        session_id: str | None,
        profile: Mapping[str, Any],
        *,
        kind: str,
        stamp: float,
        reason: str,
        previous: Mapping[str, Any] | None = None,
        requested_by: str | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> str:
        binding_id = _uuid()
        utc = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        conn.execute(
            "INSERT INTO executor_bindings(binding_id,resource_id,generation,provider,session_id,profile_json,kind,"
            "previous_generation,previous_provider,previous_session_id,reason,requested_by,created_at,created_at_utc,receipt_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                binding_id, resource_id, generation, provider, session_id, _canonical(dict(profile)), kind,
                previous["generation"] if previous else None, previous["provider"] if previous else None,
                previous["session_id"] if previous else None, reason, requested_by, stamp, utc,
                _canonical(dict(receipt or {})),
            ),
        )
        return binding_id

    @staticmethod
    def _binding_view(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "binding_id": row["binding_id"],
            "generation": row["generation"],
            "provider": row["provider"],
            "session_id": row["session_id"],
            "profile": _decode(row["profile_json"], {}),
            "kind": row["kind"],
            "previous_generation": row["previous_generation"],
            "previous_provider": row["previous_provider"],
            "previous_session_id": row["previous_session_id"],
            "reason": row["reason"],
            "requested_by": row["requested_by"],
            "created_at": row["created_at"],
            "created_at_utc": row["created_at_utc"],
            "receipt": _decode(row["receipt_json"], {}),
        }

    @staticmethod
    def _current_binding(conn: sqlite3.Connection, resource_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM executor_bindings WHERE resource_id=? ORDER BY generation DESC LIMIT 1", (resource_id,)
        ).fetchone()

    @staticmethod
    def _session_in_use(conn: sqlite3.Connection, session_id: str, resource_id: str) -> bool:
        """Whether another resource's current executor already owns ``session_id``."""
        return conn.execute(
            "SELECT 1 FROM executor_bindings b WHERE b.session_id=? AND b.resource_id!=? AND b.generation="
            "(SELECT MAX(generation) FROM executor_bindings WHERE resource_id=b.resource_id) LIMIT 1",
            (session_id, resource_id),
        ).fetchone() is not None

    @contextlib.contextmanager
    def _transaction(self) -> Iterable[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextlib.contextmanager
    def controller_lock(self) -> Iterable[None]:
        """Hold the process and OS lock used by one complete controller pump."""
        with self._local_lock:
            with _ExclusiveLock(self.lock_path):
                yield

    def _time(self) -> float:
        return _now_value(self.now)

    # ---- public resource lifecycle -------------------------------------

    def register_resource(
        self,
        resource_id: str,
        kind: str,
        dedicated_thread_id: str | None = None,
        initial_attestation: Mapping[str, Any] | None = None,
        initial_release_evidence: Mapping[str, Any] | None = None,
        *,
        attestation: Mapping[str, Any] | None = None,
        initial_release: Mapping[str, Any] | None = None,
        **_kwargs: Any,
    ) -> dict[str, Any]:
        """Register a phone/GPU after an explicit idle/release attestation.

        Existing identical registrations are idempotent.  Changing the kind or
        dedicated thread is a user conflict and cannot take HOME of a live
        resource.  The attestation is an adapter-owned fact; the queue stores
        it and never invents one.
        """
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("invalid_resource_id")
        resource_id = resource_id.strip()
        if not isinstance(kind, str) or kind.lower() not in _KINDS:
            raise ValueError("invalid_resource_kind")
        kind = kind.lower()
        if dedicated_thread_id is not None and (
            not isinstance(dedicated_thread_id, str) or not dedicated_thread_id.strip()
        ):
            raise ValueError("invalid_dedicated_thread_id")
        if kind == "phone" and dedicated_thread_id is None:
            raise ValueError("phone_dedicated_thread_required")
        supplied = [x for x in (initial_attestation, initial_release_evidence, attestation, initial_release) if x is not None]
        if len(supplied) > 1:
            # Different aliases with the same object are harmless; distinct
            # values make the call ambiguous and must not silently win.
            canonical = {_canonical(x) for x in supplied}
            if len(canonical) > 1:
                raise ValueError("multiple_initial_attestations")
        evidence = supplied[0] if supplied else None
        if evidence is None:
            raise ValueError("initial_attestation_required")
        attestation_data = self._validate_initial_attestation(resource_id, kind, evidence)
        stamp = self._time()
        encoded = _canonical(attestation_data)
        attestation_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
            if row is None:
                if dedicated_thread_id is not None and self._session_in_use(conn, dedicated_thread_id, resource_id):
                    raise OperationConflict("resource_dedicated_thread_conflict")
                try:
                    conn.execute(
                        "INSERT INTO resources(resource_id,kind,dedicated_thread_id,attestation_json,attestation_hash,generation,created_at,updated_at) "
                        "VALUES(?,?,?,?,?,0,?,?)",
                        (resource_id, kind, dedicated_thread_id, encoded, attestation_hash, stamp, stamp),
                    )
                except sqlite3.IntegrityError as exc:
                    raise OperationConflict("resource_dedicated_thread_conflict") from exc
                self._insert_binding_tx(
                    conn, resource_id, 1, _LEGACY_PROVIDER if dedicated_thread_id else _NO_PROVIDER, dedicated_thread_id, {},
                    kind="registration", stamp=stamp, reason="resource registration",
                )
            else:
                if row["kind"] != kind or row["dedicated_thread_id"] != dedicated_thread_id:
                    raise OperationConflict("resource_identity_conflict")
                old_attestation = _decode(row["attestation_json"], {})
                old_device_identity = old_attestation.get("device_identity")
                new_device_identity = attestation_data["device_identity"]
                if old_device_identity != new_device_identity:
                    nonterminal = conn.execute(
                        "SELECT 1 FROM tasks WHERE resource_id=? AND queue_state!='terminal' LIMIT 1",
                        (resource_id,),
                    ).fetchone()
                    if nonterminal is not None:
                        raise OperationConflict("resource_device_identity_conflict")
                # A stable device may publish a refreshed observation while a
                # task is running.  Tasks retain their own immutable identity
                # and attestation hash snapshot.
                if row["attestation_hash"] != attestation_hash:
                    conn.execute(
                        "UPDATE resources SET attestation_json=?,attestation_hash=?,updated_at=? WHERE resource_id=?",
                        (encoded, attestation_hash, stamp, resource_id),
                    )
            return self._resource_projection(conn, resource_id)

    @staticmethod
    def _validate_initial_attestation(resource_id: str, kind: str, value: Mapping[str, Any]) -> dict[str, Any]:
        data = _as_json_object(value, "initial_attestation")
        device_identity = _pick(data, "device_identity", "device_id", "physical_identity")
        if not isinstance(device_identity, str) or not device_identity.strip():
            raise ValueError("initial_device_identity_required")
        data["device_identity"] = device_identity.strip()
        bound = _pick(data, "resource_id", "id")
        if bound is not None and bound != resource_id:
            raise ValueError("initial_attestation_resource_mismatch")
        attested_kind = _pick(data, "kind", "resource_kind")
        if attested_kind is not None and str(attested_kind).lower() != kind:
            raise ValueError("initial_attestation_kind_mismatch")
        state = str(_pick(data, "state", "status", "device_state") or "").lower()
        idle = _truth(_pick(data, "idle", "released", "verified_idle")) or state in {"idle", "released", "available"}
        if not idle:
            raise ValueError("initial_resource_not_idle")
        if kind == "phone":
            # Initial registration may use a concise attestation, but if any
            # cleanup fields are supplied they must affirm release.
            for name in ("input_released", "screen_released", "capture_stopped", "capture_flushed"):
                if name in data and not _truth(data[name]):
                    raise ValueError("initial_phone_release_not_verified")
        if kind == "gpu" and "device_idle" in data and not _truth(data["device_idle"]):
            raise ValueError("initial_gpu_not_idle")
        data.setdefault("resource_id", resource_id)
        data.setdefault("kind", kind)
        data.setdefault("state", "idle")
        data.setdefault("idle", True)
        return data

    def _resource_projection(self, conn: sqlite3.Connection, resource_id: str) -> dict[str, Any]:
        row = conn.execute("SELECT * FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
        if row is None:
            raise QueueNotFound("resource_not_found")
        lease = conn.execute("SELECT * FROM leases WHERE resource_id=?", (resource_id,)).fetchone()
        active_task = None
        if lease is not None:
            active_task = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (lease["operation_id"],)).fetchone()
        resource_state = "idle"
        if lease is not None:
            if active_task is not None and active_task["substate"] == "unknown":
                resource_state = "blocked"
            elif active_task is not None and active_task["cancel_requested"]:
                resource_state = "cancel_requested"
            else:
                resource_state = "busy"
        blocker = _decode(active_task["blocker_json"], None) if active_task is not None else None
        latest = conn.execute(
            "SELECT r.* FROM receipts r JOIN tasks t ON t.operation_id=r.operation_id "
            "WHERE t.resource_id=? ORDER BY r.created_at DESC,r.rowid DESC LIMIT 1",
            (resource_id,),
        ).fetchone()
        last_receipt = self._receipt_projection(latest)
        binding = self._binding_view(self._current_binding(conn, resource_id))
        history = [
            {key: item[key] for key in ("generation", "provider", "session_id", "kind", "reason", "created_at_utc")}
            for item in (
                self._binding_view(entry)
                for entry in conn.execute("SELECT * FROM executor_bindings WHERE resource_id=? ORDER BY generation", (resource_id,))
            )
        ]
        return {
            "resource_id": row["resource_id"],
            "kind": row["kind"],
            # The current executor's session (the legacy dedicated thread until a
            # rebind); the immutable registration identity stays separate.
            "dedicated_thread_id": binding["session_id"] if binding is not None else row["dedicated_thread_id"],
            "registered_dedicated_thread_id": row["dedicated_thread_id"],
            "executor": binding,
            "executor_history": history,
            "generation": row["generation"],
            "attestation": _decode(row["attestation_json"], {}),
            "attestation_hash": row["attestation_hash"],
            "device_identity": _decode(row["attestation_json"], {}).get("device_identity"),
            "state": resource_state,
            "blocker_code": active_task["blocker_code"] if active_task is not None else None,
            "blocker": blocker,
            "last_receipt": last_receipt,
            "last_receipt_ref": latest["receipt_id"] if latest is not None else None,
            "active_operation": lease["operation_id"] if lease is not None else None,
            "active_operation_id": lease["operation_id"] if lease is not None else None,
            "active_job_id": self._job_for_operation(conn, lease["operation_id"]) if lease is not None else None,
            "owner": lease["owner"] if lease is not None else None,
            "owner_pid": lease["owner_pid"] if lease is not None else None,
            "owner_create_time": lease["owner_create_time"] if lease is not None else None,
            "active_generation": lease["generation"] if lease is not None else None,
            "lease_generation": lease["generation"] if lease is not None else None,
            "lease_until": lease["lease_until"] if lease is not None else None,
            "last_heartbeat_at": lease["last_heartbeat_at"] if lease is not None else None,
            "heartbeat_at": lease["last_heartbeat_at"] if lease is not None else None,
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _job_for_operation(conn: sqlite3.Connection, operation_id: str) -> str | None:
        row = conn.execute("SELECT job_id FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
        return row["job_id"] if row is not None else None

    # ---- public task lifecycle -----------------------------------------

    def enqueue(
        self,
        operation_id: str,
        resource_id: str,
        project: str,
        input: Any = None,
        procedure: Any = None,
        launch: Any = None,
        dependencies: Iterable[str] | None = None,
        priority: int = 0,
        review_ref: str | None = None,
        *,
        request: Any = None,
        payload: Any = None,
    ) -> dict[str, Any]:
        """Insert an immutable operation or return its existing projection.

        ``launch``, ``request`` and ``payload`` are aliases because adapters
        use all three names.  Exactly one non-``None`` value is retained.
        Re-enqueueing an operation id with a changed immutable value raises
        :class:`OperationConflict`; a new Gateway job key is never minted.
        """
        if not isinstance(operation_id, str) or not operation_id.strip():
            raise ValueError("invalid_operation_id")
        operation_id = operation_id.strip()
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("invalid_resource_id")
        resource_id = resource_id.strip()
        if not isinstance(project, str):
            raise ValueError("invalid_project")
        project = project.strip()
        if not project:
            raise ValueError("invalid_project")
        priority = _safe_int(priority, name="priority")
        if review_ref is not None and (not isinstance(review_ref, str) or not review_ref.strip()):
            raise ValueError("invalid_review_ref")
        review_ref = review_ref.strip() if isinstance(review_ref, str) else None
        deps = _normalise_dependencies(dependencies, operation_id)
        candidates = [("launch", launch), ("request", request), ("payload", payload)]
        provided = [(name, value) for name, value in candidates if value is not None]
        if len(provided) > 1:
            if len({_canonical(value) for _, value in provided}) > 1:
                raise ValueError("multiple_payload_aliases")
        payload_value = provided[0][1] if provided else {}
        input_json = _canonical(input)
        procedure_json = _canonical(procedure)
        payload_json = _canonical(payload_value)
        hashes = {
            "input_hash": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            "procedure_hash": hashlib.sha256(procedure_json.encode("utf-8")).hexdigest(),
            "payload_hash": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
        }
        operation_key = _operation_key(operation_id)
        job_id = stable_job_id(operation_key)
        stamp = self._time()
        with self._transaction() as conn:
            resource = conn.execute("SELECT attestation_hash,attestation_json FROM resources WHERE resource_id=?", (resource_id,)).fetchone()
            if resource is None:
                raise QueueNotFound("resource_not_found")
            resource_attestation = _decode(resource["attestation_json"], {})
            device_identity = resource_attestation.get("device_identity")
            if not isinstance(device_identity, str) or not device_identity:
                raise QueueError("resource_device_identity_missing")
            old = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            if old is not None:
                immutable_matches = (
                    old["resource_id"] == resource_id
                    and old["project"] == project
                    and old["input_hash"] == hashes["input_hash"]
                    and old["procedure_hash"] == hashes["procedure_hash"]
                    and old["payload_hash"] == hashes["payload_hash"]
                    and _decode(old["dependencies_json"], []) == deps
                    and old["review_ref"] == review_ref
                    and old["operation_key"] == operation_key
                    and old["job_id"] == job_id
                )
                if not immutable_matches:
                    # Explicitly preserve a user conflict in the ledger when
                    # possible, while refusing to mutate the original row.
                    raise OperationConflict("operation_payload_conflict")
                return self._task_projection(conn, operation_id)
            binding = self._current_binding(conn, resource_id)
            if binding is None:
                raise QueueError("resource_executor_binding_missing")
            # The task snapshots the executor it was prepared for.  A launch for
            # the other provider is never queued under this binding.
            launch_route = payload_value.get("route") if isinstance(payload_value, Mapping) else None
            if (launch_route == CLAUDE_ROUTE) != (binding["provider"] == CLAUDE_PROVIDER):
                raise QueueError("executor_launch_route_mismatch")
            seq_row = conn.execute("SELECT COALESCE(MAX(enqueue_seq),0)+1 AS seq FROM tasks").fetchone()
            sequence = int(seq_row["seq"])
            reviewed = 0
            initial_state = "ready" if reviewed and not deps else "blocked"
            blocker = None
            blocker_detail: dict[str, Any] | None = None
            if review_ref is not None:
                blocker = "review_required"
                blocker_detail = {"review_ref": review_ref}
            else:
                blocker = "review_required"
                blocker_detail = {"review_ref": None}
            conn.execute(
                "INSERT INTO tasks(operation_id,operation_key,job_id,resource_id,project,input_json,input_hash,procedure_json,procedure_hash," \
                "payload_json,payload_hash,resource_attestation_hash,device_identity,dependencies_json,priority,enqueue_seq,review_ref,reviewed,queue_state,substate,blocker_code,blocker_json," \
                "created_at,updated_at,executor_provider,executor_session_id,executor_generation) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    operation_id,
                    operation_key,
                    job_id,
                    resource_id,
                    project,
                    input_json,
                    hashes["input_hash"],
                    procedure_json,
                    hashes["procedure_hash"],
                    payload_json,
                    hashes["payload_hash"],
                    resource["attestation_hash"],
                    device_identity,
                    _canonical(deps),
                    priority,
                    sequence,
                    review_ref,
                    reviewed,
                    initial_state,
                    None,
                    blocker,
                    _canonical(blocker_detail) if blocker_detail is not None else None,
                    stamp,
                    stamp,
                    binding["provider"],
                    binding["session_id"],
                    binding["generation"],
                ),
            )
            return self._task_projection(conn, operation_id)

    def review(
        self,
        operation_id: str,
        input_hash: str,
        procedure_hash: str,
        review_ref: str | None = None,
    ) -> dict[str, Any]:
        """Pin review hashes and recompute readiness without model inference."""
        if not isinstance(input_hash, str) or not isinstance(procedure_hash, str):
            raise ValueError("invalid_review_hash")
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise QueueNotFound("operation_not_found")
            if input_hash != row["input_hash"] or procedure_hash != row["procedure_hash"]:
                conn.execute(
                    "UPDATE tasks SET queue_state='blocked',blocker_code='user_conflict',blocker_json=?,updated_at=? WHERE operation_id=? AND queue_state!='terminal'",
                    (_canonical({"reason": "review_hash_mismatch"}), self._time(), operation_id),
                )
                raise OperationConflict("review_hash_mismatch")
            expected = row["review_ref"]
            supplied_ref = review_ref if review_ref is not None else expected
            if expected is not None and supplied_ref != expected:
                conn.execute(
                    "UPDATE tasks SET queue_state='blocked',blocker_code='user_conflict',blocker_json=?,updated_at=? WHERE operation_id=? AND queue_state!='terminal'",
                    (_canonical({"reason": "review_ref_mismatch", "expected": expected}), self._time(), operation_id),
                )
                raise OperationConflict("review_ref_mismatch")
            if row["queue_state"] == "terminal":
                return self._task_projection(conn, operation_id)
            stamp = self._time()
            conn.execute(
                "UPDATE tasks SET reviewed=1,reviewed_input_hash=?,reviewed_procedure_hash=?,queue_state=CASE WHEN queue_state='blocked' THEN 'ready' ELSE queue_state END," \
                "blocker_code=NULL,blocker_json=NULL,updated_at=? WHERE operation_id=?",
                (input_hash, procedure_hash, stamp, operation_id),
            )
            self._reconcile_readiness_tx(conn, operation_id, stamp)
            self._insert_receipt_tx(
                conn,
                operation_id,
                "review",
                stamp,
                data={"review_ref": supplied_ref, "input_hash": input_hash, "procedure_hash": procedure_hash},
            )
            return self._task_projection(conn, operation_id)

    def set_priority(self, operation_id: str, priority: int) -> dict[str, Any]:
        priority = _safe_int(priority, name="priority")
        with self._transaction() as conn:
            row = conn.execute("SELECT queue_state FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise QueueNotFound("operation_not_found")
            if row["queue_state"] != "ready" and row["queue_state"] != "blocked":
                raise QueueError("priority_only_queued")
            conn.execute("UPDATE tasks SET priority=?,updated_at=? WHERE operation_id=?", (priority, self._time(), operation_id))
            return self._task_projection(conn, operation_id)

    def cancel(self, operation_id: str, reason: str | None = None) -> dict[str, Any]:
        reason = str(reason) if reason is not None else "cancelled_by_user"
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None:
                raise QueueNotFound("operation_not_found")
            stamp = self._time()
            if row["queue_state"] == "terminal":
                return self._task_projection(conn, operation_id)
            if row["queue_state"] in {"ready", "blocked"}:
                conn.execute(
                    "UPDATE tasks SET queue_state='terminal',substate=NULL,outcome='cancelled',terminal_at=?,cancel_reason=?,blocker_code=NULL,blocker_json=NULL,updated_at=? WHERE operation_id=?",
                    (stamp, reason, stamp, operation_id),
                )
                self._insert_receipt_tx(
                    conn,
                    operation_id,
                    "cancelled",
                    stamp,
                    outcome="cancelled",
                    data={"reason": reason, "queued": True},
                )
                return self._task_projection(conn, operation_id)
            # A running cancellation is durable intent only.  The lease stays
            # in place until the worker proves it released all resources.
            conn.execute(
                "UPDATE tasks SET cancel_requested=1,cancel_reason=?,updated_at=? WHERE operation_id=? AND queue_state='running'",
                (reason, stamp, operation_id),
            )
            self._insert_receipt_tx(
                conn,
                operation_id,
                "cancel_intent",
                stamp,
                owner=row["owner"],
                generation=row["generation"],
                job_id=row["job_id"],
                payload_hash=row["payload_hash"],
                data={"reason": reason},
            )
            return self._task_projection(conn, operation_id)

    def heartbeat(self, operation_id: str, owner: str, generation: int) -> dict[str, Any] | None:
        """Extend a matching lease; expiry itself never releases it."""
        if not isinstance(owner, str):
            return None
        generation = _safe_int(generation, name="generation", minimum=0)
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            lease = conn.execute("SELECT * FROM leases WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or lease is None or row["queue_state"] != "running":
                return None
            if lease["owner"] != owner or lease["generation"] != generation:
                return None
            stamp = self._time()
            until = stamp + 60.0
            conn.execute("UPDATE leases SET lease_until=?,last_heartbeat_at=? WHERE operation_id=? AND owner=? AND generation=?", (until, stamp, operation_id, owner, generation))
            conn.execute("UPDATE tasks SET lease_until=?,last_heartbeat_at=?,updated_at=? WHERE operation_id=?", (until, stamp, stamp, operation_id))
            return self._task_projection(conn, operation_id)

    def rebind_executor(
        self,
        resource_id: str,
        provider: str,
        session_id: str,
        *,
        profile: Mapping[str, Any],
        reason: str,
        expected_generation: int | None = None,
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Move an idle resource to a new executor provider/session.

        The resource, its device identity and every past task, receipt and
        operation id are untouched.  The move appends one immutable binding row
        (the migration receipt: previous provider/session/generation, new
        provider/session/profile/generation, UTC time and reason).  It is refused
        unless the ledger itself shows no lease and no running or unknown
        operation for the resource.  Queued operations prepared for the previous
        binding are not rewritten: they become ``executor_rebind_required`` and
        must be cancelled and enqueued again.  Repeating the identical request
        is a no-op.
        """
        if not isinstance(resource_id, str) or not resource_id.strip():
            raise ValueError("invalid_resource_id")
        resource_id = resource_id.strip()
        if provider not in _REBIND_TARGETS:
            raise ValueError("invalid_executor_provider")
        try:
            session_id = canonical_session_id(session_id)
            profile = normalize_profile(profile)
        except ClaudeExecutorError as exc:
            raise ValueError(exc.code) from None
        # Codex keeps its existing launch/config permission behavior. Claude's
        # acceptEdits mode must not be presented as a Codex permission setting.
        if provider == _LEGACY_PROVIDER and profile["permission_mode"] != "default":
            raise ValueError("invalid_permission_mode")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
            raise ValueError("invalid_rebind_reason")
        if requested_by is not None and (not isinstance(requested_by, str) or not requested_by.strip() or len(requested_by) > 256):
            raise ValueError("invalid_requested_by")
        if expected_generation is not None:
            expected_generation = _safe_int(expected_generation, name="expected_generation", minimum=1)
        stamp = self._time()
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM resources WHERE resource_id=?", (resource_id,)).fetchone() is None:
                raise QueueNotFound("resource_not_found")
            current = self._current_binding(conn, resource_id)
            if current is None:
                raise QueueError("resource_executor_binding_missing")
            if (current["kind"] == "rebind" and current["provider"] == provider and current["session_id"] == session_id
                    and _decode(current["profile_json"], {}) == profile):
                return {"idempotent": True, "binding": self._binding_view(current),
                        "resource": self._resource_projection(conn, resource_id)}
            lease = conn.execute("SELECT operation_id FROM leases WHERE resource_id=?", (resource_id,)).fetchone()
            if lease is not None:
                active = conn.execute("SELECT substate FROM tasks WHERE operation_id=?", (lease["operation_id"],)).fetchone()
                raise QueueError("resource_dispatch_unknown" if active is not None and active["substate"] == "unknown" else "resource_busy")
            if conn.execute("SELECT 1 FROM tasks WHERE resource_id=? AND queue_state='running' LIMIT 1", (resource_id,)).fetchone():
                raise QueueError("resource_busy")
            if expected_generation is not None and expected_generation != current["generation"]:
                raise OperationConflict("executor_generation_conflict")
            if self._session_in_use(conn, session_id, resource_id):
                raise OperationConflict("executor_session_conflict")
            if conn.execute("SELECT 1 FROM executor_bindings WHERE resource_id=? AND session_id=?", (resource_id, session_id)).fetchone():
                raise OperationConflict("executor_session_reused")
            queued = [
                row["operation_id"] for row in conn.execute(
                    "SELECT operation_id FROM tasks WHERE resource_id=? AND queue_state IN ('blocked','ready') ORDER BY enqueue_seq",
                    (resource_id,),
                )
            ]
            generation = int(current["generation"]) + 1
            previous = {"generation": current["generation"], "provider": current["provider"], "session_id": current["session_id"]}
            self._insert_binding_tx(
                conn, resource_id, generation, provider, session_id, profile, kind="rebind", stamp=stamp,
                reason=reason.strip(), previous=previous, requested_by=requested_by.strip() if requested_by else None,
                receipt={"schema": 1, "observed": {"leases": 0, "running_tasks": 0}, "queued_operations_blocked": queued},
            )
            conn.execute("UPDATE resources SET updated_at=? WHERE resource_id=?", (stamp, resource_id))
            self._reconcile_readiness_tx(conn, None, stamp)
            for operation_id in queued:
                self._insert_receipt_tx(
                    conn, operation_id, "executor_binding_stale", stamp,
                    data={"previous": previous, "current": {"generation": generation, "provider": provider, "session_id": session_id}},
                )
            return {"idempotent": False, "binding": self._binding_view(self._current_binding(conn, resource_id)),
                    "resource": self._resource_projection(conn, resource_id)}

    def executor_launch_mode(self, resource_id: str) -> str:
        """``start`` until a task has been queued or claimed under the current binding, then ``resume``."""
        conn = self._connect()
        try:
            current = self._current_binding(conn, resource_id)
            if current is None:
                raise QueueNotFound("resource_not_found")
            used = conn.execute(
                "SELECT 1 FROM tasks WHERE resource_id=? AND executor_generation=? AND NOT (queue_state='terminal' AND attempts=0) LIMIT 1",
                (resource_id, current["generation"]),
            ).fetchone()
            return "resume" if used is not None else "start"
        finally:
            conn.close()

    def latest_terminal_job(self, resource_id: str) -> dict[str, Any] | None:
        """The most recent operation that was claimed and has since reached a terminal state."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT operation_id,job_id,outcome,terminal_at FROM tasks "
                "WHERE resource_id=? AND queue_state='terminal' AND attempts>0 ORDER BY terminal_at DESC,enqueue_seq DESC LIMIT 1",
                (resource_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    # ---- read API -------------------------------------------------------

    def task(self, operation_id: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            return self._task_projection(conn, operation_id)
        finally:
            conn.close()

    def status(self, operation_id: str | None = None, *, resource_id: str | None = None) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            if resource_id is not None:
                return self._resource_projection(conn, resource_id)
            if operation_id is not None:
                return self._task_projection(conn, operation_id)
            tasks = [self._task_projection(conn, row["operation_id"]) for row in conn.execute("SELECT operation_id FROM tasks ORDER BY enqueue_seq")]
            resources = [self._resource_projection(conn, row["resource_id"]) for row in conn.execute("SELECT resource_id FROM resources ORDER BY resource_id")]
            return {"resources": resources, "tasks": tasks}
        finally:
            conn.close()

    def receipts(self, operation_id: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if operation_id is None:
                rows = conn.execute("SELECT * FROM receipts ORDER BY created_at,receipt_id").fetchall()
            else:
                rows = conn.execute("SELECT * FROM receipts WHERE operation_id=? ORDER BY created_at,receipt_id", (operation_id,)).fetchall()
            return [self._receipt_projection(row) for row in rows]
        finally:
            conn.close()

    # ---- controller internals ------------------------------------------

    def _task_projection(self, conn: sqlite3.Connection, operation_id: str) -> dict[str, Any] | None:
        row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
        if row is None:
            return None
        resource = conn.execute(
            "SELECT kind,dedicated_thread_id,attestation_json,attestation_hash FROM resources WHERE resource_id=?",
            (row["resource_id"],),
        ).fetchone()
        payload = _decode(row["payload_json"], {})
        if not isinstance(payload, Mapping):
            payload = {}
        last = conn.execute("SELECT * FROM receipts WHERE operation_id=? ORDER BY created_at DESC, rowid DESC LIMIT 1", (operation_id,)).fetchone()
        # The executor is the task's own snapshot, never the resource's current
        # binding, so a rebind cannot change what an earlier task refers to.
        snapshot = conn.execute(
            "SELECT * FROM executor_bindings WHERE resource_id=? AND generation=?",
            (row["resource_id"], row["executor_generation"]),
        ).fetchone()
        current = self._current_binding(conn, row["resource_id"])
        executor = {
            "provider": row["executor_provider"],
            "session_id": row["executor_session_id"],
            "generation": row["executor_generation"],
            "profile": _decode(snapshot["profile_json"], {}) if snapshot is not None else {},
            "current_generation": current["generation"] if current is not None else None,
            "binding_current": current is not None and current["generation"] == row["executor_generation"],
        }
        return {
            "operation_id": row["operation_id"],
            "operation_key": row["operation_key"],
            "idempotency_key": row["operation_key"],
            "job_id": row["job_id"],
            "resource_id": row["resource_id"],
            "resource_kind": resource["kind"] if resource is not None else None,
            "dedicated_thread_id": row["executor_session_id"],
            "executor": executor,
            "executor_provider": row["executor_provider"],
            "executor_session_id": row["executor_session_id"],
            "executor_generation": row["executor_generation"],
            "resource_attestation_hash": row["resource_attestation_hash"],
            "attestation_hash": row["resource_attestation_hash"],
            "device_identity": row["device_identity"],
            "project": row["project"],
            "input": _decode(row["input_json"]),
            "procedure": _decode(row["procedure_json"]),
            "payload": payload,
            "launch": payload,
            # Gateway adapters consume a prepared launch mapping directly;
            # expose the operation key as their idempotency key alongside the
            # richer queue projection.
            "op_key": row["operation_key"],
            "input_hash": row["input_hash"],
            "procedure_hash": row["procedure_hash"],
            "payload_hash": row["payload_hash"],
            "request_fingerprint": row["payload_hash"],
            "dependencies": _decode(row["dependencies_json"], []),
            "priority": row["priority"],
            "enqueue_seq": row["enqueue_seq"],
            "review_ref": row["review_ref"],
            "reviewed": bool(row["reviewed"]),
            "reviewed_input_hash": row["reviewed_input_hash"],
            "reviewed_procedure_hash": row["reviewed_procedure_hash"],
            "state": row["queue_state"],
            "queue_state": row["queue_state"],
            "substate": row["substate"],
            "blocker_code": row["blocker_code"],
            "blocker": _decode(row["blocker_json"], None),
            "owner": row["owner"],
            "generation": row["generation"],
            "claim_generation": row["generation"] if row["generation"] is not None else (last["generation"] if last is not None else None),
            "owner_pid": row["owner_pid"],
            "owner_create_time": row["owner_create_time"],
            "lease_until": row["lease_until"],
            "last_heartbeat_at": row["last_heartbeat_at"],
            "cancel_requested": bool(row["cancel_requested"]),
            "cancel_reason": row["cancel_reason"],
            "cancel_sent": bool(row["cancel_sent"]),
            "attempts": row["attempts"],
            "outcome": row["outcome"],
            "terminal_at": row["terminal_at"],
            "last_receipt": self._receipt_projection(last) if last is not None else None,
            "result_receipt_ref": row["last_receipt_id"],
            "release_receipt_ref": last["receipt_id"] if last is not None and last["kind"] == "release" else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _receipt_projection(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "receipt_id": row["receipt_id"],
            "operation_id": row["operation_id"],
            "kind": row["kind"],
            "attempt": row["attempt"],
            "created_at": row["created_at"],
            "owner": row["owner"],
            "generation": row["generation"],
            "job_id": row["job_id"],
            "payload_hash": row["payload_hash"],
            "outcome": row["outcome"],
            "data": _decode(row["data_json"], {}),
        }

    def _insert_receipt_tx(
        self,
        conn: sqlite3.Connection,
        operation_id: str,
        kind: str,
        stamp: float,
        *,
        attempt: int | None = None,
        owner: str | None = None,
        generation: int | None = None,
        job_id: str | None = None,
        payload_hash: str | None = None,
        outcome: str | None = None,
        data: Mapping[str, Any] | None = None,
    ) -> str:
        receipt_id = _uuid()
        encoded = _canonical(dict(data or {}))
        conn.execute(
            "INSERT INTO receipts(receipt_id,operation_id,kind,attempt,created_at,owner,generation,job_id,payload_hash,outcome,data_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (receipt_id, operation_id, kind, attempt, stamp, owner, generation, job_id, payload_hash, outcome, encoded),
        )
        conn.execute("UPDATE tasks SET last_receipt_id=?,updated_at=? WHERE operation_id=?", (receipt_id, stamp, operation_id))
        return receipt_id

    def _reconcile_readiness_tx(self, conn: sqlite3.Connection, operation_id: str | None, stamp: float) -> None:
        query = "SELECT * FROM tasks WHERE queue_state IN ('blocked','ready')"
        params: tuple[Any, ...] = ()
        if operation_id is not None:
            query += " AND operation_id=?"
            params = (operation_id,)
        rows = conn.execute(query, params).fetchall()
        current_bindings: dict[str, sqlite3.Row | None] = {}
        for row in rows:
            if row["blocker_code"] in {"user_conflict", "manual_block"}:
                continue
            deps = _decode(row["dependencies_json"], [])
            if row["resource_id"] not in current_bindings:
                current_bindings[row["resource_id"]] = self._current_binding(conn, row["resource_id"])
            current = current_bindings[row["resource_id"]]
            if current is not None and row["executor_generation"] != current["generation"]:
                # Prepared for an earlier executor: never rewritten or reused.
                # Cancel it and enqueue a new operation prepared for the
                # current binding.
                state, code, detail = "blocked", _STALE_BINDING_CODE, {
                    "task_executor": {
                        "provider": row["executor_provider"],
                        "session_id": row["executor_session_id"],
                        "generation": row["executor_generation"],
                    },
                    "current_executor": {
                        "provider": current["provider"],
                        "session_id": current["session_id"],
                        "generation": current["generation"],
                    },
                    "action": "cancel_and_reprepare",
                }
            elif not row["reviewed"]:
                state, code, detail = "blocked", "review_required", {"review_ref": row["review_ref"]}
            else:
                missing: list[str] = []
                pending: list[str] = []
                failed: list[str] = []
                for dependency in deps:
                    dep = conn.execute("SELECT queue_state,outcome FROM tasks WHERE operation_id=?", (dependency,)).fetchone()
                    if dep is None:
                        missing.append(dependency)
                    elif dep["queue_state"] != "terminal":
                        pending.append(dependency)
                    elif dep["outcome"] != "succeeded":
                        failed.append(dependency)
                if missing:
                    state, code, detail = "blocked", "dependency_missing", {"dependencies": missing}
                elif failed:
                    state, code, detail = "blocked", "dependency_failed", {"dependencies": failed}
                elif pending:
                    state, code, detail = "blocked", "dependency_pending", {"dependencies": pending}
                else:
                    state, code, detail = "ready", None, None
            conn.execute(
                "UPDATE tasks SET queue_state=?,blocker_code=?,blocker_json=?,updated_at=? WHERE operation_id=? AND queue_state IN ('blocked','ready')",
                (state, code, _canonical(detail) if detail is not None else None, stamp, row["operation_id"]),
            )

    def _claim_next(
        self,
        owner: str,
        lease_seconds: float,
        *,
        owner_pid: int | None = None,
        owner_create_time: float | None = None,
    ) -> dict[str, Any] | None:
        stamp = self._time()
        if owner_pid is None:
            owner_pid = os.getpid()
        if owner_create_time is None:
            owner_create_time = _current_process_create_time()
        with self._transaction() as conn:
            self._reconcile_readiness_tx(conn, None, stamp)
            # A task prepared for an earlier executor binding is never claimed
            # (readiness already blocks it; the predicate is the second fence).
            row = conn.execute(
                "SELECT t.* FROM tasks t LEFT JOIN leases l ON l.resource_id=t.resource_id "
                "WHERE t.queue_state='ready' AND l.resource_id IS NULL AND t.executor_generation="
                "(SELECT MAX(generation) FROM executor_bindings b WHERE b.resource_id=t.resource_id) "
                "ORDER BY t.priority DESC,t.enqueue_seq ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            resource = conn.execute("SELECT * FROM resources WHERE resource_id=?", (row["resource_id"],)).fetchone()
            if resource is None:
                return None
            generation = int(resource["generation"]) + 1
            lease_until = stamp + max(0.1, float(lease_seconds))
            attempt = int(row["attempts"]) + 1
            conn.execute("UPDATE resources SET generation=?,updated_at=? WHERE resource_id=?", (generation, stamp, row["resource_id"]))
            conn.execute(
                "INSERT INTO leases(resource_id,operation_id,owner,generation,owner_pid,owner_create_time,acquired_at,lease_until,last_heartbeat_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (row["resource_id"], row["operation_id"], owner, generation, owner_pid, owner_create_time, stamp, lease_until, stamp),
            )
            conn.execute(
                "UPDATE tasks SET queue_state='running',substate='running',owner=?,generation=?,owner_pid=?,owner_create_time=?,lease_until=?,last_heartbeat_at=?,attempts=?,blocker_code=NULL,blocker_json=NULL,updated_at=? WHERE operation_id=? AND queue_state='ready'",
                (owner, generation, owner_pid, owner_create_time, lease_until, stamp, attempt, stamp, row["operation_id"]),
            )
            self._insert_receipt_tx(
                conn,
                row["operation_id"],
                "intent",
                stamp,
                attempt=attempt,
                owner=owner,
                generation=generation,
                job_id=row["job_id"],
                payload_hash=row["payload_hash"],
                data={
                    "state": "claimed", "lease_until": lease_until, "owner_pid": owner_pid, "owner_create_time": owner_create_time,
                    "executor": {
                        "provider": row["executor_provider"],
                        "session_id": row["executor_session_id"],
                        "generation": row["executor_generation"],
                    },
                },
            )
            return self._task_projection(conn, row["operation_id"])

    def _active_tasks(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT operation_id FROM tasks WHERE queue_state='running' ORDER BY enqueue_seq").fetchall()
            return [self._task_projection(conn, row["operation_id"]) for row in rows]
        finally:
            conn.close()

    def _lease_matches(self, operation_id: str, owner: str | None, generation: int | None) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT t.queue_state,t.owner,t.generation,l.owner AS lease_owner,l.generation AS lease_generation "
                "FROM tasks t LEFT JOIN leases l ON l.operation_id=t.operation_id WHERE t.operation_id=?",
                (operation_id,),
            ).fetchone()
            return bool(
                row is not None
                and row["queue_state"] == "running"
                and row["lease_owner"] == owner
                and row["lease_generation"] == generation
                and row["owner"] == owner
                and row["generation"] == generation
            )
        finally:
            conn.close()

    def _record_observation(
        self,
        operation_id: str,
        owner: str,
        generation: int,
        kind: str,
        data: Mapping[str, Any],
        *,
        substate: str | None = None,
        lease_seconds: float | None = None,
    ) -> bool:
        stamp = self._time()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            lease = conn.execute("SELECT * FROM leases WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or lease is None or row["queue_state"] != "running":
                return False
            if lease["owner"] != owner or lease["generation"] != generation:
                return False
            until = lease["lease_until"]
            if lease_seconds is not None:
                until = stamp + max(0.1, float(lease_seconds))
                conn.execute("UPDATE leases SET lease_until=? WHERE operation_id=? AND owner=? AND generation=?", (until, operation_id, owner, generation))
            conn.execute(
                "UPDATE tasks SET substate=?,blocker_code=?,blocker_json=?,lease_until=?,updated_at=? WHERE operation_id=? AND owner=? AND generation=?",
                (
                    substate or row["substate"] or "running",
                    kind if substate == "unknown" else None,
                    _canonical(dict(data)) if substate == "unknown" else None,
                    until,
                    stamp,
                    operation_id,
                    owner,
                    generation,
                ),
            )
            self._insert_receipt_tx(
                conn,
                operation_id,
                kind,
                stamp,
                attempt=row["attempts"],
                owner=owner,
                generation=generation,
                job_id=row["job_id"],
                payload_hash=row["payload_hash"],
                data=dict(data),
            )
            if kind == "cancel_request":
                request_status = _status_of(data)
                request_ok = data.get("ok") is True or request_status in {"requested", "accepted", "acknowledged"}
                conn.execute(
                    "UPDATE tasks SET cancel_sent=? WHERE operation_id=? AND owner=? AND generation=?",
                    (1 if request_ok else 0, operation_id, owner, generation),
                )
            return True

    def _mark_unknown(self, task: Mapping[str, Any], kind: str, data: Mapping[str, Any]) -> bool:
        return self._record_observation(
            str(task["operation_id"]),
            str(task["owner"]),
            int(task["generation"]),
            kind,
            data,
            substate="unknown",
        )

    def _mark_running(self, task: Mapping[str, Any], kind: str, data: Mapping[str, Any]) -> bool:
        return self._record_observation(
            str(task["operation_id"]),
            str(task["owner"]),
            int(task["generation"]),
            kind,
            data,
            substate="running",
            lease_seconds=None,
        )

    def _release_evidence_error(self, task: Mapping[str, Any], reason: str, response: Mapping[str, Any] | None = None) -> bool:
        data: dict[str, Any] = {"reason": reason}
        if response:
            data["response_status"] = _status_of(response)
            if "job_id" in response:
                data["job_id"] = response.get("job_id")
        return self._mark_unknown(task, "release_blocked", data)

    def _finalize(
        self,
        task: Mapping[str, Any],
        evidence: Mapping[str, Any],
        outcome: str,
    ) -> bool:
        stamp = self._time()
        operation_id = str(task["operation_id"])
        owner = str(task["owner"])
        generation = int(task["generation"])
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            lease = conn.execute("SELECT * FROM leases WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or lease is None or row["queue_state"] != "running":
                return False
            if lease["owner"] != owner or lease["generation"] != generation:
                return False
            effective_outcome = "cancelled" if row["cancel_requested"] else outcome
            receipt_data = copy.deepcopy(dict(evidence))
            job_outcome = receipt_data.get("job_outcome", outcome)
            if job_outcome not in _OUTCOMES:
                job_outcome = outcome
            self._insert_receipt_tx(
                conn,
                operation_id,
                "release",
                stamp,
                attempt=row["attempts"],
                owner=owner,
                generation=generation,
                job_id=row["job_id"],
                payload_hash=row["payload_hash"],
                outcome=effective_outcome,
                data={"terminal": True, "job_outcome": job_outcome, "effective_outcome": effective_outcome, "evidence": receipt_data},
            )
            conn.execute(
                "UPDATE tasks SET queue_state='terminal',substate=NULL,owner=NULL,generation=NULL,owner_pid=NULL,owner_create_time=NULL,lease_until=NULL,last_heartbeat_at=NULL,outcome=?,terminal_at=?,updated_at=? WHERE operation_id=?",
                (effective_outcome, stamp, stamp, operation_id),
            )
            conn.execute("DELETE FROM leases WHERE operation_id=? AND owner=? AND generation=?", (operation_id, owner, generation))
            return True

    def _retry_after_reconcile(self, task: Mapping[str, Any], evidence: Mapping[str, Any], lease_seconds: float = 60.0) -> bool:
        """Record a safe same-key resend while retaining the current lease.

        The adapter's explicit ``can_retry_same_key`` response is authoritative
        only for the reservation attempt.  The resource remains fenced to this
        owner/generation throughout the resend; there is no idle window and no
        new operation key.
        """
        stamp = self._time()
        operation_id, owner, generation = str(task["operation_id"]), str(task["owner"]), int(task["generation"])
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE operation_id=?", (operation_id,)).fetchone()
            lease = conn.execute("SELECT * FROM leases WHERE operation_id=?", (operation_id,)).fetchone()
            if row is None or lease is None or row["queue_state"] != "running" or row["cancel_requested"]:
                return False
            if lease["owner"] != owner or lease["generation"] != generation:
                return False
            attempt = int(row["attempts"]) + 1
            lease_until = stamp + max(0.1, float(lease_seconds))
            conn.execute(
                "UPDATE tasks SET attempts=?,substate='running',lease_until=?,last_heartbeat_at=?,updated_at=? WHERE operation_id=? AND owner=? AND generation=?",
                (attempt, lease_until, stamp, stamp, operation_id, owner, generation),
            )
            conn.execute(
                "UPDATE leases SET lease_until=?,last_heartbeat_at=? WHERE operation_id=? AND owner=? AND generation=?",
                (lease_until, stamp, operation_id, owner, generation),
            )
            self._insert_receipt_tx(
                conn,
                operation_id,
                "intent",
                stamp,
                attempt=attempt,
                owner=owner,
                generation=generation,
                job_id=row["job_id"],
                payload_hash=row["payload_hash"],
                data={"retry_same_key": True, "reconcile": copy.deepcopy(dict(evidence)), "lease_until": lease_until},
            )
            return True

    # ---- evidence checks ------------------------------------------------

    def _validate_release_evidence(self, task: Mapping[str, Any], evidence: Mapping[str, Any], outcome: str) -> tuple[bool, str]:
        if not isinstance(evidence, Mapping):
            return False, "release_evidence_missing"
        if evidence.get("user_conflict") is True or evidence.get("state") == "user_conflict":
            return False, "user_conflict"
        expected_device_identity = task.get("device_identity")
        if not isinstance(expected_device_identity, str) or not expected_device_identity:
            return False, "device_identity_missing"
        evidence_device = _pick(evidence, "device_identity", "device_id", "physical_identity")
        device_blob = _pick(evidence, "device", "device_evidence", "release_device")
        if evidence_device is None and isinstance(device_blob, Mapping):
            evidence_device = _pick(device_blob, "device_identity", "device_id", "physical_identity")
        if evidence_device is None and isinstance(evidence.get("release_receipt"), Mapping):
            evidence_device = _pick(evidence["release_receipt"], "device_identity", "device_id", "physical_identity")
        if evidence_device != expected_device_identity:
            return False, "device_identity_mismatch"
        expected = {
            "operation_id": task["operation_id"],
            "resource_id": task["resource_id"],
            "job_id": task["job_id"],
            "thread_id": task.get("dedicated_thread_id"),
            "owner": task["owner"],
            "generation": task["generation"],
            "payload_hash": task["payload_hash"],
        }
        aliases = {
            "operation_id": ("operation_id", "op_id"),
            "resource_id": ("resource_id", "resource"),
            "job_id": ("job_id", "gateway_job_id"),
            "thread_id": ("thread_id", "dedicated_thread_id", "worker_thread_id"),
            "owner": ("owner", "controller_owner"),
            "generation": ("generation", "lease_generation"),
            "payload_hash": ("payload_hash", "request_fingerprint", "payload_digest"),
        }
        for name, wanted in expected.items():
            actual = _pick(evidence, *aliases[name])
            # GPU adapters have no dedicated persistent thread; they must still
            # emit the binding key explicitly with null/empty value.
            if name == "thread_id" and wanted is None and actual == "":
                actual = None
            if actual != wanted:
                return False, f"release_binding_{name}_mismatch"
        # The executor the task was claimed under.  A Claude task must carry it
        # explicitly; older Codex receipts predate it and are only checked if
        # they state it.
        executor = task.get("executor")
        if isinstance(executor, Mapping):
            for name, wanted in (
                ("executor_provider", executor.get("provider")),
                ("executor_session_id", executor.get("session_id")),
                ("executor_generation", executor.get("generation")),
            ):
                if name in evidence:
                    if evidence[name] != wanted:
                        return False, f"release_binding_{name}_mismatch"
                elif executor.get("provider") == CLAUDE_PROVIDER:
                    return False, f"release_binding_{name}_missing"
        evidence_outcome = _pick(evidence, "outcome", "terminal_outcome")
        allowed_evidence_outcomes = {outcome}
        if evidence_outcome is not None and str(evidence_outcome).lower() not in allowed_evidence_outcomes:
            return False, "release_outcome_mismatch"

        process = _pick(evidence, "process", "process_evidence", "process_release")
        if not isinstance(process, Mapping):
            process = {}
            for name in ("process_terminal", "process_identity", "process_identity_verified", "process_create_time", "root_worker", "owned_children", "captures", "captures_stopped", "captures_flushed"):
                if name in evidence:
                    process[name] = evidence[name]
        terminal = _pick(process, "terminal", "process_terminal", "ended", "exited", "terminated")
        state = str(_pick(process, "state", "status") or "").lower()
        if not _truth(terminal) and state not in {"exited", "terminated", "stopped", "idle", "released"}:
            return False, "process_not_terminal"
        identity = _pick(process, "identity", "identity_check", "process_identity")
        identity_verified = _truth(_pick(process, "identity_verified", "verified"))
        create_time = _pick(process, "create_time", "process_create_time", "worker_create_time")
        if isinstance(identity, Mapping):
            identity_verified = identity_verified or _truth(_pick(identity, "verified", "identity_verified", "owned"))
            if create_time is None:
                create_time = _pick(identity, "create_time", "process_create_time")
        elif identity is not None:
            identity_verified = identity_verified or identity in {"verified", "owned", "match"}
        if not identity_verified:
            return False, "process_identity_not_verified"
        if isinstance(create_time, bool) or not isinstance(create_time, (int, float)) or not math.isfinite(float(create_time)):
            return False, "process_create_time_missing"
        root = _pick(process, "root_worker", "root", "worker")
        root_verified = _truth(_pick(process, "root_worker_verified", "root_verified"))
        if isinstance(root, Mapping):
            root_verified = root_verified or _truth(_pick(root, "verified", "owned", "identity_verified"))
            root_create = _pick(root, "create_time", "worker_create_time")
            if root_create is not None and (isinstance(root_create, bool) or not isinstance(root_create, (int, float))):
                return False, "root_worker_create_time_invalid"
        elif root is not None:
            root_verified = root_verified or _truth(root)
        if not root_verified:
            return False, "root_worker_not_verified"
        children = _pick(process, "owned_children", "children", "owned_child_processes")
        if not isinstance(children, list):
            return False, "owned_children_missing"
        captures = _pick(process, "captures", "capture", "capture_evidence")
        if not isinstance(captures, Mapping):
            captures = {}
            for name in ("captures_stopped", "captures_flushed", "capture_stopped", "capture_flushed", "stdout_flushed", "stderr_flushed"):
                if name in process:
                    captures[name] = process[name]
                elif name in evidence:
                    captures[name] = evidence[name]
        stopped = _truth(_pick(captures, "stopped", "capture_stopped", "stop_verified"))
        flushed = _truth(_pick(captures, "flushed", "capture_flushed", "flush_verified", "stdout_flushed"))
        # A process with no capture stream can state captures=[] and still
        # satisfy the flush boundary explicitly.
        if not stopped or not flushed:
            return False, "captures_not_stopped_and_flushed"

        conn = self._connect()
        try:
            resource = conn.execute("SELECT kind FROM resources WHERE resource_id=?", (task["resource_id"],)).fetchone()
        finally:
            conn.close()
        if resource is None:
            return False, "resource_not_found"
        device = _pick(evidence, "device", "device_evidence", "release_device")
        if not isinstance(device, Mapping):
            device = {}
        device_state = str(_pick(device, "state", "status", "device_state") or "").lower()
        idle = _truth(_pick(device, "idle", "released", "device_idle", "gpu_device_idle")) or device_state in {"idle", "released", "available"}
        if not idle:
            return False, "device_not_idle"
        if resource["kind"] == "phone":
            input_part = _pick(device, "input", "input_state")
            screen_part = _pick(device, "screen", "screen_state")
            capture_part = _pick(device, "capture", "captures")
            input_ok = _truth(_pick(device, "input_released", "input_idle", "phone_input_stopped")) or (isinstance(input_part, Mapping) and _truth(_pick(input_part, "released", "idle", "stopped")))
            screen_value = _pick(device, "screen_state", "phone_screen_state")
            screen_observed = isinstance(screen_value, str) and bool(screen_value.strip())
            screen_appropriate = _truth(_pick(device, "screen_appropriate", "screen_released"))
            if isinstance(screen_part, Mapping):
                screen_appropriate = screen_appropriate or _truth(_pick(screen_part, "appropriate", "released", "idle"))
            screen_ok = screen_observed and screen_appropriate
            capture_stopped = _truth(_pick(device, "capture_stopped"))
            capture_flushed = _truth(_pick(device, "capture_flushed"))
            if isinstance(capture_part, Mapping):
                capture_stopped = capture_stopped or _truth(_pick(capture_part, "stopped"))
                capture_flushed = capture_flushed or _truth(_pick(capture_part, "flushed"))
            capture_ok = capture_stopped and capture_flushed
            if not input_ok or not screen_ok or not capture_ok:
                return False, "phone_release_not_verified"
        else:
            child_exit = _pick(device, "children_exited", "child_exit_verified", "gpu_children_exited")
            if not _truth(child_exit):
                return False, "gpu_children_not_exited"
        return True, "ok"


class Controller:
    """The singleton local pump which reconciles, claims, dispatches, and releases."""

    def __init__(self, queue: Queue, runner: Runner, lease_seconds: float = 60, owner: str | None = None):
        self.queue = queue
        self.runner = runner
        self.lease_seconds = max(0.1, float(lease_seconds))
        self.owner = owner or f"rq-controller:{os.getpid()}:{uuid.uuid4()}"
        self._pumping = False

    @staticmethod
    def _await_if_needed(value: Any) -> Any:
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(value)
        # A synchronous pump called from an event loop cannot safely nest
        # asyncio.run.  Run the adapter coroutine on a short-lived helper
        # thread; the runner itself remains the authority for its I/O.
        result: list[Any] = []
        error: list[BaseException] = []

        def run() -> None:
            try:
                result.append(asyncio.run(value))
            except BaseException as exc:  # pragma: no cover - uncommon adapter shape
                error.append(exc)

        thread = threading.Thread(target=run, name="resource-queue-runner", daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        return result[0] if result else None

    def _call(self, name: str, *args: Any) -> Any:
        method = getattr(self.runner, name, None)
        if not callable(method):
            raise AttributeError(f"runner_missing_{name}")
        return self._await_if_needed(method(*args))

    @staticmethod
    def _runner_task(task: Mapping[str, Any]) -> dict[str, Any]:
        """Build the adapter-facing launch snapshot.

        Queue callers store a prepared launch under ``payload``/``launch``;
        Gateway adapters consume command/cwd/op_key at the top level.  Keep
        both views and let queue-owned fence fields win over payload fields.
        """
        result = copy.deepcopy(dict(task))
        launch = task.get("launch", task.get("payload", {}))
        if isinstance(launch, Mapping):
            for key, value in launch.items():
                if key not in {"owner", "generation", "operation_id", "resource_id", "job_id", "payload_hash"}:
                    result.setdefault(key, copy.deepcopy(value))
        result["op_key"] = task["operation_key"]
        result["operation_key"] = task["operation_key"]
        result["job_id"] = task["job_id"]
        # The queue's immutable payload hash is the external Gateway request
        # fingerprint.  Any same-named field inside a launch is descriptive
        # input and cannot override the ledger's pinned value.
        result["dispatcher_request_fingerprint"] = task["payload_hash"]
        result["request_fingerprint"] = task["payload_hash"]
        result["task_id"] = result.get("task_id") or task["operation_id"]
        result["thread_id"] = result.get("thread_id", task.get("dedicated_thread_id"))
        return result

    def _lookup_or_inspect(self, task: Mapping[str, Any]) -> tuple[Any, str | None]:
        runner_task = self._runner_task(task)
        if callable(getattr(self.runner, "lookup", None)):
            method_name = "lookup"
        elif callable(getattr(self.runner, "status", None)):
            # GatewayRunner uses ``status`` for this read-only operation.
            method_name = "status"
        elif callable(getattr(self.runner, "reconcile", None)):
            # ``reconcile`` is an equivalent spelling used by a local adapter.
            method_name = "reconcile"
        else:
            method_name = "inspect"
        if not callable(getattr(self.runner, method_name, None)):
            return None, "runner_missing_lookup_and_inspect"
        if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
            return None, "runner_action_fenced"
        try:
            response = self._call(method_name, runner_task)
        except BaseException as exc:
            return None, _error_text(exc)
        if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
            return None, "runner_action_fenced"
        return response, None

    def _release_candidate(self, task: Mapping[str, Any], response: Mapping[str, Any], outcome: str) -> tuple[dict[str, Any] | None, str | None]:
        candidate = _extract_release_evidence(response)
        if candidate is None:
            # Some adapters expose a separate inspect record after lookup says
            # terminal.  It must still be verified before release.
            if callable(getattr(self.runner, "inspect", None)):
                if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                    return None, "runner_action_fenced"
                try:
                    inspected = self._call("inspect", self._runner_task(task))
                except BaseException as exc:
                    return None, _error_text(exc)
                if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                    return None, "runner_action_fenced"
                candidate = _extract_release_evidence(_strip_response(inspected))
        if candidate is None:
            return None, "release_evidence_missing"
        verifier = getattr(self.runner, "verify_release", None)
        verified: Any = None
        if callable(verifier):
            if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                return None, "runner_action_fenced"
            try:
                verified = self._await_if_needed(verifier(self._runner_task(task), candidate))
            except BaseException as exc:
                return None, _error_text(exc)
            if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                return None, "runner_action_fenced"
            if verified is False or verified is None:
                return None, "runner_release_verification_failed"
            if isinstance(verified, Mapping):
                if verified.get("ok") is False or str(verified.get("status", "")).lower() in {"blocked", "unknown", "error", "failed"}:
                    return None, str(verified.get("error") or "runner_release_verification_failed")
                if verified.get("ok") is not True and str(verified.get("status", "")).lower() not in {"released", "verified", "complete", "completed"}:
                    return None, "runner_release_verification_unconfirmed"
                # A verifier may return a normalised proof.  Merge it over the
                # adapter's raw receipt while preserving the binding fields.
                nested_proof = verified.get("release_evidence")
                if isinstance(nested_proof, Mapping):
                    candidate = {**candidate, **copy.deepcopy(dict(nested_proof))}
                candidate = {**candidate, **copy.deepcopy(dict(verified))}
                # GatewayRunner returns an independently checked identity list
                # and device attestation rather than the queue's generic
                # process shape.  Translate that result without weakening the
                # check: the adapter already proved every identity is absent
                # with its exact pid/create-time pair.
                receipt = verified.get("receipt")
                if isinstance(receipt, Mapping):
                    candidate = {**copy.deepcopy(dict(receipt)), **candidate}
                identities = verified.get("identities")
                if isinstance(identities, list) and identities:
                    root = next((item for item in identities if isinstance(item, Mapping) and item.get("label") == "root"), None)
                    worker = next((item for item in identities if isinstance(item, Mapping) and item.get("label") == "worker"), None)
                    owned = [copy.deepcopy(dict(item)) for item in identities if isinstance(item, Mapping) and item not in (root, worker)]
                    identity = root or worker
                    if isinstance(identity, Mapping):
                        candidate.setdefault("process", {
                            "terminal": True,
                            "identity": {"verified": True, "pid": identity.get("pid"), "create_time": identity.get("create_time")},
                            "root_worker": {"verified": True, **(dict(worker) if isinstance(worker, Mapping) else dict(identity))},
                            "owned_children": owned,
                            "captures": {"stopped": True, "flushed": True},
                        })
                if isinstance(verified.get("device"), Mapping):
                    candidate.setdefault("device", copy.deepcopy(dict(verified["device"])))
        elif callable(getattr(self.runner, "inspect", None)):
            # inspect() was already used above when needed; call it once more
            # only when it yielded no independent release proof.
            if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                return None, "runner_action_fenced"
            try:
                inspected = self._call("inspect", self._runner_task(task))
            except BaseException as exc:
                return None, _error_text(exc)
            if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                return None, "runner_action_fenced"
            inspect_evidence = _extract_release_evidence(_strip_response(inspected))
            if inspect_evidence is None:
                return None, "runner_inspect_release_verification_failed"
            candidate = {**candidate, **inspect_evidence}
        elif callable(getattr(self.runner, "release_probe", None)):
            # A trusted local actor may expose an explicitly named release
            # probe instead of the generic inspect method.  It still has to
            # return a positive probe; no status acknowledgement is enough.
            if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                return None, "runner_action_fenced"
            try:
                probe = self._await_if_needed(self.runner.release_probe(self._runner_task(task)))
            except BaseException as exc:
                return None, _error_text(exc)
            if not self.queue._lease_matches(task["operation_id"], task.get("owner"), task.get("generation")):
                return None, "runner_action_fenced"
            if not isinstance(probe, Mapping) or probe.get("ok") is not True or probe.get("clear") is not True:
                return None, "runner_release_probe_failed"
            candidate = {**candidate, **copy.deepcopy(dict(probe))}
        else:
            return None, "runner_release_verifier_missing"
        # Gateway adapters may shape their release evidence from the durable
        # cancel intent and therefore report ``cancelled`` even when the job
        # had already completed successfully.  Preserve that actor-reported
        # value while normalising the queue proof to the observed job outcome;
        # _finalize applies the effective queue cancellation atomically.
        candidate_outcome = _pick(candidate, "outcome", "terminal_outcome")
        if task.get("cancel_requested") and outcome != "cancelled" and str(candidate_outcome).lower() == "cancelled":
            candidate = {
                **candidate,
                "job_outcome": outcome,
                "release_evidence_outcome": "cancelled",
                "outcome": outcome,
            }
        okay, reason = self.queue._validate_release_evidence(task, candidate, outcome)
        if not okay:
            return None, reason
        return candidate, None

    def _observe(self, task: Mapping[str, Any], response: Any, source: str) -> str:
        data = _strip_response(response)
        # Runner I/O can overlap the public durable cancel call.  Re-read the
        # fenced task before interpreting a response so a stale snapshot can
        # never turn a cancellation race into an unqualified success.
        original_owner = task.get("owner")
        original_generation = task.get("generation")
        current = self.queue.task(str(task["operation_id"]))
        if (
            current is None
            or current.get("state") != "running"
            or current.get("owner") != original_owner
            or current.get("generation") != original_generation
            or not self.queue._lease_matches(current["operation_id"], current.get("owner"), current.get("generation"))
        ):
            return "stale"
        task = current
        if data is None:
            self.queue._mark_unknown(task, source + "_unknown", {"reason": "empty_runner_response"})
            return "unknown"
        status = _status_of(data)
        # Keep the actor's original terminal outcome for receipt provenance;
        # _finalize applies an effective cancelled projection if durable cancel
        # intent won the race.
        outcome = _outcome_of(data, cancellation_requested=False)
        supplied_job_outcome = _pick(data, "job_outcome", "job_terminal_outcome")
        if isinstance(supplied_job_outcome, str) and supplied_job_outcome.lower() in _OUTCOMES:
            outcome = supplied_job_outcome.lower()
        # Evidence may be returned as a bare mapping with an outcome but no
        # status field.  Treat the explicit outcome as terminal before the
        # unknown-status guard.
        if outcome is None and (status in _UNKNOWN_STATUS or status == "" or status == "unknown"):
            self.queue._mark_unknown(task, source + "_unknown", {"status": status or "unknown"})
            return "unknown"
        if status in _RUNNING_STATUS or (outcome is None and not _truth(data.get("terminal"))):
            self.queue._mark_running(task, source, data)
            return "running"
        if outcome is None:
            self.queue._mark_unknown(task, source + "_unknown", {"status": status})
            return "unknown"
        # A supplied job id is immutable and must agree before any evidence is
        # considered.  A missing id is filled from the task's stable id.
        supplied_job = _pick(data, "job_id", "gateway_job_id")
        if supplied_job is not None and supplied_job != task["job_id"]:
            self.queue._release_evidence_error(task, "job_id_mismatch", data)
            return "unknown"
        candidate, reason = self._release_candidate(task, data, outcome)
        if candidate is None:
            self.queue._release_evidence_error(task, reason or "release_evidence_missing", data)
            return "unknown"
        if self.queue._finalize(task, candidate, outcome):
            return "terminal"
        return "stale"

    def _reconcile_active(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        # Snapshot once.  Every response is applied under the owner/generation
        # fence; a stale response can never release a newer lease.
        for task in self.queue._active_tasks():
            if task is None:
                continue
            response, error = self._lookup_or_inspect(task)
            if error is not None:
                self.queue._mark_unknown(task, "lookup_unknown", {"reason": error})
                actions.append({"operation_id": task["operation_id"], "action": "lookup_unknown"})
                continue
            response_data = _strip_response(response)
            if (
                response_data is not None
                and not task.get("cancel_requested")
                and _status_of(response_data) in {"not_found", "missing", "ended_without_receipt"}
                and response_data.get("can_retry_same_key") is True
            ):
                key = _pick(response_data, "operation_key", "op_key", "idempotency_key")
                payload_hash = _pick(response_data, "payload_hash", "request_fingerprint", "payload_digest")
                key_ok = key in (None, task.get("operation_key"))
                payload_ok = payload_hash in (None, task.get("payload_hash"))
                if key_ok and payload_ok and self.queue._retry_after_reconcile(task, response_data, self.lease_seconds):
                    actions.append({"operation_id": task["operation_id"], "action": "retry_same_key"})
                    retry_task = self.queue.task(task["operation_id"])
                    if retry_task is not None:
                        actions.append(self._dispatch_claimed(retry_task))
                    continue
                self.queue._mark_unknown(task, "lookup_unknown", {"reason": "retry_binding_unverified"})
                actions.append({"operation_id": task["operation_id"], "action": "lookup_unknown"})
                continue
            result = self._observe(task, response, "lookup")
            actions.append({"operation_id": task["operation_id"], "action": "lookup", "result": result})
            current = self.queue.task(task["operation_id"])
            if current is None or current["state"] != "running":
                continue
            if current.get("cancel_requested") and not current.get("cancel_sent"):
                if not self.queue._lease_matches(current["operation_id"], current["owner"], current["generation"]):
                    actions.append({"operation_id": task["operation_id"], "action": "cancel_fenced"})
                    continue
                try:
                    cancel_method = "request_cancel" if callable(getattr(self.runner, "request_cancel", None)) else "cancel"
                    cancel_response = self._call(cancel_method, self._runner_task(current))
                except BaseException as exc:
                    self.queue._mark_unknown(current, "cancel_unknown", {"reason": _error_text(exc)})
                    actions.append({"operation_id": task["operation_id"], "action": "cancel_unknown"})
                    continue
                data = _strip_response(cancel_response) or {"status": "unknown"}
                self.queue._record_observation(
                    current["operation_id"],
                    current["owner"],
                    int(current["generation"]),
                    "cancel_request",
                    data,
                    substate="unknown" if _status_of(data) in _UNKNOWN_STATUS else "running",
                )
                actions.append({"operation_id": task["operation_id"], "action": "cancel_request", "status": _status_of(data)})
        return actions

    def _dispatch_claimed(self, task: Mapping[str, Any]) -> dict[str, Any]:
        if not self.queue._lease_matches(task["operation_id"], task["owner"], task["generation"]):
            return {"operation_id": task["operation_id"], "action": "start_fenced", "result": "stale"}
        try:
            response = self._call("start", self._runner_task(task))
        except BaseException as exc:
            current = self.queue.task(str(task["operation_id"]))
            if current is not None and current.get("state") == "running" and self.queue._lease_matches(current["operation_id"], current.get("owner"), current.get("generation")):
                self.queue._mark_unknown(current, "start_unknown", {"reason": _error_text(exc)})
            return {"operation_id": task["operation_id"], "action": "start", "result": "unknown"}
        result = self._observe(task, response, "start")
        return {"operation_id": task["operation_id"], "action": "start", "result": result}

    def pump(self) -> dict[str, Any]:
        """Reconcile old leases, claim in priority/FIFO order, and dispatch.

        The OS lock intentionally remains held while runner calls are in
        flight.  A second process therefore cannot reconcile, claim, or issue
        a release action concurrently with this controller.
        """
        if self._pumping:
            raise QueueError("controller_pump_reentrant")
        self._pumping = True
        actions: list[dict[str, Any]] = []
        try:
            with self.queue.controller_lock():
                actions.extend(self._reconcile_active())
                while True:
                    task = self.queue._claim_next(self.owner, self.lease_seconds)
                    if task is None:
                        break
                    actions.append(self._dispatch_claimed(task))
                status = self.queue.status() or {"resources": [], "tasks": []}
                status["actions"] = actions
                status["controller_owner"] = self.owner
                return status
        finally:
            self._pumping = False

    def reconcile(self) -> dict[str, Any]:
        """Run only the active-lease reconciliation portion of a pump."""
        with self.queue.controller_lock():
            actions = self._reconcile_active()
            status = self.queue.status() or {"resources": [], "tasks": []}
            status["actions"] = actions
            status["controller_owner"] = self.owner
            return status


# Acceptance callers use the descriptive name while the short ``Queue`` name
# remains the documented constructor.  Both names intentionally identify the
# same implementation and storage format.
ResourceQueue = Queue
