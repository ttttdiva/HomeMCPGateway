"""Fixed, read-only stdio scope for a single authenticated AoiTalk AgentRun.

The trusted 41 launcher owns this process and supplies its immutable scope. It
must recheck live Agent authority before each call and close the child on revoke
or cancellation. Environment identifiers are correlation data, not credentials
or a replacement for 41 authentication. This is not a host-shell/OS sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
import codecs
import fnmatch
import math
import os
from pathlib import Path
import re
import stat
import time
from typing import Any, Mapping

PROFILE = "local_agent_read_only"
TOOLS = ("list_directory", "path_info", "read_text", "search_text")
IDENTIFIER = re.compile(r"^[A-Za-z0-9._:-]{1,256}$")
ENV_KEYS = frozenset({
    "PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
    "PYTHONIOENCODING", "PYTHONUTF8", "PYTHONUNBUFFERED", "HOME_MCP_PROFILE",
    "HOME_MCP_WORKSPACE", "HOME_MCP_AGENT_RUN_ID", "HOME_MCP_OWNER_ID",
})
SECRET_NAMES = frozenset({
    ".git", ".ssh", ".aws", ".azure", ".kube", ".gnupg", ".config", ".local",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", "credentials",
    "credential", "secret", "secrets",
    "credentials.json", "secrets.json", "tokens.json", "token.json", "key.json",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
})
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".kdbx", ".secret", ".secrets")
# Keep at least the deny patterns in 41 src/security/sensitive_files.py; do not
# import application modules across the two independently managed venvs.
SECRET_PATTERN = re.compile(r"(?i)^(?:secrets?\.(?:json|ya?ml|toml|ini|env)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?)$")


class ScopeError(ValueError):
    """Fixed public error codes; never include a rejected path or host detail."""


def _secret(part: str) -> bool:
    lowered = part.casefold()
    return (lowered in SECRET_NAMES or lowered == ".env" or lowered.startswith(".env.")
            or lowered.endswith(SECRET_SUFFIXES) or bool(SECRET_PATTERN.fullmatch(part)))


def _integer(value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ScopeError("invalid_input")
    return value


def _timeout(value: Any) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 10):
        raise ScopeError("invalid_input")
    return float(value)


def clean_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """Allowlist only OS/Python essentials and this child's fixed scope."""
    result = {key: value for key, value in environ.items() if key.upper() in ENV_KEYS}
    result["HOME_MCP_AOITALK_ENABLED"] = "0"
    return result


@dataclass(frozen=True)
class ReadOnlyScope:
    workspace: Path
    agent_run_id: str
    owner_id: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str]) -> "ReadOnlyScope":
        if environ.get("HOME_MCP_PROFILE") != PROFILE:
            raise ScopeError("invalid_profile")
        raw = environ.get("HOME_MCP_WORKSPACE", "")
        run_id = environ.get("HOME_MCP_AGENT_RUN_ID", "")
        owner_id = environ.get("HOME_MCP_OWNER_ID", "")
        if not raw or not IDENTIFIER.fullmatch(run_id) or not IDENTIFIER.fullmatch(owner_id):
            raise ScopeError("invalid_scope")
        path = Path(raw)
        if not path.is_absolute() or path.name.casefold() == ".local":
            raise ScopeError("invalid_scope")
        try:
            scope = cls(path, run_id, owner_id)
            guarded = scope.path(str(path))
            if not guarded.is_dir():
                raise ScopeError("invalid_scope")
            return cls(guarded, run_id, owner_id)
        except (OSError, ValueError):
            raise ScopeError("invalid_scope") from None

    def path(self, value: str) -> Path:
        if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
            raise ScopeError("invalid_input")
        # Reject Windows device paths, UNC, ADS and ambiguous Win32 aliases.
        if value.startswith(("\\\\", "//")):
            raise ScopeError("scope_denied")
        supplied = Path(value)
        if any(part == ".." for part in supplied.parts):
            raise ScopeError("scope_denied")
        candidate = supplied if supplied.is_absolute() else self.workspace / supplied
        try:
            relative_parts = candidate.relative_to(self.workspace).parts
        except ValueError:
            raise ScopeError("scope_denied") from None
        for part in candidate.parts[1:] if candidate.anchor else candidate.parts:
            if ":" in part or part.endswith((".", " ")):
                raise ScopeError("scope_denied")
            # A trusted, explicit workspace may live under the repository's
            # .local scratch directory. Do not grant access to .local app data
            # underneath an ordinary workspace or accept .local itself as root.
            if _secret(part) and (part.casefold() != ".local" or any(p.casefold() == ".local" for p in relative_parts)):
                raise ScopeError("scope_denied")
        # Check every ancestor, including the configured workspace's ancestors.
        for component in (*reversed(candidate.parents), candidate):
            info = component.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise ScopeError("scope_denied")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(self.workspace.resolve(strict=True))
        except ValueError:
            raise ScopeError("scope_denied") from None
        info = resolved.stat()
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            raise ScopeError("scope_denied")
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ScopeError("scope_denied")
        return resolved

    def _opened_path(self, descriptor: int) -> Path:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt
            function = ctypes.WinDLL("kernel32", use_last_error=True).GetFinalPathNameByHandleW
            function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
            function.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = function(msvcrt.get_osfhandle(descriptor), buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                raise ScopeError("scope_denied")
            raw = buffer.value
            if raw.startswith("\\\\?\\UNC\\"):
                raw = "\\\\" + raw[8:]
            elif raw.startswith("\\\\?\\"):
                raw = raw[4:]
            return self.path(raw)
        # Linux draft tests use the open descriptor, not an unchecked path reread.
        if Path("/proc/self/fd").is_dir():
            return self.path(os.readlink(f"/proc/self/fd/{descriptor}"))
        raise ScopeError("unsupported_platform")

    def read(self, path: str, max_chars: int) -> dict[str, Any]:
        maximum = _integer(max_chars, 1, 24000)
        candidate = self.path(path)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(candidate, flags)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
                raise ScopeError("scope_denied")
            opened = self._opened_path(descriptor)
            if opened != candidate:
                raise ScopeError("scope_denied")
            current = candidate.stat()
            if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
                raise ScopeError("scope_denied")
            raw = os.read(descriptor, min(96004, maximum * 4 + 4))
            if b"\x00" in raw:
                raise ScopeError("unsupported_binary")
            try:
                text = codecs.getincrementaldecoder("utf-8")("strict").decode(raw, final=info.st_size <= len(raw))
            except UnicodeError:
                raise ScopeError("unsupported_text_encoding") from None
            return {"path": str(candidate), "text": text[:maximum],
                    "truncated": len(text) > maximum or info.st_size > len(raw)}
        finally:
            os.close(descriptor)

    def files(self, root: str, *, recursive: bool, maximum: int, timeout_sec: float, progress: dict | None = None):
        initial = self.path(root)
        if not initial.is_dir():
            raise ScopeError("invalid_input")
        pending = [initial]
        deadline = time.monotonic() + _timeout(timeout_sec)
        examined = 0
        while pending:
            directory = self.path(str(pending.pop()))
            with os.scandir(directory) as entries:
                for entry in entries:
                    examined += 1
                    if examined > maximum or time.monotonic() >= deadline:
                        if progress is not None:
                            progress["truncated"] = True
                        return
                    try:
                        candidate = self.path(entry.path)
                    except (OSError, ScopeError):
                        continue
                    is_dir = candidate.is_dir()
                    yield candidate, is_dir
                    if recursive and is_dir:
                        pending.append(candidate)

    def call(self, tool: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        """The same closed allowlist is used for registration and execution."""
        if tool not in TOOLS:
            return {"ok": False, "error": "tool_not_allowed"}
        try:
            args = dict(arguments)
            if tool == "read_text":
                if set(args) - {"path", "max_chars"} or "path" not in args:
                    raise ScopeError("invalid_input")
                result = self.read(args["path"], args.get("max_chars", 12000))
            elif tool == "path_info":
                if set(args) != {"path"}:
                    raise ScopeError("invalid_input")
                path = self.path(args["path"])
                info = path.stat()
                result = {"path": str(path), "is_directory": path.is_dir(), "size": info.st_size}
            elif tool == "list_directory":
                if set(args) - {"path", "max_results"} or "path" not in args:
                    raise ScopeError("invalid_input")
                maximum = _integer(args.get("max_results", 100), 1, 500)
                entries = []
                progress = {}
                for path, is_dir in self.files(args["path"], recursive=False, maximum=maximum + 1, timeout_sec=5, progress=progress):
                    entries.append({"path": str(path), "is_directory": is_dir})
                result = {"entries": sorted(entries[:maximum], key=lambda row: row["path"]),
                          "truncated": len(entries) > maximum or progress.get("truncated", False)}
            else:
                allowed = {"root", "query", "file_glob", "max_results", "max_files", "timeout_sec"}
                if set(args) - allowed or not {"root", "query"} <= set(args):
                    raise ScopeError("invalid_input")
                query = args["query"]
                pattern = args.get("file_glob", "*")
                if (not isinstance(query, str) or not 1 <= len(query) <= 1000
                        or not isinstance(pattern, str) or not 1 <= len(pattern) <= 128
                        or any(c in pattern for c in "/\\:")):
                    raise ScopeError("invalid_input")
                maximum = _integer(args.get("max_results", 30), 1, 100)
                max_files = _integer(args.get("max_files", 100), 1, 500)
                timeout = _timeout(args.get("timeout_sec", 5))
                matches = []
                checked = 0
                deadline = time.monotonic() + timeout
                for path, is_dir in self.files(args["root"], recursive=True, maximum=max_files, timeout_sec=timeout):
                    if time.monotonic() >= deadline or len(matches) >= maximum:
                        break
                    if is_dir or not fnmatch.fnmatch(path.name, pattern):
                        continue
                    checked += 1
                    try:
                        text = self.read(str(path), 24000)["text"]
                    except (OSError, ScopeError):
                        continue
                    for line_no, line in enumerate(text.splitlines(), 1):
                        if query in line:
                            matches.append({"path": str(path), "line": line_no, "text": line[:1000]})
                            if len(matches) >= maximum:
                                break
                result = {"matches": matches, "files_checked": checked,
                          "bounded": True, "max_chars_per_file": 24000}
            return {"ok": True, **result}
        except ScopeError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, ValueError, TypeError):
            return {"ok": False, "error": "path_unavailable"}


def create_server(scope: ReadOnlyScope):
    # Do not import the unrestricted server/core/desktop/HTTP/delegation modules.
    from mcp.server import MCPServer
    from mcp.types import ToolAnnotations
    server = MCPServer("Home MCP Gateway local agent", instructions=(
        "Read-only workspace tools for one AoiTalk AgentRun. No host commands, "
        "writes, desktop, images, outbound requests, or redelegation are available."
    ))
    annotations = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                                  idempotent_hint=True, open_world_hint=False)

    def list_directory(path: str, max_results: int = 100) -> dict[str, Any]:
        """List bounded workspace entries; credential files and links are omitted."""
        return scope.call("list_directory", {"path": path, "max_results": max_results})

    def path_info(path: str) -> dict[str, Any]:
        """Read metadata for a regular file or directory inside this workspace."""
        return scope.call("path_info", {"path": path})

    def read_text(path: str, max_chars: int = 12000) -> dict[str, Any]:
        """Read at most 24000 UTF-8 characters from a workspace file."""
        return scope.call("read_text", {"path": path, "max_chars": max_chars})

    def search_text(root: str, query: str, file_glob: str = "*", max_results: int = 30,
                    max_files: int = 100, timeout_sec: float = 5) -> dict[str, Any]:
        """Bounded literal search over allowed workspace files; no shell or regex."""
        return scope.call("search_text", {"root": root, "query": query, "file_glob": file_glob,
                          "max_results": max_results, "max_files": max_files, "timeout_sec": timeout_sec})

    functions = {fn.__name__: fn for fn in (list_directory, path_info, read_text, search_text)}
    for name in TOOLS:
        server.tool(annotations=annotations)(functions[name])
    return server


def main() -> None:
    create_server(ReadOnlyScope.from_environment(os.environ)).run()
