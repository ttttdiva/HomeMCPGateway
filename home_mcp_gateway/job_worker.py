"""Independent per-job supervisor. Sole metadata writer while alive."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import time

# Also works when executed directly, outside the installed package's cwd.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psutil
from home_mcp_gateway.jobs import _read, _save, _terminate_tree, process_create_time


def _claim_once(path: Path) -> bool:
    """A keyed reservation has one worker bootstrap for its entire lifetime, including crashes."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as claim:
        claim.write(f"{os.getpid()}\n")
        claim.flush()
        os.fsync(claim.fileno())
    return True


def _consume_launch_fence(path: Path) -> bool:
    """Persist a one-shot Popen fence. A crash after this point is unknown, never retried."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as fence:
        fence.write(f"{os.getpid()}\n")
        fence.flush()
        os.fsync(fence.fileno())
    return True


def main(directory: Path) -> None:
    path = directory / "metadata.json"
    data = _read(path)
    keyed = bool(data.get("idempotency_key"))
    if keyed and not _claim_once(directory / "worker.claim"):
        return
    data.update(worker_pid=os.getpid(), worker_create_time=psutil.Process().create_time())
    _save(path, data)
    proc = None
    try:
        if data.get("scheduler_task"):
            from home_mcp_gateway.windows_job_launch import consume_environment, remove_task
            consume_environment(directory)
            try:
                remove_task(data["scheduler_task"])
                data["scheduler_task_removed"] = True
            except Exception as exc:
                data["scheduler_cleanup_error"] = str(exc)
        with Path(data["stdout_path"]).open("ab", buffering=0) as out, Path(data["stderr_path"]).open("ab", buffering=0) as err:
            if keyed:
                if not _consume_launch_fence(directory / "launch.fence"):
                    data.update(status="unknown", error_code="launch_fence_consumed", finished_at=time.time())
                    _save(path, data)
                    return
                data["launch_fence_state"] = "consumed"
                _save(path, data)
                guard = "open"
                if data.get("dispatcher_binding") is not None:
                    try:
                        from home_mcp_gateway.work_dispatcher_codex import worker_launch_guard
                        launch_data = {
                            "command": data.get("command"), "cwd": data.get("cwd"),
                            "idempotency_key": data.get("idempotency_key"),
                            "request_fingerprint": data.get("request_fingerprint"),
                            "dispatcher_request_fingerprint": data.get("dispatcher_request_fingerprint"),
                            "explicit_env_digest": data.get("explicit_env_digest")}
                        if data.get("transport") == "codex_app_server":
                            launch_data.update(transport=data["transport"], app_server=data.get("app_server"))
                        guard = worker_launch_guard(data["dispatcher_binding"], launch_data)
                    except Exception:
                        guard = "unavailable"
                if guard != "open":
                    if guard == "closed":
                        data.update(status="not_launched", launch_result="gate_closed", finished_at=time.time())
                    elif guard == "mismatch":
                        data.update(status="unknown", launch_result="request_mismatch", error_code="request_mismatch",
                                    finished_at=time.time())
                    else:
                        data.update(status="unknown", launch_result="guard_unavailable", finished_at=time.time())
                    _save(path, data)
                    return
            if data.get("transport") == "codex_app_server":
                # The opt-in adapter never falls back to legacy exec on failure.
                if not keyed or data.get("dispatcher_binding") is None:
                    data.update(status="failed", error_code="appserver_unbound_launch", finished_at=time.time())
                    _save(path, data)
                    return
                from home_mcp_gateway.codex_app_server_runtime import run_app_server, RuntimeFailure
                try:
                    run_app_server(directory, data, lambda value: _save(path, value))
                except (RuntimeFailure, ValueError) as exc:
                    data.update(status="failed", error_code=(exc.code if isinstance(exc, RuntimeFailure)
                                else "appserver_spec_invalid"), finished_at=time.time())
                    _save(path, data)
                return
            proc = subprocess.Popen(data["command"], cwd=data["cwd"], shell=isinstance(data["command"], str),
                                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                                    **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))
            data.update(pid=proc.pid, process_create_time=process_create_time(proc), status="running")
            _save(path, data)
            stopped = False
            while proc.poll() is None:
                request = directory / "stop.json"
                if request.exists():
                    stop = _read(request)
                    request.unlink(missing_ok=True)
                    data["status"] = "stopping"
                    _save(path, data)
                    data["stop_result"] = _terminate_tree(proc.pid, data["process_create_time"], stop["force"])
                    stopped = True
                time.sleep(.05)
            data.update(exit_code=proc.wait(), status="stopped" if stopped else "exited", finished_at=time.time())
            _save(path, data)
    except Exception as exc:
        if proc is not None and proc.poll() is None:
            _terminate_tree(proc.pid, psutil.Process(proc.pid).create_time())
        if keyed:
            data.update(status="unknown", error_code="worker_execution_unknown",
                        error="Keyed worker outcome is unknown; inspect the exact Gateway worker log.", finished_at=time.time(),
                        exit_code=proc.poll() if proc else None)
        else:
            data.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=time.time(),
                        exit_code=proc.poll() if proc else None)
        _save(path, data)


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    with (directory / "worker.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        main(directory)
