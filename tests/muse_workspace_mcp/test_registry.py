from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import (
    assert_error_code,
    invoke,
    make_entry,
    make_service,
    result_text,
    utc_iso,
    write_registry,
)


def _result_text(value):
    if isinstance(value, str):
        return value
    if hasattr(value, "model_dump"):
        return json.dumps(value.model_dump())
    return json.dumps(value, default=str)


def test_list_workspaces_exposes_registry_metadata_without_paths(service, snapshot_env):
    listing = _result_text(invoke(service, "list_workspaces"))
    assert "snap-one" in listing
    assert "snapshot" in listing
    assert "test-repo" in listing
    # A remote caller must not receive the host filesystem path or secret state
    # path as a side effect of listing workspaces.
    assert str(snapshot_env["root"]) not in listing
    assert str(snapshot_env["secrets"]) not in listing


def test_use_workspace_selects_only_registered_id_and_explicit_workspace_is_honored(snapshot_env):
    second = snapshot_env["snapshots"] / "snapshot-two"
    second.mkdir()
    (second / "two.txt").write_text("second", encoding="utf-8")
    entry_two = make_entry("snap-two", second)
    first = snapshot_env["entry"]
    write_registry(snapshot_env["registry"], [first, entry_two], "snap-one")
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])

    selected = invoke(service, "use_workspace", id="snap-two")
    assert "snap-two" in _result_text(selected)
    assert "two.txt" in result_text(invoke(service, "list_dir", path="."))
    assert "README.md" in result_text(invoke(service, "list_dir", path=".", workspace="snap-one"))
    assert "two.txt" in result_text(invoke(service, "list_dir", path=".", workspace="snap-two"))

    write_registry(snapshot_env["registry"], [entry_two], "snap-two")
    assert_error_code(lambda: invoke(service, "use_workspace", id="snap-one"), "workspace")
    assert_error_code(lambda: invoke(service, "list_dir", path=".", workspace="missing"), "workspace")


def test_expired_and_unregistered_workspace_fail_closed(snapshot_env):
    expired_root = snapshot_env["snapshots"] / "expired"
    expired_root.mkdir()
    expired = make_entry("expired", expired_root, expires_at=utc_iso(timedelta(hours=-1)))
    expired["created_at"] = utc_iso(timedelta(hours=-2))
    write_registry(snapshot_env["registry"], [expired], "expired")
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])
    assert_error_code(lambda: invoke(service, "list_dir", path="."), "expired")
    assert_error_code(lambda: invoke(service, "use_workspace", id="expired"), "expired")

    write_registry(snapshot_env["registry"], [], None)
    try:
        listing = _result_text(invoke(service, "list_workspaces"))
    except Exception as exc:
        assert "workspace" in str(exc)
    else:
        assert "expired" not in listing
    assert_error_code(lambda: invoke(service, "use_workspace", id="expired"), "workspace")


@pytest.mark.parametrize("invalid_kind", ["", "repo", "worktreee", "snapshot "])
def test_registry_rejects_unknown_kind_and_malformed_entry(snapshot_env, invalid_kind):
    entry = make_entry("bad-kind", snapshot_env["root"], kind=invalid_kind)
    write_registry(snapshot_env["registry"], [entry], "bad-kind")
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])
    assert_error_code(lambda: invoke(service, "list_workspaces"), "registry")


def test_registry_rejects_snapshot_marked_writable(snapshot_env):
    entry = make_entry("bad-snapshot", snapshot_env["root"], kind="snapshot", writable=True)
    write_registry(snapshot_env["registry"], [entry], "bad-snapshot")
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])
    assert_error_code(lambda: invoke(service, "list_workspaces"), "registry")


def test_registry_rejects_main_worktree_path_and_wrong_worktree_shape(snapshot_env):
    current_worktree = Path(__file__).resolve().parents[2]
    main_tree = current_worktree.parent.parent
    cases = [
        make_entry("main", main_tree, kind="worktree", writable=True),
        make_entry("wrong-shape", snapshot_env["tmp"] / "ordinary", kind="worktree", writable=True),
    ]
    (snapshot_env["tmp"] / "ordinary").mkdir()
    for entry in cases:
        write_registry(snapshot_env["registry"], [entry], entry["id"])
        service = make_service(snapshot_env["registry"], snapshot_env["audit"])
        assert_error_code(lambda: invoke(service, "list_workspaces"), "workspace")


def test_registry_accepts_real_linked_task_worktree_as_read_only_metadata(snapshot_env):
    # This test does not write to the task worktree. It only proves that the
    # linked-worktree shape can be recognized by registration validation.
    current_worktree = Path(__file__).resolve().parents[2]
    git_file = current_worktree / ".git"
    if not git_file.is_file():
        pytest.fail("test worktree must be a linked worktree with a .git file")
    entry = make_entry("linked", current_worktree, kind="worktree", writable=False)
    write_registry(snapshot_env["registry"], [entry], "linked")
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])
    listing = _result_text(invoke(service, "list_workspaces"))
    assert "linked" in listing


def test_registry_file_is_reloaded_after_unregister_without_restart(snapshot_env):
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])
    assert "snap-one" in _result_text(invoke(service, "list_workspaces"))
    write_registry(snapshot_env["registry"], [], None)
    try:
        listing = _result_text(invoke(service, "list_workspaces"))
    except Exception as exc:
        assert "workspace" in str(exc)
    else:
        assert "snap-one" not in listing
    assert_error_code(lambda: invoke(service, "list_dir", path="."), "workspace")


def test_live_snapshot_remains_usable_when_expired_sibling_is_registered(snapshot_env):
    expired_root = snapshot_env["snapshots"] / "expired-sibling"
    expired_root.mkdir()
    (expired_root / "expired.txt").write_text("expired", encoding="utf-8")
    expired = make_entry("expired-sibling", expired_root, expires_at=utc_iso(timedelta(hours=-1)))
    expired["created_at"] = utc_iso(timedelta(hours=-2))
    write_registry(snapshot_env["registry"], [snapshot_env["entry"], expired], "snap-one")
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])

    listing = _result_text(invoke(service, "list_workspaces"))
    assert "snap-one" in listing
    assert "expired-sibling" not in listing
    live = invoke(service, "read_file", path="README.md", workspace="snap-one")
    assert "hello" in (live.get("content", "") if isinstance(live, dict) else str(live))
    assert_error_code(
        lambda: invoke(service, "use_workspace", id="expired-sibling"),
        "expired",
    )
    assert_error_code(
        lambda: invoke(service, "read_file", path="expired.txt", workspace="expired-sibling"),
        "expired",
    )
