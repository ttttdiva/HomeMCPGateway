"""Local Gateway adapter for the durable resource queue.

The queue ledger owns admission, priority, and dispatch.  This module is the
small boundary between that ledger and :mod:`home_mcp_gateway.jobs`:

* a prepared launch is immutable and is replayed with one operation-derived
  idempotency key;
* a retry reconciles the exact keyed job before attempting another start;
* cancellation is a durable, task-scoped request for the prepared actor to
  consume; it is never implemented as a generic process kill; and
* a release is accepted only after the exact job, process identities, child
  identities, and actor/device receipt all agree.

There is deliberately no service loop here.  A caller invokes ``pump`` on the
queue controller when it has an event or an explicit wake-up.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any, Iterable, Mapping, MutableMapping, Sequence
import uuid

from . import jobs
from .resource_queue_claude import (
    CLAUDE_PROVIDER, CLAUDE_ROUTE, MAX_STREAM_BYTES, ClaudeExecutorError, build_claude_session_command,
    parse_claude_command, parse_claude_stream_json,
)


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_CANCEL_NAME_RE = re.compile(r"^[0-9a-fA-F-]{36}\.json$")
_TERMINAL_STATUSES = frozenset({
    "exited", "stopped", "succeeded", "failed", "cancelled", "canceled",
    "completed", "not_launched",
})
_ACTIVE_STATUSES = frozenset({"starting", "running", "stopping"})
_BAD_IDENTITY_STATES = frozenset({"alive", "access_denied", "pid_reused"})


class GatewayRunnerError(ValueError):
    """A safe, stable adapter error code."""

    def __init__(self, code: str, **details: Any):
        self.code = str(code)
        self.details = {key: value for key, value in details.items() if value is not None}
        super().__init__(self.code)


def _safe_error(exc: BaseException) -> str:
    if isinstance(exc, GatewayRunnerError):
        return exc.code
    if isinstance(exc, (FileNotFoundError, PermissionError, OSError)):
        return "io_error"
    if isinstance(exc, (ValueError, TypeError, KeyError, UnicodeError)):
        return "invalid_request"
    return "adapter_error"


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _env_digest(env: Mapping[str, str] | None) -> str:
    """Use the jobs implementation when available, retaining a test seam."""
    helper = getattr(jobs, "_explicit_env_digest", None)
    if callable(helper):
        return str(helper(dict(env or {})))
    return hashlib.sha256(_canonical_json(dict(env or {})).encode("utf-8")).hexdigest()


def _normalize_cwd(cwd: str | os.PathLike[str]) -> str:
    if not isinstance(cwd, (str, os.PathLike)):
        raise GatewayRunnerError("invalid_cwd")
    raw = os.fspath(cwd)
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise GatewayRunnerError("invalid_cwd")
    return str(Path(raw).expanduser().resolve())


def _validate_env(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is None:
        return {}
    if not isinstance(env, Mapping):
        raise GatewayRunnerError("invalid_env")
    result: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key or "\x00" in key:
            raise GatewayRunnerError("invalid_env")
        if not isinstance(value, str) or "\x00" in value:
            raise GatewayRunnerError("invalid_env")
        result[key] = value
    return result


def _validate_command(command: str | Sequence[str]) -> str | list[str]:
    if isinstance(command, str):
        if not command or "\x00" in command:
            raise GatewayRunnerError("invalid_command")
        return command
    if not isinstance(command, Sequence) or isinstance(command, (bytes, bytearray)):
        raise GatewayRunnerError("invalid_command")
    values = list(command)
    if not values or any(not isinstance(value, str) or "\x00" in value for value in values):
        raise GatewayRunnerError("invalid_command")
    return values


def _validate_op_key(op_key: Any) -> str:
    if not isinstance(op_key, str) or not op_key or len(op_key) > 512 or "\x00" in op_key:
        raise GatewayRunnerError("invalid_op_key")
    return op_key


def _validate_thread_id(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 256 or "\x00" in value:
        raise GatewayRunnerError("invalid_thread_id")
    # Codex thread/session ids are UUIDs in the JSONL contract.  Keep the
    # builder usable with an opaque CLI session id, while canonicalizing UUIDs
    # for identity comparisons where possible.
    try:
        return str(uuid.UUID(value)).lower()
    except (ValueError, AttributeError):
        return value


def _validate_fingerprint(value: Any) -> str:
    if not _is_sha256(value):
        raise GatewayRunnerError("invalid_dispatcher_request_fingerprint")
    return value


def _runtime_root(runtime_dir: str | None) -> Path:
    helper = getattr(jobs, "runtime_root", None)
    if callable(helper):
        return Path(helper(runtime_dir)).resolve()
    return Path(runtime_dir or os.environ.get("HOME_MCP_RUNTIME_DIR") or Path(__file__).resolve().parents[1] / ".runtime").expanduser().resolve()


def expected_job_id(op_key: str) -> str:
    """Return the exact keyed jobs UUID for one queue operation."""
    try:
        return str(jobs.stable_job_id(_validate_op_key(op_key))).lower()
    except AttributeError as exc:  # pragma: no cover - defensive test seam
        raise GatewayRunnerError("jobs_unavailable") from exc


def cancellation_path(op_key: str, runtime_dir: str | None = None) -> Path:
    """Return the deterministic sidecar path the prepared actor must poll."""
    job_id = expected_job_id(op_key)
    return _runtime_root(runtime_dir) / "resource-queue" / "cancellations" / f"{job_id}.json"


def _ensure_cancel_path(path: Path, runtime_dir: str | None = None) -> Path:
    root = _runtime_root(runtime_dir)
    resolved = path.expanduser().resolve()
    allowed = (root / "resource-queue" / "cancellations").resolve()
    try:
        resolved.relative_to(allowed)
    except ValueError as exc:
        raise GatewayRunnerError("invalid_cancellation_path") from exc
    if not _SAFE_CANCEL_NAME_RE.fullmatch(resolved.name):
        raise GatewayRunnerError("invalid_cancellation_path")
    return resolved


def _atomic_json_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp = Path(raw_temp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        # Best effort directory flush.  Windows may reject opening a directory;
        # the file itself has already been durably flushed in that case.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        temp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise GatewayRunnerError("receipt_missing") from exc
    except (OSError, ValueError, UnicodeError) as exc:
        raise GatewayRunnerError("receipt_invalid") from exc
    if not isinstance(value, dict):
        raise GatewayRunnerError("receipt_invalid")
    return value


def build_codex_resume_command(
    codex_executable: str,
    thread_id: str,
    prompt: str,
    *,
    model: str | None = None,
    effort: str | None = None,
    sandbox: str | None = None,
    approval_policy: str | None = None,
    json_output: bool = True,
    extra_args: Sequence[str] = (),
    config_overrides: Sequence[str] = (),
) -> list[str]:
    """Build an actual ``codex exec resume THREAD_ID PROMPT`` invocation.

    ``prompt`` is copied byte-for-byte; callers must include any cancellation
    or cleanup procedure in the reviewed prompt themselves.  This function
    never silently turns a fresh ``exec`` into a labelled resume.
    """
    if not isinstance(codex_executable, str) or not codex_executable or "\x00" in codex_executable:
        raise GatewayRunnerError("invalid_codex_executable")
    if Path(codex_executable).suffix.casefold() in {".ps1", ".cmd", ".bat"}:
        # jobs.job_start receives argv directly.  Shell wrappers can open a
        # visible console or resolve a different Codex route; callers must
        # bind a native executable (the Windows vendor codex.exe is suitable).
        raise GatewayRunnerError("codex_executable_must_be_native")
    thread = _validate_thread_id(thread_id)
    if not isinstance(prompt, str) or not prompt or "\x00" in prompt:
        raise GatewayRunnerError("invalid_prompt")
    if not isinstance(extra_args, Sequence) or isinstance(extra_args, (str, bytes, bytearray)):
        raise GatewayRunnerError("invalid_extra_args")
    if any(not isinstance(value, str) or not value or "\x00" in value for value in extra_args):
        raise GatewayRunnerError("invalid_extra_args")
    if not isinstance(config_overrides, Sequence) or isinstance(config_overrides, (str, bytes, bytearray)):
        raise GatewayRunnerError("invalid_config_overrides")
    if any(not isinstance(value, str) or not value or "\x00" in value or "=" not in value for value in config_overrides):
        raise GatewayRunnerError("invalid_config_overrides")
    # Keep the two positional arguments in the documented order.  This lets
    # the adapter verify the session id at argv[3] before any process starts;
    # options accepted by the resume parser may follow the prompt.
    result = [codex_executable, "exec", "resume", thread, prompt]
    if model is not None:
        if not isinstance(model, str) or not model or "\x00" in model:
            raise GatewayRunnerError("invalid_model")
        result.extend(("-m", model))
    if effort is not None:
        if not isinstance(effort, str) or not effort or "\x00" in effort:
            raise GatewayRunnerError("invalid_effort")
        # Codex CLI's current resume route accepts the same config override as
        # ``exec``.  Keep this as a caller choice rather than an ambient value.
        result.extend(("-c", f'model_reasoning_effort="{effort}"'))
    if sandbox is not None:
        if not isinstance(sandbox, str) or not sandbox or "\x00" in sandbox:
            raise GatewayRunnerError("invalid_sandbox")
        result.extend(("-c", f'sandbox_mode="{sandbox}"'))
    if approval_policy is not None:
        if not isinstance(approval_policy, str) or not approval_policy or "\x00" in approval_policy:
            raise GatewayRunnerError("invalid_approval_policy")
        result.extend(("-c", f'approval_policy="{approval_policy}"'))
    for override in config_overrides:
        result.extend(("-c", override))
    if json_output:
        result.append("--json")
    result.extend(list(extra_args))
    return result


# Compatibility spelling used by callers that treat the command as a generic
# prepared route.
make_codex_resume_argv = build_codex_resume_command


@dataclass(frozen=True)
class PreparedLaunch:
    """Immutable launch data reviewed by the queue controller."""

    op_key: str
    command: str | list[str]
    cwd: str
    env: dict[str, str]
    explicit_env_digest: str
    dispatcher_request_fingerprint: str
    cancel_path: str
    thread_id: str | None = None
    task_id: str | None = None
    route: str = "prepared"
    owned_processes: tuple[dict[str, Any], ...] = ()
    receipt_path: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "op_key": self.op_key,
            "command": copy.deepcopy(self.command),
            "cwd": self.cwd,
            "env": dict(self.env),
            "explicit_env_digest": self.explicit_env_digest,
            "dispatcher_request_fingerprint": self.dispatcher_request_fingerprint,
            "cancel_path": self.cancel_path,
            "thread_id": self.thread_id,
            "task_id": self.task_id,
            "route": self.route,
            "owned_processes": copy.deepcopy(list(self.owned_processes)),
            "receipt_path": self.receipt_path,
            "metadata": copy.deepcopy(self.metadata),
        }


def _launch_from_mapping(value: PreparedLaunch | Mapping[str, Any]) -> PreparedLaunch:
    if isinstance(value, PreparedLaunch):
        return value
    if not isinstance(value, Mapping):
        raise GatewayRunnerError("invalid_prepared_launch")
    op_key = _validate_op_key(value.get("op_key") or value.get("operation_key"))
    command = _validate_command(value.get("command"))
    cwd = _normalize_cwd(value.get("cwd"))
    env = _validate_env(value.get("env"))
    digest = _env_digest(env)
    supplied_digest = value.get("explicit_env_digest")
    if supplied_digest is not None and supplied_digest != digest:
        raise GatewayRunnerError("explicit_env_digest_mismatch")
    fp = value.get("dispatcher_request_fingerprint")
    if fp is None:
        fp = value.get("request_fingerprint")
    if fp is None:
        fp = hashlib.sha256(_canonical_json({"command": command, "cwd": cwd, "env": env}).encode("utf-8")).hexdigest()
    fp = _validate_fingerprint(fp)
    raw_cancel = value.get("cancel_path")
    path = _ensure_cancel_path(Path(raw_cancel) if raw_cancel else cancellation_path(op_key, value.get("runtime_dir")), value.get("runtime_dir"))
    thread = value.get("thread_id")
    if thread is not None:
        thread = _validate_thread_id(thread)
    task_id = value.get("task_id")
    if task_id is not None and (not isinstance(task_id, str) or not task_id or "\x00" in task_id):
        raise GatewayRunnerError("invalid_task_id")
    owned = value.get("owned_processes", value.get("owned_identities", ()))
    if owned is None:
        owned = ()
    if not isinstance(owned, Sequence) or isinstance(owned, (str, bytes, bytearray)):
        raise GatewayRunnerError("invalid_owned_processes")
    receipt_path = value.get("receipt_path")
    if receipt_path is not None and (not isinstance(receipt_path, str) or not receipt_path or "\x00" in receipt_path):
        raise GatewayRunnerError("invalid_receipt_path")
    metadata = value.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise GatewayRunnerError("invalid_prepared_launch")
    route = value.get("route") or "prepared"
    if not isinstance(route, str) or not route or "\x00" in route:
        raise GatewayRunnerError("invalid_route")
    if route == "codex-exec-resume":
        if thread is None or not isinstance(command, list) or len(command) < 5 or command[1:3] != ["exec", "resume"]:
            raise GatewayRunnerError("resume_binding_invalid")
        if command[3] != thread:
            raise GatewayRunnerError("resume_thread_mismatch")
    if route == CLAUDE_ROUTE:
        try:
            claude = parse_claude_command(command)
        except ClaudeExecutorError as exc:
            raise GatewayRunnerError(exc.code) from None
        if thread is None or claude["session_id"] != thread:
            raise GatewayRunnerError("resume_thread_mismatch")
        # The approved list is bound twice: in the argv the job runs and in the
        # launch metadata (and so the payload hash); they must be the same list.
        if metadata.get("allowed_tools", []) != claude["allowed_tools"]:
            raise GatewayRunnerError("allowed_tools_metadata_mismatch")
        if env:
            # Explicit overrides could change the model or credentials; the
            # existing logged-in profile is used as is.
            raise GatewayRunnerError("claude_env_not_allowed")
    return PreparedLaunch(
        op_key=op_key,
        command=copy.deepcopy(command),
        cwd=cwd,
        env=dict(env),
        explicit_env_digest=digest,
        dispatcher_request_fingerprint=fp,
        cancel_path=str(path),
        thread_id=thread,
        task_id=task_id,
        route=route,
        owned_processes=tuple(copy.deepcopy(dict(item)) for item in owned if isinstance(item, Mapping)),
        receipt_path=receipt_path,
        metadata=copy.deepcopy(dict(metadata)),
    )


def _metadata_from_status(status: Mapping[str, Any]) -> dict[str, Any]:
    return dict(status)


class GatewayRunner:
    """Run one prepared queue task through keyed :mod:`jobs` primitives."""

    def __init__(self, runtime_dir: str | None = None, *, jobs_api: Any = None, clock: Any = time.time):
        self.runtime_dir = runtime_dir
        self.jobs_api = jobs_api or jobs
        self.clock = clock

    def prepare(
        self,
        op_key: str,
        command: str | Sequence[str],
        cwd: str,
        *,
        env: Mapping[str, str] | None = None,
        dispatcher_request_fingerprint: str | None = None,
        request_fingerprint: str | None = None,
        thread_id: str | None = None,
        task_id: str | None = None,
        route: str = "prepared",
        cancel_path: str | os.PathLike[str] | None = None,
        owned_processes: Sequence[Mapping[str, Any]] = (),
        receipt_path: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> PreparedLaunch:
        """Freeze all launch inputs before queue dispatch or retry."""
        op_key = _validate_op_key(op_key)
        command = _validate_command(command)
        cwd = _normalize_cwd(cwd)
        env = _validate_env(env)
        fp = dispatcher_request_fingerprint if dispatcher_request_fingerprint is not None else request_fingerprint
        if fp is None:
            fp = hashlib.sha256(_canonical_json({"command": command, "cwd": cwd, "env": env}).encode("utf-8")).hexdigest()
        fp = _validate_fingerprint(fp)
        path = _ensure_cancel_path(Path(cancel_path) if cancel_path else cancellation_path(op_key, self.runtime_dir), self.runtime_dir)
        # The directory is safe to create before the jobs reservation.  The
        # reservation directory itself remains exclusively owned by job_start.
        path.parent.mkdir(parents=True, exist_ok=True)
        prepared = {
            "op_key": op_key, "command": command, "cwd": cwd, "env": env,
            "explicit_env_digest": _env_digest(env),
            "dispatcher_request_fingerprint": fp, "cancel_path": str(path),
            "runtime_dir": self.runtime_dir,
            "thread_id": thread_id, "task_id": task_id, "route": route,
            "owned_processes": owned_processes, "receipt_path": receipt_path,
            "metadata": metadata or {},
        }
        return _launch_from_mapping(prepared)

    def prepare_codex_resume(
        self,
        op_key: str,
        codex_executable: str,
        thread_id: str,
        prompt: str,
        cwd: str,
        *,
        env: Mapping[str, str] | None = None,
        dispatcher_request_fingerprint: str | None = None,
        model: str | None = None,
        effort: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
        extra_args: Sequence[str] = (),
        config_overrides: Sequence[str] = (),
        receipt_path: str | None = None,
        task_id: str | None = None,
        owned_processes: Sequence[Mapping[str, Any]] = (),
    ) -> PreparedLaunch:
        command = build_codex_resume_command(
            codex_executable, thread_id, prompt, model=model, effort=effort,
            sandbox=sandbox, approval_policy=approval_policy, extra_args=extra_args,
            config_overrides=config_overrides,
        )
        return self.prepare(
            op_key, command, cwd, env=env,
            dispatcher_request_fingerprint=dispatcher_request_fingerprint,
            thread_id=thread_id, task_id=task_id, route="codex-exec-resume",
            owned_processes=owned_processes, receipt_path=receipt_path,
            metadata={"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()},
        )

    def prepare_claude_session(
        self,
        op_key: str,
        claude_executable: str,
        session_id: str,
        prompt: str,
        cwd: str,
        *,
        model: str,
        effort: str,
        permission_mode: str,
        session_mode: str,
        allowed_tools: Sequence[str] = (),
        dispatcher_request_fingerprint: str | None = None,
        receipt_path: str | None = None,
        task_id: str | None = None,
        owned_processes: Sequence[Mapping[str, Any]] = (),
    ) -> PreparedLaunch:
        """Prepare one ``claude -p`` turn bound to an explicit session, profile and permission mode.

        ``allowed_tools`` is this task's exact ``Bash(<command>)`` approvals; it is part of the
        immutable argv and launch metadata and is never stored on the resource.
        """
        try:
            command = build_claude_session_command(
                claude_executable, session_id, prompt, model=model, effort=effort,
                permission_mode=permission_mode, session_mode=session_mode, allowed_tools=allowed_tools,
            )
        except ClaudeExecutorError as exc:
            raise GatewayRunnerError(exc.code) from None
        from .work_dispatcher_codex import validate_command_line
        try:
            validate_command_line(command)
        except ValueError:
            raise GatewayRunnerError("command_line_too_long") from None
        return self.prepare(
            op_key, command, cwd, dispatcher_request_fingerprint=dispatcher_request_fingerprint,
            thread_id=session_id, task_id=task_id, route=CLAUDE_ROUTE,
            owned_processes=owned_processes, receipt_path=receipt_path,
            metadata={"prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(), "provider": CLAUDE_PROVIDER,
                      "session_mode": session_mode, "allowed_tools": list(allowed_tools),
                      "allowed_tools_sha256": hashlib.sha256(_canonical_json(list(allowed_tools)).encode("utf-8")).hexdigest()},
        )

    # Explicit alias for callers that describe the operation as enqueue launch.
    prepare_launch = prepare

    def _call_job_status(self, job_id: str) -> dict[str, Any]:
        status_fn = getattr(self.jobs_api, "job_status", None)
        if not callable(status_fn):
            raise GatewayRunnerError("jobs_unavailable")
        try:
            result = status_fn(job_id, self.runtime_dir)
        except FileNotFoundError as exc:
            raise GatewayRunnerError("job_not_found") from exc
        except KeyError as exc:
            # Injected jobs seams commonly model a missing keyed reservation
            # with a mapping lookup; it is equivalent to FileNotFoundError
            # until the exact reservation directory is checked below.
            raise GatewayRunnerError("job_not_found") from exc
        except (PermissionError, OSError) as exc:
            raise GatewayRunnerError("job_status_unavailable") from exc
        if not isinstance(result, Mapping):
            raise GatewayRunnerError("job_status_invalid")
        return dict(result)

    def _reservation_exists(self, job_id: str) -> bool:
        directory_fn = getattr(self.jobs_api, "_directory", None) or getattr(jobs, "_directory", None)
        if not callable(directory_fn):
            return False
        try:
            path = directory_fn(job_id, self.runtime_dir)
            return Path(path).exists()
        except (OSError, ValueError, TypeError):
            # An inability to prove the reservation directory absent is an
            # unknown outcome, never an authoritative absence.
            return True

    def _authoritative_absence(self, prepared: PreparedLaunch, error: str) -> dict[str, Any] | None:
        if error != "job_not_found" or self._reservation_exists(expected_job_id(prepared.op_key)):
            return None
        return {
            "ok": False,
            "job_id": expected_job_id(prepared.op_key),
            "operation_key": prepared.op_key,
            "op_key": prepared.op_key,
            "payload_hash": prepared.dispatcher_request_fingerprint,
            "request_fingerprint": prepared.dispatcher_request_fingerprint,
            "status": "not_found",
            "authoritative_absence": True,
            "can_retry_same_key": True,
        }

    def _launch_for_task(self, task: PreparedLaunch | Mapping[str, Any]) -> PreparedLaunch:
        """Convert a queue task projection into the immutable launch seam.

        ``resource_queue.Queue`` stores the prepared launch under ``launch``
        (also exposed as ``payload``), while its operation-derived key and
        payload digest live beside it.  Keeping this conversion here means the
        controller never has to know how Gateway jobs are keyed.
        """
        if isinstance(task, PreparedLaunch):
            return task
        if not isinstance(task, Mapping):
            raise GatewayRunnerError("invalid_task")
        if "command" in task and "cwd" in task:
            raw = dict(task)
        else:
            raw_payload = task.get("launch", task.get("payload"))
            if not isinstance(raw_payload, Mapping):
                raise GatewayRunnerError("prepared_launch_missing")
            raw = dict(raw_payload)
        raw.setdefault("op_key", task.get("operation_key", task.get("op_key")))
        if "operation_key" in task and "payload_hash" in task:
            # Queue-owned launches use the ledger's pinned digest, whether
            # this is a raw projection (CLI) or a flattened controller task.
            # The nested prepared fingerprint describes the pre-enqueue input.
            fingerprint = _validate_fingerprint(task["payload_hash"])
            raw["dispatcher_request_fingerprint"] = fingerprint
            raw["request_fingerprint"] = fingerprint
        elif raw.get("dispatcher_request_fingerprint") is None:
            raw["dispatcher_request_fingerprint"] = (
                raw.get("request_fingerprint")
                or task.get("dispatcher_request_fingerprint")
                or task.get("request_fingerprint")
                or task.get("payload_hash")
            )
        if raw.get("thread_id") is None:
            raw["thread_id"] = task.get("dedicated_thread_id") or task.get("thread_id")
        if raw.get("task_id") is None:
            raw["task_id"] = task.get("operation_id")
        if raw.get("runtime_dir") is None:
            raw["runtime_dir"] = self.runtime_dir
        prepared = _launch_from_mapping(raw)
        if "dedicated_thread_id" in task:
            expected_thread = task.get("dedicated_thread_id")
            if expected_thread is None:
                if prepared.thread_id is not None:
                    raise GatewayRunnerError("dedicated_thread_mismatch")
            elif prepared.thread_id != _validate_thread_id(expected_thread):
                raise GatewayRunnerError("dedicated_thread_mismatch")
        # The queue's operation key and stable job id are immutable bindings;
        # a payload cannot smuggle a different key into a running lease.
        operation_key = task.get("operation_key")
        if operation_key is not None and prepared.op_key != operation_key:
            raise GatewayRunnerError("operation_key_mismatch")
        job_id = task.get("job_id")
        if job_id is not None and str(job_id).lower() != expected_job_id(prepared.op_key):
            raise GatewayRunnerError("job_identity_mismatch")
        executor = task.get("executor")
        if isinstance(executor, Mapping):
            self._check_executor(prepared, executor)
        return prepared

    @staticmethod
    def _check_executor(prepared: PreparedLaunch, executor: Mapping[str, Any]) -> None:
        """The launch must be the one route and profile the task's own executor binding names."""
        claude_launch = prepared.route == CLAUDE_ROUTE
        if claude_launch != (executor.get("provider") == CLAUDE_PROVIDER):
            raise GatewayRunnerError("executor_provider_mismatch")
        if claude_launch:
            fields = parse_claude_command(prepared.command)
            profile = executor.get("profile")
            if (executor.get("session_id") != prepared.thread_id or not isinstance(profile, Mapping)
                    or profile.get("model") != fields["model"] or profile.get("effort") != fields["effort"]
                    or profile.get("permission_mode") != fields["permission_mode"]
                    or profile.get("cwd") != prepared.cwd):
                raise GatewayRunnerError("executor_profile_mismatch")

    @staticmethod
    def _task_binding(task: Mapping[str, Any], launch: PreparedLaunch, outcome: str | None = None) -> dict[str, Any]:
        """Return the queue release binding fields without inventing values."""
        data: dict[str, Any] = {
            "operation_id": task.get("operation_id"),
            "op_key": launch.op_key,
            "resource_id": task.get("resource_id"),
            "job_id": task.get("job_id") or expected_job_id(launch.op_key),
            # Queue's current projection treats the resource thread as an
            # optional task binding.  Use only the explicit projection value
            # here; a launch-local thread id remains checked by verify_release
            # but must not be invented as a queue binding.
            "thread_id": task.get("dedicated_thread_id"),
            "owner": task.get("owner"),
            "generation": task.get("generation"),
            "payload_hash": task.get("payload_hash") or task.get("request_fingerprint") or launch.dispatcher_request_fingerprint,
        }
        if outcome is not None:
            data["outcome"] = outcome
            data["job_outcome"] = outcome
        expected_device = task.get("device_identity")
        if expected_device is not None:
            data["device_identity"] = expected_device
        executor = task.get("executor")
        if isinstance(executor, Mapping) and executor.get("provider") == CLAUDE_PROVIDER:
            data.update(
                executor_provider=executor.get("provider"),
                executor_session_id=executor.get("session_id"),
                executor_generation=executor.get("generation"),
            )
        return data

    def _release_evidence_for_task(
        self,
        task: Mapping[str, Any],
        launch: PreparedLaunch,
        receipt: Mapping[str, Any],
        verified: Mapping[str, Any],
        outcome: str,
    ) -> dict[str, Any]:
        """Shape adapter proof for ``resource_queue.Controller``."""
        device = dict(verified.get("device") or receipt.get("device") or {})
        ids = list(verified.get("identities") or [])
        root = next((item for item in ids if item.get("label") == "root"), None)
        worker = next((item for item in ids if item.get("label") == "worker"), None)
        children = [item for item in ids if item.get("label") not in {"root", "worker"} and not str(item.get("label", "")).startswith("capture[")]
        captures = [item for item in ids if item.get("label") in {str(x.get("label")) for x in (verified.get("capture_identities") or []) if isinstance(x, Mapping)} or str(item.get("label", "")).startswith("capture[")]
        process = {
            "terminal": True,
            "state": "exited",
            "identity_verified": True,
            "create_time": root.get("create_time") if root else None,
            "root_worker_verified": True,
            "root_worker": {
                "verified": True,
                "pid": worker.get("pid") if worker else None,
                "create_time": worker.get("create_time") if worker else None,
            },
            "owned_children": children,
            "captures": {"stopped": True, "flushed": True, "identities": captures},
        }
        evidence = self._task_binding(task, launch, outcome)
        evidence.update({
            "receipt_state": "released",
            "process": process,
            "device": device,
            "release_receipt": copy.deepcopy(dict(receipt)),
        })
        executor_result = verified.get("thread_result")
        if isinstance(executor_result, Mapping) and executor_result.get("provider") == CLAUDE_PROVIDER:
            evidence["executor_result"] = {
                key: executor_result[key] for key in
                ("provider", "session_id", "model", "effort", "effort_evidence", "permission_mode", "allowed_tools", "state",
                 "terminal_subtype", "log_digest", "evidence_digest")
                if key in executor_result
            }
        if isinstance(verified.get("interrupted_recovery"), Mapping):
            evidence["interrupted_recovery"] = copy.deepcopy(verified["interrupted_recovery"])
        return evidence

    @staticmethod
    def _actual_outcome(
        observed: Mapping[str, Any],
        *,
        thread_result: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
    ) -> str:
        """Derive the actor/job outcome without consulting cancel intent.

        The queue applies a durable cancellation request atomically when it
        finalizes.  The adapter therefore reports what the job actually did;
        a request must never rewrite a failed turn as ``cancelled`` (or a
        successful exit as ``cancelled``) before the queue sees the proof.
        """
        exit_code = observed.get("exit_code")
        if type(exit_code) is int and exit_code != 0:
            return "failed"
        if isinstance(thread_result, Mapping):
            state = str(thread_result.get("state") or "").lower()
            if state == "failed":
                return "failed"
        for source in (observed, receipt or {}):
            raw = source.get("job_outcome", source.get("outcome", source.get("terminal_outcome")))
            if isinstance(raw, str) and raw.lower() in {"failed", "cancelled"}:
                return raw.lower()
        if isinstance(thread_result, Mapping) and thread_result.get("state") == "succeeded":
            return "succeeded"
        status = str(observed.get("status") or "").lower()
        if status in {"failed", "cancelled"}:
            return status
        if type(exit_code) is int:
            return "failed" if exit_code != 0 else "succeeded"
        return "failed"

    def _read_exact_metadata(self, job_id: str, status: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Read private metadata when available; public status is accepted only when complete."""
        directory_fn = getattr(self.jobs_api, "_directory", None)
        read_fn = getattr(self.jobs_api, "_read", None)
        if callable(directory_fn) and callable(read_fn):
            try:
                directory = directory_fn(job_id, self.runtime_dir)
            except (FileNotFoundError, PermissionError, OSError, ValueError, TypeError):
                directory = None
            raw = None
            if directory is not None:
                try:
                    raw = read_fn(Path(directory) / "metadata.json")
                except (FileNotFoundError, PermissionError, OSError, ValueError, TypeError):
                    raw = None
            if isinstance(raw, Mapping):
                return dict(raw)
        if isinstance(status, Mapping):
            return _metadata_from_status(status)
        raise GatewayRunnerError("job_metadata_incomplete")

    def _expected_internal_fingerprint(self, launch: PreparedLaunch) -> str | None:
        helper = getattr(self.jobs_api, "_keyed_fingerprint", None)
        if callable(helper):
            try:
                return str(helper(launch.command, launch.cwd, launch.env, launch.dispatcher_request_fingerprint, None))
            except (TypeError, ValueError, OSError):
                return None
        helper = getattr(self.jobs_api, "_keyed_fingerprint_from_env_digest", None)
        if callable(helper):
            try:
                return str(helper(launch.command, launch.cwd, launch.explicit_env_digest,
                                  launch.dispatcher_request_fingerprint, None))
            except (TypeError, ValueError, OSError):
                return None
        return None

    def reconcile(self, launch: PreparedLaunch | Mapping[str, Any], *, status: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Reconcile the durable keyed job using exact immutable launch fields."""
        prepared = self._launch_for_task(launch)
        job_id = expected_job_id(prepared.op_key)
        try:
            observed = dict(status) if status is not None else self._call_job_status(job_id)
        except GatewayRunnerError as exc:
            absent = self._authoritative_absence(prepared, exc.code)
            if absent is not None:
                return absent
            if exc.code == "job_not_found":
                return {"ok": False, "job_id": job_id, "status": "unknown", "error": "reservation_incomplete"}
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": exc.code}
        if not observed or "job_id" not in observed:
            absent = self._authoritative_absence(prepared, "job_not_found")
            if absent is not None:
                return absent
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": "job_metadata_incomplete"}
        try:
            metadata = self._read_exact_metadata(job_id, observed)
        except GatewayRunnerError as exc:
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": exc.code}
        checks = {
            "job_id": metadata.get("job_id") == job_id,
            "idempotency_key": metadata.get("idempotency_key") == prepared.op_key,
            "command": metadata.get("command") == prepared.command,
            "cwd": metadata.get("cwd") == prepared.cwd,
            "explicit_env_digest": metadata.get("explicit_env_digest") == prepared.explicit_env_digest,
            "dispatcher_request_fingerprint": metadata.get("dispatcher_request_fingerprint") == prepared.dispatcher_request_fingerprint,
        }
        expected_internal = self._expected_internal_fingerprint(prepared)
        if expected_internal is not None:
            checks["request_fingerprint"] = metadata.get("request_fingerprint") == expected_internal
        if not all(checks.values()):
            bad = next(key for key, value in checks.items() if not value)
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": "job_metadata_mismatch",
                    "mismatch": bad, "checks": checks, "job": metadata}
        combined = dict(metadata)
        combined.update(observed)
        combined["job_id"] = job_id
        combined["cancel_path"] = prepared.cancel_path
        combined["ok"] = True
        return combined

    def start(self, launch: PreparedLaunch | Mapping[str, Any]) -> dict[str, Any]:
        """Start or replay one prepared keyed job, then reconcile its metadata."""
        task = launch if isinstance(launch, Mapping) and "operation_id" in launch else None
        prepared = self._launch_for_task(launch)
        job_id = expected_job_id(prepared.op_key)
        if task is not None and isinstance(task.get("executor"), Mapping) and task["executor"].get("binding_current") is False:
            # Lookup and release of a finished task stay valid after a rebind;
            # a new launch for a superseded executor does not.
            return {"ok": False, "job_id": job_id, "status": "blocked", "error": "executor_binding_stale"}
        cancel = Path(prepared.cancel_path)
        cancel.parent.mkdir(parents=True, exist_ok=True)
        # Reconnect first.  A caller retrying after a lost response must not
        # issue a second route merely because the first response was absent.
        preflight = self.reconcile(prepared)
        if preflight.get("ok"):
            if task is not None:
                self._attach_task_release(task, prepared, preflight)
            return preflight
        if not (preflight.get("error") == "job_not_found" or
                (preflight.get("status") == "not_found" and preflight.get("authoritative_absence") is True)):
            return preflight
        existing_request = None
        if cancel.exists():
            try:
                existing_request = _read_json(cancel)
            except GatewayRunnerError as exc:
                return {"ok": False, "job_id": job_id, "status": "unknown", "error": exc.code,
                        "cancel_path": str(cancel)}
            if existing_request.get("job_id") != job_id or existing_request.get("op_key") != prepared.op_key:
                return {"ok": False, "job_id": job_id, "status": "blocked", "error": "cancellation_binding_mismatch",
                        "cancel_path": str(cancel)}
            # A pre-existing request is an actor instruction, never permission
            # to launch.  The queue must reconcile an already durable job or
            # leave the operation unknown for an explicit owner decision.
            current = self.reconcile(prepared)
            if current.get("ok"):
                current["cancellation_requested"] = True
                current["cancel_path"] = prepared.cancel_path
                return current
            return {"ok": False, "job_id": job_id, "status": "unknown",
                    "error": "cancellation_requested", "cancellation_requested": True,
                    "cancel_path": prepared.cancel_path}
        start_fn = getattr(self.jobs_api, "job_start", None)
        if not callable(start_fn):
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": "jobs_unavailable"}
        try:
            started = start_fn(
                copy.deepcopy(prepared.command), cwd=prepared.cwd, env=dict(prepared.env),
                runtime_dir=self.runtime_dir, idempotency_key=prepared.op_key,
                request_fingerprint=prepared.dispatcher_request_fingerprint,
            )
        except (ValueError, TypeError, OSError, RuntimeError) as exc:
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": _safe_error(exc)}
        if not isinstance(started, Mapping):
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": "job_start_invalid"}
        started = dict(started)
        returned_id = started.get("job_id")
        if returned_id is not None and str(returned_id).lower() != job_id:
            return {"ok": False, "job_id": job_id, "status": "unknown", "error": "job_identity_mismatch"}
        reconciled = self.reconcile(prepared, status=started if started.get("job_id") else None)
        # A successful start response without exact metadata is never success.
        if not reconciled.get("ok"):
            reconciled.setdefault("start", started)
        else:
            reconciled["start"] = started
        reconciled["cancel_path"] = prepared.cancel_path
        if task is not None:
            self._attach_task_release(task, prepared, reconciled)
        return reconciled

    def _attach_task_release(self, task: Mapping[str, Any], prepared: PreparedLaunch, response: MutableMapping[str, Any]) -> None:
        """Attach an actor receipt proof when an already-started job is terminal."""
        if response.get("status") not in _TERMINAL_STATUSES:
            return
        receipt: dict[str, Any] | None = None
        if prepared.receipt_path:
            try:
                receipt = _read_json(Path(prepared.receipt_path))
            except GatewayRunnerError:
                receipt = None
        if receipt is None:
            # Queue Controller treats a terminal mapping containing ``job_id``
            # as a candidate release record.  Keep the durable job outcome in
            # a separate field until the actor receipt arrives, so the queue
            # records an explicit unknown/pending state instead of mistaking
            # process termination for resource release.
            response["job_status"] = response.get("status")
            response["status"] = "unknown"
            response["error"] = "release_receipt_pending"
            return
        verified = self.verify_release(task, receipt)
        if verified.get("ok"):
            outcome = self._actual_outcome(
                response,
                thread_result=verified.get("thread_result") if isinstance(verified.get("thread_result"), Mapping) else None,
                receipt=receipt,
            )
            response["terminal"] = True
            response["outcome"] = outcome
            response["release_evidence"] = self._release_evidence_for_task(task, prepared, receipt, verified, outcome)
        else:
            response["job_status"] = response.get("status")
            response["status"] = "unknown"
            response["error"] = verified.get("error", "release_receipt_pending")

    # Names used by queue-controller adapters.
    dispatch = start
    run = start

    def status(self, launch: PreparedLaunch | Mapping[str, Any]) -> dict[str, Any]:
        return self.reconcile(launch)

    def lookup(self, task: PreparedLaunch | Mapping[str, Any]) -> dict[str, Any]:
        """Controller-facing durable lookup; never starts a second job."""
        response = self.reconcile(task)
        if isinstance(task, Mapping) and "operation_id" in task and response.get("ok"):
            prepared = self._launch_for_task(task)
            self._attach_task_release(task, prepared, response)
        return response

    def request_cancel(
        self,
        launch: PreparedLaunch | Mapping[str, Any],
        *,
        reason: str = "requested",
        requested_by: str | None = None,
    ) -> dict[str, Any]:
        """Durably request actor cancellation; no process is signalled here."""
        prepared = self._launch_for_task(launch)
        job_id = expected_job_id(prepared.op_key)
        path = _ensure_cancel_path(Path(prepared.cancel_path), self.runtime_dir)
        if not isinstance(reason, str) or not reason or "\x00" in reason or len(reason) > 512:
            raise GatewayRunnerError("invalid_cancel_reason")
        if requested_by is not None and (not isinstance(requested_by, str) or not requested_by or "\x00" in requested_by):
            raise GatewayRunnerError("invalid_cancel_requester")
        if path.exists():
            existing = _read_json(path)
            if existing.get("job_id") != job_id or existing.get("op_key") != prepared.op_key:
                return {"ok": False, "status": "blocked", "error": "cancellation_binding_mismatch",
                        "job_id": job_id, "cancel_path": str(path)}
            return {"ok": True, "status": "requested", "cancellation_requested": True,
                    "request_acknowledged": False, "job_id": job_id, "cancel_path": str(path),
                    "request": existing}
        request = {
            "schema": 1,
            "kind": "resource_queue_cancellation_request",
            "job_id": job_id,
            "op_key": prepared.op_key,
            "requested_at": float(self.clock()),
            "reason": reason,
            "requested_by": requested_by,
            "state": "requested",
            "actor_contract": "The prepared actor must poll this exact file, stop owned input/capture/GPU work, flush capture, and publish a bound receipt.",
        }
        _atomic_json_write(path, request)
        return {"ok": True, "status": "requested", "cancellation_requested": True,
                "request_acknowledged": False, "job_id": job_id, "cancel_path": str(path),
                "request": request}

    cancel = request_cancel
    cancellation_request = request_cancel

    def read_cancellation(self, launch: PreparedLaunch | Mapping[str, Any]) -> dict[str, Any] | None:
        prepared = self._launch_for_task(launch)
        path = _ensure_cancel_path(Path(prepared.cancel_path), self.runtime_dir)
        if not path.exists():
            return None
        request = _read_json(path)
        if request.get("job_id") != expected_job_id(prepared.op_key) or request.get("op_key") != prepared.op_key:
            raise GatewayRunnerError("cancellation_binding_mismatch")
        return request

    def _identity(self, pid: Any, create_time: Any) -> str:
        identity_fn = getattr(self.jobs_api, "identity", None) or getattr(jobs, "identity", None)
        if not callable(identity_fn):
            raise GatewayRunnerError("identity_unavailable")
        try:
            return str(identity_fn(pid, create_time))
        except (OSError, ValueError, TypeError, RuntimeError) as exc:
            raise GatewayRunnerError("identity_unavailable") from exc

    @staticmethod
    def _identity_fields(item: Mapping[str, Any], *, label: str) -> tuple[Any, Any]:
        pid = item.get("pid")
        create_time = item.get("create_time")
        if create_time is None:
            create_time = item.get("process_create_time")
        if type(pid) is not int or pid <= 0 or type(create_time) not in (int, float) or not math.isfinite(float(create_time)) or float(create_time) <= 0:
            raise GatewayRunnerError("release_incomplete", identity=label)
        return pid, create_time

    def _verify_dead_identity(self, item: Mapping[str, Any], *, label: str) -> dict[str, Any]:
        pid, create_time = self._identity_fields(item, label=label)
        state = self._identity(pid, create_time)
        if state in _BAD_IDENTITY_STATES:
            raise GatewayRunnerError("release_identity_not_proven", identity=label, state=state)
        if state != "missing":
            raise GatewayRunnerError("release_identity_not_proven", identity=label, state=state)
        return {"label": label, "pid": pid, "create_time": create_time, "identity": state}

    @staticmethod
    def _attested_bool(device: Mapping[str, Any], key: str, nested: str | None = None) -> bool:
        if isinstance(device.get(key), bool):
            return bool(device[key])
        if nested:
            value = device.get(nested)
            if isinstance(value, Mapping) and isinstance(value.get(key), bool):
                return bool(value[key])
        return False

    @staticmethod
    def _receipt_identity_list(receipt: Mapping[str, Any], *names: str) -> tuple[list[Any] | None, bool]:
        """Return an explicitly supplied identity list and whether it was bound."""
        for name in names:
            if name in receipt:
                value = receipt[name]
                if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                    return list(value), True
                return None, True
        return None, False

    def _verify_thread_output(
        self,
        observed: Mapping[str, Any],
        receipt: Mapping[str, Any],
        expected_thread: str | None,
    ) -> dict[str, Any] | None:
        """Parse the exact JSONL stream so a receipt label cannot fake a thread."""
        if expected_thread is None:
            return None
        raw: bytes | None = None
        inline = receipt.get("stdout_jsonl", receipt.get("jsonl"))
        if isinstance(inline, str):
            try:
                raw = inline.encode("utf-8", errors="strict")
            except UnicodeEncodeError:
                return {"error": "thread_result_invalid"}
        else:
            path_value = observed.get("stdout_path")
            if isinstance(path_value, str) and path_value:
                path = Path(path_value)
                try:
                    max_bytes = 64 << 20
                    if path.stat().st_size > max_bytes:
                        return {"error": "thread_result_too_large"}
                    raw = path.read_bytes()
                except (OSError, ValueError):
                    return {"error": "thread_result_unavailable"}
        if raw is None:
            return {"error": "thread_result_missing"}
        try:
            from .work_dispatcher_codex import parse_codex_jsonl
            parsed = parse_codex_jsonl(raw, expected_thread_id=_validate_thread_id(expected_thread))
        except (OSError, ValueError, TypeError, UnicodeError):
            return {"error": "thread_result_invalid"}
        if parsed.get("state") not in {"succeeded", "failed"}:
            return {"error": parsed.get("code", "thread_result_invalid"), "parsed": parsed}
        if parsed.get("thread_id") != _validate_thread_id(expected_thread):
            return {"error": "thread_identity_mismatch", "parsed": parsed}
        return parsed

    @staticmethod
    def _verify_claude_output(observed: Mapping[str, Any], prepared: PreparedLaunch) -> dict[str, Any]:
        """Verify the job's own stdout against the session and profile in its argv.

        Unlike the Codex path an inline receipt copy is never accepted: the
        stream must be the exact log of the keyed job.
        """
        fields = parse_claude_command(prepared.command)
        path_value = observed.get("stdout_path")
        if not isinstance(path_value, str) or not path_value:
            return {"error": "thread_result_missing"}
        path = Path(path_value)
        try:
            if path.stat().st_size > MAX_STREAM_BYTES:
                return {"error": "thread_result_too_large"}
            raw = path.read_bytes()
        except (OSError, ValueError):
            return {"error": "thread_result_unavailable"}
        parsed = parse_claude_stream_json(
            raw, expected_session_id=fields["session_id"], expected_model=fields["model"],
            expected_effort=fields["effort"], expected_permission_mode=fields["permission_mode"],
            expected_allowed_tools=fields["allowed_tools"],
            # The argv the keyed job actually recorded (already matched to the
            # prepared command by reconcile), passed on as explicit evidence.
            observed_launch_command=observed.get("command"),
        )
        if parsed.get("state") not in {"succeeded", "failed"}:
            return {"error": parsed.get("code", "thread_result_invalid"), "parsed": parsed}
        return parsed

    def _verify_interrupted_codex(
        self, task: Mapping[str, Any] | None, observed: Mapping[str, Any],
        receipt: Mapping[str, Any], thread: str | None,
    ) -> dict[str, Any]:
        """Recover cleanup after an external stop, never manufacture a CLI result.

        Called only after normal binding/process/capture/device verification.
        The durable cancellation and jobs stop record are both required.
        """
        recovery = receipt.get("interrupted_recovery")
        stop = observed.get("stop_result")
        if (task is None or task.get("cancel_requested") is not True or not thread
                or observed.get("status") != "stopped" or observed.get("exit_code") == 0
                or not isinstance(stop, Mapping) or stop.get("remaining_pids") != []
                or stop.get("errors") != [] or observed.get("pid") not in (stop.get("affected_pids") or [])
                or not isinstance(recovery, Mapping)
                or not all(isinstance(recovery.get(k), str) and recovery[k].strip()
                           for k in ("requested_by", "reason", "stdout_sha256"))
                or "stdout_jsonl" in receipt or "jsonl" in receipt):
            return {"error": "interrupted_recovery_not_proven"}
        try:
            path = Path(observed["stdout_path"])
            if path.stat().st_size > 64 << 20:
                return {"error": "thread_result_too_large"}
            raw = path.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            if digest != recovery["stdout_sha256"]:
                return {"error": "interrupted_stdout_digest_mismatch"}
            events = [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]
            threads = [event.get("thread_id") for event in events if event.get("type") == "thread.started"]
            if not threads or any(value != thread for value in threads):
                return {"error": "thread_identity_mismatch"}
            from .work_dispatcher_codex import parse_codex_jsonl
            parsed = parse_codex_jsonl(raw, expected_thread_id=thread)
            if (parsed.get("code") != "terminal_record_invalid"
                    or any(event.get("type") in {"turn.completed", "turn.failed"} for event in events)):
                return {"error": "interrupted_stream_not_proven"}
        except (OSError, KeyError, ValueError, TypeError, AttributeError, UnicodeError):
            return {"error": "interrupted_stream_not_proven"}
        return {"mode": "externally_stopped_codex", "thread_id": thread,
                "stdout_sha256": digest, "stdout_bytes": len(raw), "parser_result": parsed,
                "requested_by": recovery["requested_by"], "reason": recovery["reason"],
                "original_success": False, "terminal_record_observed": False}

    def verify_release(
        self,
        launch: PreparedLaunch | Mapping[str, Any],
        receipt: Mapping[str, Any] | str | os.PathLike[str] | None = None,
        *,
        expected_thread_id: str | None = None,
    ) -> dict[str, Any]:
        """Verify a bound cleanup receipt and exact durable job termination."""
        task = launch if isinstance(launch, Mapping) and "operation_id" in launch else None
        prepared = self._launch_for_task(launch)
        if receipt is None:
            if not prepared.receipt_path:
                return {"ok": False, "status": "blocked", "error": "receipt_missing",
                        "job_id": expected_job_id(prepared.op_key)}
            receipt = prepared.receipt_path
        if isinstance(receipt, (str, os.PathLike)):
            try:
                receipt_data = _read_json(Path(receipt))
            except GatewayRunnerError as exc:
                return {"ok": False, "status": "blocked", "error": exc.code,
                        "job_id": expected_job_id(prepared.op_key)}
        elif isinstance(receipt, Mapping):
            receipt_data = dict(receipt)
        else:
            return {"ok": False, "status": "blocked", "error": "receipt_invalid",
                    "job_id": expected_job_id(prepared.op_key)}
        job_id = expected_job_id(prepared.op_key)
        actor_receipt = receipt_data.get("release_receipt") if isinstance(receipt_data.get("release_receipt"), Mapping) else receipt_data
        if not isinstance(actor_receipt, Mapping):
            return {"ok": False, "status": "blocked", "error": "receipt_invalid", "job_id": job_id}
        outer_job_id = receipt_data.get("job_id")
        outer_op_key = receipt_data.get("op_key")
        if task is None:
            if outer_job_id != job_id or outer_op_key != prepared.op_key:
                return {"ok": False, "status": "blocked", "error": "receipt_binding_mismatch", "job_id": job_id}
        elif outer_job_id != job_id:
            return {"ok": False, "status": "blocked", "error": "receipt_binding_job_id_mismatch", "job_id": job_id}
        elif outer_op_key != prepared.op_key:
            return {"ok": False, "status": "blocked", "error": "receipt_binding_op_key_mismatch", "job_id": job_id}
        if actor_receipt.get("job_id") not in (None, job_id) or actor_receipt.get("op_key") not in (None, prepared.op_key):
            return {"ok": False, "status": "blocked", "error": "receipt_binding_mismatch", "job_id": job_id}
        if task is not None:
            # Every queue-owned binding is required in the submitted receipt.
            # Missing values must never be filled from the current lease: doing
            # so would let a stale actor release a newer generation.
            expected_binding = {
                "operation_id": task.get("operation_id"),
                "op_key": prepared.op_key,
                "resource_id": task.get("resource_id"),
                "job_id": task.get("job_id") or job_id,
                "thread_id": task.get("dedicated_thread_id"),
                "owner": task.get("owner"),
                "generation": task.get("generation"),
                "payload_hash": task.get("payload_hash"),
            }
            for name, wanted in expected_binding.items():
                if name not in receipt_data:
                    return {"ok": False, "status": "blocked", "error": f"receipt_binding_{name}_missing", "job_id": job_id}
                actual = receipt_data.get(name)
                if actual != wanted:
                    return {"ok": False, "status": "blocked", "error": f"receipt_binding_{name}_mismatch", "job_id": job_id}
            # Actor records may repeat the binding; if they do, it must agree.
            for name, wanted in expected_binding.items():
                if name in actor_receipt and name == "thread_id" and wanted is None:
                    # Queue's current task projection may omit the resource's
                    # persistent thread while the prepared launch still binds
                    # the actual resumed Codex thread.
                    if actor_receipt.get(name) not in (None, prepared.thread_id):
                        return {"ok": False, "status": "blocked", "error": f"receipt_binding_{name}_mismatch", "job_id": job_id}
                    continue
                if name in actor_receipt and actor_receipt.get(name) != wanted:
                    return {"ok": False, "status": "blocked", "error": f"receipt_binding_{name}_mismatch", "job_id": job_id}
            # The executor binding the task was claimed under.  A Claude receipt
            # must state it; a Codex receipt from before the executor migration
            # is only compared when it carries it.
            executor = task.get("executor")
            if isinstance(executor, Mapping):
                for name, wanted in (
                    ("executor_provider", executor.get("provider")),
                    ("executor_session_id", executor.get("session_id")),
                    ("executor_generation", executor.get("generation")),
                ):
                    if name in receipt_data or name in actor_receipt:
                        if receipt_data.get(name, actor_receipt.get(name)) != wanted:
                            return {"ok": False, "status": "blocked", "error": f"receipt_binding_{name}_mismatch", "job_id": job_id}
                    elif executor.get("provider") == CLAUDE_PROVIDER:
                        return {"ok": False, "status": "blocked", "error": f"receipt_binding_{name}_missing", "job_id": job_id}
            # ``device_identity`` is the explicit identity binding when the
            # adapter supplies one.  An attestation hash alone identifies the
            # registration record, not the actor's concrete device identity;
            # do not silently reinterpret it as one.
            expected_device = task.get("device_identity")
            if not isinstance(expected_device, str) or not expected_device:
                return {"ok": False, "status": "blocked", "error": "receipt_binding_device_identity_missing", "job_id": job_id}
            if "device_identity" not in receipt_data and "device_identity" not in actor_receipt:
                return {"ok": False, "status": "blocked", "error": "receipt_binding_device_identity_missing", "job_id": job_id}
            actual_device = receipt_data.get("device_identity", actor_receipt.get("device_identity"))
            if actual_device != expected_device:
                return {"ok": False, "status": "blocked", "error": "receipt_binding_device_identity_mismatch", "job_id": job_id}
        if receipt_data.get("user_conflict") is True or receipt_data.get("state") == "user_conflict" or actor_receipt.get("user_conflict") is True or actor_receipt.get("state") == "user_conflict":
            return {"ok": False, "status": "blocked", "error": "user_conflict", "job_id": job_id}
        thread = expected_thread_id or prepared.thread_id
        actor_thread = actor_receipt.get("thread_id")
        if thread is not None and (receipt_data.get("thread_id") != _validate_thread_id(thread) or actor_thread not in (None, _validate_thread_id(thread))):
            return {"ok": False, "status": "blocked", "error": "thread_identity_mismatch", "job_id": job_id}
        observed = self.reconcile(prepared)
        if not observed.get("ok"):
            return {"ok": False, "status": "blocked", "error": observed.get("error", "job_metadata_incomplete"),
                    "job_id": job_id, "job": observed}
        if observed.get("status") not in _TERMINAL_STATUSES:
            return {"ok": False, "status": "blocked", "error": "job_not_terminal", "job_id": job_id, "job": observed}
        if type(observed.get("exit_code")) is not int:
            return {"ok": False, "status": "blocked", "error": "job_exit_incomplete", "job_id": job_id, "job": observed}
        identities: list[dict[str, Any]] = []
        try:
            identities.append(self._verify_dead_identity(observed, label="root"))
            worker = {"pid": observed.get("worker_pid"), "create_time": observed.get("worker_create_time")}
            identities.append(self._verify_dead_identity(worker, label="worker"))
        except GatewayRunnerError as exc:
            return {"ok": False, "status": "blocked", "error": exc.code, "job_id": job_id,
                    "identity": exc.details.get("identity"), "identity_state": exc.details.get("state")}
        owned, owned_bound = self._receipt_identity_list(receipt_data, "owned_identities", "owned_processes")
        if not owned_bound:
            owned, owned_bound = self._receipt_identity_list(actor_receipt, "owned_identities", "owned_processes")
        if not owned_bound or owned is None:
            return {"ok": False, "status": "blocked", "error": "release_incomplete", "job_id": job_id}
        if not isinstance(owned, Sequence) or isinstance(owned, (str, bytes, bytearray)):
            return {"ok": False, "status": "blocked", "error": "release_incomplete", "job_id": job_id}
        try:
            for index, item in enumerate(owned):
                if not isinstance(item, Mapping):
                    raise GatewayRunnerError("release_incomplete", identity=f"owned[{index}]")
                identities.append(self._verify_dead_identity(item, label=str(item.get("label") or f"owned[{index}]")))
        except GatewayRunnerError as exc:
            return {"ok": False, "status": "blocked", "error": exc.code, "job_id": job_id,
                    "identity": exc.details.get("identity"), "identity_state": exc.details.get("state")}
        capture_ids, capture_bound = self._receipt_identity_list(receipt_data, "capture_identities", "capture_processes")
        if not capture_bound:
            capture_ids, capture_bound = self._receipt_identity_list(actor_receipt, "capture_identities", "capture_processes")
        if not capture_bound or capture_ids is None:
            return {"ok": False, "status": "blocked", "error": "capture_identities_missing", "job_id": job_id}
        try:
            for index, item in enumerate(capture_ids):
                if not isinstance(item, Mapping):
                    raise GatewayRunnerError("release_incomplete", identity=f"capture[{index}]")
                identities.append(self._verify_dead_identity(item, label=str(item.get("label") or f"capture[{index}]")))
        except GatewayRunnerError as exc:
            return {"ok": False, "status": "blocked", "error": exc.code, "job_id": job_id,
                    "identity": exc.details.get("identity"), "identity_state": exc.details.get("state")}
        device = receipt_data.get("device")
        if not isinstance(device, Mapping):
            device = actor_receipt.get("device")
        if not isinstance(device, Mapping):
            return {"ok": False, "status": "blocked", "error": "device_attestation_incomplete", "job_id": job_id}
        if device.get("user_conflict") is True or str(device.get("state", device.get("status", ""))).lower() == "user_conflict":
            return {"ok": False, "status": "blocked", "error": "user_conflict", "job_id": job_id}
        kind = str(task.get("resource_kind") or device.get("kind") or device.get("resource_kind") or "").lower() if task is not None else str(device.get("kind") or device.get("resource_kind") or "").lower()
        capture_part = device.get("capture", device.get("captures"))
        capture_stopped = self._attested_bool(device, "capture_stopped", "capture")
        capture_flushed = self._attested_bool(device, "capture_flushed", "capture")
        if isinstance(capture_part, Mapping):
            capture_stopped = capture_stopped or capture_part.get("stopped") is True
            capture_flushed = capture_flushed or capture_part.get("flushed") is True
        required_attestation = {
            "capture_stopped": capture_stopped,
            "capture_flushed": capture_flushed,
            "device_idle": self._attested_bool(device, "device_idle") or str(device.get("state", device.get("device_state", device.get("status", "")))).lower() in {"idle", "released", "available"},
        }
        if kind == "phone":
            input_part = device.get("phone_input", device.get("input"))
            phone_input_stopped = self._attested_bool(device, "phone_input_stopped", "phone_input")
            if isinstance(input_part, Mapping):
                phone_input_stopped = phone_input_stopped or any(
                    input_part.get(name) is True or input_part.get(name) in {1, "true", "verified", "stopped", "released", "idle"}
                    for name in ("stopped", "released", "idle")
                )
            screen_part = device.get("screen")
            if isinstance(screen_part, Mapping):
                screen_state = device.get("screen_state", device.get("phone_screen_state", screen_part.get("state", screen_part.get("status"))))
                screen_appropriate = (
                    self._attested_bool(device, "screen_appropriate")
                    or self._attested_bool(device, "appropriate_screen_verified")
                    or self._attested_bool(screen_part, "appropriate")
                    or self._attested_bool(screen_part, "appropriate_screen_verified")
                )
            else:
                screen_state = device.get("screen_state", device.get("phone_screen_state"))
                screen_appropriate = self._attested_bool(device, "screen_appropriate") or self._attested_bool(device, "appropriate_screen_verified")
            required_attestation.update({
                "phone_input_stopped": phone_input_stopped,
                "screen_state": isinstance(screen_state, str) and bool(screen_state.strip()),
                "screen_appropriate": screen_appropriate,
            })
        elif kind == "gpu":
            gpu_part = device.get("gpu")
            children_exited = self._attested_bool(device, "gpu_children_exited", "gpu")
            gpu_idle = self._attested_bool(device, "gpu_device_idle", "gpu") or self._attested_bool(device, "device_idle")
            if isinstance(gpu_part, Mapping):
                children_exited = children_exited or gpu_part.get("children_exited") is True or gpu_part.get("child_exit_verified") is True
                gpu_idle = gpu_idle or gpu_part.get("device_idle") is True or gpu_part.get("idle") is True
            required_attestation["gpu_children_exited"] = children_exited
            required_attestation["gpu_device_idle"] = gpu_idle
        else:
            return {"ok": False, "status": "blocked", "error": "device_kind_missing", "job_id": job_id}
        if not all(required_attestation.values()):
            missing = next(key for key, value in required_attestation.items() if not value)
            return {"ok": False, "status": "blocked", "error": "device_attestation_incomplete",
                    "missing": missing, "job_id": job_id}
        receipt_state = receipt_data.get("receipt_state") or actor_receipt.get("receipt_state") or actor_receipt.get("status")
        if receipt_state not in ("released", "cleaned", "stopped", "completed"):
            return {"ok": False, "status": "blocked", "error": "receipt_not_complete", "job_id": job_id}
        interrupted_recovery = None
        if "interrupted_recovery" in receipt_data and prepared.route != CLAUDE_ROUTE:
            interrupted_recovery = self._verify_interrupted_codex(task, observed, receipt_data, thread)
            if interrupted_recovery.get("error"):
                return {"ok": False, "status": "blocked", "job_id": job_id, **interrupted_recovery}
            thread_result = None
        elif prepared.route == CLAUDE_ROUTE:
            thread_result = self._verify_claude_output(observed, prepared)
        else:
            thread_result = self._verify_thread_output(observed, actor_receipt, thread)
        if thread_result is not None and thread_result.get("error"):
            return {"ok": False, "status": "blocked", "error": thread_result["error"], "job_id": job_id,
                    "thread_result": thread_result}
        result = {"ok": True, "status": "released", "job_id": job_id,
                "exit_code": observed.get("exit_code"), "identities": identities,
                "capture_identities": capture_ids,
                "device": dict(device), "receipt": receipt_data, "job": observed}
        if thread_result is not None:
            result["thread_result"] = thread_result
        if interrupted_recovery is not None:
            result["interrupted_recovery"] = interrupted_recovery
        if task is not None:
            outcome = self._actual_outcome(observed, thread_result=thread_result, receipt=actor_receipt)
            result["outcome"] = outcome
            if thread_result is not None and outcome == "succeeded" and thread_result.get("state") != "succeeded":
                return {"ok": False, "status": "blocked", "error": "thread_result_outcome_mismatch", "job_id": job_id,
                        "thread_result": thread_result}
            result["release_evidence"] = self._release_evidence_for_task(task, prepared, receipt_data, result, outcome)
        return result

    release = verify_release
    release_receipt = verify_release


__all__ = [
    "GatewayRunner", "GatewayRunnerError", "PreparedLaunch", "build_codex_resume_command", "CLAUDE_ROUTE",
    "make_codex_resume_argv", "expected_job_id", "cancellation_path",
]
