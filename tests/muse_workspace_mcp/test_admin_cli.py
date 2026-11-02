from __future__ import annotations

import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from conftest import run_hidden, utc_iso


def _admin(
    registry: Path, *args: str, secret_file: Path | None = None
) -> subprocess.CompletedProcess[str]:
    if secret_file is None:
        secret_file = registry.parent / "path.secret"
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if not secret_file.exists():
        secret_file.write_text("a" * 32, encoding="ascii")
    return run_hidden(
        [
            sys.executable,
            "-m",
            "muse_workspace_mcp.admin",
            *args,
            "--registry",
            str(registry),
            "--secret-file",
            str(secret_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


def test_admin_register_list_use_unregister_and_snapshot_read_only(snapshot_env):
    registry = snapshot_env["registry"]
    result = _admin(
        registry,
        "register",
        "--id",
        "snap-cli",
        "--kind",
        "snapshot",
        "--path",
        str(snapshot_env["root"]),
        "--snapshots-root",
        str(snapshot_env["snapshots"]),
        "--ttl-hours",
        "24",
    )
    assert result.returncode == 0, result.stderr
    listed = _admin(registry, "list")
    assert listed.returncode == 0, listed.stderr
    records = json.loads(listed.stdout)
    items = records.get("workspaces", []) if isinstance(records, dict) else records
    assert any(item.get("id") == "snap-cli" for item in items)
    document = json.loads(registry.read_text(encoding="utf-8"))
    snap = next(item for item in document["workspaces"] if item["id"] == "snap-cli")
    assert snap["writable"] is False

    selected = _admin(registry, "use", "--id", "snap-cli")
    assert selected.returncode == 0, selected.stderr
    document = json.loads(registry.read_text(encoding="utf-8"))
    assert document["active"] == "snap-cli"

    removed = _admin(registry, "unregister", "--id", "snap-cli")
    assert removed.returncode == 0, removed.stderr
    document = json.loads(registry.read_text(encoding="utf-8"))
    assert all(item["id"] != "snap-cli" for item in document["workspaces"])


def test_admin_rejects_main_tree_wrong_shape_and_expiry_beyond_seven_days(snapshot_env):
    registry = snapshot_env["registry"]
    current_worktree = Path(__file__).resolve().parents[2]
    main_tree = current_worktree.parent.parent
    cases = [
        ("main", str(main_tree), ["--kind", "worktree"]),
        ("wrong", str(snapshot_env["tmp"]), ["--kind", "worktree"]),
        ("too-long", str(current_worktree), ["--kind", "worktree", "--ttl-hours", "169"]),
    ]
    for workspace_id, path, extra in cases:
        result = _admin(
            registry,
            "register",
            "--id",
            workspace_id,
            *extra,
            "--path",
            path,
        )
        assert result.returncode != 0
        assert "\\n" not in result.stderr
        assert len(result.stderr) < 200


def test_admin_rejects_expired_explicit_date_and_unknown_use(snapshot_env):
    registry = snapshot_env["registry"]
    expired = _admin(
        registry,
        "register",
        "--id",
        "expired-cli",
        "--kind",
        "worktree",
        "--path",
        str(Path(__file__).resolve().parents[2]),
        "--expires-at",
        utc_iso(timedelta(hours=-1)),
    )
    # The current task worktree is valid, but an already expired timestamp is
    # rejected by registration before it is written.
    assert expired.returncode != 0
    unknown = _admin(registry, "use", "--id", "does-not-exist")
    assert unknown.returncode != 0
    assert "does-not-exist" not in unknown.stderr


def test_admin_rejects_registry_outside_secret_file_directory(snapshot_env):
    secret_file = snapshot_env["secrets"] / "path.secret"
    secret_file.write_text("b" * 32, encoding="ascii")
    outside_registry = snapshot_env["tmp"] / "outside" / "workspaces.json"
    result = _admin(outside_registry, "list", secret_file=secret_file)
    assert result.returncode != 0
    assert "registry" in result.stderr.lower()
    assert str(outside_registry) not in result.stderr
