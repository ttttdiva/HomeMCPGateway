"""Supervise a local authenticated MCP HTTP server with safe console status output.

The backend writes stdout/stderr only to files. Do not echo secret-bearing
request paths, headers, or exception text into the visible terminal.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


def _stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Visible, safe-status MCP HTTP supervisor")
    parser.add_argument("--label", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--python", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--log", required=True)
    parser.add_argument("--stderr-log", required=True)
    parser.add_argument("--heartbeat-seconds", type=int, default=60)
    parser.add_argument("backend_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or args.heartbeat_seconds < 5:
        parser.error("Invalid port or heartbeat interval.")
    backend_args = args.backend_args[1:] if args.backend_args[:1] == ["--"] else args.backend_args
    stdout_log = Path(args.log)
    stderr_log = Path(args.stderr_log)
    if not stdout_log.parent.is_dir() or not stderr_log.parent.is_dir():
        raise SystemExit("Server log directory is missing.")

    print(f"[{_stamp()}] [{args.label}] Starting MCP HTTP server on 127.0.0.1:{args.port}", flush=True)
    print(f"[{_stamp()}] [{args.label}] Logs: {stdout_log} | errors: {stderr_log}", flush=True)
    if _port_open(args.port):
        print(f"[{_stamp()}] [{args.label}] ERROR | Port {args.port} is already in use; refusing duplicate start.", flush=True)
        return 1

    child: subprocess.Popen[bytes] | None = None
    try:
        with stdout_log.open("wb") as out, stderr_log.open("wb") as err:
            creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            child = subprocess.Popen(
                [args.python, args.entry, *backend_args],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                creationflags=creationflags,
            )
            last_ready: bool | None = None
            last_report = 0.0
            while child.poll() is None:
                ready = _port_open(args.port)
                now = time.monotonic()
                if last_ready is None or ready != last_ready:
                    state = "READY" if ready else "WAITING"
                    print(f"[{_stamp()}] [{args.label}] {state} | port={args.port}", flush=True)
                    last_ready = ready
                    last_report = now
                elif now - last_report >= args.heartbeat_seconds:
                    state = "READY" if ready else "WAITING"
                    print(f"[{_stamp()}] [{args.label}] heartbeat | state={state} | port={args.port}", flush=True)
                    last_report = now
                time.sleep(2)

            exit_code = child.wait()
            print(f"[{_stamp()}] [{args.label}] STOPPED | exit={exit_code} | error log: {stderr_log}", flush=True)
            return exit_code
    except KeyboardInterrupt:
        print(f"[{_stamp()}] [{args.label}] Interrupted; stopping server.", flush=True)
        return 130
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)


if __name__ == "__main__":
    sys.exit(main())
