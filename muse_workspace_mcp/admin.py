"""Operator-only workspace registry command line interface.

The public MCP server never exposes these operations.  An operator prepares a
registry with commands such as::

    python -m muse_workspace_mcp.admin register --secret-file C:\\...\\secret \\
        --id task-123 --kind worktree --path D:\\Dev\\repo\\.worktrees\\muse-task-123
    python -m muse_workspace_mcp.admin use --secret-file C:\\...\\secret --id task-123

Validation of linked worktrees, snapshots, expiry, symlink/junction policy,
and ownership is intentionally delegated to the core ``WorkspaceRegistry``.
This CLI only parses and shapes the entry; it never edits the JSON registry
directly, so the server and operator commands share exactly one policy.
"""
from __future__ import annotations

import argparse
import dataclasses
import inspect
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


DEFAULT_TTL_HOURS = 24.0
MAX_TTL_HOURS = 24.0 * 7.0
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


def _registry_module() -> Any:
    """Load the policy registry lazily so ``--help`` works without setup."""

    from . import registry

    return registry


def _fixed_code(exc: BaseException) -> str:
    for attr in ("code", "error_code", "safe_code"):
        candidate = getattr(exc, attr, None)
        if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", candidate):
            return candidate
    if exc.__class__.__name__ in {"WorkspaceError", "RegistryError"}:
        first = exc.args[0] if exc.args else None
        if isinstance(first, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", first):
            return first
    if isinstance(exc, ValueError) and exc.args and exc.args[0] == "invalid_registry":
        return "invalid_registry"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_input"
    return "internal_error"


def _secret_registry_path(
    registry: str | None,
    secret_file: str | None,
) -> Path:
    if not secret_file:
        raise ValueError("invalid_registry")

    secret_parent = Path(os.path.realpath(os.fspath(Path(secret_file).parent)))
    expected = secret_parent / "workspaces.json"
    candidate = Path(registry) if registry else expected
    # The registry must remain the sibling workspaces.json under the same
    # owner-only directory as the path/token secret files.  Compare resolved
    # paths so relative aliases and junctioned parents cannot bypass this
    # boundary; the core registry separately rejects a symlinked file.
    if candidate.name.casefold() != "workspaces.json":
        raise ValueError("invalid_registry")
    if Path(os.path.realpath(os.fspath(candidate))) != Path(os.path.realpath(os.fspath(expected))):
        raise ValueError("invalid_registry")
    return expected


def _new_registry(path: Path) -> Any:
    """Return either a class-backed registry or the core module/path pair."""

    module = _registry_module()
    cls = getattr(module, "WorkspaceRegistry", None)
    if cls is None:
        return (module, path)
    try:
        return cls(path)
    except TypeError:
        # Keep compatibility with a keyword-only constructor without catching
        # policy errors from a successfully-created instance.
        return cls(registry_path=path)


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call one of the small accepted registry protocol variants."""

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(*args, **kwargs)
    params = signature.parameters
    accepts_extra = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in params.values()
    )
    if kwargs and not accepts_extra and not any(key in params for key in kwargs):
        # A registry implementation may use one entry positional argument.
        if len(args) == 1:
            return fn(args[0])
    return fn(*args, **kwargs)


def _register(
    registry: Any,
    entry: Mapping[str, Any],
    *,
    snapshots_root: str | Path | None = None,
) -> Any:
    if isinstance(registry, tuple) and len(registry) == 2:
        module, path = registry
        fn = getattr(module, "register_workspace", None)
        if callable(fn):
            return fn(path, entry, snapshots_root=snapshots_root)
    for name in ("register", "register_workspace"):
        fn = getattr(registry, name, None)
        if callable(fn):
            try:
                return _call(fn, entry, snapshots_root=snapshots_root)
            except TypeError:
                return _call(fn, **dict(entry))
    raise RuntimeError("internal_error")


def _list(registry: Any) -> Any:
    if isinstance(registry, tuple) and len(registry) == 2:
        module, path = registry
        fn = getattr(module, "list_entries", None) or getattr(module, "list_workspace_entries", None)
        if callable(fn):
            return fn(path)
    for name in ("list_workspaces", "list_registered", "list_entries"):
        fn = getattr(registry, name, None)
        if callable(fn):
            return fn()
    # A ``list`` method is accepted only after the explicit names to avoid
    # accidentally calling Python's list constructor on a registry object.
    fn = getattr(registry, "list", None)
    if callable(fn):
        return fn()
    raise RuntimeError("internal_error")


def _use(registry: Any, workspace_id: str) -> Any:
    if isinstance(registry, tuple) and len(registry) == 2:
        module, path = registry
        fn = getattr(module, "set_active", None)
        if callable(fn):
            return fn(path, workspace_id)
    for name in ("use_workspace", "select_workspace", "use"):
        fn = getattr(registry, name, None)
        if callable(fn):
            return fn(workspace_id)
    raise RuntimeError("internal_error")


def _unregister(registry: Any, workspace_id: str) -> Any:
    if isinstance(registry, tuple) and len(registry) == 2:
        module, path = registry
        fn = getattr(module, "unregister_workspace", None)
        if callable(fn):
            return fn(path, workspace_id)
    for name in ("unregister", "unregister_workspace", "remove"):
        fn = getattr(registry, name, None)
        if callable(fn):
            return fn(workspace_id)
    raise RuntimeError("internal_error")


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "model_dump") and callable(value.model_dump):
        return _jsonable(value.model_dump())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return _jsonable(vars(value))
    return value


def _print_result(value: Any) -> None:
    try:
        json.dump(_jsonable(value), sys.stdout, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        json.dump(str(value), sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _parse_expiry(expires_at: str | None, ttl_hours: float) -> str:
    if expires_at:
        # Parse and normalise only to reject malformed operator input.  The
        # registry remains the source of truth for policy and max expiry.
        try:
            value = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("invalid_expiry") from None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        current = datetime.now(timezone.utc)
        normalised = value.astimezone(timezone.utc)
        if normalised <= current or normalised > current + timedelta(hours=MAX_TTL_HOURS):
            raise ValueError("invalid_expiry")
        return normalised.isoformat().replace("+00:00", "Z")

    try:
        ttl = float(ttl_hours)
    except (TypeError, ValueError):
        raise ValueError("invalid_expiry") from None
    if not 0 < ttl <= MAX_TTL_HOURS:
        raise ValueError("invalid_expiry")
    value = datetime.now(timezone.utc) + timedelta(hours=ttl)
    return value.isoformat().replace("+00:00", "Z")


def _id(value: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValueError("invalid_workspace")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage Muse MCP workspace registrations")
    parser.add_argument("--dev-root", default=None, help="development root (production default is set in registry.py)")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(command: argparse.ArgumentParser) -> None:
        command.add_argument("--registry", default=None, help="explicit workspaces.json path")
        command.add_argument("--secret-file", default=None, help="use its owner-only directory/workspaces.json")

    register = sub.add_parser("register", help="register one validated workspace")
    common(register)
    register.add_argument("--id", required=True)
    register.add_argument("--kind", choices=("worktree", "snapshot"), required=True)
    register.add_argument("--path", required=True)
    register.add_argument("--repo-label", default="")
    writable = register.add_mutually_exclusive_group()
    writable.add_argument("--writable", dest="writable", action="store_true")
    writable.add_argument("--read-only", dest="writable", action="store_false")
    register.set_defaults(writable=None)
    register.add_argument("--checks", nargs="*", default=[])
    register.add_argument("--expires-at", default=None)
    register.add_argument("--ttl-hours", type=float, default=DEFAULT_TTL_HOURS)
    register.add_argument(
        "--snapshots-root",
        default=None,
        help="absolute root containing snapshot directories",
    )

    listing = sub.add_parser("list", help="list current registrations")
    common(listing)

    use = sub.add_parser("use", help="select the active registered workspace")
    common(use)
    use.add_argument("--id", required=True)

    unregister = sub.add_parser("unregister", help="remove one registration")
    common(unregister)
    unregister.add_argument("--id", required=True)
    return parser


def _entry(args: argparse.Namespace) -> dict[str, Any]:
    workspace_id = _id(args.id)
    kind = args.kind
    writable = bool(args.writable) if args.writable is not None else kind == "worktree"
    if kind == "snapshot":
        # Fail before the registry is called so a typo cannot ask the core to
        # make a snapshot writable.  The core repeats this invariant.
        writable = False
    checks = list(args.checks or [])
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name) for name in checks):
        raise ValueError("invalid_check")
    return {
        "id": workspace_id,
        "kind": kind,
        "path": str(Path(args.path)),
        "writable": writable,
        "repo_label": str(args.repo_label or ""),
        "checks": checks,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "expires_at": _parse_expiry(args.expires_at, args.ttl_hours),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.dev_root:
        from .registry import configure_dev_root

        configure_dev_root(args.dev_root)
    try:
        path = _secret_registry_path(args.registry, args.secret_file)
        registry = _new_registry(path)
        if args.command == "register":
            snapshots_root = Path(args.snapshots_root) if args.snapshots_root else None
            result = _register(registry, _entry(args), snapshots_root=snapshots_root)
        elif args.command == "list":
            result = _list(registry)
        elif args.command == "use":
            result = _use(registry, _id(args.id))
        elif args.command == "unregister":
            result = _unregister(registry, _id(args.id))
        else:  # pragma: no cover - argparse enforces the choices
            raise ValueError("invalid_command")
        _print_result(result)
        return 0
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        # Fixed code only.  In particular, never print a RegistryError's
        # filesystem detail or a JSON entry containing source content.
        print(_fixed_code(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - covered by CLI subprocess tests
    raise SystemExit(main())
