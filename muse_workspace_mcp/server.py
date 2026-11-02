"""FastMCP HTTP server for the Muse workspace service.

The workspace policy and all filesystem/git operations live in
``muse_workspace_mcp.service``.  This module is deliberately a thin adapter:
it registers the stable MCP tool names, keeps the endpoint URL stable, and
adds the legacy path-secret plus bearer-token guard around the FastMCP ASGI
application.

The module supports both the production entry point::

    python -m muse_workspace_mcp.server --secret-file ... --token-file ...

and an in-process test/integration entry point via :func:`build_server` and
:func:`build_app`.
"""
from __future__ import annotations

import argparse
import hmac
import inspect
import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


DEFAULT_PREFIX = "/muse-mcp"
DEFAULT_PORT = 8765
DEFAULT_TTL_MINUTES = 90.0
SECRET_RE = re.compile(r"[0-9A-Fa-f]{32,}\Z")
PREFIX_RE = re.compile(r"(?:/[a-z0-9-]+)+\Z")
SAFE_ERROR_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")


class ServerConfigurationError(ValueError):
    """Raised before the HTTP server starts for invalid local settings."""


def _service_type() -> type[Any]:
    """Import the policy layer lazily.

    Keeping this import lazy makes ``muse_workspace_mcp.server`` importable in
    small tooling environments and lets tests inject a fake service without
    requiring filesystem setup first.
    """

    from .service import WorkspaceService

    return WorkspaceService


def _validate_prefix(prefix: str) -> str:
    if not isinstance(prefix, str) or not PREFIX_RE.fullmatch(prefix):
        raise ServerConfigurationError("invalid_prefix")
    return prefix


def _validate_secret(secret: str) -> str:
    if not isinstance(secret, str) or not SECRET_RE.fullmatch(secret):
        raise ServerConfigurationError("invalid_secret")
    return secret


def _read_secret_file(path: str | os.PathLike[str], *, token: bool = False) -> str:
    """Read a secret/token from a regular file without exposing its value.

    The caller supplies the file path as a local operator setting.  We reject
    symlinks and non-regular files because the deployed service is intended to
    have file-only secret inputs.  The token has a looser character policy to
    preserve existing deployments, while requiring a useful minimum length.
    """

    candidate = Path(path)
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise ServerConfigurationError("invalid_secret_file")
        value = candidate.read_text(encoding="ascii").strip()
    except ServerConfigurationError:
        raise
    except (OSError, UnicodeError):
        raise ServerConfigurationError("invalid_secret_file") from None
    if token:
        if len(value) < 32 or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in value):
            raise ServerConfigurationError("invalid_token")
    else:
        _validate_secret(value)
    return value


def _normalise_output(value: Any) -> Any:
    """Keep the old tools' text contract while allowing service data objects.

    FastMCP 1.25 serialises dictionaries as structured content.  The previous
    review server returned text, and Muse integrations expect a text block, so
    service mappings/sequences are encoded deterministically here.  Existing
    string responses remain byte-for-byte untouched.
    """

    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _safe_error_code(exc: BaseException) -> str:
    """Convert a policy-layer exception to a fixed, non-sensitive code."""

    for attr in ("code", "error_code", "safe_code"):
        candidate = getattr(exc, attr, None)
        if isinstance(candidate, str) and SAFE_ERROR_RE.fullmatch(candidate):
            return candidate

    # WorkspaceError implementations commonly store their code as the first
    # argument.  Only accept a token-shaped value; never forward arbitrary
    # exception text (which may contain a path, patch, or secret).
    if exc.__class__.__name__ in {"WorkspaceError", "RegistryError", "CheckError"}:
        first = exc.args[0] if exc.args else None
        if isinstance(first, str) and SAFE_ERROR_RE.fullmatch(first):
            return first

    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, PermissionError):
        return "permission_denied"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_request"
    return "internal_error"


def _supports_parameter(fn: Callable[..., Any], name: str) -> bool:
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in parameters or any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )


def _call_service(
    service: Any,
    method_name: str,
    *args: Any,
    workspace: str | None = None,
    **kwargs: Any,
) -> Any:
    """Call a service method while accommodating the core's id naming.

    ``WorkspaceService`` uses ``workspace`` for the public API.  The small
    amount of signature inspection keeps this adapter useful with test fakes
    and with older core implementations that called it ``workspace_id``; it
    never drops a supplied selector when a matching parameter exists.
    """

    fn = getattr(service, method_name, None)
    if fn is None and method_name == "use_workspace":
        fn = getattr(service, "select_workspace", None)
    if fn is None or not callable(fn):
        raise RuntimeError("internal_error")

    if workspace is not None:
        if _supports_parameter(fn, "workspace"):
            kwargs["workspace"] = workspace
        elif _supports_parameter(fn, "workspace_id"):
            kwargs["workspace_id"] = workspace
        else:
            # A service without a selector cannot safely serve a caller that
            # explicitly requested one.
            raise ValueError("invalid_workspace")
    elif _supports_parameter(fn, "workspace"):
        kwargs["workspace"] = None
    elif _supports_parameter(fn, "workspace_id"):
        kwargs["workspace_id"] = None
    return fn(*args, **kwargs)


def _tool_call(
    service: Any,
    method_name: str,
    *args: Any,
    workspace: str | None = None,
    **kwargs: Any,
) -> Any:
    try:
        return _normalise_output(
            _call_service(service, method_name, *args, workspace=workspace, **kwargs)
        )
    except BaseException as exc:
        # Do not allow cancellation/system-exit exceptions to be converted to
        # user-visible strings.  Normal service errors are fixed-code errors.
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ValueError(_safe_error_code(exc)) from None


def _service_audit_path(service: Any) -> Path | None:
    for name in ("audit_path", "_audit_path"):
        value = getattr(service, name, None)
        if value is not None:
            try:
                return Path(value)
            except (TypeError, ValueError):
                return None
    return None


def _append_auth_audit(audit_path: Path | None, result: str) -> None:
    """Append a content-free authentication audit record."""

    if audit_path is None:
        return
    try:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"tool": "_auth", "result": result}
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        # An audit sink must never turn a safe authentication failure into a
        # traceback that might disclose local details.
        return


def build_server(
    service: Any,
    secret: str,
    *,
    port: int = DEFAULT_PORT,
    prefix: str = DEFAULT_PREFIX,
) -> FastMCP:
    """Build a FastMCP instance with the stable Muse tool surface.

    Args:
        service: An injected ``WorkspaceService`` (or protocol-compatible
            fake).  When omitted, callers must use :func:`build_from_paths`.
        secret: Path secret read by the caller from ``--secret-file``.
        port: Local listen port used by FastMCP's settings.
        prefix: Stable ingress prefix, normally ``/muse-mcp``.

    The returned object is not started.  Use ``server.run("streamable-http")``
    for a no-token local test, or :func:`build_app` with the bearer token for
    the production guarded app.
    """

    secret = _validate_secret(secret)
    prefix = _validate_prefix(prefix)
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ServerConfigurationError("invalid_port") from None
    if not 1 <= port <= 65535:
        raise ServerConfigurationError("invalid_port")

    endpoint = f"{prefix}/{secret}/mcp"
    mcp = FastMCP(
        "muse-workspace-mcp",
        instructions=(
            "Workspace-scoped Muse MCP server. Filesystem and git policy is "
            "enforced by the selected registered workspace."
        ),
        host="127.0.0.1",
        port=port,
        streamable_http_path=endpoint,
        stateless_http=True,
        json_response=True,
        # The ingress endpoint is intentionally bound to loopback and is
        # protected by the capability path. DNS rebinding checks would reject
        # the Cloudflare forwarding host before the application sees it.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    @mcp.tool()
    def list_workspaces() -> Any:
        """List currently registered, non-expired workspaces."""

        return _tool_call(service, "list_workspaces")

    @mcp.tool()
    def use_workspace(id: str) -> Any:  # noqa: A002 - public MCP schema is ``id``
        """Select a registered workspace by id for subsequent calls."""

        if not isinstance(id, str) or not id:
            raise ValueError("invalid_workspace")
        return _tool_call(service, "use_workspace", id)

    @mcp.tool()
    def list_dir(path: str = ".", workspace: str | None = None) -> Any:
        """List entries in a registered workspace directory."""

        return _tool_call(service, "list_dir", path, workspace=workspace)

    @mcp.tool()
    def read_file(
        path: str,
        start_line: int = 1,
        end_line: int = 0,
        workspace: str | None = None,
    ) -> Any:
        """Read bounded UTF-8 text from a registered workspace."""

        return _tool_call(
            service,
            "read_file",
            path,
            start_line,
            end_line,
            workspace=workspace,
        )

    @mcp.tool()
    def grep_text(
        pattern: str,
        path: str = ".",
        ignore_case: bool = True,
        workspace: str | None = None,
    ) -> Any:
        """Search bounded text in a registered workspace."""

        return _tool_call(
            service,
            "grep_text",
            pattern,
            path,
            ignore_case,
            workspace=workspace,
        )

    @mcp.tool()
    def write_file(
        path: str,
        content: str,
        create_only: bool = False,
        workspace: str | None = None,
    ) -> Any:
        """Atomically write one UTF-8 text file in a writable workspace."""

        return _tool_call(
            service,
            "write_file",
            path,
            content,
            create_only,
            workspace=workspace,
        )

    @mcp.tool()
    def apply_patch(patch: str, workspace: str | None = None) -> Any:
        """Validate and atomically apply a non-binary unified diff."""

        return _tool_call(service, "apply_patch", patch, workspace=workspace)

    @mcp.tool()
    def git_status(workspace: str | None = None) -> Any:
        """Return bounded status for a registered workspace."""

        return _tool_call(service, "git_status", workspace=workspace)

    @mcp.tool()
    def git_diff(
        path: str | None = None,
        staged: bool = False,
        workspace: str | None = None,
    ) -> Any:
        """Return bounded, read-only git diff output."""

        return _tool_call(
            service,
            "git_diff",
            path,
            staged,
            workspace=workspace,
        )

    @mcp.tool()
    def run_check(
        name: str,
        args: list[str] | None = None,
        workspace: str | None = None,
    ) -> Any:
        """Run one configured allow-listed check, when checks are enabled."""

        return _tool_call(
            service,
            "run_check",
            name,
            args,
            workspace=workspace,
        )

    # The wrapper uses this metadata for an authentication audit sink and for
    # tests that need to assert the fixed endpoint without reaching private
    # FastMCP settings.
    setattr(mcp, "_muse_endpoint_path", endpoint)
    setattr(mcp, "_muse_service", service)
    setattr(mcp, "_muse_audit_path", _service_audit_path(service))
    return mcp


def build_from_paths(
    *,
    registry_path: str | os.PathLike[str],
    audit_path: str | os.PathLike[str],
    secret: str,
    checks_enabled: bool = False,
    check_runner: Callable[..., Any] | None = None,
    port: int = DEFAULT_PORT,
    prefix: str = DEFAULT_PREFIX,
) -> FastMCP:
    """Construct a production server and its policy service from paths."""

    cls = _service_type()
    try:
        service = cls(
            registry_path=Path(registry_path),
            audit_path=Path(audit_path),
            checks_enabled=checks_enabled,
            check_runner=check_runner,
        )
    except TypeError:
        # The agreed constructor is positional in the production service, but
        # this keeps the integration helper usable with a small keyword-only
        # fake without hiding policy-layer errors after construction.
        service = cls(
            Path(registry_path),
            Path(audit_path),
            checks_enabled=checks_enabled,
            check_runner=check_runner,
        )
    return build_server(service, secret=secret, port=port, prefix=prefix)


def _auth_guard(
    inner: Any,
    *,
    endpoint_path: str,
    token: str,
    audit_path: Path | None,
) -> Callable[..., Any]:
    expected = f"Bearer {token}".encode("ascii") if token else b""

    async def guarded(scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await inner(scope, receive, send)
            return

        # Return 404 before auth for every path other than the capability URL.
        # This prevents probing an arbitrary URL from learning whether the
        # bearer token is valid, and matches the legacy path-secret behavior.
        if scope.get("path") != endpoint_path:
            _append_auth_audit(audit_path, "path_rejected")
            await _send_plain(send, 404, b"not found")
            return

        supplied = b""
        for key, value in scope.get("headers") or ():
            if key.lower() == b"authorization":
                supplied = value
                break
        if not hmac.compare_digest(supplied, expected):
            _append_auth_audit(audit_path, "rejected")
            await _send_plain(send, 401, b"unauthorized")
            return

        await inner(scope, receive, send)

    return guarded


async def _send_plain(send: Any, status: int, body: bytes) -> None:
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"text/plain; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_app(
    server_or_service: Any | None = None,
    secret: str | os.PathLike[str] | None = None,
    token: str = "",
    *legacy: Any,
    port: int = DEFAULT_PORT,
    prefix: str = DEFAULT_PREFIX,
    audit_path: str | os.PathLike[str] | None = None,
    registry_path: str | os.PathLike[str] | None = None,
    workspaces_path: str | os.PathLike[str] | None = None,
    checks_enabled: bool = False,
    check_runner: Callable[..., Any] | None = None,
) -> Any:
    """Return the bearer-guarded ASGI app.

    The primary integration-test form is::

        build_app(
            workspaces_path=registry,
            audit_path=audit,
            secret=path_secret,
            token=bearer_token,
            port=8765,
            prefix="/muse-mcp",
            checks_enabled=False,
        )

    ``build_app(server, token=...)`` wraps an already-built FastMCP, while
    ``build_app(service, secret, token, ...)`` wraps an injected service.  A
    legacy positional form with ``registry, audit, secret, port, prefix,
    token, checks_enabled, runner`` is accepted for local harnesses only.
    The returned object is an ASGI callable; it is ready for uvicorn and does
    not expose the unguarded FastMCP app.
    """

    # Compatibility with the short-lived positional helper used by older
    # integration fixtures.  The explicit keyword form above is preferred.
    if legacy:
        if len(legacy) < 2 or server_or_service is None or secret is None:
            raise TypeError("invalid_build_app_arguments")
        if isinstance(server_or_service, FastMCP):
            raise TypeError("invalid_build_app_arguments")
        registry_path = os.fspath(server_or_service)
        audit_path = os.fspath(secret)
        # At this point ``token`` still contains the third positional value
        # (the path secret); the bearer token is the third item in ``legacy``.
        actual_secret = token
        port = legacy[0]
        prefix = legacy[1]
        token = legacy[2] if len(legacy) >= 3 else ""
        checks_enabled = bool(legacy[3]) if len(legacy) >= 4 else False
        check_runner = legacy[4] if len(legacy) >= 5 else None
        secret = actual_secret
        server_or_service = None

    if server_or_service is None:
        registry = workspaces_path if workspaces_path is not None else registry_path
        if registry is None or audit_path is None or not isinstance(secret, str):
            raise TypeError("registry_path, audit_path, and secret are required")
        server = build_from_paths(
            registry_path=registry,
            audit_path=audit_path,
            secret=secret,
            checks_enabled=checks_enabled,
            check_runner=check_runner,
            port=port,
            prefix=prefix,
        )
    elif isinstance(server_or_service, FastMCP):
        server = server_or_service
        if secret is not None:
            # Accept the convenient ``build_app(server, secret, token)`` form
            # used by a few in-process harnesses, but validate it before the
            # endpoint is obtained.  The already-built FastMCP remains the
            # authority for the actual route.
            if not isinstance(secret, str):
                raise TypeError("secret must be text")
            _validate_secret(secret)
    else:
        if not isinstance(secret, str):
            raise TypeError("secret is required when passing a service")
        server = build_server(
            server_or_service,
            secret=secret,
            port=port,
            prefix=prefix,
        )

    endpoint_path = getattr(server, "_muse_endpoint_path", None)
    if not isinstance(endpoint_path, str):
        endpoint_path = str(getattr(server.settings, "streamable_http_path", ""))
    if not endpoint_path:
        raise ServerConfigurationError("invalid_endpoint")
    # ``build_app`` is the guarded entry point.  An empty token must never
    # silently turn authentication off; use ``build_server`` directly only
    # for an intentionally unguarded in-process FastMCP harness.
    if not isinstance(token, str):
        raise ServerConfigurationError("invalid_token")
    if len(token) < 32:
        raise ServerConfigurationError("invalid_token")
    if any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in token):
        raise ServerConfigurationError("invalid_token")

    sink = Path(audit_path) if audit_path is not None else getattr(server, "_muse_audit_path", None)
    inner = server.streamable_http_app()
    return _auth_guard(
        inner,
        endpoint_path=endpoint_path,
        token=token,
        audit_path=sink,
    )


# Compatibility spelling used by the old review server and by a few local
# scripts.  ``build`` requires a service and keeps all secret validation.
build = build_server


def _default_registry_path(secret_file: Path) -> Path:
    return secret_file.parent / "workspaces.json"


def _registry_sibling(secret_file: Path, explicit: str | os.PathLike[str] | None) -> Path:
    """Resolve the CLI registry to the secret file's sibling workspaces.json."""

    secret_parent = Path(os.path.realpath(os.fspath(secret_file.parent)))
    expected = secret_parent / "workspaces.json"
    candidate = Path(explicit) if explicit is not None else expected
    if candidate.name.casefold() != "workspaces.json":
        raise ServerConfigurationError("invalid_registry")
    if Path(os.path.realpath(os.fspath(candidate))) != Path(os.path.realpath(os.fspath(expected))):
        raise ServerConfigurationError("invalid_registry")
    return expected


def _default_audit_path(secret_file: Path) -> Path:
    return secret_file.parent / "muse-workspace-mcp-audit.jsonl"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Muse workspace MCP server")
    parser.add_argument("--secret-file", required=True, help="file containing the URL path secret")
    parser.add_argument("--token-file", required=True, help="file containing the bearer token")
    parser.add_argument("--registry", "--workspaces-file", dest="registry", default=None)
    parser.add_argument("--audit", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--checks-enabled", action="store_true")
    parser.add_argument("--dev-root", default=None, help="development root that holds <repo>/.worktrees/muse-<task> (production default is set in registry.py)")
    parser.add_argument("--ttl-minutes", type=float, default=DEFAULT_TTL_MINUTES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> None:
    args = _parse_args(argv)
    if args.dev_root:
        from .registry import configure_dev_root

        configure_dev_root(args.dev_root)
    secret_file = Path(args.secret_file)
    secret = _read_secret_file(secret_file)
    token = _read_secret_file(args.token_file, token=True)
    registry_path = _registry_sibling(secret_file, args.registry)
    audit_path = Path(args.audit) if args.audit else _default_audit_path(secret_file)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    server = build_from_paths(
        registry_path=registry_path,
        audit_path=audit_path,
        secret=secret,
        checks_enabled=bool(args.checks_enabled),
        port=args.port,
        prefix=args.prefix,
    )

    ttl = float(args.ttl_minutes)
    if ttl > 0:
        if ttl > 40000:
            raise SystemExit("invalid_ttl")
        timer = threading.Timer(ttl * 60, lambda: os._exit(0))
        timer.daemon = True
        timer.start()

    # Import uvicorn only for the real guarded process.  Tests can use
    # ``build_app`` without importing or binding a network socket.
    import uvicorn

    uvicorn.run(
        build_app(server, token=token, audit_path=audit_path),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":  # pragma: no cover - exercised by CLI tests
    main()
