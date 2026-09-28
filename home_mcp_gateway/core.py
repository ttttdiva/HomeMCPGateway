from __future__ import annotations

import base64
import ctypes
import fnmatch
import glob
import hashlib
import json
import locale
import math
import os
import platform
import queue
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import psutil

_BG: dict[int, dict[str, Any]] = {}

# Search and glob are deliberately bounded even when callers omit the newer
# optional limits.  A gateway tool must not be able to spend many minutes
# recursively walking a repository after its MCP request has already timed
# out.  The values are intentionally generous for normal source trees while
# providing a finite upper bound for accidental broad searches.
_DEFAULT_SEARCH_TIMEOUT_SEC = 30.0
_DEFAULT_SEARCH_MAX_FILES = 10000
_DEFAULT_GLOB_TIMEOUT_SEC = 10.0
_DEFAULT_GLOB_MAX_RESULTS = 1000
_SEARCH_CHUNK_CHARS = 64 * 1024
_SEARCH_MAX_LINE_CHARS = 64 * 1024
_RG_STREAM_QUEUE_SIZE = 4
_RG_STREAM_CHUNK_BYTES = 64 * 1024
_RG_STREAM_MAX_LINE_BYTES = 1024 * 1024
_RG_STREAM_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_DEFAULT_SEARCH_MAX_RESULTS = 10_000


# Windows Job Object flag.  Keeping the definition here avoids a dependency
# on pywin32 while allowing timeout cleanup to include descendants that have
# already outlived the shell process which created them.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9


def _decode(data: bytes | str) -> str:
    if not data:
        return ""
    if isinstance(data, str):
        return data
    encodings = ["utf-8-sig", locale.getpreferredencoding(False), "cp932", "utf-8"]
    seen: set[str] = set()
    for enc in encodings:
        if not enc or enc.lower() in seen:
            continue
        seen.add(enc.lower())
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("utf-8", errors="replace")


def _limit(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars and max_chars > 0 and len(text) > max_chars:
        return text[:max_chars], True
    return text, False


def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def system_info() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    disks = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
            disks.append({
                "device": part.device,
                "mountpoint": part.mountpoint,
                "fstype": part.fstype,
                "total": usage.total,
                "used": usage.used,
                "free": usage.free,
                "percent": usage.percent,
            })
        except Exception:
            continue

    addresses: dict[str, list[str]] = {}
    for name, entries in psutil.net_if_addrs().items():
        addresses[name] = [e.address for e in entries]

    gpus: list[dict[str, str]] = []
    nvidia_smi = shutil.which("nvidia-smi")
    if nvidia_smi:
        try:
            p = subprocess.run(
                [nvidia_smi, "--query-gpu=name,driver_version,memory.total,memory.free", "--format=csv,noheader,nounits"],
                capture_output=True,
                timeout=10,
            )
            if p.returncode == 0:
                for line in _decode(p.stdout).splitlines():
                    parts = [x.strip() for x in line.split(",")]
                    if len(parts) >= 4:
                        gpus.append({"name": parts[0], "driver": parts[1], "memory_total_mib": parts[2], "memory_free_mib": parts[3]})
        except Exception:
            pass

    # Report the implementation actually imported by this serving process,
    # not a fresh child interpreter or a version string read from disk.
    from . import desktop
    return {
        "gateway": {"pid": os.getpid(), "started_at": psutil.Process().create_time(),
                    "repository_root": str(Path(__file__).resolve().parents[1]),
                    "desktop_implementation": desktop.IMPLEMENTATION,
                    "desktop_tools": [tool.__name__ for tool in desktop.TOOLS],
                    "uia_backend": desktop.desktop_uia.BACKEND},
        "hostname": socket.gethostname(),
        "platform": platform.platform(),
        "os": os.name,
        "python": sys.version,
        "python_executable": sys.executable,
        "cwd": os.getcwd(),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "memory": {"total": vm.total, "available": vm.available, "used": vm.used, "percent": vm.percent},
        "disks": disks,
        "network_interfaces": addresses,
        "gpus": gpus,
    }


def get_environment(name: str = "") -> dict[str, Any]:
    if name:
        return {"name": name, "value": os.environ.get(name)}
    return {"environment": dict(os.environ)}


def set_environment(name: str, value: str | None = None) -> dict[str, Any]:
    if value is None:
        old = os.environ.pop(name, None)
        return {"name": name, "removed": True, "old_value": old}
    old = os.environ.get(name)
    os.environ[name] = value
    return {"name": name, "value": value, "old_value": old}


def list_directory(path: str, recursive: bool = False) -> dict[str, Any]:
    p = Path(path).expanduser()
    items: list[dict[str, Any]] = []
    iterator = p.rglob("*") if recursive else p.iterdir()
    for item in iterator:
        try:
            st = item.stat()
            items.append({
                "path": str(item),
                "name": item.name,
                "is_dir": item.is_dir(),
                "is_file": item.is_file(),
                "size": st.st_size,
                "mtime": st.st_mtime,
            })
        except Exception as exc:
            items.append({"path": str(item), "error": repr(exc)})
    return {"path": str(p), "items": items}


def _positive_budget(value: float | int | None, default: float) -> float:
    """Return a finite positive budget, falling back to *default*.

    Tool arguments arrive over JSON, so malformed values should not turn a
    bounded operation into an unbounded one.  ``bool`` is accepted by Python
    for historical compatibility but is treated like its numeric value.
    """

    try:
        candidate = float(value) if value is not None else default
    except (TypeError, ValueError):
        candidate = default
    if not math.isfinite(candidate) or candidate <= 0:
        return default
    return candidate


def _positive_limit(value: int | None, default: int) -> int:
    """Return a finite positive result/file limit.

    Older callers did not have a limit argument at all.  Treating zero and
    negative values as the safe default keeps those calls bounded while still
    allowing callers to request a precise positive limit.
    """

    try:
        candidate = int(value) if value is not None else default
    except (TypeError, ValueError):
        candidate = default
    if candidate <= 0:
        return default
    return min(candidate, 1_000_000)


def _iter_bounded_glob(pattern: str, recursive: bool, deadline: float):
    """Yield glob matches while checking the deadline during directory scans.

    ``glob.iglob`` is lazy, but a recursive ``**`` iterator can still spend a
    long time inside a single ``next()`` while it descends a large directory.
    Walking one directory at a time with ``os.scandir`` gives this function a
    check point for every entry instead.  The stack also avoids Python call
    stack growth on deeply nested repositories.
    """

    expanded = str(Path(pattern).expanduser())
    path = Path(expanded)
    parts = list(path.parts)
    if path.is_absolute():
        base = Path(parts.pop(0))
    else:
        base = Path(".")

    # (directory currently being expanded, segment index)
    pending: list[tuple[Path, int]] = [(base, 0)]
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError
        directory, index = pending.pop()
        if index >= len(parts):
            yield str(directory)
            continue

        segment = parts[index]
        if segment == "**" and recursive:
            # ``**`` matches zero directory components as well as one or more.
            if index == len(parts) - 1:
                # Terminal ** includes the current directory and every file
                # and directory below it.  Child directories are scheduled
                # as states, so each is yielded exactly once at the start of
                # its own state rather than once here and once via zero-depth
                # matching.
                yield str(directory)
                try:
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            if time.monotonic() >= deadline:
                                raise TimeoutError
                            try:
                                if entry.name.startswith("."):
                                    continue
                                entry_path = Path(entry.path)
                                if entry.is_dir(follow_symlinks=False):
                                    pending.append((entry_path, index))
                                else:
                                    yield str(entry_path)
                            except OSError:
                                continue
                except FileNotFoundError:
                    continue
                continue

            pending.append((directory, index + 1))
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        if time.monotonic() >= deadline:
                            raise TimeoutError
                        try:
                            if entry.name.startswith("."):
                                continue
                            entry_path = Path(entry.path)
                            if entry.is_dir(follow_symlinks=False):
                                pending.append((entry_path, index))
                        except OSError:
                            continue
            except FileNotFoundError:
                continue
            continue

        # A literal segment can be followed without scanning its parent.
        if not glob.has_magic(segment):
            child = directory / segment
            try:
                if index == len(parts) - 1:
                    if child.exists() or child.is_symlink():
                        yield str(child)
                elif child.is_dir():
                    pending.append((child, index + 1))
            except OSError:
                pass
            continue

        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    name = entry.name
                    # Match pathlib/glob's usual POSIX dot-file behaviour.
                    if name.startswith(".") and not segment.startswith("."):
                        continue
                    if not fnmatch.fnmatch(name, segment):
                        continue
                    child = Path(entry.path)
                    try:
                        if index == len(parts) - 1:
                            yield str(child)
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append((child, index + 1))
                    except OSError:
                        continue
        except FileNotFoundError:
            continue


def glob_paths(
    pattern: str,
    recursive: bool = True,
    max_results: int = _DEFAULT_GLOB_MAX_RESULTS,
    timeout_sec: float = _DEFAULT_GLOB_TIMEOUT_SEC,
) -> dict[str, Any]:
    """Return bounded glob matches.

    ``glob.glob`` materialises every match before returning, which made a
    recursive ``**`` pattern dangerous on a large repository.  The bounded
    scandir iterator lets us stop once the result or time budget is reached;
    no worker thread is left running after a request returns.
    """

    expanded_pattern = str(Path(pattern).expanduser())
    result_limit = _positive_limit(max_results, _DEFAULT_GLOB_MAX_RESULTS)
    budget = _positive_budget(timeout_sec, _DEFAULT_GLOB_TIMEOUT_SEC)
    deadline = time.monotonic() + budget
    matches: list[str] = []
    timed_out = False
    truncated = False

    iterator = None
    try:
        iterator = _iter_bounded_glob(expanded_pattern, recursive, deadline)
        while len(matches) < result_limit:
            try:
                matches.append(next(iterator))
            except StopIteration:
                break
        else:
            # There may be more matches, but probing one more value is not
            # worth potentially starting another expensive directory scan.
            truncated = True
    except TimeoutError:
        timed_out = True
    except (OSError, ValueError):
        # Match the historical best-effort nature of glob_paths while still
        # returning a stable bounded response shape.
        pass
    finally:
        if iterator is not None:
            iterator.close()

    return {
        "pattern": pattern,
        "matches": matches,
        "truncated": truncated,
        "timed_out": timed_out,
        "partial": timed_out or truncated,
    }


_DEFAULT_SEARCH_EXCLUDES = (
    ".git", ".venv", "venv", "node_modules", "build", "dist", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".local",
)


def _iter_search_files(
    root_path: Path,
    excluded: set[str],
    deadline: float,
):
    """Yield files from a directory stack with cooperative cancellation."""

    pending = [root_path]
    while pending:
        if time.monotonic() >= deadline:
            raise TimeoutError
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name not in excluded:
                                pending.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            yield Path(entry.path)
                    except OSError:
                        continue
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue


def _iter_file_matches_bounded(
    path: Path,
    needle: str,
    case_sensitive: bool,
    deadline: float,
):
    """Yield matching lines without reading a huge one-line file at once."""

    normalized_needle = needle if case_sensitive else needle.lower()
    tail_length = min(_SEARCH_MAX_LINE_CHARS, max(0, len(normalized_needle) - 1))
    with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
        line_number = 1
        line_buffer = ""
        match_tail = ""
        line_matches = not normalized_needle
        saw_content = False
        for chunk in iter(lambda: handle.read(_SEARCH_CHUNK_CHARS), ""):
            if time.monotonic() >= deadline:
                raise TimeoutError
            pieces = chunk.splitlines(keepends=True)
            for piece in pieces:
                if time.monotonic() >= deadline:
                    raise TimeoutError
                has_newline = piece.endswith(("\n", "\r"))
                content = piece.rstrip("\r\n")
                saw_content = saw_content or bool(content) or has_newline
                if len(line_buffer) < _SEARCH_MAX_LINE_CHARS:
                    line_buffer += content[:_SEARCH_MAX_LINE_CHARS - len(line_buffer)]
                normalized_content = content if case_sensitive else content.lower()
                if normalized_needle and normalized_needle in (match_tail + normalized_content):
                    line_matches = True
                if has_newline:
                    if line_matches:
                        yield line_number, line_buffer
                    line_number += 1
                    line_buffer = ""
                    match_tail = ""
                    line_matches = not normalized_needle
                else:
                    match_tail = (match_tail + normalized_content)[-tail_length:] if tail_length else ""
        if line_buffer or match_tail or saw_content:
            if line_matches:
                yield line_number, line_buffer


def _search_text_python(
    root_path: Path,
    query: str,
    file_glob: str,
    case_sensitive: bool,
    max_results: int,
    timeout_sec: float = _DEFAULT_SEARCH_TIMEOUT_SEC,
    max_files: int | None = _DEFAULT_SEARCH_MAX_FILES,
    deadline: float | None = None,
) -> dict[str, Any]:
    """Portable, bounded fallback used when ripgrep is unavailable.

    The previous implementation used ``Path.rglob`` with no deadline.  Apart
    from walking arbitrarily large trees, that also meant an rg timeout was
    followed by a second unbounded scan.  A directory stack built from
    ``os.scandir`` lets us prune excluded directories before descending and
    check both budgets between entries and fixed-size text chunks.
    """

    needle = query if case_sensitive else query.lower()
    results: list[dict[str, Any]] = []
    scanned = 0
    visited_files = 0
    excluded = set(_DEFAULT_SEARCH_EXCLUDES)
    timeout = _positive_budget(timeout_sec, _DEFAULT_SEARCH_TIMEOUT_SEC)
    file_limit = _positive_limit(max_files, _DEFAULT_SEARCH_MAX_FILES) if max_files is not None else None
    result_limit = _positive_limit(max_results, _DEFAULT_SEARCH_MAX_RESULTS)
    if deadline is None:
        deadline = time.monotonic() + timeout
    timed_out = False
    file_limit_hit = False
    match_limit_hit = False
    output_limit_hit = False
    result_bytes = 0

    try:
        for item in _iter_search_files(root_path, excluded, deadline):
            visited_files += 1
            if file_limit is not None and visited_files > file_limit:
                file_limit_hit = True
                break
            if not fnmatch.fnmatch(item.name, file_glob):
                continue
            scanned += 1
            try:
                for line_no, line in _iter_file_matches_bounded(item, needle, case_sensitive, deadline):
                    entry_bytes = len(str(item).encode("utf-8", errors="replace")) + len(line.encode("utf-8", errors="replace")) + 64
                    if result_bytes + entry_bytes > _RG_STREAM_MAX_OUTPUT_BYTES:
                        output_limit_hit = True
                        break
                    results.append({"path": str(item), "line": line_no, "text": line})
                    result_bytes += entry_bytes
                    if len(results) >= result_limit:
                        match_limit_hit = True
                        break
                if match_limit_hit or output_limit_hit:
                    break
            except TimeoutError:
                timed_out = True
                break
            except (OSError, UnicodeError):
                continue
    except TimeoutError:
        timed_out = True
    except OSError:
        # A path can disappear while walking (or be inaccessible).  Return
        # the partial result rather than turning a best-effort search into a
        # gateway error.
        pass

    truncated = match_limit_hit or output_limit_hit
    partial = timed_out or file_limit_hit or match_limit_hit or output_limit_hit
    return {
        "root": str(root_path),
        "scanned_files": scanned,
        "results": results,
        "truncated": truncated,
        "backend": "python",
        "excluded_directories": list(_DEFAULT_SEARCH_EXCLUDES),
        "timed_out": timed_out,
        "partial": partial,
    }


def _rg_json_text(field: dict[str, Any]) -> str:
    if "text" in field:
        return str(field["text"])
    if "bytes" in field:
        try:
            return base64.b64decode(field["bytes"]).decode("utf-8", errors="replace")
        except Exception:
            return ""
    return ""


def _rg_stdout_reader(
    stream: Any,
    lines: queue.Queue[bytes],
    done: threading.Event,
    overflow: threading.Event,
) -> None:
    """Read rg stdout in fixed chunks into a bounded line queue."""

    pending = bytearray()
    try:
        while True:
            chunk = stream.read(_RG_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            pending.extend(chunk)
            if len(pending) > _RG_STREAM_MAX_LINE_BYTES and b"\n" not in pending:
                overflow.set()
                return
            while b"\n" in pending:
                raw_line, _, remainder = pending.partition(b"\n")
                pending = bytearray(remainder)
                if len(raw_line) > _RG_STREAM_MAX_LINE_BYTES:
                    overflow.set()
                    return
                try:
                    lines.put(bytes(raw_line).rstrip(b"\r"), timeout=0.05)
                except queue.Full:
                    overflow.set()
                    return
        if pending:
            if len(pending) > _RG_STREAM_MAX_LINE_BYTES:
                overflow.set()
            else:
                try:
                    lines.put(bytes(pending).rstrip(b"\r"), timeout=0.05)
                except queue.Full:
                    overflow.set()
    except (OSError, ValueError):
        overflow.set()
    finally:
        done.set()


def _rg_path_reader(
    stream: Any,
    paths: queue.Queue[bytes],
    done: threading.Event,
    overflow: threading.Event,
) -> None:
    """Read ``rg --files -0`` output into a bounded NUL-delimited queue."""

    pending = bytearray()
    try:
        while True:
            chunk = stream.read(_RG_STREAM_CHUNK_BYTES)
            if not chunk:
                break
            pending.extend(chunk)
            if len(pending) > _RG_STREAM_MAX_LINE_BYTES and b"\0" not in pending:
                overflow.set()
                return
            while b"\0" in pending:
                raw_path, _, remainder = pending.partition(b"\0")
                pending = bytearray(remainder)
                if len(raw_path) > _RG_STREAM_MAX_LINE_BYTES:
                    overflow.set()
                    return
                try:
                    paths.put(bytes(raw_path), timeout=0.05)
                except queue.Full:
                    overflow.set()
                    return
        if pending:
            # rg --files -0 should terminate every path with NUL.  Treat an
            # unterminated final path as usable but keep the bounded reader
            # safe if a replacement backend violates that contract.
            if len(pending) > _RG_STREAM_MAX_LINE_BYTES:
                overflow.set()
            else:
                try:
                    paths.put(bytes(pending), timeout=0.05)
                except queue.Full:
                    overflow.set()
    except (OSError, ValueError):
        overflow.set()
    finally:
        done.set()


def _rg_stderr_drainer(stream: Any, done: threading.Event) -> None:
    """Drain stderr without retaining unbounded diagnostic output."""

    try:
        while stream.read(_RG_STREAM_CHUNK_BYTES):
            pass
    except (OSError, ValueError):
        pass
    finally:
        done.set()


def _stop_rg_process(
    process: subprocess.Popen[Any],
    windows_job: tuple[Any, Any, Any] | None,
) -> None:
    """Stop an rg process tree and release captured pipes promptly."""

    _kill_process_tree(process)
    _enable_windows_job_kill_on_close(windows_job)
    _close_windows_job(windows_job)
    try:
        process.wait(timeout=0.5)
    except (subprocess.TimeoutExpired, OSError):
        pass
    for stream in (process.stdout, process.stderr):
        try:
            if stream is not None:
                stream.close()
        except OSError:
            pass


def _enumerate_rg_files(
    command: list[str],
    max_files: int,
    deadline: float,
) -> tuple[list[str], bool, bool, int | None, bool]:
    """Enumerate at most ``max_files`` paths with one bounded rg walk."""

    kwargs: dict[str, Any] = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    elif os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    windows_job = _create_windows_job(process)
    paths: queue.Queue[bytes] = queue.Queue(maxsize=_RG_STREAM_QUEUE_SIZE)
    stdout_done = threading.Event()
    stderr_done = threading.Event()
    overflow = threading.Event()
    stdout_thread = threading.Thread(
        target=_rg_path_reader,
        args=(process.stdout, paths, stdout_done, overflow),
        name="home-mcp-rg-files-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_rg_stderr_drainer,
        args=(process.stderr, stderr_done),
        name="home-mcp-rg-files-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    found: list[str] = []
    timed_out = False
    truncated = False
    stop_reason = ""
    try:
        while True:
            if overflow.is_set():
                stop_reason = "output_limit"
                truncated = True
                break
            now = time.monotonic()
            if now >= deadline:
                stop_reason = "timeout"
                timed_out = True
                break
            if len(found) >= max_files:
                stop_reason = "max_files"
                truncated = True
                break
            if process.poll() is not None and stdout_done.is_set() and stderr_done.is_set() and paths.empty():
                break
            try:
                raw_path = paths.get(timeout=min(0.05, max(0.001, deadline - now)))
            except queue.Empty:
                continue
            path_text = raw_path.decode("utf-8", errors="replace")
            if path_text:
                found.append(path_text)
                if len(found) >= max_files:
                    stop_reason = "max_files"
                    truncated = True
                    break
    finally:
        if stop_reason:
            _stop_rg_process(process, windows_job)
            windows_job = None
        else:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                stop_reason = "timeout"
                _stop_rg_process(process, windows_job)
                windows_job = None
        _close_windows_job(windows_job)
        stdout_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
    return found, timed_out, truncated, process.returncode, stop_reason == "max_files"


def _run_rg_stream(
    command: list[str],
    root_path: Path,
    max_results: int,
    file_limit: int | None,
    deadline: float,
    output_limit_bytes: int = _RG_STREAM_MAX_OUTPUT_BYTES,
) -> tuple[dict[str, Any], int | None]:
    """Run fixed-argv rg with bounded streaming output.

    The parser stops at the first result limit, output budget, or deadline.
    ``scanned_files`` is derived from both rg's summary and unique paths seen
    in begin/match events, so early termination still reports useful progress.
    """

    kwargs: dict[str, Any] = {}
    if os.name == "nt" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
    elif os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **kwargs,
    )
    windows_job = _create_windows_job(process)
    lines: queue.Queue[bytes] = queue.Queue(maxsize=_RG_STREAM_QUEUE_SIZE)
    stdout_done = threading.Event()
    stderr_done = threading.Event()
    overflow = threading.Event()
    stdout_thread = threading.Thread(
        target=_rg_stdout_reader,
        args=(process.stdout, lines, stdout_done, overflow),
        name="home-mcp-rg-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_rg_stderr_drainer,
        args=(process.stderr, stderr_done),
        name="home-mcp-rg-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    result_limit = _positive_limit(max_results, _DEFAULT_SEARCH_MAX_RESULTS)
    results: list[dict[str, Any]] = []
    total_matches = 0
    summary_scanned = 0
    seen_files: set[str] = set()
    output_bytes = 0
    timed_out = False
    truncated = False
    partial = False
    stop_reason = ""
    file_limit_hit = False

    def parse_line(raw_line: bytes) -> None:
        nonlocal total_matches, summary_scanned, output_bytes, truncated, stop_reason, file_limit_hit
        output_bytes += len(raw_line) + 1
        if output_bytes > output_limit_bytes:
            stop_reason = "output_limit"
            truncated = True
            return
        try:
            event = json.loads(raw_line.decode("utf-8", errors="replace"))
        except json.JSONDecodeError:
            return
        event_type = event.get("type")
        data = event.get("data", {})
        if event_type in {"begin", "match", "end"}:
            path_text = _rg_json_text(data.get("path", {}))
            if path_text:
                if path_text not in seen_files:
                    if file_limit is not None and len(seen_files) >= file_limit:
                        file_limit_hit = True
                        truncated = True
                        stop_reason = "max_files"
                        return
                    seen_files.add(path_text)
        if event_type == "match":
            total_matches += 1
            if len(results) < result_limit:
                results.append({
                    "path": _rg_json_text(data.get("path", {})),
                    "line": int(data.get("line_number") or 0),
                    "text": _rg_json_text(data.get("lines", {})).rstrip("\r\n"),
                })
            if len(results) >= result_limit:
                # Contract: reaching max_results marks the response
                # truncated because additional matches are unknown without an
                # unbounded lookahead.
                truncated = True
                stop_reason = "max_results"
        elif event_type == "summary":
            try:
                summary_scanned = int(data.get("stats", {}).get("searches", 0))
            except (TypeError, ValueError):
                summary_scanned = 0
            if file_limit is not None and summary_scanned > file_limit:
                file_limit_hit = True
                truncated = True

    try:
        while True:
            if overflow.is_set():
                stop_reason = stop_reason or "output_limit"
                truncated = True
                break
            now = time.monotonic()
            if now >= deadline:
                stop_reason = "timeout"
                timed_out = True
                break
            if process.poll() is not None and stdout_done.is_set() and stderr_done.is_set() and lines.empty():
                break
            try:
                raw_line = lines.get(timeout=min(0.05, max(0.001, deadline - now)))
            except queue.Empty:
                continue
            parse_line(raw_line)
            if stop_reason:
                break
    finally:
        if stop_reason:
            partial = True
            _stop_rg_process(process, windows_job)
            windows_job = None
        else:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                timed_out = True
                partial = True
                stop_reason = "timeout"
                _stop_rg_process(process, windows_job)
                windows_job = None
        _close_windows_job(windows_job)
        stdout_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    scanned = max(summary_scanned, len(seen_files))
    if file_limit_hit and file_limit is not None:
        scanned = min(scanned, file_limit)
    result = {
        "root": str(root_path),
        "scanned_files": scanned,
        "results": results,
        "truncated": truncated,
        "backend": "ripgrep",
        "excluded_directories": list(_DEFAULT_SEARCH_EXCLUDES),
        "timed_out": timed_out,
        "partial": partial or file_limit_hit,
        "_output_bytes": output_bytes,
    }
    return result, process.returncode


def _rg_exclude_args() -> list[str]:
    args: list[str] = []
    for directory in _DEFAULT_SEARCH_EXCLUDES:
        args.extend(["--glob", f"!**/{directory}/**"])
    return args


def _rg_files_command(rg: str, root_path: Path, file_glob: str) -> list[str]:
    return [
        rg,
        "--files",
        "-0",
        "--hidden",
        "--no-ignore",
        "--glob",
        file_glob,
        *_rg_exclude_args(),
        "--",
        str(root_path),
    ]


def _rg_search_command(
    rg: str,
    query: str,
    file_glob: str,
    case_sensitive: bool,
    paths: list[str],
) -> list[str]:
    command = [
        rg,
        "--json",
        "--fixed-strings",
        "--hidden",
        "--no-ignore",
        "--glob",
        file_glob,
        *_rg_exclude_args(),
    ]
    if not case_sensitive:
        command.append("--ignore-case")
    command.extend(["--", query, *paths])
    return command


def _rg_path_batches(paths: list[str]) -> list[list[str]]:
    """Split absolute paths below conservative argv limits."""

    # CreateProcess has a 32K command-line limit on Windows.  Keep a much
    # smaller budget for all hosts so long path/Unicode quoting stays safe.
    budget = 24_000 if os.name == "nt" else 100_000
    batches: list[list[str]] = []
    current: list[str] = []
    current_bytes = 0
    for path in paths:
        path_bytes = len(os.fsencode(path)) + 1
        if current and current_bytes + path_bytes > budget:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(path)
        current_bytes += path_bytes
    if current:
        batches.append(current)
    return batches


def _search_rg_file_set(
    rg: str,
    root_path: Path,
    query: str,
    file_glob: str,
    case_sensitive: bool,
    max_results: int,
    paths: list[str],
    deadline: float,
    enumeration_timed_out: bool,
    enumeration_truncated: bool,
) -> tuple[dict[str, Any], bool]:
    """Search an already bounded file set in command-line-safe batches."""

    result_limit = _positive_limit(max_results, _DEFAULT_SEARCH_MAX_RESULTS)
    aggregate_results: list[dict[str, Any]] = []
    output_used = 0
    timed_out = enumeration_timed_out
    partial = enumeration_timed_out or enumeration_truncated
    truncated = enumeration_truncated
    error = False

    if enumeration_timed_out:
        return {
            "root": str(root_path),
            "scanned_files": len(paths),
            "results": [],
            "truncated": True,
            "backend": "ripgrep",
            "excluded_directories": list(_DEFAULT_SEARCH_EXCLUDES),
            "timed_out": True,
            "partial": True,
        }, False

    for batch in _rg_path_batches(paths):
        if time.monotonic() >= deadline:
            timed_out = True
            partial = True
            break
        remaining_results = max(1, result_limit - len(aggregate_results))
        remaining_output = _RG_STREAM_MAX_OUTPUT_BYTES - output_used
        if remaining_output <= 0:
            truncated = True
            partial = True
            break
        batch_result, returncode = _run_rg_stream(
            _rg_search_command(rg, query, file_glob, case_sensitive, batch),
            root_path,
            remaining_results,
            len(batch),
            deadline,
            remaining_output,
        )
        output_used += int(batch_result.pop("_output_bytes", 0))
        aggregate_results.extend(batch_result.get("results", []))
        if len(aggregate_results) > result_limit:
            del aggregate_results[result_limit:]
        timed_out = timed_out or bool(batch_result.get("timed_out"))
        partial = partial or bool(batch_result.get("partial"))
        truncated = truncated or bool(batch_result.get("truncated"))
        if returncode not in (0, 1) and not batch_result.get("partial"):
            error = True
            break
        if timed_out or len(aggregate_results) >= result_limit or output_used >= _RG_STREAM_MAX_OUTPUT_BYTES:
            break

    return {
        "root": str(root_path),
        # The enumeration set is the exact bounded set searched, including
        # files with no matching lines.
        "scanned_files": len(paths),
        "results": aggregate_results,
        "truncated": truncated or len(aggregate_results) >= result_limit,
        "backend": "ripgrep",
        "excluded_directories": list(_DEFAULT_SEARCH_EXCLUDES),
        "timed_out": timed_out,
        "partial": partial or error,
    }, error


def search_text(
    root: str,
    query: str,
    file_glob: str = "*",
    case_sensitive: bool = False,
    max_results: int = 0,
    timeout_sec: float = _DEFAULT_SEARCH_TIMEOUT_SEC,
    max_files: int | None = None,
) -> dict[str, Any]:
    root_path = Path(root).expanduser()
    timeout = _positive_budget(timeout_sec, _DEFAULT_SEARCH_TIMEOUT_SEC)
    file_limit = _positive_limit(max_files, _DEFAULT_SEARCH_MAX_FILES) if max_files is not None else None
    deadline = time.monotonic() + timeout
    rg = shutil.which("rg")
    if not rg:
        return _search_text_python(
            root_path,
            query,
            file_glob,
            case_sensitive,
            max_results,
            timeout,
            file_limit,
            deadline,
        )

    if max_files is not None:
        enumeration_error = False
        try:
            paths, enumeration_timed_out, enumeration_truncated, enumeration_returncode, enumeration_stopped_by_limit = _enumerate_rg_files(
                _rg_files_command(rg, root_path, file_glob),
                file_limit,
                deadline,
            )
        except OSError:
            enumeration_error = True
            paths = []
            enumeration_timed_out = False
            enumeration_truncated = False
            enumeration_returncode = None
            enumeration_stopped_by_limit = False
        if (enumeration_error or (enumeration_returncode not in (None, 0, 1) and not enumeration_stopped_by_limit)) and not enumeration_timed_out:
            return _search_text_python(
                root_path,
                query,
                file_glob,
                case_sensitive,
                max_results,
                timeout,
                file_limit,
                deadline,
            )
        result, error = _search_rg_file_set(
            rg,
            root_path,
            query,
            file_glob,
            case_sensitive,
            max_results,
            paths,
            deadline,
            enumeration_timed_out,
            enumeration_truncated,
        )
        if error:
            return _search_text_python(
                root_path,
                query,
                file_glob,
                case_sensitive,
                max_results,
                timeout,
                file_limit,
                deadline,
            )
        return result

    command = [
        *_rg_search_command(rg, query, file_glob, case_sensitive, [str(root_path)]),
    ]

    try:
        result, returncode = _run_rg_stream(command, root_path, max_results, file_limit, deadline)
        result.pop("_output_bytes", None)
        if returncode not in (0, 1) and not result["partial"]:
            return _search_text_python(
                root_path,
                query,
                file_glob,
                case_sensitive,
                max_results,
                timeout,
                file_limit,
                deadline,
            )
        return result
    except OSError:
        # rg can disappear between ``which`` and process creation.  This is a
        # launch failure, not an execution timeout, so a bounded fallback is
        # still appropriate.
        return _search_text_python(
            root_path,
            query,
            file_glob,
            case_sensitive,
            max_results,
            timeout,
            file_limit,
            deadline,
        )


def path_info(path: str) -> dict[str, Any]:
    p = Path(path).expanduser()
    exists = p.exists() or p.is_symlink()
    out: dict[str, Any] = {
        "path": str(p),
        "absolute": str(p.absolute()),
        "exists": exists,
        "is_file": p.is_file(),
        "is_dir": p.is_dir(),
        "is_symlink": p.is_symlink(),
    }
    if exists:
        st = p.lstat()
        out.update({
            "size": st.st_size,
            "mtime": st.st_mtime,
            "ctime": st.st_ctime,
            "mode": st.st_mode,
        })
        if p.is_symlink():
            out["symlink_target"] = os.readlink(p)
    return out


def read_text(path: str, encoding: str = "utf-8", max_chars: int = 0) -> dict[str, Any]:
    p = Path(path).expanduser()
    text = p.read_text(encoding=encoding, errors="replace")
    text, truncated = _limit(text, max_chars)
    return {"path": str(p), "text": text, "truncated": truncated}


def write_text(path: str, text: str, encoding: str = "utf-8", create_parents: bool = True) -> dict[str, Any]:
    p = Path(path).expanduser()
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding=encoding)
    return {"path": str(p), "bytes": p.stat().st_size}


def append_text(path: str, text: str, encoding: str = "utf-8", create_parents: bool = True) -> dict[str, Any]:
    p = Path(path).expanduser()
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding=encoding) as f:
        f.write(text)
    return {"path": str(p), "bytes": p.stat().st_size}


def read_file_base64(path: str) -> dict[str, Any]:
    p = Path(path).expanduser()
    data = p.read_bytes()
    return {"path": str(p), "size": len(data), "base64": base64.b64encode(data).decode("ascii")}


def write_file_base64(path: str, base64_data: str, create_parents: bool = True) -> dict[str, Any]:
    p = Path(path).expanduser()
    if create_parents:
        p.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(base64_data)
    p.write_bytes(data)
    return {"path": str(p), "bytes": len(data)}


def make_directory(path: str, parents: bool = True, exist_ok: bool = True) -> dict[str, Any]:
    p = Path(path).expanduser()
    p.mkdir(parents=parents, exist_ok=exist_ok)
    return {"path": str(p), "created": True}


def copy_path(source: str, destination: str, overwrite: bool = True) -> dict[str, Any]:
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists() and not overwrite:
            raise FileExistsError(str(dst))
        shutil.copytree(src, dst, dirs_exist_ok=overwrite)
    else:
        if dst.exists() and not overwrite:
            raise FileExistsError(str(dst))
        shutil.copy2(src, dst)
    return {"source": str(src), "destination": str(dst)}


def move_path(source: str, destination: str) -> dict[str, Any]:
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()
    dst.parent.mkdir(parents=True, exist_ok=True)
    result = shutil.move(str(src), str(dst))
    return {"source": str(src), "destination": result}


def delete_path(path: str, recursive: bool = True) -> dict[str, Any]:
    p = Path(path).expanduser()
    if p.is_dir() and not p.is_symlink():
        if recursive:
            shutil.rmtree(p)
        else:
            p.rmdir()
    else:
        p.unlink()
    return {"path": str(p), "deleted": True}


def hash_file(path: str, algorithm: str = "sha256") -> dict[str, Any]:
    p = Path(path).expanduser()
    h = hashlib.new(algorithm)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return {"path": str(p), "algorithm": algorithm, "digest": h.hexdigest(), "size": p.stat().st_size}


def _create_windows_job(process: subprocess.Popen[Any]) -> tuple[Any, Any, Any] | None:
    """Assign a process to a kill-on-close Windows Job Object.

    A shell can exit while a descendant keeps the captured pipe open.  In
    that case ``psutil.Process(process.pid).children()`` is empty by the time
    the timeout handler runs, so a best-effort tree walk cannot clean the
    descendant.  A Job Object follows the process lifetime instead.  The
    returned tuple keeps the loaded kernel32 module alive alongside the
    handle; callers must close it on every path.
    """

    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class _LargeInteger(ctypes.Structure):
            _fields_ = [("QuadPart", ctypes.c_longlong)]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", _LargeInteger),
                ("PerJobUserTimeLimit", _LargeInteger),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.INT,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        # Do not enable KILL_ON_JOB_CLOSE until a timeout is confirmed.  A
        # successful normal command may intentionally launch a background
        # process, and the historical run_command behavior left that process
        # alive after the shell returned.
        info = _ExtendedLimitInformation()
        ok = kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        process_handle = wintypes.HANDLE(getattr(process, "_handle"))
        ok = ok and kernel32.AssignProcessToJobObject(job, process_handle)
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return kernel32, job, _ExtendedLimitInformation
    except Exception:
        # Job Objects are an enhancement over the psutil/taskkill fallback;
        # restricted Windows hosts can deny one of the calls above.
        return None


def _enable_windows_job_kill_on_close(job: tuple[Any, Any, Any] | None) -> None:
    if job is None:
        return
    kernel32, handle, info_type = job
    try:
        info = info_type()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        kernel32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
    except Exception:
        pass


def _close_windows_job(job: tuple[Any, Any, Any] | None) -> None:
    if job is None:
        return
    kernel32, handle, _info_type = job
    try:
        kernel32.CloseHandle(handle)
    except Exception:
        pass


def _kill_process_tree(process: subprocess.Popen[Any], force: bool = True) -> list[int]:
    """Terminate *process* and descendants without waiting on inherited pipes.

    ``subprocess.run(..., timeout=...)`` kills only the direct process and then
    calls ``communicate()`` again.  On Windows a shell child can retain the
    stdout/stderr pipe, so that second communicate can block for the child's
    full lifetime.  Capture descendants first, terminate the tree, and let
    the caller perform only a short, bounded final collection.
    """

    affected: list[int] = []
    descendants: list[psutil.Process] = []
    try:
        root = psutil.Process(process.pid)
        descendants = root.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        descendants = []

    # taskkill /T is the most reliable Windows primitive for a shell tree,
    # including descendants which have already detached from psutil's view.
    # The command itself is bounded and never inherits the target's pipes.
    if os.name == "nt":
        try:
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1.0,
                creationflags=creationflags,
                check=False,
            )
            affected.append(process.pid)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif hasattr(os, "killpg"):
        # ``start_new_session=True`` below gives the process its own group.
        try:
            os.killpg(process.pid, 9 if force else 15)
            affected.append(process.pid)
        except (OSError, ProcessLookupError):
            pass

    # Keep a psutil fallback for non-Windows platforms and for Windows where
    # taskkill is unavailable (for example, constrained test environments).
    for child in reversed(descendants):
        try:
            (child.kill() if force else child.terminate())
            affected.append(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    try:
        (process.kill() if force else process.terminate())
        affected.append(process.pid)
    except (OSError, ProcessLookupError):
        pass
    return list(dict.fromkeys(affected))


def _collect_after_kill(
    process: subprocess.Popen[Any],
    stdout: bytes | str | None,
    stderr: bytes | str | None,
    wait_sec: float = 0.5,
) -> tuple[bytes | str | None, bytes | str | None]:
    """Collect output after a tree kill, never waiting indefinitely."""

    try:
        output, errors = process.communicate(timeout=wait_sec)
        return output, errors
    except subprocess.TimeoutExpired as exc:
        # At this point the tree was killed; a detached process may still hold
        # a pipe in a hostile environment.  Closing our ends prevents the
        # gateway request from waiting on it forever.  Keep the bytes already
        # observed by communicate for diagnostic output.
        observed_out = exc.output if exc.output is not None else stdout
        observed_err = exc.stderr if exc.stderr is not None else stderr
        for stream in (process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            process.wait(timeout=wait_sec)
        except (subprocess.TimeoutExpired, OSError):
            pass
        return observed_out, observed_err


def _run(
    command: str | list[str],
    cwd: str | None = None,
    timeout_sec: float = 0,
    env: dict[str, str] | None = None,
    shell: bool = False,
    max_output_chars: int = 0,
) -> dict[str, Any]:
    start = time.monotonic()
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        # A separate process group is useful for Ctrl+C/taskkill and avoids a
        # shell descendant inheriting the gateway's console.  CREATE_NO_WINDOW
        # preserves the previous non-interactive gateway behaviour.
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        flags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if flags:
            kwargs["creationflags"] = flags
    else:
        # Gives killpg a safe boundary for shell=True and direct Python tools.
        kwargs["start_new_session"] = True

    proc: subprocess.Popen[Any] | None = None
    windows_job: tuple[Any, Any, Any] | None = None
    try:
        proc = subprocess.Popen(
            command,
            cwd=str(Path(cwd).expanduser()) if cwd else None,
            env=_env(env),
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **kwargs,
        )
        windows_job = _create_windows_job(proc)
        proc_stdout, proc_stderr = proc.communicate(timeout=None if timeout_sec <= 0 else timeout_sec)
        stdout, out_trunc = _limit(_decode(proc_stdout), max_output_chars)
        stderr, err_trunc = _limit(_decode(proc_stderr), max_output_chars)
        return {
            "command": command,
            "cwd": cwd,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_truncated": out_trunc,
            "stderr_truncated": err_trunc,
            "duration_sec": time.monotonic() - start,
        }
    except subprocess.TimeoutExpired as exc:
        if proc is not None:
            _kill_process_tree(proc)
            # Closing a kill-on-close Job Object removes descendants even if
            # the direct shell process has already exited and is no longer
            # visible to psutil/taskkill.
            _enable_windows_job_kill_on_close(windows_job)
            _close_windows_job(windows_job)
            windows_job = None
            stdout_raw, stderr_raw = _collect_after_kill(proc, exc.stdout, exc.stderr)
        else:
            stdout_raw, stderr_raw = exc.stdout, exc.stderr
        stdout_text, out_trunc = _limit(_decode(stdout_raw or b""), max_output_chars)
        stderr_text, err_trunc = _limit(_decode(stderr_raw or b""), max_output_chars)
        return {
            "command": command,
            "cwd": cwd,
            "timed_out": True,
            "timeout_sec": timeout_sec,
            "stdout": stdout_text,
            "stderr": stderr_text,
            "stdout_truncated": out_trunc,
            "stderr_truncated": err_trunc,
            "returncode": proc.returncode if proc is not None else None,
            "duration_sec": time.monotonic() - start,
        }
    finally:
        _close_windows_job(windows_job)


def run_command(
    command: str,
    cwd: str | None = None,
    timeout_sec: float = 0,
    env: dict[str, str] | None = None,
    max_output_chars: int = 0,
) -> dict[str, Any]:
    return _run(command, cwd=cwd, timeout_sec=timeout_sec, env=env, shell=True, max_output_chars=max_output_chars)


def run_powershell(
    script: str,
    cwd: str | None = None,
    timeout_sec: float = 0,
    env: dict[str, str] | None = None,
    max_output_chars: int = 0,
) -> dict[str, Any]:
    exe = shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    if not exe:
        raise FileNotFoundError("PowerShell (pwsh or powershell.exe) was not found")
    return _run([exe, "-NoProfile", "-NonInteractive", "-Command", script], cwd, timeout_sec, env, False, max_output_chars)


def run_python(
    code: str,
    cwd: str | None = None,
    timeout_sec: float = 0,
    env: dict[str, str] | None = None,
    max_output_chars: int = 0,
) -> dict[str, Any]:
    return _run([sys.executable, "-c", code], cwd, timeout_sec, env, False, max_output_chars)


def start_process(command: str, cwd: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    run_dir = Path(tempfile.mkdtemp(prefix="home_mcp_process_"))
    stdout_path = run_dir / "stdout.log"
    stderr_path = run_dir / "stderr.log"
    stdout_f = stdout_path.open("ab", buffering=0)
    stderr_f = stderr_path.open("ab", buffering=0)
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    proc = subprocess.Popen(
        command,
        cwd=str(Path(cwd).expanduser()) if cwd else None,
        env=_env(env),
        shell=True,
        stdin=subprocess.DEVNULL,
        stdout=stdout_f,
        stderr=stderr_f,
        **kwargs,
    )
    _BG[proc.pid] = {
        "process": proc,
        "stdout_file": stdout_f,
        "stderr_file": stderr_f,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
        "command": command,
        "cwd": cwd,
        "started_at": time.time(),
    }
    return {
        "pid": proc.pid,
        "command": command,
        "cwd": cwd,
        "stdout_path": str(stdout_path),
        "stderr_path": str(stderr_path),
    }


def list_processes(
    name_contains: str = "",
    detailed: bool = False,
    include_details: bool | None = None,
) -> dict[str, Any]:
    """List processes using cheap attributes by default.

    ``status``, ``create_time`` and especially ``ppid`` can trigger an
    expensive query for every process on Windows.  They are intentionally
    opt-in; ``process_status(pid)`` remains the detailed per-process API.
    ``include_details`` is accepted as a descriptive compatibility alias for
    callers that prefer that spelling.
    """

    if include_details is not None:
        detailed = bool(include_details)
    attrs = ["pid", "name", "exe", "cmdline", "username"]
    if detailed:
        attrs.extend(["ppid", "status", "create_time"])
    needle = str(name_contains or "").lower()
    items: list[dict[str, Any]] = []
    for p in psutil.process_iter(attrs):
        try:
            info = dict(p.info)
            cmdline = info.get("cmdline")
            if isinstance(cmdline, (list, tuple)):
                cmdline_text = " ".join(str(part) for part in cmdline)
            else:
                cmdline_text = str(cmdline or "")
            text = " ".join([
                str(info.get("name") or ""),
                str(info.get("exe") or ""),
                cmdline_text,
            ]).lower()
            if needle and needle not in text:
                continue
            items.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return {"processes": items}


def process_status(pid: int, tail_chars: int = 20000) -> dict[str, Any]:
    out: dict[str, Any] = {"pid": pid}
    try:
        p = psutil.Process(pid)
        out.update({
            "running": p.is_running(),
            "status": p.status(),
            "name": p.name(),
            "exe": p.exe() if p.exe() else None,
            "cmdline": p.cmdline(),
            "create_time": p.create_time(),
        })
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        out.update({"running": False, "process_error": repr(exc)})

    reg = _BG.get(pid)
    if reg:
        proc = reg["process"]
        out["returncode"] = proc.poll()
        for key in ("stdout_path", "stderr_path"):
            path = Path(reg[key])
            try:
                text = _decode(path.read_bytes())
                out[key.replace("_path", "")] = text[-tail_chars:] if tail_chars > 0 else text
                out[key] = str(path)
            except Exception as exc:
                out[key.replace("_path", "_error")] = repr(exc)
    return out


def kill_process(pid: int, recursive: bool = True, force: bool = True) -> dict[str, Any]:
    p = psutil.Process(pid)
    targets = p.children(recursive=True) if recursive else []
    targets.append(p)
    acted: list[int] = []
    for proc in reversed(targets):
        try:
            (proc.kill() if force else proc.terminate())
            acted.append(proc.pid)
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(targets, timeout=5)
    reg = _BG.pop(pid, None)
    if reg:
        try:
            reg["stdout_file"].close()
            reg["stderr_file"].close()
        except Exception:
            pass
    return {"pid": pid, "affected_pids": acted, "force": force}


def http_request(
    method: str,
    url: str,
    headers: dict[str, str] | None = None,
    body: str | None = None,
    body_base64: str | None = None,
    timeout_sec: float = 60,
    verify_tls: bool = True,
    max_response_bytes: int = 0,
) -> dict[str, Any]:
    data: bytes | None = None
    if body_base64 is not None:
        data = base64.b64decode(body_base64)
    elif body is not None:
        data = body.encode("utf-8")

    req = urllib.request.Request(url=url, data=data, headers=headers or {}, method=method.upper())
    context = None
    if url.lower().startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()

    try:
        with urllib.request.urlopen(req, timeout=None if timeout_sec <= 0 else timeout_sec, context=context) as resp:
            raw = resp.read() if max_response_bytes <= 0 else resp.read(max_response_bytes + 1)
            truncated = max_response_bytes > 0 and len(raw) > max_response_bytes
            if truncated:
                raw = raw[:max_response_bytes]
            ctype = resp.headers.get_content_type()
            charset = resp.headers.get_content_charset()
            result: dict[str, Any] = {
                "url": resp.geturl(),
                "status": resp.status,
                "reason": resp.reason,
                "headers": dict(resp.headers.items()),
                "content_type": ctype,
                "bytes": len(raw),
                "truncated": truncated,
            }
            if ctype.startswith("text/") or "json" in ctype or "xml" in ctype or charset:
                result["text"] = raw.decode(charset or "utf-8", errors="replace")
            else:
                result["base64"] = base64.b64encode(raw).decode("ascii")
            return result
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return {
            "url": url,
            "status": exc.code,
            "reason": exc.reason,
            "headers": dict(exc.headers.items()) if exc.headers else {},
            "text": _decode(raw),
        }


def download_url(
    url: str,
    destination: str,
    headers: dict[str, str] | None = None,
    timeout_sec: float = 0,
    verify_tls: bool = True,
) -> dict[str, Any]:
    dst = Path(destination).expanduser()
    dst.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url=url, headers=headers or {}, method="GET")
    context = None
    if url.lower().startswith("https://") and not verify_tls:
        context = ssl._create_unverified_context()
    with urllib.request.urlopen(req, timeout=None if timeout_sec <= 0 else timeout_sec, context=context) as resp, dst.open("wb") as f:
        shutil.copyfileobj(resp, f)
    return {"url": url, "destination": str(dst), "bytes": dst.stat().st_size}


def screenshot(path: str = "") -> dict[str, Any]:
    from PIL import ImageGrab

    if path:
        dst = Path(path).expanduser()
        dst.parent.mkdir(parents=True, exist_ok=True)
    else:
        dst = Path(tempfile.gettempdir()) / f"home_mcp_screenshot_{int(time.time())}.png"
    image = ImageGrab.grab(all_screens=True)
    image.save(dst)
    return {"path": str(dst), "width": image.width, "height": image.height}
