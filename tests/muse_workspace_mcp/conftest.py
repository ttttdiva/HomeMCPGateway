"""Shared fixtures for the Muse workspace MCP tests.

The fixtures deliberately use a temporary registry and temporary source roots.
They do not write to the repository worktree that contains this test suite.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import pytest


def run_hidden(args: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run a test helper without opening a visible Windows console."""

    if os.name == "nt":
        kwargs.setdefault("creationflags", getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    return subprocess.run(args, **kwargs)


def utc_iso(delta: timedelta = timedelta(hours=24)) -> str:
    value = datetime.now(timezone.utc) + delta
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_service_class():
    """Import the public service class without making tests depend on layout."""

    try:
        from muse_workspace_mcp.service import WorkspaceService
    except ImportError:
        from muse_workspace_mcp import WorkspaceService
    return WorkspaceService


def make_entry(
    workspace_id: str,
    root: Path,
    *,
    kind: str = "snapshot",
    writable: bool = False,
    checks: list[str] | None = None,
    expires_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": workspace_id,
        "kind": kind,
        "path": str(root),
        "writable": writable,
        "repo_label": "test-repo",
        "checks": list(checks or []),
        "created_at": utc_iso(timedelta(0)),
        "expires_at": expires_at or utc_iso(),
    }


def write_registry(path: Path, entries: list[dict[str, Any]], active: str | None) -> None:
    payload = {"version": 1, "active": active, "workspaces": entries}
    snapshot_roots = {
        Path(str(entry["path"])).parent
        for entry in entries
        if entry.get("kind") == "snapshot" and isinstance(entry.get("path"), str)
    }
    if len(snapshot_roots) == 1:
        payload["snapshots_root"] = str(next(iter(snapshot_roots)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # On POSIX this is owner-only; on Windows it is harmless and keeps the
    # security intent explicit in tests.
    try:
        path.chmod(0o600)
    except OSError:
        pass


def make_service(registry: Path, audit: Path, **kwargs: Any) -> Any:
    cls = load_service_class()
    try:
        return cls(registry_path=registry, audit_path=audit, **kwargs)
    except TypeError:
        # Keep the test useful if the implementation uses positional paths but
        # still exposes the agreed constructor semantics.
        return cls(registry, audit, **kwargs)


def invoke(service: Any, method: str, **kwargs: Any) -> Any:
    """Invoke a synchronous or asynchronous service method."""

    fn = getattr(service, method)
    result = fn(**kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def error_text(exc: BaseException) -> str:
    code = getattr(exc, "code", None) or getattr(exc, "error_code", None)
    if code:
        return str(code)
    return str(exc)


def result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def assert_error_code(call: Callable[[], Any], code: str) -> None:
    with pytest.raises(Exception) as caught:
        call()
    text = error_text(caught.value).lower()
    assert code.lower() in text, f"expected fixed error {code!r}, got {caught.value!r}"


@pytest.fixture
def snapshot_env(tmp_path: Path) -> dict[str, Any]:
    base = tmp_path / "snapshot-case"
    secrets = base / "secrets"
    snapshots = base / "snapshots"
    root = snapshots / "snapshot-one"
    root.mkdir(parents=True)
    (root / "README.md").write_text("hello\nsecond line\n", encoding="utf-8")
    (root / "plain.txt").write_text("needle\nother\n", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "module.py").write_text("answer = 42\n", encoding="utf-8")
    registry = secrets / "workspaces.json"
    audit = secrets / "audit.jsonl"
    entry = make_entry("snap-one", root)
    write_registry(registry, [entry], "snap-one")
    return {
        "tmp": base,
        "secrets": secrets,
        "snapshots": snapshots,
        "root": root,
        "registry": registry,
        "audit": audit,
        "entry": entry,
    }


def real_dev_root() -> Path:
    """The directory that holds the repositories (e.g. ``D:\\Dev``); observed read-only."""

    here = Path(__file__).resolve()
    package_root = next(
        (parent for parent in here.parents if (parent / "muse_workspace_mcp").is_dir()),
        here.parents[2],
    )
    if package_root.parent.name.casefold() == ".worktrees":
        return package_root.parent.parent.parent
    return package_root.parent


def list_names(directory: Path) -> set[str]:
    try:
        return set(os.listdir(directory))
    except OSError:
        return set()


@pytest.fixture(scope="session", autouse=True)
def guard_real_dev_root_stays_clean():
    """Fail the session if any test leaves a new entry directly under the dev root."""

    root = real_dev_root()
    before = list_names(root)
    yield
    created = sorted(list_names(root) - before)
    assert not created, f"tests created entries directly under the dev root: {created}"


@pytest.fixture
def writable_env(tmp_path: Path, request: pytest.FixtureRequest) -> dict[str, Any]:
    """A temporary worktree-shaped source root for service operation tests.

    Registration validation is tested separately against real/fake entries. The
    file operation tests use this isolated root so they never modify the task
    worktree. A small git repository is initialized for patch and git tests.
    """

    base = tmp_path / "write-case"
    secrets = base / "secrets"
    # Everything lives under pytest's tmp_path (a private "Dev" tree). Nothing
    # is ever created under the real development root.
    dev_root = Path(os.path.realpath(tmp_path)) / "Dev"
    repo_holder = dev_root
    repo = dev_root / "repo"
    repo.mkdir(parents=True)
    from muse_workspace_mcp import registry as registry_module

    previous_dev_root = registry_module.current_dev_root()
    registry_module.configure_dev_root(dev_root)
    root = repo / ".worktrees" / "muse-test-task"
    (repo / "README.md").write_text("before\n", encoding="utf-8")
    (repo / "plain.txt").write_text("needle\n", encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "example.py").write_text("def test_fixture():\n    assert True\n", encoding="utf-8")
    run_hidden(["git", "init", "--quiet", str(repo)], check=True, capture_output=True)
    run_hidden(
        ["git", "-C", str(repo), "-c", "user.name=test", "-c", "user.email=test@example.invalid", "add", "."],
        check=True,
        capture_output=True,
    )
    run_hidden(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "initial",
        ],
        check=True,
        capture_output=True,
    )
    run_hidden(
        ["git", "-C", str(repo), "worktree", "add", "-b", "muse-test-task", str(root), "HEAD"],
        check=True,
        capture_output=True,
    )

    def cleanup() -> None:
        run_hidden(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(root)],
            check=False,
            capture_output=True,
        )
        shutil.rmtree(repo_holder, ignore_errors=True)
        registry_module.configure_dev_root(previous_dev_root)

    request.addfinalizer(cleanup)
    registry = secrets / "workspaces.json"
    audit = secrets / "audit.jsonl"
    entry = make_entry("work-one", root, kind="worktree", writable=True, checks=["pytest", "ruff", "tsc"])
    write_registry(registry, [entry], "work-one")
    return {
        "tmp": base,
        "secrets": secrets,
        "root": root,
        "repo": repo,
        "registry": registry,
        "audit": audit,
        "entry": entry,
    }


@pytest.fixture
def service(snapshot_env: dict[str, Any]) -> Any:
    return make_service(snapshot_env["registry"], snapshot_env["audit"])


@pytest.fixture
def writable_service(writable_env: dict[str, Any]) -> Any:
    return make_service(writable_env["registry"], writable_env["audit"])
