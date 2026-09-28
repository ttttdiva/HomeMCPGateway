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


def main(directory: Path) -> None:
    path = directory / "metadata.json"
    data = _read(path)
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
        data.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=time.time(),
                    exit_code=proc.poll() if proc else None)
        _save(path, data)


if __name__ == "__main__":
    directory = Path(sys.argv[1])
    with (directory / "worker.log").open("a", encoding="utf-8", buffering=1) as log:
        sys.stdout = sys.stderr = log
        main(directory)
