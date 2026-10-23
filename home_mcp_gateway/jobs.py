"""Disk-backed jobs supervised independently of the MCP transport process."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import uuid
import hashlib

import psutil

from .core import _decode

ACTIVE = {"starting", "running", "stopping"}


def process_create_time(proc: subprocess.Popen) -> float:
    # GetProcessTimes uses the retained handle even if a very short command has
    # already exited. Looking up its PID at this point races with termination.
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        times = [wintypes.FILETIME() for _ in range(4)]
        fn = ctypes.WinDLL("kernel32", use_last_error=True).GetProcessTimes
        fn.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        fn.restype = wintypes.BOOL
        if not fn(int(proc._handle), *(ctypes.byref(t) for t in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        return ticks / 10_000_000 - 11644473600
    return psutil.Process(proc.pid).create_time()


def runtime_root(runtime_dir: str | None = None) -> Path:
    return Path(runtime_dir or os.environ.get("HOME_MCP_RUNTIME_DIR") or
                Path(__file__).resolve().parents[1] / ".runtime").expanduser().resolve()


def _directory(job_id: str, runtime_dir: str | None = None) -> Path:
    # A job ID is an identifier, not a filesystem path. Runtime paths remain arbitrary.
    return runtime_root(runtime_dir) / "jobs" / str(uuid.UUID(job_id))


def stable_job_id(idempotency_key: str) -> str:
    """Pure shared UUID codec for one keyed reservation; callers must keep the exact key stable."""
    if not isinstance(idempotency_key, str) or not idempotency_key:
        raise ValueError("invalid_idempotency_key")
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "home-mcp-gateway-job:" + idempotency_key)).lower()


def _save(path: Path, data: dict) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(20):
            try:
                temp.replace(path)
                break
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(.025)
    finally:
        temp.unlink(missing_ok=True)


def _read(path: Path) -> dict:
    # Windows can briefly deny opening metadata while the worker atomically
    # replaces it. Mirror _save's bounded sharing-violation retry; persistent
    # permission errors and malformed/missing metadata still propagate.
    for attempt in range(20):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(.025)
    raise AssertionError("unreachable metadata read state")


def _explicit_env_digest(env: dict[str, str] | None) -> str:
    """Hash only caller-supplied environment overrides, never inherited ambient variables."""
    return hashlib.sha256(json.dumps(env or {}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _keyed_fingerprint_from_env_digest(command: str | list[str], cwd: str, explicit_env_digest: str,
                                       request_fingerprint: str | None,
                                       dispatcher_binding: dict[str, Any] | None) -> str:
    """Rebuild keyed identity from immutable request fields without consulting process environment."""
    if isinstance(command, str):
        normalized_command: Any = {"shell": command}
    else:
        normalized_command = {"argv": command}
    payload = {"command": normalized_command, "cwd": cwd, "env_sha256": explicit_env_digest,
               "request_fingerprint": request_fingerprint, "dispatcher_binding": dispatcher_binding}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()


def _keyed_fingerprint(command: str | list[str], cwd: str, env: dict[str, str] | None,
                       request_fingerprint: str | None, dispatcher_binding: dict[str, Any] | None) -> str:
    """Hash the explicit immutable keyed launch request without exposing env values."""
    return _keyed_fingerprint_from_env_digest(command, cwd, _explicit_env_digest(env), request_fingerprint, dispatcher_binding)


def _dispatcher_binding_valid(binding: Any) -> bool:
    """Accept only the narrow v2 dispatcher-to-job guard record."""
    if not isinstance(binding, dict) or set(binding) != {
            "db_path", "manifest_id", "manifest_digest", "root_source_id", "root_epoch", "generation",
            "job_id", "op_key", "request_digest", "gateway_job_id", "request_fingerprint"}:
        return False
    if not all(isinstance(binding.get(key), str) and binding[key] for key in
               ("db_path", "manifest_id", "root_source_id", "job_id", "op_key")):
        return False
    if any(not isinstance(binding.get(key), str) or len(binding[key]) != 64 or
           any(c not in "0123456789abcdef" for c in binding[key]) for key in
           ("manifest_digest", "request_digest", "request_fingerprint")):
        return False
    if type(binding.get("root_epoch")) is not int or binding["root_epoch"] < 0:
        return False
    if type(binding.get("generation")) is not int or binding["generation"] < 1:
        return False
    try:
        if str(uuid.UUID(binding.get("gateway_job_id", ""))) != binding["gateway_job_id"].lower():
            return False
    except (ValueError, AttributeError):
        return False
    return True


def identity(pid: int | None, create_time: float | None) -> str:
    """Never identify a process by PID alone, including on reboot/PID reuse."""
    if pid is None or create_time is None:
        return "missing"
    try:
        proc = psutil.Process(pid)
        if abs(proc.create_time() - create_time) > .001:
            return "pid_reused"
        return "alive" if proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE else "missing"
    except psutil.NoSuchProcess:
        return "missing"
    except psutil.AccessDenied:
        return "access_denied"


def _terminate_tree(pid: int, create_time: float, force: bool = True) -> dict:
    state = identity(pid, create_time)
    if state != "alive":
        return {"identity": state, "affected_pids": []}
    parent = psutil.Process(pid)
    # Check this exact Process object too, closing the gap after the initial lookup.
    if abs(parent.create_time() - create_time) > .001:
        return {"identity": "pid_reused", "affected_pids": []}
    children = parent.children(recursive=True)
    targets = list(reversed(children)) + [parent]
    affected, errors = [], []
    for proc in targets:
        try:
            (proc.kill if force else proc.terminate)()
            affected.append(proc.pid)
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied as exc:
            errors.append(str(exc))
    _, alive = psutil.wait_procs(targets, timeout=2)
    return {"identity": state, "affected_pids": affected, "remaining_pids": [p.pid for p in alive], "errors": errors}


def _progress_metadata(result: dict[str, Any]) -> dict[str, Any]:
    """Add cheap progress fields without copying log contents into job_list."""
    started_at = result.get("started_at")
    finished_at = result.get("finished_at")
    if isinstance(started_at, (int, float)):
        end = finished_at if isinstance(finished_at, (int, float)) else time.time()
        result["elapsed_sec"] = round(max(0.0, end - started_at), 3)

    mtimes = []
    for name in ("stdout", "stderr"):
        raw_path = result.get(f"{name}_path")
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            stat = path.stat()
            if stat.st_size:
                mtimes.append(stat.st_mtime)
        except OSError:
            pass
    result["last_output_at"] = max(mtimes) if mtimes else None
    return result


def _last_output(result: dict[str, Any], max_bytes: int = 4000) -> str:
    """Read only the newest non-empty log tail for bounded wait diagnostics."""
    candidates = []
    for name in ("stdout", "stderr"):
        raw_path = result.get(f"{name}_path")
        if not raw_path:
            continue
        path = Path(raw_path)
        try:
            stat = path.stat()
            if stat.st_size:
                candidates.append((stat.st_mtime, path))
        except OSError:
            pass
    if not candidates:
        return ""
    _, path = max(candidates, key=lambda item: item[0])
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - max_bytes))
            return _decode(stream.read())
    except OSError:
        return ""


def job_start(command: str | list[str], cwd: str | None = None, env: dict[str, str] | None = None,
              runtime_dir: str | None = None, *, idempotency_key: str | None = None,
              request_fingerprint: str | None = None,
              dispatcher_binding: dict[str, Any] | None = None) -> dict[str, Any]:
    """Start a persistent Windows workspace job. String command uses the workspace shell; argv list avoids shell quoting. Returns job ID, identity, logs and Git provenance."""
    keyed = idempotency_key is not None
    if keyed:
        if (not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 512
                or (request_fingerprint is not None and (not isinstance(request_fingerprint, str) or len(request_fingerprint) != 64
                    or any(c not in "0123456789abcdef" for c in request_fingerprint)))
                or (dispatcher_binding is not None and not _dispatcher_binding_valid(dispatcher_binding))):
            return {"ok": False, "error": "invalid_idempotency_binding", "status": "unknown"}
    elif request_fingerprint is not None or dispatcher_binding is not None:
        return {"ok": False, "error": "invalid_idempotency_binding", "status": "unknown"}
    working = str(Path(cwd or os.getcwd()).expanduser().resolve())
    child_env = dict(os.environ)
    child_env.update(env or {})
    explicit_env_digest = _explicit_env_digest(env) if keyed else None
    request_hash = (_keyed_fingerprint_from_env_digest(command, working, explicit_env_digest, request_fingerprint, dispatcher_binding)
                    if keyed else None)
    job_id = stable_job_id(idempotency_key) if keyed else str(uuid.uuid4())
    directory = _directory(job_id, runtime_dir)
    if keyed:
        directory.parent.mkdir(parents=True, exist_ok=True)
        try:
            directory.mkdir()
        except FileExistsError:
            metadata_path = directory / "metadata.json"
            try:
                state = _read(metadata_path)
            except (FileNotFoundError, PermissionError, ValueError, OSError):
                return {"ok": False, "job_id": job_id, "status": "unknown", "error": "reservation_unknown",
                        "idempotency_conflict": False}
            if (state.get("idempotency_key") != idempotency_key or state.get("request_fingerprint") != request_hash
                    or state.get("explicit_env_digest") != explicit_env_digest
                    or state.get("command") != command or state.get("cwd") != working
                    or state.get("dispatcher_binding") != dispatcher_binding):
                return {"ok": False, "job_id": job_id, "status": "unknown", "error": "idempotency_conflict",
                        "idempotency_conflict": True}
            try:
                return job_status(job_id, runtime_dir)
            except (FileNotFoundError, PermissionError, ValueError, OSError):
                return {"ok": False, "job_id": job_id, "status": "unknown", "error": "reservation_unknown"}
    else:
        directory.mkdir(parents=True)
    metadata = {"job_id": job_id, "pid": None, "process_create_time": None, "command": command,
                "cwd": working, "started_at": time.time(), "finished_at": None, "status": "starting",
                "exit_code": None, "worker_pid": None, "worker_create_time": None,
                "stdout_path": str(directory / "stdout.log"), "stderr_path": str(directory / "stderr.log"),
                "worker_log_path": str(directory / "worker.log"),
                "runtime_dir": str(runtime_root(runtime_dir))}
    if keyed:
        metadata.update(idempotency_key=idempotency_key, request_fingerprint=request_hash,
                        dispatcher_request_fingerprint=request_fingerprint, dispatcher_binding=dispatcher_binding,
                        explicit_env_digest=explicit_env_digest,
                        launch_fence_state="pending")
    try:
        from .git_qa import git_status
        metadata["git_at_start"] = git_status(working)
    except (OSError, ValueError, subprocess.SubprocessError):
        metadata["git_at_start"] = None
    _save(directory / "metadata.json", metadata)
    worker = None
    try:
        # Run by absolute script path so recovery does not depend on cwd/PYTHONPATH.
        if os.name == "nt":
            from .windows_job_launch import launch
            launch(directory, child_env, metadata)
        else:
            with (directory / "worker.log").open("ab", buffering=0) as log:
                worker = subprocess.Popen([sys.executable, str(Path(__file__).with_name("job_worker.py")), str(directory)],
                                          stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=child_env, start_new_session=True)
        # Reap the worker without making its lifetime depend on this daemon thread.
        if worker:
            threading.Thread(target=worker.wait, daemon=True).start()
    except Exception as exc:
        if keyed:
            metadata.update(status="unknown", error_code="worker_launch_unknown",
                            error="Keyed launch outcome is unknown; inspect the exact Gateway job metadata and worker log.",
                            finished_at=time.time())
        else:
            metadata.update(status="failed", error=str(exc), finished_at=time.time())
        _save(directory / "metadata.json", metadata)
        return metadata
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = _read(directory / "metadata.json")
        if state["status"] != "starting":
            return job_status(job_id, runtime_dir)
        if worker is not None and worker.poll() is not None:
            state.update(status="failed", error="Job worker exited during startup; inspect worker.log", finished_at=time.time())
            _save(directory / "metadata.json", state)
            return state
        time.sleep(.05)
    return {**state, "startup_pending": True}


def job_status(job_id: str, runtime_dir: str | None = None) -> dict[str, Any]:
    """Rediscover metadata, verify process identity, and report cheap progress timestamps."""
    try:
        result = _read(_directory(job_id, runtime_dir) / "metadata.json")
    except FileNotFoundError:
        directory = _directory(job_id, runtime_dir)
        if directory.exists():
            return {"job_id": str(uuid.UUID(job_id)), "status": "unknown", "error_code": "reservation_incomplete",
                    "process_identity": "missing", "worker_identity": "missing"}
        raise
    result["process_identity"] = identity(result.get("pid"), result.get("process_create_time"))
    result["worker_identity"] = identity(result.get("worker_pid"), result.get("worker_create_time"))
    if (result["status"] in ACTIVE and result["process_identity"] in {"missing", "pid_reused"}
            and result["worker_identity"] in {"missing", "pid_reused"}):
        # The worker can publish its final metadata and exit between our first
        # read and the identity checks. Re-read AFTER detecting its exit rather
        # than turning a completed short-lived job into a spurious 'lost' job.
        # A genuinely lost worker still has no final status/exit code to invent.
        finalized = _read(_directory(job_id, runtime_dir) / "metadata.json")
        if finalized["status"] not in ACTIVE:
            result.update(finalized)
    if result["status"] in ACTIVE:
        if result["process_identity"] == "pid_reused":
            result.update(status="lost", error="PID was reused; the unrelated process will not be signalled")
        elif result["worker_identity"] in {"missing", "pid_reused"} and result["process_identity"] == "missing":
            if result["status"] != "starting" or time.time() - result["started_at"] > 15:
                result.update(status="lost", error="Worker and job are gone; exit code cannot be recovered")
        elif result["process_identity"] == "access_denied":
            result.update(status="unknown", error="Access denied when verifying process identity")
    return _progress_metadata(result)


def job_tail(job_id: str, max_bytes: int = 20000, runtime_dir: str | None = None) -> dict[str, Any]:
    """Read bounded stdout/stderr log tails after reconnect/restart. max_bytes=0 reads complete logs."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be nonnegative")
    result = job_status(job_id, runtime_dir)
    for name in ("stdout", "stderr"):
        path = Path(result[f"{name}_path"])
        if not path.exists():
            result[name] = ""
            continue
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - max_bytes) if max_bytes else 0)
            result[name] = _decode(stream.read())
            result[f"{name}_truncated"] = bool(max_bytes and size > max_bytes)
    return result


def job_wait(job_id: str, timeout_sec: float = 30, runtime_dir: str | None = None) -> dict[str, Any]:
    """Wait up to timeout_sec; timeout returns running state plus bounded latest output."""
    if timeout_sec < 0:
        raise ValueError("timeout_sec must be nonnegative")
    deadline = time.monotonic() + timeout_sec
    while True:
        result = job_status(job_id, runtime_dir)
        if result["status"] not in ACTIVE or time.monotonic() >= deadline:
            timed_out = result["status"] in ACTIVE
            response = {**result, "timed_out": timed_out}
            if timed_out:
                response["last_output"] = _last_output(result)
            return response
        time.sleep(min(.1, max(0, deadline - time.monotonic())))


def job_stop(job_id: str, force: bool = True, timeout_sec: float = 10, runtime_dir: str | None = None) -> dict[str, Any]:
    """Stop a job and its descendants after identity verification; default force=True. Keeps metadata/logs."""
    if timeout_sec < 0:
        raise ValueError("timeout_sec must be nonnegative")
    result = job_status(job_id, runtime_dir)
    if result["status"] not in ACTIVE:
        return result
    directory = _directory(job_id, runtime_dir)
    if result["worker_identity"] == "alive" or result["status"] == "starting":
        _save(directory / "stop.json", {"force": force})
        return job_wait(job_id, timeout_sec, runtime_dir)
    if result["process_identity"] == "alive":
        stopped = _terminate_tree(result["pid"], result["process_create_time"], force)
        # No live worker is present to record the final state. Never invent an exit code.
        result.update(stop_result=stopped)
        if identity(result["pid"], result["process_create_time"]) == "missing":
            result.update(status="stopped", finished_at=time.time())
            _save(directory / "metadata.json", result)
    return result


def job_list(runtime_dir: str | None = None, active_only: bool = False) -> dict[str, Any]:
    """Discover jobs from disk, including jobs started by previous Gateway instances."""
    items, errors = [], []
    for path in (runtime_root(runtime_dir) / "jobs").glob("*/metadata.json"):
        try:
            item = job_status(path.parent.name, runtime_dir)
            if not active_only or item["status"] in ACTIVE:
                items.append(item)
        except (ValueError, OSError) as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {"jobs": sorted(items, key=lambda item: item["started_at"], reverse=True), "errors": errors}


TOOLS = [job_start, job_status, job_tail, job_wait, job_stop, job_list]
