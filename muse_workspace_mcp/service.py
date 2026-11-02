"""Core policy service for registered Muse workspaces."""
from __future__ import annotations

import inspect
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Mapping, Sequence

from .checks import CHECK_OUTPUT_BYTES, CHECK_TIMEOUT_SECONDS, Runner, command_for, validate_check
from .errors import WorkspaceError
from .paths import (
    MAX_GREP_BYTES,
    MAX_GREP_FILE_BYTES,
    MAX_GREP_FILES,
    MAX_GREP_MATCHES,
    MAX_LIST_ENTRIES,
    MAX_READ_BYTES,
    MAX_READ_CHARS,
    MAX_READ_LINES,
    MAX_WRITE_BYTES,
    canonical_root,
    is_denied_name,
    is_allowed_extension,
    safe_path,
    safe_relative_name,
    validate_lexical_path,
)
from .registry import load_registry, set_active, validate_workspace_entry

MAX_PATCH_BYTES = 1024 * 1024
MAX_PATCH_FILES = 128
MAX_PATCH_LINES = 50_000
MAX_GIT_STATUS_BYTES = 128 * 1024
MAX_GIT_DIFF_BYTES = 512 * 1024
MAX_GIT_PATHS = 512
MAX_LINE_ARGS = 1_000_000
MAX_WRITES_PER_MINUTE = 60
MAX_WORKSPACE_FILES = 10_000
# Keep path nesting and the amount of directory creation as separate limits.
# The latter is intentionally lower so one write cannot provision a long tree
# even when all of its parent components are missing.
MAX_WRITE_PARENT_DEPTH = 6
MAX_WRITE_DIRECTORIES = 4


def _raise(code: str) -> None:
    raise WorkspaceError(code)


def _bounded_int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _proc_kwargs() -> dict[str, Any]:
    value: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "stdin": subprocess.PIPE,
        "shell": False,
    }
    if os.name == "nt":
        value["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return value


def _git_env() -> dict[str, str]:
    env = {
        key: os.environ[key]
        for key in ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL")
        if key in os.environ
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_PAGER": "cat",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "NUL",
        }
    )
    return env


def _drain_pipe(stream: Any, maximum: int, result: dict[str, Any], key: str) -> None:
    kept = bytearray()
    truncated = False
    try:
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            if not isinstance(chunk, (bytes, bytearray)):
                chunk = bytes(chunk)
            if len(kept) < maximum:
                remaining = maximum - len(kept)
                kept.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    truncated = True
            else:
                truncated = True
    except (OSError, ValueError, TypeError):
        truncated = True
    result[key] = bytes(kept)
    result[key + "_truncated"] = truncated


def _git_run(
    worktree: Path,
    args: Sequence[str],
    *,
    input_data: bytes | None = None,
    max_output: int = MAX_GIT_DIFF_BYTES,
    timeout: float = 20.0,
) -> tuple[int, bytes, bytes, bool]:
    if not args or any(not isinstance(item, str) or "\x00" in item for item in args):
        _raise("invalid_input")
    try:
        process = subprocess.Popen(
            [
                "git",
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=NUL",
                "-c",
                "core.autocrlf=input",
                "-C",
                os.fspath(worktree),
                "--literal-pathspecs",
                *args,
            ],
            cwd=os.fspath(worktree),
            env=_git_env(),
            **_proc_kwargs(),
        )
    except OSError:
        _raise("git_error")
    # Keep the small fake-process seam used by tests and embedding callers.
    # Real subprocesses expose pipes and wait(); those use bounded drain
    # threads so communicate() cannot retain an unbounded Git response.
    if not all(hasattr(process, attribute) for attribute in ("stdout", "stderr", "stdin", "wait")):
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.communicate()
            _raise("git_error")
        stdout = stdout or b""
        stderr = stderr or b""
        return (
            process.returncode,
            stdout[:max_output],
            stderr[:max_output],
            len(stdout) > max_output or len(stderr) > max_output,
        )

    output: dict[str, Any] = {}
    stdout_thread = threading.Thread(
        target=_drain_pipe, args=(process.stdout, max_output, output, "stdout"), daemon=True
    )
    stderr_thread = threading.Thread(
        target=_drain_pipe, args=(process.stderr, max_output, output, "stderr"), daemon=True
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        try:
            if process.stdin is not None:
                if input_data is not None:
                    process.stdin.write(input_data)
                    process.stdin.flush()
                process.stdin.close()
        except (BrokenPipeError, OSError, ValueError):
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except (OSError, ValueError):
                pass
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            _raise("git_error")
    finally:
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
    stdout = output.get("stdout", b"")
    stderr = output.get("stderr", b"")
    truncated = bool(output.get("stdout_truncated") or output.get("stderr_truncated"))
    return process.returncode, stdout, stderr, truncated


class WorkspaceService:
    """Reread registry state and enforce bounded filesystem/Git operations."""

    def __init__(
        self,
        registry_path: str | os.PathLike[str],
        audit_path: str | os.PathLike[str],
        *,
        checks_enabled: bool = False,
        check_runner: Any = None,
    ) -> None:
        try:
            registry = Path(registry_path)
            audit = Path(audit_path)
        except (TypeError, ValueError):
            _raise("invalid_input")
        if not registry.is_absolute() or not audit.is_absolute() or not isinstance(checks_enabled, bool):
            _raise("invalid_input")
        self.registry_path = registry
        self.audit_path = audit
        self._audit_path = audit
        self.checks_enabled = checks_enabled
        self.check_runner = check_runner if check_runner is not None else Runner()
        self._write_lock = threading.Lock()
        self._write_events: list[float] = []
        self._mutation_lock = threading.Lock()
        self._runner_lock = threading.Lock()

    def _registry(self) -> dict[str, Any]:
        # Parse the complete schema while retaining expired siblings.  Each
        # operation validates only the selected entry as live.
        return load_registry(self.registry_path, require_live=False)

    def _entry(self, workspace: object = None) -> tuple[dict[str, Any], dict[str, Any], Path]:
        document = self._registry()
        selected = document.get("active") if workspace is None else workspace
        if isinstance(selected, Mapping):
            selected = selected.get("id")
        if not isinstance(selected, str) or not selected:
            _raise("workspace_not_found")
        found = [item for item in document.get("workspaces", []) if item.get("id") == selected]
        if len(found) != 1:
            _raise("workspace_not_found")
        entry = validate_workspace_entry(
            found[0],
            snapshots_root=document.get("snapshots_root"),
            require_live=True,
        )
        try:
            root = canonical_root(entry["path"])
        except WorkspaceError:
            _raise("invalid_workspace")
        return document, entry, root

    @staticmethod
    def _relative(root: Path, value: Path | str | None) -> str | None:
        if value is None:
            return None
        try:
            result = Path(value).relative_to(root).as_posix()
            return result if result not in {"", "."} else "."
        except (TypeError, ValueError):
            return None

    def _audit(
        self,
        operation: str,
        entry: Mapping[str, Any] | None,
        *,
        root: Path | None = None,
        path: Path | str | None = None,
        outcome: str = "ok",
        **metadata: Any,
    ) -> None:
        record: dict[str, Any] = {"tool": operation, "result": outcome}
        if entry is not None:
            record["workspace"] = entry.get("id")
        if root is not None:
            relative = self._relative(root, path)
            if relative is not None:
                record["path"] = relative
        size = metadata.get("size", metadata.get("bytes"))
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            record["size"] = size
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            if self.audit_path.exists() or self.audit_path.is_symlink():
                info = os.lstat(self.audit_path)
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                    return
            with self.audit_path.open("ab") as handle:
                handle.write((json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return

    @staticmethod
    def _mutable(entry: Mapping[str, Any]) -> None:
        if bool(entry.get("read_only", False)) or entry.get("writable") is False:
            _raise("workspace_read_only")

    def _reserve_writes(self, count: int = 1) -> None:
        if not isinstance(count, int) or count < 1 or count > MAX_WRITES_PER_MINUTE:
            _raise("limit_exceeded")
        current = time.monotonic()
        with self._write_lock:
            self._write_events = [stamp for stamp in self._write_events if current - stamp < 60.0]
            if len(self._write_events) + count > MAX_WRITES_PER_MINUTE:
                _raise("limit_exceeded")
            self._write_events.extend([current] * count)

    @staticmethod
    def _check_workspace_file_count(root: Path, additional: int = 0) -> None:
        if not isinstance(additional, int) or isinstance(additional, bool) or additional < 0:
            _raise("invalid_input")
        pending = [root]
        count = 0
        while pending:
            current = pending.pop()
            try:
                children = os.scandir(current)
            except OSError:
                _raise("path_denied")
            with children:
                for child in children:
                    if child.name.casefold() == ".git":
                        continue
                    try:
                        info = os.lstat(child.path)
                    except OSError:
                        _raise("path_denied")
                    attrs = int(getattr(info, "st_file_attributes", 0) or 0)
                    if stat.S_ISLNK(info.st_mode) or attrs & 0x0400:
                        count += 1
                        if count + additional > MAX_WORKSPACE_FILES:
                            _raise("limit_exceeded")
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        pending.append(Path(child.path))
                    else:
                        count += 1
                        if count + additional > MAX_WORKSPACE_FILES:
                            _raise("limit_exceeded")

    def list_workspaces(self) -> dict[str, Any]:
        document = self._registry()
        values: list[dict[str, Any]] = []
        for item in document.get("workspaces", []):
            try:
                entry = validate_workspace_entry(
                    item, snapshots_root=document.get("snapshots_root"), require_live=True
                )
            except WorkspaceError as exc:
                if exc.code == "workspace_expired":
                    continue
                raise
            values.append(
                {
                    "id": entry["id"],
                    "kind": entry.get("kind", "worktree"),
                    "repo_label": entry.get("repo_label"),
                    "expires_at": entry["expires_at"],
                    "read_only": bool(entry.get("read_only", False)),
                    "checks": list(entry.get("checks", [])),
                }
            )
        active = document.get("active")
        if active not in {item["id"] for item in values}:
            active = None
        result = {"workspaces": values, "active": active}
        self._audit("list_workspaces", None, count=len(values))
        return result

    def use_workspace(self, id: str) -> dict[str, Any]:
        if not isinstance(id, str) or not id:
            _raise("invalid_input")
        document = self._registry()
        found = [item for item in document.get("workspaces", []) if item.get("id") == id]
        if len(found) != 1:
            _raise("workspace_not_found")
        entry = validate_workspace_entry(
            found[0], snapshots_root=document.get("snapshots_root"), require_live=True
        )
        set_active(self.registry_path, id, snapshots_root=document.get("snapshots_root"))
        self._audit("use_workspace", entry)
        return {"active": id, "workspace": id}

    def list_dir(self, path: str = ".", workspace: str | None = None) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            directory = safe_path(root, path, allow_directory=True, allow_file=False)
            values: list[dict[str, Any]] = []
            truncated = False
            try:
                iterator = os.scandir(directory)
            except OSError:
                _raise("path_not_found")
            with iterator as children:
                for child in children:
                    if len(values) >= MAX_LIST_ENTRIES:
                        truncated = True
                        break
                    if is_denied_name(child.name):
                        continue
                    try:
                        candidate = safe_path(
                            root,
                            child.path,
                            allow_absolute=True,
                            allow_directory=True,
                            allow_file=True,
                        )
                        info = os.lstat(candidate)
                    except WorkspaceError:
                        continue
                    if stat.S_ISDIR(info.st_mode):
                        kind = "directory"
                    elif stat.S_ISREG(info.st_mode):
                        kind = "file"
                    else:
                        continue
                    values.append(
                        {
                            "name": child.name,
                            "path": self._relative(root, candidate),
                            "kind": kind,
                            "size": int(info.st_size) if kind == "file" else None,
                        }
                    )
            values.sort(key=lambda item: str(item["name"]).casefold())
            result = {
                "workspace": entry["id"],
                "path": self._relative(root, directory) or ".",
                "entries": values,
                "truncated": truncated,
            }
            self._audit("list_dir", entry, root=root, path=directory, count=len(values), truncated=truncated)
            return result
        except WorkspaceError as exc:
            self._audit("list_dir", None, outcome=exc.code)
            raise

    def _read_bytes(self, path: Path, *, maximum: int = MAX_READ_BYTES) -> tuple[bytes, bool, int]:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            _raise("path_not_found")
        except OSError:
            _raise("path_denied")
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                _raise("path_denied")
            raw = os.read(descriptor, maximum + 1)
            return raw[:maximum], len(raw) > maximum or info.st_size > maximum, int(info.st_size)
        except WorkspaceError:
            raise
        except OSError:
            _raise("path_denied")
        finally:
            try:
                os.close(descriptor)
            except OSError:
                pass

    @staticmethod
    def _decode(raw: bytes) -> str:
        if b"\x00" in raw:
            _raise("binary_file")
        while True:
            try:
                return raw.decode("utf-8", "strict")
            except UnicodeDecodeError as exc:
                if exc.end == len(raw) and len(raw) > exc.start:
                    raw = raw[:exc.start]
                    continue
                _raise("unsupported_encoding")

    def _read_text_internal(
        self, root: Path, path: object, start_line: int = 1, end_line: int = 0
    ) -> tuple[Path, str, bool, int]:
        if not _bounded_int(start_line, 1, MAX_LINE_ARGS) or not _bounded_int(end_line, 0, MAX_LINE_ARGS):
            _raise("invalid_input")
        if end_line and end_line < start_line:
            _raise("invalid_input")
        candidate = safe_path(root, path, allow_directory=False, allow_file=True)
        if not is_allowed_extension(candidate):
            _raise("path_denied")
        raw, truncated, size = self._read_bytes(candidate)
        text = self._decode(raw)
        lines = text.splitlines(keepends=True)
        page_end = end_line if end_line else start_line + MAX_READ_LINES - 1
        if page_end > start_line + MAX_READ_LINES - 1:
            page_end = start_line + MAX_READ_LINES - 1
            truncated = True
        if len(lines) > page_end:
            truncated = True
        text = "".join(lines[start_line - 1 : page_end])
        if len(text) > MAX_READ_CHARS:
            text = text[:MAX_READ_CHARS]
            truncated = True
        return candidate, text, truncated, size

    def read_file(
        self,
        path: str,
        start_line: int = 1,
        end_line: int = 0,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            candidate, text, truncated, size = self._read_text_internal(root, path, start_line, end_line)
            result = {
                "workspace": entry["id"],
                "path": self._relative(root, candidate),
                "content": text,
                "start_line": start_line,
                "end_line": end_line,
                "size_bytes": size,
                "truncated": truncated,
            }
            self._audit("read_file", entry, root=root, path=candidate, bytes=len(text.encode("utf-8")))
            return result
        except WorkspaceError as exc:
            self._audit("read_file", None, outcome=exc.code)
            raise

    def _walk_files(self, root: Path, initial: Path) -> list[Path]:
        result: list[Path] = []
        pending = [initial]
        while pending and len(result) < MAX_GREP_FILES:
            current = pending.pop()
            try:
                info = os.lstat(current)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                result.append(current)
                continue
            if not stat.S_ISDIR(info.st_mode):
                continue
            try:
                children = list(os.scandir(current))
            except OSError:
                continue
            for child in sorted(children, key=lambda item: item.name.casefold(), reverse=True):
                if is_denied_name(child.name):
                    continue
                try:
                    candidate = safe_path(
                        root,
                        child.path,
                        allow_absolute=True,
                        allow_directory=True,
                        allow_file=True,
                    )
                except WorkspaceError:
                    continue
                pending.append(candidate)
                if len(pending) + len(result) >= MAX_GREP_FILES:
                    break
        return result

    def grep_text(
        self,
        pattern: str,
        path: str = ".",
        ignore_case: bool = True,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            if not isinstance(pattern, str) or not 1 <= len(pattern) <= 200 or "\x00" in pattern:
                _raise("invalid_input")
            if not isinstance(ignore_case, bool):
                _raise("invalid_input")
            initial = safe_path(root, path, allow_directory=True, allow_file=True)
            files = self._walk_files(root, initial)
            try:
                matcher = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
            except re.error:
                _raise("invalid_input")
            matches: list[dict[str, Any]] = []
            checked = 0
            examined_bytes = 0
            truncated = len(files) >= MAX_GREP_FILES
            for candidate in files:
                if len(matches) >= MAX_GREP_MATCHES or examined_bytes >= MAX_GREP_BYTES:
                    truncated = True
                    break
                try:
                    if not is_allowed_extension(candidate):
                        continue
                    if os.lstat(candidate).st_size > MAX_GREP_FILE_BYTES:
                        truncated = True
                        continue
                    raw, was_truncated, _ = self._read_bytes(
                        candidate, maximum=MAX_GREP_BYTES - examined_bytes
                    )
                    examined_bytes += len(raw)
                    text = self._decode(raw)
                    if was_truncated:
                        truncated = True
                except WorkspaceError as exc:
                    if exc.code in {"binary_file", "unsupported_encoding", "path_denied", "path_not_found"}:
                        continue
                    raise
                checked += 1
                for number, line in enumerate(text.splitlines(), 1):
                    if matcher.search(line):
                        matches.append(
                            {
                                "path": self._relative(root, candidate),
                                "line": number,
                                "text": line[:200],
                            }
                        )
                        if len(matches) >= MAX_GREP_MATCHES:
                            truncated = True
                            break
            result = {
                "workspace": entry["id"],
                "path": self._relative(root, initial),
                "matches": matches,
                "files_checked": checked,
                "truncated": truncated,
            }
            self._audit("grep_text", entry, root=root, path=initial, matches=len(matches), files=checked)
            return result
        except WorkspaceError as exc:
            self._audit("grep_text", None, outcome=exc.code)
            raise

    @staticmethod
    def _rollback_write_directories(
        created: Sequence[tuple[Path, tuple[int, int]]],
    ) -> None:
        """Remove only the empty directories created by a failed write.

        The identity check prevents rollback from removing a directory that a
        concurrent actor replaced after it was created.  ``os.rmdir`` is
        deliberately used instead of a recursive operation, so a new file or
        child directory prevents removal and remains available for recovery.
        """
        for directory, identity in reversed(created):
            try:
                info = os.lstat(directory)
                attributes = int(getattr(info, "st_file_attributes", 0) or 0)
                if (
                    not stat.S_ISDIR(info.st_mode)
                    or stat.S_ISLNK(info.st_mode)
                    or attributes & 0x0400
                    or (int(getattr(info, "st_dev", 0)), int(getattr(info, "st_ino", 0))) != identity
                ):
                    continue
                os.rmdir(directory)
            except OSError:
                continue

    @staticmethod
    def _write_path_parts(path: object) -> list[str]:
        """Validate write syntax and return normalized relative components."""
        raw = validate_lexical_path(path, relative=True)
        return [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]

    def _write_parent(
        self,
        root: Path,
        path: object,
    ) -> tuple[Path, Path, list[tuple[Path, tuple[int, int]]]]:
        """Resolve a write target and roll back provisioning on resolution failure."""
        created: list[tuple[Path, tuple[int, int]]] = []
        try:
            return self._write_parent_impl(root, path, created)
        except BaseException:
            self._rollback_write_directories(created)
            raise

    def _write_parent_impl(
        self,
        root: Path,
        path: object,
        created: list[tuple[Path, tuple[int, int]]],
    ) -> tuple[Path, Path, list[tuple[Path, tuple[int, int]]]]:
        """Resolve a write target, creating missing parents one level at a time."""
        parts = self._write_path_parts(path)
        parent_depth = max(len(parts) - 1, 0)
        if parent_depth > MAX_WRITE_PARENT_DEPTH:
            _raise("limit_exceeded")

        # A root target is handled by safe_path so callers retain its existing
        # ``is_directory`` result rather than creating anything implicitly.
        if not parts:
            target = safe_path(root, path, allow_missing=True, allow_directory=False, allow_file=True)
            parent = safe_path(
                root,
                str(target.parent),
                allow_absolute=True,
                allow_directory=True,
                allow_file=False,
            )
            return parent, target, []

        # ``validate_lexical_path`` treats both slash styles as separators on
        # every platform.  Feed the normalized spelling to safe_path as well
        # so POSIX callers cannot accidentally create a literal backslash
        # filename while provisioning a parent directory.
        normalized_path = "/".join(parts)
        for index in range(parent_depth):
            relative_parent = "/".join(parts[: index + 1])
            # safe_path performs the existing component, deny-list, reserved
            # name, ADS, short-name, and reparse checks before we touch a
            # missing component.
            candidate = safe_path(
                root,
                relative_parent,
                allow_missing=True,
                allow_directory=True,
                allow_file=False,
            )
            try:
                os.lstat(candidate)
            except FileNotFoundError:
                if len(created) >= MAX_WRITE_DIRECTORIES:
                    _raise("limit_exceeded")
                try:
                    # One level only: never let an OS helper create an
                    # unchecked intermediate path.
                    os.mkdir(candidate, 0o700)
                except FileExistsError:
                    # A concurrent creator may have won the race.  Re-run the
                    # full boundary check before using that entry.
                    candidate = safe_path(
                        root,
                        relative_parent,
                        allow_missing=False,
                        allow_directory=True,
                        allow_file=False,
                    )
                except OSError:
                    _raise("path_denied")
                else:
                    try:
                        info = os.lstat(candidate)
                    except OSError:
                        _raise("path_denied")
                    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
                    if (
                        not stat.S_ISDIR(info.st_mode)
                        or stat.S_ISLNK(info.st_mode)
                        or attributes & 0x0400
                    ):
                        _raise("path_denied")
                    created.append(
                        (
                            candidate,
                            (int(getattr(info, "st_dev", 0)), int(getattr(info, "st_ino", 0))),
                        )
                    )
                    # Record the just-created directory before the final
                    # realpath/containment check so a failed check can roll it
                    # back safely by identity.
                    candidate = safe_path(
                        root,
                        relative_parent,
                        allow_missing=False,
                        allow_directory=True,
                        allow_file=False,
                    )
            except OSError:
                _raise("path_denied")

        target = safe_path(root, normalized_path, allow_missing=True, allow_directory=False, allow_file=True)
        parent = safe_path(
            root,
            str(target.parent),
            allow_absolute=True,
            allow_directory=True,
            allow_file=False,
        )
        return parent, target, created

    def _write_locked(
        self,
        root: Path,
        path: str,
        encoded: bytes,
        create_only: bool,
    ) -> tuple[Path, bool]:
        """Write one file while the mutation lock is held."""
        created_directories: list[tuple[Path, tuple[int, int]]] = []
        completed = False
        try:
            parent, target, created_directories = self._write_parent(root, path)
            exists = False
            try:
                info = os.lstat(target)
                exists = True
                if stat.S_ISDIR(info.st_mode):
                    _raise("is_directory")
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    _raise("path_denied")
                if create_only:
                    _raise("already_exists")
                mode = stat.S_IMODE(info.st_mode)
            except FileNotFoundError:
                mode = 0o600
            except OSError:
                _raise("path_denied")
            self._check_workspace_file_count(root, additional=0 if exists else 1)
            self._reserve_writes()
            try:
                descriptor, temp_name = tempfile.mkstemp(prefix=".muse-write-", dir=os.fspath(parent))
            except OSError:
                _raise("write_failed")
            try:
                with os.fdopen(descriptor, "wb") as handle:
                    descriptor = -1
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.chmod(temp_name, mode)
                except OSError:
                    pass
                if create_only:
                    try:
                        # Same-directory hard-link creation is atomic and refuses
                        # to clobber a target created concurrently.
                        os.link(temp_name, target)
                    except FileExistsError:
                        _raise("already_exists")
                    except OSError:
                        _raise("write_failed")
                else:
                    os.replace(temp_name, target)
            except OSError:
                _raise("write_failed")
            finally:
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                try:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                except OSError:
                    pass
            final = safe_path(
                root,
                str(target),
                allow_absolute=True,
                allow_directory=False,
                allow_file=True,
            )
            completed = True
            return final, not exists
        finally:
            if not completed:
                self._rollback_write_directories(created_directories)

    def write_file(
        self,
        path: str,
        content: str,
        create_only: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            self._mutable(entry)
            if not isinstance(content, str) or not isinstance(create_only, bool) or "\x00" in content:
                _raise("invalid_input")
            try:
                encoded = content.encode("utf-8", "strict")
            except UnicodeEncodeError:
                _raise("invalid_input")
            if len(encoded) > MAX_WRITE_BYTES:
                _raise("limit_exceeded")
            with self._mutation_lock:
                final, created = self._write_locked(root, path, encoded, create_only)
            result = {
                "workspace": entry["id"],
                "path": self._relative(root, final),
                "bytes_written": len(encoded),
                "created": created,
            }
            self._audit("write_file", entry, root=root, path=final, bytes=len(encoded), created=created)
            return result
        except WorkspaceError as exc:
            self._audit("write_file", None, outcome=exc.code)
            raise

    def _patch_paths(self, patch: str) -> list[str]:
        if not isinstance(patch, str) or not patch:
            _raise("invalid_patch")
        try:
            size = len(patch.encode("utf-8", "strict"))
        except UnicodeEncodeError:
            _raise("invalid_patch")
        if size > MAX_PATCH_BYTES or "\x00" in patch:
            _raise("limit_exceeded")
        lines = patch.splitlines()
        if len(lines) > MAX_PATCH_LINES or any(
            line.startswith(("GIT binary patch", "Subproject commit")) for line in lines
        ):
            _raise("invalid_patch")
        if any(
            line.startswith(("old mode ", "new mode ", "deleted file mode ", "new file mode "))
            for line in lines
        ):
            _raise("invalid_patch")
        if any(line.startswith("index ") and line.rstrip().endswith(" 120000") for line in lines):
            _raise("invalid_patch")
        if any(line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")) for line in lines):
            _raise("invalid_patch")
        found: list[str] = []
        header_paths: list[str] = []
        for line in lines:
            if line.startswith("diff --git "):
                pieces = line[11:].split(" ")
                if len(pieces) != 2 or any('"' in piece or "\\" in piece for piece in pieces):
                    _raise("invalid_patch")
                for value in pieces:
                    if not value.startswith(("a/", "b/")):
                        _raise("invalid_patch")
                    try:
                        normalized = safe_relative_name(value[2:])
                    except WorkspaceError:
                        _raise("invalid_patch")
                    if normalized not in header_paths:
                        header_paths.append(normalized)
            elif line.startswith(("--- ", "+++ ")):
                value = line[4:].split("\t", 1)[0].strip()
                if value == "/dev/null":
                    continue
                if value.startswith(("a/", "b/")):
                    value = value[2:]
                try:
                    normalized = safe_relative_name(value)
                except WorkspaceError:
                    _raise("invalid_patch")
                if normalized not in found:
                    found.append(normalized)
            elif line.startswith(("rename from ", "rename to ", "copy from ", "copy to ")):
                value = line.split(" ", 2)[-1].strip()
                try:
                    normalized = safe_relative_name(value)
                except WorkspaceError:
                    _raise("invalid_patch")
                if normalized not in found:
                    found.append(normalized)
        if not found or len(found) > MAX_PATCH_FILES:
            _raise("invalid_patch")
        if any(value not in found for value in header_paths):
            _raise("invalid_patch")
        return found

    def _patch_line_endings(self, root: Path, paths: Sequence[str]) -> dict[str, bytes]:
        """Capture existing target line-ending styles before Git mutates them."""
        styles: dict[str, bytes] = {}
        for name in paths:
            try:
                candidate = safe_path(root, name, allow_directory=False, allow_file=True)
                info = os.lstat(candidate)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    continue
                # EOL restoration is part of the patch contract.  Refuse an
                # existing target that cannot be read within the bounded
                # preservation budget before Git has a chance to mutate it.
                if int(info.st_size) > MAX_READ_BYTES:
                    _raise("limit_exceeded")
                raw, _, _ = self._read_bytes(candidate, maximum=MAX_READ_BYTES)
            except WorkspaceError as exc:
                if exc.code == "path_not_found":
                    continue
                raise
            if b"\r\n" in raw:
                styles[name] = b"\r\n"
            elif b"\n" in raw:
                styles[name] = b"\n"
        return styles

    @staticmethod
    def _normalize_line_endings(raw: bytes, style: bytes) -> bytes:
        if style == b"\r\n":
            return raw.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        return raw.replace(b"\r\n", b"\n")

    @staticmethod
    def _replace_file_bytes(path: Path, data: bytes, mode: int) -> None:
        descriptor = -1
        temp_name: str | None = None
        try:
            descriptor, temp_name = tempfile.mkstemp(prefix=".muse-eol-", dir=os.fspath(path.parent))
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, mode)
            except OSError:
                pass
            os.replace(temp_name, path)
        except OSError:
            _raise("patch_failed")
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            if temp_name is not None:
                try:
                    if os.path.exists(temp_name):
                        os.unlink(temp_name)
                except OSError:
                    pass

    def _preserve_patch_line_endings(self, root: Path, styles: Mapping[str, bytes]) -> None:
        """Restore each existing target's CRLF/LF convention after Git apply."""
        for name, style in styles.items():
            try:
                candidate = safe_path(root, name, allow_directory=False, allow_file=True)
                info = os.lstat(candidate)
                if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                    continue
                # The preflight capture rejects oversized existing targets, so
                # this check is defensive for an unexpectedly enlarged file.
                if int(info.st_size) > MAX_READ_BYTES:
                    _raise("limit_exceeded")
                raw, truncated, _ = self._read_bytes(candidate, maximum=MAX_READ_BYTES)
                if truncated:
                    _raise("limit_exceeded")
            except WorkspaceError as exc:
                if exc.code == "path_not_found":
                    # A deletion patch has no target whose convention can be
                    # retained.
                    continue
                raise
            normalized = self._normalize_line_endings(raw, style)
            if normalized != raw:
                self._replace_file_bytes(candidate, normalized, stat.S_IMODE(info.st_mode))

    def _apply_patch_locked(self, root: Path, patch: str, paths: list[str]) -> bytes:
        """Validate the file-count delta and apply one patch under the mutation lock."""
        additional_files = 0
        for name in paths:
            try:
                safe_path(root, name, allow_directory=False, allow_file=True)
            except WorkspaceError as exc:
                if exc.code != "path_not_found":
                    raise
                safe_path(root, name, allow_missing=True, allow_directory=False, allow_file=True)
                additional_files += 1
        self._check_workspace_file_count(root, additional=additional_files)
        self._reserve_writes(len(paths))
        encoded = patch.encode("utf-8", "strict")
        line_endings = self._patch_line_endings(root, paths)
        check_args = ["apply", "--check", "--whitespace=nowarn"]
        check_code, _, _, check_truncated = _git_run(
            root,
            check_args,
            input_data=encoded,
            max_output=64 * 1024,
        )
        apply_args = ["apply", "--whitespace=nowarn"]
        if check_truncated or check_code != 0:
            # The fallback changes exactly one Git apply behavior.  It is
            # checked independently and the same argv (minus --check) is used
            # for the actual mutation only when that check succeeds.
            fallback_check_args = [*check_args, "--ignore-whitespace"]
            fallback_code, _, _, fallback_truncated = _git_run(
                root,
                fallback_check_args,
                input_data=encoded,
                max_output=64 * 1024,
            )
            if fallback_truncated or fallback_code != 0:
                _raise("patch_check_failed")
            apply_args = [item for item in fallback_check_args if item != "--check"]
        apply_code, _, _, apply_truncated = _git_run(
            root,
            apply_args,
            input_data=encoded,
            max_output=64 * 1024,
        )
        if apply_truncated or apply_code != 0:
            _raise("patch_failed")
        self._preserve_patch_line_endings(root, line_endings)
        return encoded

    def apply_patch(self, patch: str, workspace: str | None = None) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            self._mutable(entry)
            paths = self._patch_paths(patch)
            with self._mutation_lock:
                encoded = self._apply_patch_locked(root, patch, paths)
            result = {"workspace": entry["id"], "checked": True, "applied": True, "files": paths}
            audit_target = root / paths[0] if len(paths) == 1 else root
            self._audit("apply_patch", entry, root=root, path=audit_target, size=len(encoded))
            return result
        except WorkspaceError as exc:
            self._audit("apply_patch", None, outcome=exc.code)
            raise

    def _git_names(self, root: Path, *, staged: bool) -> tuple[list[str], bool]:
        args = ["diff", "--name-only", "-z", "--no-ext-diff", "--no-renames", "--no-color"]
        if staged:
            args.append("--cached")
        args.append("--")
        code, stdout, _, truncated = _git_run(root, args, max_output=MAX_GIT_STATUS_BYTES)
        if code != 0:
            _raise("git_error")
        names: list[str] = []
        for raw in stdout.split(b"\x00"):
            if not raw:
                continue
            try:
                name = safe_relative_name(raw.decode("utf-8", "strict"))
            except (UnicodeDecodeError, WorkspaceError):
                continue
            if name not in names:
                names.append(name)
        return names, truncated

    def git_status(self, workspace: str | None = None) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            status_args = [
                "status",
                "--porcelain=v1",
                "--branch",
                "--untracked-files=all",
                "-z",
                "--no-ext-diff",
                "--",
                ".",
            ]
            code, stdout, _, truncated = _git_run(
                root, status_args, max_output=MAX_GIT_STATUS_BYTES
            )
            if code != 0:
                # Some Git versions do not expose --no-ext-diff on status;
                # it has no status semantics, so retry the fixed status
                # template without that diff-only option.
                fallback = [item for item in status_args if item != "--no-ext-diff"]
                code, stdout, _, fallback_truncated = _git_run(
                    root, fallback, max_output=MAX_GIT_STATUS_BYTES
                )
                truncated = truncated or fallback_truncated
            if code != 0:
                _raise("git_error")
            tokens = stdout.decode("utf-8", "replace").split("\x00")
            branch = ""
            values: list[dict[str, str]] = []
            index = 0
            while index < len(tokens):
                token = tokens[index]
                index += 1
                if not token:
                    continue
                if token.startswith("## "):
                    branch = token[3:][:256]
                    continue
                if len(token) < 4:
                    continue
                status = token[:2]
                names = [token[3:]]
                if status[0] in {"R", "C"} and index < len(tokens):
                    names.append(tokens[index])
                    index += 1
                try:
                    checked = [safe_relative_name(name) for name in names]
                except WorkspaceError:
                    continue
                values.append({"status": status, "path": checked[-1]})
            status = "\n".join(
                ([f"## {branch}"] if branch else [])
                + [f"{row['status']} {row['path']}" for row in values]
            )
            result = {
                "workspace": entry["id"],
                "branch": branch,
                "entries": values,
                "status": status,
                "truncated": truncated,
            }
            self._audit("git_status", entry, count=len(values), truncated=truncated)
            return result
        except WorkspaceError as exc:
            self._audit("git_status", None, outcome=exc.code)
            raise

    def git_diff(
        self,
        path: str | None = None,
        staged: bool = False,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        try:
            _, entry, root = self._entry(workspace)
            if not isinstance(staged, bool):
                _raise("invalid_input")
            prefix = None
            if path is not None:
                selected = safe_path(root, path, allow_directory=True, allow_file=True)
                prefix = self._relative(root, selected)
                if prefix == ".":
                    prefix = None
            names, names_truncated = self._git_names(root, staged=staged)
            if prefix is not None:
                safe_prefix = safe_relative_name(prefix)
                names = [name for name in names if name == safe_prefix or name.startswith(safe_prefix + "/")]
            checked_names: list[str] = []
            for name in names:
                try:
                    safe_path(root, name, allow_directory=True, allow_file=True)
                except WorkspaceError as exc:
                    if exc.code != "path_not_found":
                        continue
                    # A deleted path is safe only after the same ancestor
                    # checks pass with an explicitly allowed missing final
                    # component.  Symlinks, reparse points, hardlinks, and
                    # denied names still raise here and are skipped.
                    try:
                        safe_path(root, name, allow_missing=True, allow_directory=True, allow_file=True)
                    except WorkspaceError:
                        continue
                checked_names.append(name)
            names = checked_names
            if not names:
                result = {
                    "workspace": entry["id"],
                    "path": prefix,
                    "staged": staged,
                    "diff": "",
                    "files": [],
                    "truncated": names_truncated,
                }
                self._audit("git_diff", entry, root=root, path=root / prefix if prefix else root, size=0)
                return result
            if len(names) > MAX_GIT_PATHS:
                names = names[:MAX_GIT_PATHS]
                names_truncated = True
            args = [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--no-color",
                "--binary",
            ]
            if staged:
                args.append("--cached")
            args.extend(["--", *names])
            code, stdout, _, output_truncated = _git_run(root, args, max_output=MAX_GIT_DIFF_BYTES)
            if code != 0:
                _raise("git_error")
            result = {
                "workspace": entry["id"],
                "path": prefix,
                "staged": staged,
                "diff": stdout[:MAX_GIT_DIFF_BYTES].decode("utf-8", "replace"),
                "files": names,
                "truncated": names_truncated or output_truncated,
            }
            self._audit(
                "git_diff",
                entry,
                root=root,
                path=root / prefix if prefix else root,
                size=len(result["diff"].encode("utf-8")),
            )
            return result
        except WorkspaceError as exc:
            self._audit("git_diff", None, outcome=exc.code)
            raise

    @staticmethod
    def _invoke_runner(runner: Any, argv: list[str], root: Path) -> Any:
        fn = runner.run if hasattr(runner, "run") else runner if callable(runner) else None
        if fn is None:
            _raise("check_runner_unavailable")
        try:
            params = inspect.signature(fn).parameters
        except (TypeError, ValueError):
            params = {}
        kwargs: dict[str, Any] = {}
        if "cwd" in params or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
            kwargs["cwd"] = root
        if "timeout" in params or any(item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()):
            kwargs["timeout"] = CHECK_TIMEOUT_SECONDS
        if "max_output_bytes" in params or any(
            item.kind is inspect.Parameter.VAR_KEYWORD for item in params.values()
        ):
            kwargs["max_output_bytes"] = CHECK_OUTPUT_BYTES
        elif "max_output" in params:
            kwargs["max_output"] = CHECK_OUTPUT_BYTES
        try:
            if kwargs:
                return fn(argv, **kwargs)
            return fn(argv, root, CHECK_TIMEOUT_SECONDS, CHECK_OUTPUT_BYTES)
        except WorkspaceError:
            raise
        except (OSError, TypeError, ValueError):
            _raise("check_failed")

    def run_check(
        self,
        name: str,
        args: list[str] | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        try:
            if not self.checks_enabled:
                _raise("checks_disabled")
            _, entry, root = self._entry(workspace)
            selected, checked_args = validate_check(name, args)
            allowed = {str(item).casefold() for item in entry.get("checks", [])}
            if selected not in allowed:
                _raise("check_not_allowed")
            for argument in checked_args:
                if argument.startswith("-"):
                    continue
                try:
                    safe_path(root, argument, allow_directory=True, allow_file=True)
                except WorkspaceError:
                    _raise("check_not_allowed")
            argv = command_for(selected, checked_args)
            if not self._runner_lock.acquire(blocking=False):
                _raise("check_busy")
            try:
                raw = self._invoke_runner(self.check_runner, argv, root)
            finally:
                self._runner_lock.release()
            if hasattr(raw, "as_dict"):
                raw = raw.as_dict()
            elif isinstance(raw, tuple) and len(raw) >= 3:
                raw = {"returncode": raw[0], "stdout": raw[1], "stderr": raw[2]}
            if not isinstance(raw, Mapping):
                _raise("check_failed")
            code = raw.get("returncode", raw.get("code", 1))
            stdout = raw.get("stdout", "")
            stderr = raw.get("stderr", "")
            if not isinstance(code, int) or not isinstance(stdout, str) or not isinstance(stderr, str):
                _raise("check_failed")
            result = {
                "workspace": entry["id"],
                "name": selected,
                "returncode": code,
                "stdout": stdout[:CHECK_OUTPUT_BYTES],
                "stderr": stderr[:CHECK_OUTPUT_BYTES],
                "ok": code == 0,
                "timed_out": bool(raw.get("timed_out", False)),
                "truncated": bool(raw.get("truncated", False))
                or len(stdout) > CHECK_OUTPUT_BYTES
                or len(stderr) > CHECK_OUTPUT_BYTES,
            }
            self._audit("run_check", entry, outcome=str(code))
            return result
        except WorkspaceError as exc:
            self._audit("run_check", None, outcome=exc.code)
            raise


__all__ = ["WorkspaceService"]
