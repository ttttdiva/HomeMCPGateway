"""Fail-closed workspace registry and workspace identity validation."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Mapping

from .checks import ALLOWED_CHECKS
from .errors import WorkspaceError
from .paths import canonical_root, safe_path, validate_lexical_path

REGISTRY_VERSION = 1
MAX_REGISTRY_BYTES = 2 * 1024 * 1024
MAX_WORKSPACES = 128
WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
COMMIT_ID = re.compile(r"^[0-9a-fA-F]{7,64}$")
REPO_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _raise(code: str) -> None:
    raise WorkspaceError(code)


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_time(value: object) -> datetime:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            _raise("invalid_registry")
    if not isinstance(value, str) or not value or len(value) > 128:
        _raise("invalid_registry")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _raise("invalid_registry")
    if parsed.tzinfo is None:
        _raise("invalid_registry")
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _proc_kwargs() -> dict[str, Any]:
    kwargs: dict[str, Any] = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    return kwargs


def _git(path: Path, *args: str, timeout: float = 8.0) -> tuple[int, str, str]:
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
    try:
        completed = subprocess.run(
            [
                "git",
                "--no-pager",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.hooksPath=NUL",
                "-C",
                os.fspath(path),
                *args,
            ],
            cwd=os.fspath(path),
            env=env,
            timeout=timeout,
            check=False,
            **_proc_kwargs(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 127, "", ""
    return completed.returncode, completed.stdout or "", completed.stderr or ""


def _same(left: object, right: object) -> bool:
    try:
        return os.path.normcase(os.path.normpath(os.fspath(left))) == os.path.normcase(
            os.path.normpath(os.fspath(right))
        )
    except (OSError, TypeError, ValueError):
        return False


# The only fixed development root lives here, as the production default.
# Servers and the admin CLI may replace it with ``--dev-root``; tests point it
# at a private temporary tree so they never touch the real root.
PRODUCTION_DEV_ROOT = Path(r"D:\Dev")
_dev_root: Path = PRODUCTION_DEV_ROOT


def configure_dev_root(path: object) -> Path:
    """Set the directory whose child repositories may host ``.worktrees\\muse-*``."""

    global _dev_root
    try:
        candidate = Path(os.path.normpath(os.fspath(path)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        _raise("invalid_workspace")
    if not candidate.is_absolute():
        _raise("invalid_workspace")
    _dev_root = candidate
    return _dev_root


def current_dev_root() -> Path:
    return _dev_root


def _validate_linked_worktree(path: Path, entry: Mapping[str, Any]) -> tuple[Path, str]:
    if (
        path.parent.name.casefold() != ".worktrees"
        or not path.name.casefold().startswith("muse-")
        or len(path.name) <= len("muse-")
    ):
        _raise("invalid_workspace")
    # <dev_root>\<repo>\.worktrees\muse-<task>: the worktree must sit exactly
    # three levels below the configured development root.
    if not _same(path.parent.parent.parent, current_dev_root()):
        _raise("invalid_workspace")
    try:
        info = os.lstat(path / ".git")
    except OSError:
        _raise("invalid_workspace")
    if (
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or int(getattr(info, "st_file_attributes", 0) or 0) & 0x0400
    ):
        _raise("invalid_workspace")
    try:
        text = (path / ".git").read_text(encoding="utf-8")
        first = text.splitlines()[0]
    except (OSError, UnicodeError, IndexError):
        _raise("invalid_workspace")
    if not first.casefold().startswith("gitdir:"):
        _raise("invalid_workspace")
    raw_gitdir = first.split(":", 1)[1].strip()
    if not raw_gitdir:
        _raise("invalid_workspace")
    gitdir = Path(raw_gitdir)
    if not gitdir.is_absolute():
        gitdir = path / gitdir
    gitdir = Path(os.path.realpath(os.fspath(gitdir)))
    code, common_raw, _ = _git(path, "rev-parse", "--git-common-dir", "--no-ext-diff")
    if code != 0 or not common_raw.strip():
        _raise("invalid_workspace")
    common = Path(common_raw.splitlines()[0].strip())
    if not common.is_absolute():
        common = path / common
    common = Path(os.path.realpath(os.fspath(common)))
    worktrees_dir = common / "worktrees"
    try:
        common_path = os.path.commonpath((os.fspath(worktrees_dir), os.fspath(gitdir)))
    except (OSError, ValueError):
        _raise("invalid_workspace")
    if common_path != os.path.normpath(os.fspath(worktrees_dir)):
        _raise("invalid_workspace")
    if _same(common.parent, path):
        _raise("invalid_workspace")
    branch_code, branch, _ = _git(path, "rev-parse", "--abbrev-ref", "HEAD", "--no-ext-diff")
    branch = branch.splitlines()[0].strip() if branch.splitlines() else ""
    if branch_code != 0 or not branch or branch.casefold() in {"main", "master", "head"}:
        _raise("invalid_workspace")
    repository = entry.get("repository", entry.get("repo"))
    if repository is not None:
        if not isinstance(repository, str):
            _raise("invalid_registry")
        validate_lexical_path(repository)
        try:
            repository_path = canonical_root(repository)
        except WorkspaceError:
            _raise("invalid_workspace")
        if not (_same(repository_path, common.parent) or _same(repository_path, common)):
            _raise("invalid_workspace")
    return path, os.fspath(common)


def _validate_snapshot_path(path_value: str, snapshots_root: object | None) -> Path:
    if snapshots_root is None:
        _raise("invalid_registry")
    try:
        root = canonical_root(snapshots_root)
        path = safe_path(
            root,
            path_value,
            allow_missing=False,
            allow_directory=True,
            allow_file=False,
            allow_absolute=True,
        )
    except WorkspaceError:
        _raise("invalid_workspace")
    try:
        if os.path.commonpath((os.fspath(root), os.fspath(path))) != os.path.normpath(os.fspath(root)):
            _raise("invalid_workspace")
    except (OSError, ValueError):
        _raise("invalid_workspace")
    return path


def _snapshot_commit(entry: Mapping[str, Any]) -> str | None:
    value = entry.get("snapshot_commit", entry.get("head_commit"))
    snapshot = entry.get("snapshot")
    if isinstance(snapshot, Mapping):
        value = snapshot.get("commit", snapshot.get("head", snapshot.get("sha", value)))
    elif isinstance(snapshot, str):
        value = snapshot
    if value is None:
        return None
    if not isinstance(value, str) or not COMMIT_ID.fullmatch(value):
        _raise("invalid_registry")
    return value


def _validate_times(entry: Mapping[str, Any], now: datetime | None, require_live: bool) -> tuple[datetime, datetime]:
    created_raw = entry.get("created_at", entry.get("created", entry.get("registered_at")))
    expires_raw = entry.get("expires_at", entry.get("expires", entry.get("expiration")))
    if created_raw is None or expires_raw is None:
        _raise("invalid_registry")
    created = _parse_time(created_raw)
    expires = _parse_time(expires_raw)
    current = _now(now)
    if created > current + timedelta(minutes=5):
        _raise("invalid_registry")
    if require_live and expires <= current:
        _raise("workspace_expired")
    if expires <= created:
        _raise("invalid_registry")
    if expires - created > timedelta(days=7):
        _raise("invalid_registry")
    return created, expires


def _validate_checks(entry: Mapping[str, Any]) -> list[str]:
    value = entry.get("checks", [])
    if value is None:
        value = []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        _raise("invalid_registry")
    result = [item.casefold() for item in value]
    if len(set(result)) != len(result) or any(item not in ALLOWED_CHECKS for item in result):
        _raise("invalid_registry")
    return result


def validate_workspace_entry(
    entry: Mapping[str, Any],
    *,
    now: datetime | None = None,
    snapshots_root: object | None = None,
    snapshot_root: object | None = None,
    require_live: bool = True,
) -> dict[str, Any]:
    """Validate and normalize one worktree or snapshot entry."""
    if not isinstance(entry, Mapping):
        _raise("invalid_registry")
    if snapshots_root is None:
        snapshots_root = snapshot_root
    workspace_id = entry.get("id", entry.get("workspace_id"))
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.fullmatch(workspace_id):
        _raise("invalid_registry")
    raw_path = entry.get("path", entry.get("worktree", entry.get("workspace")))
    if not isinstance(raw_path, str):
        _raise("invalid_registry")
    if "kind" not in entry:
        _raise("invalid_registry")
    kind = entry.get("kind")
    if not isinstance(kind, str) or kind.casefold() not in {"worktree", "snapshot"}:
        _raise("invalid_registry")
    kind = kind.casefold()
    repo_label = entry.get("repo_label", entry.get("repository_label"))
    if repo_label == "":
        repo_label = None
    if repo_label is not None and (
        not isinstance(repo_label, str) or not REPO_LABEL.fullmatch(repo_label)
    ):
        _raise("invalid_registry")
    checks = _validate_checks(entry)
    created, expires = _validate_times(entry, now, require_live)
    raw_writable = entry.get("writable")
    if raw_writable is not None and not isinstance(raw_writable, bool):
        _raise("invalid_registry")
    raw_read_only = entry.get("read_only", entry.get("readonly"))
    if raw_read_only is not None and not isinstance(raw_read_only, bool):
        _raise("invalid_registry")

    # Expired entries are retained for cleanup and selection diagnostics.  Do
    # not touch their workspace roots: the path may have been deleted already,
    # and an expired sibling must not disable a live workspace.
    if not require_live and expires <= _now(now):
        if kind == "snapshot":
            if raw_writable is not False or (raw_read_only is not None and raw_read_only is not True):
                _raise("invalid_registry")
            snapshot = entry.get("snapshot")
            if snapshot is not None and (
                not isinstance(snapshot, Mapping) or snapshot.get("writable", False) is not False
            ):
                _raise("invalid_registry")
            if _snapshot_commit(entry) is not None:
                _raise("invalid_registry")
            read_only = True
        else:
            layout = entry.get("layout", "linked_worktree")
            layout_kind = layout.get("kind", layout.get("type")) if isinstance(layout, Mapping) else layout
            if layout_kind not in (None, "linked_worktree", "git_worktree", "worktree"):
                _raise("invalid_registry")
            if entry.get("snapshot") is not None:
                _raise("invalid_registry")
            _snapshot_commit(entry)  # validate an optional pinned commit format
            if raw_read_only is None:
                read_only = not raw_writable if raw_writable is not None else False
            else:
                read_only = raw_read_only
            if raw_writable is not None and raw_writable == read_only:
                _raise("invalid_registry")
        result = deepcopy(dict(entry))
        result.update(
            {
                "id": workspace_id,
                "path": raw_path,
                "kind": kind,
                "created_at": _iso(created),
                "expires_at": _iso(expires),
                "checks": checks,
                "read_only": bool(read_only),
                "writable": not bool(read_only),
            }
        )
        if kind == "worktree":
            result["layout"] = "linked_worktree"
        result["_git_common_dir"] = None
        result["_snapshot_commit"] = None
        return result

    common: str | None = None
    commit: str | None = None
    if kind == "snapshot":
        if raw_writable is not False:
            _raise("invalid_registry")
        if raw_read_only is not None and raw_read_only is not True:
            _raise("invalid_registry")
        path = _validate_snapshot_path(raw_path, snapshots_root)
        snapshot = entry.get("snapshot")
        if snapshot is not None and (
            not isinstance(snapshot, Mapping) or snapshot.get("writable", False) is not False
        ):
            _raise("invalid_registry")
        if _snapshot_commit(entry) is not None:
            _raise("invalid_registry")
        read_only = True
    else:
        try:
            path = canonical_root(raw_path)
        except WorkspaceError:
            _raise("invalid_workspace")
        path, common = _validate_linked_worktree(path, entry)
        layout = entry.get("layout", "linked_worktree")
        layout_kind = layout.get("kind", layout.get("type")) if isinstance(layout, Mapping) else layout
        if layout_kind not in (None, "linked_worktree", "git_worktree", "worktree"):
            _raise("invalid_registry")
        if entry.get("snapshot") is not None:
            _raise("invalid_registry")
        commit = _snapshot_commit(entry)
        if commit is not None:
            code, stdout, _ = _git(path, "rev-parse", "HEAD")
            if code != 0 or stdout.strip().casefold() != commit.casefold():
                _raise("workspace_changed")
        if raw_read_only is None:
            read_only = not raw_writable if raw_writable is not None else False
        else:
            read_only = raw_read_only
        if raw_writable is not None and raw_writable == read_only:
            _raise("invalid_registry")

    result = deepcopy(dict(entry))
    result.update(
        {
            "id": workspace_id,
            "path": os.fspath(path),
            "kind": kind,
            "created_at": _iso(created),
            "expires_at": _iso(expires),
            "checks": checks,
            "read_only": bool(read_only),
            "writable": not bool(read_only),
        }
    )
    if kind == "worktree":
        result["layout"] = "linked_worktree"
    result["_git_common_dir"] = common
    result["_snapshot_commit"] = commit
    return result


def _read_document(registry_path: object) -> dict[str, Any]:
    try:
        path = Path(registry_path)
    except (TypeError, ValueError):
        _raise("invalid_registry")
    if not path.is_absolute():
        _raise("invalid_registry")
    try:
        info = os.lstat(path)
    except OSError:
        _raise("invalid_registry")
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_size > MAX_REGISTRY_BYTES:
        _raise("invalid_registry")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        _raise("invalid_registry")
    if not isinstance(value, dict):
        _raise("invalid_registry")
    return value


def _raw_entries(document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], object | None]:
    source = document.get("workspaces", document.get("entries", document.get("workspace_registry")))
    if isinstance(source, Mapping):
        values = []
        for key, value in source.items():
            if not isinstance(value, Mapping):
                _raise("invalid_registry")
            item = dict(value)
            item.setdefault("id", key)
            values.append(item)
    elif isinstance(source, list) and all(isinstance(item, Mapping) for item in source):
        values = [dict(item) for item in source]
    else:
        _raise("invalid_registry")
    if len(values) > MAX_WORKSPACES:
        _raise("invalid_registry")
    active = document.get("active", document.get("active_workspace", document.get("selected")))
    if active is not None and not isinstance(active, str):
        _raise("invalid_registry")
    return values, active


def load_registry(
    registry_path: object,
    *,
    now: datetime | None = None,
    snapshots_root: object | None = None,
    snapshot_root: object | None = None,
    require_live: bool = True,
) -> dict[str, Any]:
    document = _read_document(registry_path)
    version = document.get("version", REGISTRY_VERSION)
    if isinstance(version, bool) or not isinstance(version, int) or version != REGISTRY_VERSION:
        _raise("invalid_registry")
    raw, active = _raw_entries(document)
    snapshots_root = snapshots_root or snapshot_root
    entries = [
        validate_workspace_entry(
            item,
            now=now,
            snapshots_root=snapshots_root or document.get("snapshots_root"),
            require_live=require_live,
        )
        for item in raw
    ]
    ids = [item["id"] for item in entries]
    if len(set(ids)) != len(ids) or (active is not None and active not in ids):
        _raise("invalid_registry")
    result = deepcopy(document)
    result["version"] = REGISTRY_VERSION
    result["workspaces"] = [
        {key: value for key, value in item.items() if not key.startswith("_")} for item in entries
    ]
    result["active"] = active
    return result


def _write_document(registry_path: object, document: Mapping[str, Any]) -> None:
    try:
        path = Path(registry_path)
        if not path.is_absolute():
            _raise("invalid_registry")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            info = os.lstat(path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                _raise("invalid_registry")
        encoded = (json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
        if len(encoded) > MAX_REGISTRY_BYTES:
            _raise("invalid_registry")
        fd, temp_name = tempfile.mkstemp(prefix=".muse-registry-", dir=os.fspath(path.parent))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != "nt":
                os.chmod(temp_name, 0o600)
            os.replace(temp_name, path)
        finally:
            try:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
            except OSError:
                pass
    except WorkspaceError:
        raise
    except (OSError, TypeError, ValueError):
        _raise("invalid_registry")


def list_entries(
    registry_path: object,
    *,
    now: datetime | None = None,
    snapshots_root: object | None = None,
    require_live: bool = True,
) -> list[dict[str, Any]]:
    document = load_registry(
        registry_path, now=now, snapshots_root=snapshots_root, require_live=require_live
    )
    return [dict(item) for item in document["workspaces"]]


list_workspace_entries = list_entries


def register_workspace(
    registry_path: object,
    entry: Mapping[str, Any] | str | None = None,
    *,
    workspace_id: str | None = None,
    id: str | None = None,
    path: str | None = None,
    workspace_path: str | None = None,
    repository: str | None = None,
    expires_at: object | None = None,
    created_at: object | None = None,
    kind: str = "worktree",
    snapshot: object | None = None,
    snapshot_path: str | None = None,
    read_only: bool = False,
    writable: bool | None = None,
    active: bool = False,
    snapshots_root: object | None = None,
    snapshot_root: object | None = None,
) -> dict[str, Any]:
    if isinstance(entry, Mapping):
        value = dict(entry)
    else:
        selected_id = workspace_id or id or (entry if isinstance(entry, str) else None)
        value = {"id": selected_id, "path": path or workspace_path, "kind": kind}
    if workspace_id is not None:
        value["id"] = workspace_id
    elif id is not None:
        value["id"] = id
    if path is not None:
        value["path"] = path
    elif workspace_path is not None:
        value["path"] = workspace_path
    if repository is not None:
        value["repository"] = repository
    if expires_at is not None:
        value["expires_at"] = expires_at
    if created_at is not None:
        value["created_at"] = created_at
    if snapshot is not None:
        value["snapshot"] = snapshot
    if snapshot_path is not None:
        value["snapshot_path"] = snapshot_path
    if "kind" not in value:
        value["kind"] = kind
    if "created_at" not in value and "created" not in value and "registered_at" not in value:
        value["created_at"] = _iso(_now())
    if "expires_at" not in value and "expires" not in value and "expiration" not in value:
        value["expires_at"] = _iso(_now() + timedelta(hours=24))
    if writable is not None:
        value["writable"] = writable
    elif "writable" not in value and value.get("kind", kind).casefold() == "worktree":
        value["writable"] = not read_only
    if "read_only" not in value and value.get("kind", kind).casefold() == "worktree":
        value["read_only"] = read_only
    document: dict[str, Any]
    try:
        document = load_registry(registry_path, snapshots_root=snapshots_root, require_live=False)
    except WorkspaceError as exc:
        if exc.code != "invalid_registry" or Path(registry_path).exists():
            raise
        document = {"version": REGISTRY_VERSION, "workspaces": [], "active": None}
    snapshots_root = snapshots_root or snapshot_root or document.get("snapshots_root")
    if snapshots_root is None and str(value.get("kind", "")).casefold() == "snapshot":
        try:
            candidate_root = Path(value["path"]).parent
            if candidate_root.name.casefold() == "snapshots":
                snapshots_root = candidate_root
        except (KeyError, TypeError, ValueError):
            pass
    validated = validate_workspace_entry(value, snapshots_root=snapshots_root, require_live=True)
    entries = list(document.get("workspaces", []))
    if any(item.get("id") == validated["id"] for item in entries):
        _raise("invalid_registry")
    entries.append({key: val for key, val in validated.items() if not key.startswith("_")})
    document["workspaces"] = entries
    if snapshots_root is not None:
        document["snapshots_root"] = os.fspath(Path(snapshots_root))
    if active or document.get("active") is None:
        document["active"] = validated["id"]
    _write_document(registry_path, document)
    return dict(entries[-1])


def unregister_workspace(
    registry_path: object,
    workspace_id: str | None = None,
    *,
    id: str | None = None,
    snapshots_root: object | None = None,
) -> dict[str, Any]:
    selected = workspace_id or id
    if not isinstance(selected, str) or not WORKSPACE_ID.fullmatch(selected):
        _raise("invalid_input")
    document = load_registry(registry_path, snapshots_root=snapshots_root, require_live=False)
    entries = [item for item in document["workspaces"] if item.get("id") != selected]
    if len(entries) == len(document["workspaces"]):
        _raise("workspace_not_found")
    document["workspaces"] = entries
    if document.get("active") == selected:
        document["active"] = entries[0].get("id") if entries else None
    _write_document(registry_path, document)
    return {"removed": selected, "active": document.get("active")}


def set_active(
    registry_path: object,
    workspace_id: str,
    *,
    snapshots_root: object | None = None,
) -> dict[str, Any]:
    if not isinstance(workspace_id, str) or not WORKSPACE_ID.fullmatch(workspace_id):
        _raise("invalid_input")
    document = load_registry(registry_path, snapshots_root=snapshots_root, require_live=False)
    selected = [item for item in document["workspaces"] if item.get("id") == workspace_id]
    if len(selected) != 1:
        _raise("workspace_not_found")
    validate_workspace_entry(
        selected[0],
        snapshots_root=snapshots_root or document.get("snapshots_root"),
        require_live=True,
    )
    document["active"] = workspace_id
    _write_document(registry_path, document)
    return {"active": workspace_id}


__all__ = [
    "PRODUCTION_DEV_ROOT",
    "REGISTRY_VERSION",
    "configure_dev_root",
    "current_dev_root",
    "list_entries",
    "list_workspace_entries",
    "load_registry",
    "register_workspace",
    "set_active",
    "unregister_workspace",
    "validate_workspace_entry",
]
