"""Run the generic Home MCP Gateway over an authenticated Streamable HTTP endpoint."""
from __future__ import annotations

import argparse
from contextvars import ContextVar
from datetime import datetime, timezone
import hmac
import json
import os
from pathlib import Path
import re
import sys
import threading
from typing import Any, Mapping, Sequence


_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPOSITORY_ROOT))

if os.environ.get("HOME_MCP_PROFILE"):
    raise SystemExit("HOME_MCP_PROFILE is not supported by the Muse HTTP entrypoint")

# Match gateway_stdio.py: scoped launchers must not inherit unrelated service
# credentials into the unrestricted generic tool process.
for _credential_name in (
    "CONTROL_PLANE_API_KEY",
    "OPENAI_ADMIN_KEY",
    "HOME_MCP_AOITALK_RUNTIME_API_KEY",
):
    os.environ.pop(_credential_name, None)

from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

from home_mcp_gateway.server import mcp as gateway_mcp  # noqa: E402


DEFAULT_PREFIX = "/muse-mcp"
ALLOWED_PREFIXES = ("/muse-mcp", "/claude-mcp")
MUSE_BEARER_ONLY_PATH = "/muse-bearer/mcp"
DEFAULT_PORT = 8765
PATH_SECRET_RE = re.compile(r"[0-9A-Fa-f]{32,}\Z")
AUDIT_PATH_CONTEXT: ContextVar[Path | None] = ContextVar("muse_gateway_audit_path", default=None)
_AUDIT_LOCK = threading.Lock()
_KNOWN_TOOLS = frozenset(tool.name for tool in gateway_mcp._tool_manager.list_tools())


class _ToolAuditMiddleware:
    """Write only the registered tool name, UTC time, and result code."""

    async def __call__(self, context: Any, call_next: Any) -> Any:
        if getattr(context, "method", None) != "tools/call":
            return await call_next(context)

        params = getattr(context, "params", None)
        raw_name = params.get("name") if isinstance(params, Mapping) else None
        tool_name = raw_name if isinstance(raw_name, str) and raw_name in _KNOWN_TOOLS else "unknown"
        audit_path = AUDIT_PATH_CONTEXT.get()
        if audit_path is None:
            return await call_next(context)
        if not _audit_sink_writable(audit_path):
            raise MCPError(code=-32603, message="audit_sink_unavailable")

        try:
            result = await call_next(context)
        except BaseException:
            if not _append_tool_audit(audit_path, tool_name, "error"):
                raise MCPError(code=-32603, message="audit_write_failed") from None
            raise

        result_code = "error" if getattr(result, "is_error", False) else "ok"
        if not _append_tool_audit(audit_path, tool_name, result_code):
            raise MCPError(code=-32603, message="audit_write_failed")
        return result


if not any(isinstance(item, _ToolAuditMiddleware) for item in gateway_mcp.middleware):
    gateway_mcp.middleware.append(_ToolAuditMiddleware())


def _audit_sink_writable(audit_path: Path) -> bool:
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            with audit_path.open("a", encoding="utf-8", newline="") as handle:
                handle.flush()
        return True
    except OSError:
        return False


def _append_tool_audit(audit_path: Path | None, tool_name: str, result_code: str) -> bool:
    if audit_path is None:
        return False
    record = {
        "tool": tool_name,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "result": result_code,
    }
    try:
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        with _AUDIT_LOCK:
            with audit_path.open("a", encoding="utf-8", newline="") as handle:
                handle.write(line)
                handle.flush()
    except OSError:
        return False
    return True


def _validate_path_secret(value: str) -> str:
    if not isinstance(value, str) or not PATH_SECRET_RE.fullmatch(value):
        raise ValueError("invalid_path_secret")
    return value


def _validate_token(value: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) < 32
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
    ):
        raise ValueError("invalid_token")
    return value


def _require_owner_only_acl(handle: Any) -> None:
    """Require the process user to own the object and be its only allowed SID."""
    try:
        import win32api
        import win32security

        process_token = win32security.OpenProcessToken(
            win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
        )
        try:
            current_sid = win32security.GetTokenInformation(
                process_token, win32security.TokenUser
            )[0]
            security_info = (
                win32security.OWNER_SECURITY_INFORMATION
                | win32security.DACL_SECURITY_INFORMATION
            )
            descriptor = win32security.GetSecurityInfo(
                handle, win32security.SE_FILE_OBJECT, security_info
            )
            owner_sid = descriptor.GetSecurityDescriptorOwner()
            dacl = descriptor.GetSecurityDescriptorDacl()
            current_sid_text = win32security.ConvertSidToStringSid(current_sid)
            if (
                owner_sid is None
                or dacl is None
                or win32security.ConvertSidToStringSid(owner_sid) != current_sid_text
            ):
                raise ValueError("invalid_secret_acl")

            allow_types = {
                win32security.ACCESS_ALLOWED_ACE_TYPE,
                win32security.ACCESS_ALLOWED_OBJECT_ACE_TYPE,
            }
            deny_types = {
                win32security.ACCESS_DENIED_ACE_TYPE,
                win32security.ACCESS_DENIED_OBJECT_ACE_TYPE,
            }
            owner_has_allow = False
            for index in range(dacl.GetAceCount()):
                ace = dacl.GetAce(index)
                ace_type = ace[0][0]
                if ace_type in deny_types:
                    continue
                if ace_type not in allow_types:
                    # Unknown or conditional ACEs cannot be proven owner-only.
                    raise ValueError("invalid_secret_acl")
                ace_sid = win32security.ConvertSidToStringSid(ace[-1])
                if ace_sid != current_sid_text:
                    raise ValueError("invalid_secret_acl")
                owner_has_allow = True

            if not owner_has_allow:
                raise ValueError("invalid_secret_acl")
        finally:
            win32api.CloseHandle(process_token)
    except Exception:
        raise ValueError("invalid_secret_acl") from None


def _read_secret_file(path: Path, *, token: bool = False) -> str:
    win32api = None
    directory_handle = None
    file_handle = None
    try:
        import win32api
        import win32con
        import win32file

        file_read_attributes = getattr(win32con, "FILE_READ_ATTRIBUTES", 0x80)
        directory_flags = (
            win32file.FILE_FLAG_OPEN_REPARSE_POINT
            | win32con.FILE_FLAG_BACKUP_SEMANTICS
        )
        directory_handle = win32file.CreateFile(
            os.fspath(path.parent),
            win32con.READ_CONTROL | file_read_attributes,
            win32con.FILE_SHARE_READ,
            None,
            win32con.OPEN_EXISTING,
            directory_flags,
            None,
        )
        directory_attributes = win32file.GetFileInformationByHandle(directory_handle)[0]
        if not directory_attributes & win32con.FILE_ATTRIBUTE_DIRECTORY:
            raise ValueError("invalid_secret_directory")
        if directory_attributes & win32con.FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("invalid_secret_directory")
        _require_owner_only_acl(directory_handle)

        # Open the file without following reparse points and without sharing
        # write/delete access. ACL validation and reading use this same handle.
        file_handle = win32file.CreateFile(
            os.fspath(path),
            win32con.GENERIC_READ | win32con.READ_CONTROL | file_read_attributes,
            win32con.FILE_SHARE_READ,
            None,
            win32con.OPEN_EXISTING,
            win32file.FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        file_attributes = win32file.GetFileInformationByHandle(file_handle)[0]
        if file_attributes & (
            win32con.FILE_ATTRIBUTE_DIRECTORY | win32con.FILE_ATTRIBUTE_REPARSE_POINT
        ):
            raise ValueError("invalid_secret_file")
        _require_owner_only_acl(file_handle)
        _, contents = win32file.ReadFile(file_handle, 4097)
        if len(contents) > 4096:
            raise ValueError("invalid_secret_file")
        value = contents.decode("ascii").strip()
    except UnicodeError:
        raise ValueError("invalid_secret_file") from None
    except ValueError:
        raise
    except Exception:
        raise ValueError("invalid_secret_file") from None
    finally:
        if win32api is not None:
            for handle in (file_handle, directory_handle):
                if handle is not None:
                    try:
                        win32api.CloseHandle(handle)
                    except Exception:
                        pass
    return _validate_token(value) if token else _validate_path_secret(value)


async def _send_plain(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store, no-transform"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def _prevent_http_transforms(inner: Any) -> Any:
    """Keep intermediaries from compressing or reframing MCP JSON responses."""

    async def guarded(scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return

        async def send_without_transforms(message: Mapping[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", ())
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store, no-transform"))
                message = {**message, "headers": headers}
            await send(message)

        await inner(scope, receive, send_without_transforms)

    return guarded


def _authenticated_app(
    inner: Any,
    *,
    endpoint_path: str,
    token: str,
    audit_path: Path,
    bearer_only_path: str | None = None,
) -> Any:
    expected = f"Bearer {token}".encode("ascii")

    async def guarded(scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return

        request_path = scope.get("path")
        if request_path != endpoint_path and request_path != bearer_only_path:
            await _send_plain(send, 404, b"not found")
            return

        values = [
            value
            for key, value in scope.get("headers") or ()
            if key.lower() == b"authorization"
        ]
        if len(values) != 1 or not hmac.compare_digest(values[0], expected):
            await _send_plain(send, 401, b"unauthorized")
            return

        # Muse permits only one credential per outgoing HTTP request. The
        # opt-in nonsecret alias retains Bearer authentication and delegates
        # to the same MCP handler without redirecting or leaking the path key.
        if request_path == bearer_only_path:
            scope = {**scope, "path": endpoint_path, "raw_path": endpoint_path.encode("ascii")}

        context_token = AUDIT_PATH_CONTEXT.set(audit_path)
        try:
            await inner(scope, receive, send)
        finally:
            AUDIT_PATH_CONTEXT.reset(context_token)

    return guarded


def build_app(
    path_secret: str,
    bearer_token: str,
    *,
    audit_path: str | os.PathLike[str],
    prefix: str = DEFAULT_PREFIX,
    enable_muse_bearer_only: bool = False,
) -> Any:
    """Build the protected HTTP app around the already-registered Gateway."""
    path_secret = _validate_path_secret(path_secret)
    bearer_token = _validate_token(bearer_token)
    if prefix not in ALLOWED_PREFIXES:
        raise ValueError("invalid_mcp_prefix")
    if enable_muse_bearer_only and prefix != DEFAULT_PREFIX:
        raise ValueError("muse_bearer_only_requires_muse_prefix")
    sink = Path(audit_path)
    if not _audit_sink_writable(sink):
        raise ValueError("audit_sink_unavailable")
    endpoint_path = f"{prefix}/{path_secret}/mcp"
    inner = _prevent_http_transforms(
        gateway_mcp.streamable_http_app(
            streamable_http_path=endpoint_path,
            json_response=True,
            stateless_http=True,
            host="127.0.0.1",
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
    )
    return _authenticated_app(
        inner,
        endpoint_path=endpoint_path,
        token=bearer_token,
        audit_path=sink,
        bearer_only_path=MUSE_BEARER_ONLY_PATH if enable_muse_bearer_only else None,
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the authenticated Muse Gateway HTTP endpoint")
    parser.add_argument("--secret-dir", required=True, help="directory containing path.secret and token.secret")
    parser.add_argument("--prefix", choices=ALLOWED_PREFIXES, default=DEFAULT_PREFIX, help="distinct Muse or Claude HTTP MCP URL prefix")
    parser.add_argument("--enable-muse-bearer-only", action="store_true", help="enable nonsecret Muse alias using Bearer auth only")
    parser.add_argument("--audit", default=None, help="JSONL tool audit file; defaults inside --secret-dir")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    secret_dir = Path(args.secret_dir)
    if not secret_dir.is_absolute():
        raise SystemExit("secret directory must be an absolute path")
    if args.host != "127.0.0.1":
        raise SystemExit("host must be 127.0.0.1")
    if not 1 <= args.port <= 65535:
        raise SystemExit("invalid port")

    path_secret = _read_secret_file(secret_dir / "path.secret")
    bearer_token = _read_secret_file(secret_dir / "token.secret", token=True)
    audit_path = Path(args.audit) if args.audit else secret_dir / "muse-full-gateway-audit.jsonl"
    if not audit_path.is_absolute():
        raise SystemExit("audit path must be absolute")
    app = build_app(path_secret, bearer_token, audit_path=audit_path, prefix=args.prefix, enable_muse_bearer_only=args.enable_muse_bearer_only)

    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by the entrypoint
    main()
