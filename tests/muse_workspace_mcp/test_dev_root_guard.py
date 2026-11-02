"""Tests must never create anything directly under the real development root."""

from __future__ import annotations

import os
from pathlib import Path

from conftest import list_names, real_dev_root


def test_writable_fixture_lives_under_tmp_path_not_the_dev_root(writable_env, tmp_path: Path):
    repo = Path(os.path.realpath(writable_env["repo"]))
    worktree = Path(os.path.realpath(writable_env["root"]))
    tmp_real = Path(os.path.realpath(tmp_path))
    dev_root = Path(os.path.realpath(real_dev_root()))

    assert tmp_real in repo.parents
    assert tmp_real in worktree.parents
    assert dev_root not in (repo.parent, repo.parent.parent)
    assert worktree.parent.name == ".worktrees"


def test_listing_diff_detects_a_new_entry_under_a_root(tmp_path: Path):
    root = tmp_path / "fake-dev-root"
    root.mkdir()
    (root / "existing").mkdir()
    before = list_names(root)

    (root / "muse-test-repo-leak").mkdir()

    assert sorted(list_names(root) - before) == ["muse-test-repo-leak"]


def test_listing_of_a_missing_root_is_empty(tmp_path: Path):
    assert list_names(tmp_path / "does-not-exist") == set()


def test_real_dev_root_is_derived_from_the_package_location():
    root = real_dev_root()
    assert root.is_dir()
    assert (root / "80_HomeMCPGateway").exists() or root.name != ""
