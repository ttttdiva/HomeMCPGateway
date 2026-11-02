from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from conftest import assert_error_code, error_text, invoke


def test_write_file_creates_missing_parents_through_depth_six(writable_service, writable_env):
    root = writable_env["root"]

    for depth in range(1, 5):
        components = [f"nested-{depth}-{index}" for index in range(1, depth + 1)]
        relative_parent = Path(*components)
        target = relative_parent / "created.txt"
        invoke(
            writable_service,
            "write_file",
            path=target.as_posix(),
            content=f"depth {depth}\n",
            create_only=True,
        )

        assert (root / target).read_bytes() == f"depth {depth}\n".encode("utf-8")
        assert (root / relative_parent).is_dir()
        assert all(
            (root / Path(*components[:prefix])).is_dir()
            for prefix in range(1, depth + 1)
        )

    # The lexical parent-depth limit is six. Two existing ancestors leave four
    # missing parents for the per-write directory-creation budget.
    components = [f"nested-six-{index}" for index in range(1, 7)]
    existing_parent = root.joinpath(*components[:2])
    existing_parent.mkdir(parents=True)
    target = Path(*components) / "created.txt"
    invoke(writable_service, "write_file", path=target.as_posix(), content="depth 6\n")
    assert (root / target).read_bytes() == b"depth 6\n"
    assert (root / target.parent).is_dir()


def test_write_file_rejects_parent_depth_greater_than_six_without_partial_creation(
    writable_service, writable_env
):
    root = writable_env["root"]
    relative_parent = Path(*[f"too-deep-{index}" for index in range(1, 8)])
    target = relative_parent / "created.txt"

    with pytest.raises(Exception) as caught:
        invoke(writable_service, "write_file", path=target.as_posix(), content="should not exist\n")

    assert error_text(caught.value) in {"limit_exceeded", "path_denied", "invalid_path"}
    assert not (root / relative_parent.parts[0]).exists()


def test_write_file_rejects_more_than_four_new_directories_without_partial_creation(
    writable_service, writable_env
):
    root = writable_env["root"]
    relative_parent = Path(*[f"too-many-{index}" for index in range(1, 6)])
    target = relative_parent / "created.txt"

    with pytest.raises(Exception) as caught:
        invoke(writable_service, "write_file", path=target.as_posix(), content="should not exist\n")

    assert error_text(caught.value) == "limit_exceeded"
    assert not (root / relative_parent.parts[0]).exists()


@pytest.mark.parametrize(
    "bad_path",
    [
        "CON.txt",
        "NUL",
        "PRN.log",
        "AUX/data.txt",
        "COM1/device.txt",
        "LPT1/output.txt",
        "new.txt:stream",
        ".git/new.txt",
        ".env/new.txt",
        "private/notes.txt",
        "credentials/data.txt",
        "secret/data.txt",
        "nested/../new.txt",
        "../outside.txt",
    ],
)
def test_write_file_rejects_denied_reserved_ads_git_and_parent_escape_names(
    writable_service, writable_env, bad_path
):
    root = writable_env["root"]
    with pytest.raises(Exception) as caught:
        invoke(writable_service, "write_file", path=bad_path, content="must not be written\n")

    assert "path" in error_text(caught.value).lower()
    assert not (root / "new.txt").exists()
    assert not (root / "nested").exists()
    assert not (root / "private").exists()
    assert not (root / "credentials").exists()
    assert not (root / "secret").exists()


def test_write_file_rejects_reparse_parent_without_creating_a_junction_in_worktree(
    writable_service, writable_env
):
    root = writable_env["root"]
    parent = root / "reparse-parent"
    parent.mkdir()
    target = parent / "new.txt"
    original_lstat = os.lstat

    def fake_lstat(value):
        candidate = Path(os.fspath(value))
        if os.path.normcase(os.path.normpath(os.fspath(candidate))) == os.path.normcase(
            os.path.normpath(os.fspath(parent))
        ):
            info = original_lstat(value)
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_nlink=info.st_nlink,
                st_file_attributes=0x0400,
            )
        return original_lstat(value)

    # The test only simulates a reparse-point metadata result. It never creates
    # a junction or symlink in the shared worktree.
    with mock.patch.object(os, "lstat", side_effect=fake_lstat):
        assert_error_code(
            lambda: invoke(writable_service, "write_file", path="reparse-parent/new.txt", content="x"),
            "path",
        )

    assert parent.is_dir()
    assert not target.exists()
    assert not list(parent.glob(".muse-write-*"))


def test_write_file_rolls_back_new_parents_after_atomic_replace_failure(
    writable_service, writable_env
):
    root = writable_env["root"]
    module = importlib.import_module(writable_service.__class__.__module__)
    target = Path("rollback-parent") / "nested" / "created.txt"

    with mock.patch.object(module.os, "replace", side_effect=OSError("forced test failure")):
        assert_error_code(
            lambda: invoke(writable_service, "write_file", path=target.as_posix(), content="x"),
            "write",
        )

    assert not (root / "rollback-parent").exists()


def test_apply_patch_accepts_lf_patch_for_crlf_target_and_preserves_crlf_bytes(
    writable_service, writable_env
):
    target = writable_env["root"] / "README.md"
    target.write_bytes(b"before\r\nsecond\r\n")
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,2 @@
-before
+after
 second
"""

    module = importlib.import_module(writable_service.__class__.__module__)
    real_git_run = module._git_run
    calls: list[tuple[str, ...]] = []
    strict_check_failed = False

    def force_strict_check_failure(root, args, **kwargs):
        nonlocal strict_check_failed
        calls.append(tuple(args))
        if (
            not strict_check_failed
            and list(args[:2]) == ["apply", "--check"]
            and "--ignore-whitespace" not in args
        ):
            strict_check_failed = True
            return 1, b"forced strict check failure", b"", False
        return real_git_run(root, args, **kwargs)

    with mock.patch.object(module, "_git_run", side_effect=force_strict_check_failure):
        invoke(writable_service, "apply_patch", patch=patch)

    assert strict_check_failed
    fallback_calls = [args for args in calls if "--ignore-whitespace" in args]
    assert any("--check" in args for args in fallback_calls)
    assert any("--check" not in args for args in fallback_calls)
    assert target.read_bytes() == b"after\r\nsecond\r\n"


def test_apply_patch_crlf_fallback_does_not_ignore_genuinely_different_context(
    writable_service, writable_env
):
    target = writable_env["root"] / "README.md"
    original = b"actual\r\nsecond\r\n"
    target.write_bytes(original)
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1,2 +1,2 @@
-before
+after
 second
"""

    assert_error_code(lambda: invoke(writable_service, "apply_patch", patch=patch), "patch")
    assert target.read_bytes() == original


def test_apply_patch_rejects_eol_preservation_over_read_limit_before_mutation(
    writable_service, writable_env
):
    module = importlib.import_module(writable_service.__class__.__module__)
    target = writable_env["root"] / "README.md"
    original = b"before\r\n" + (b"x" * module.MAX_READ_BYTES)
    target.write_bytes(original)
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-before
+after
"""

    assert_error_code(
        lambda: invoke(writable_service, "apply_patch", patch=patch),
        "limit_exceeded",
    )
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "patch",
    [
        """diff --git a/../outside.txt b/../outside.txt
--- a/../outside.txt
+++ b/../outside.txt
@@ -0,0 +1 @@
+outside
""",
        """diff --git a/README.md b/.git/config
--- a/README.md
+++ b/.git/config
@@ -1 +1 @@
-before
+secret
""",
        """diff --git a/secret-notes.txt b/secret-notes.txt
--- a/secret-notes.txt
+++ b/secret-notes.txt
@@ -0,0 +1 @@
+secret
""",
        """diff --git a/README.md b/README.md
GIT binary patch
literal 3
abc
""",
        """diff --git a/README.md b/README.md
old mode 100644
new mode 100755
""",
        """diff --git a/README.md b/renamed.md
similarity index 100%
rename from README.md
rename to renamed.md
""",
    ],
)
def test_apply_patch_fallback_keeps_forbidden_paths_and_features_rejected(
    writable_service, patch
):
    module = importlib.import_module(writable_service.__class__.__module__)
    fallback_calls: list[object] = []

    def force_fallback(*args, **kwargs):
        fallback_calls.append((args, kwargs))
        return 1, b"", b"", False

    # If a future whitespace fallback is reached, it must still be behind the
    # same parser policy. These forbidden patches must be rejected first.
    with mock.patch.object(module, "_git_run", side_effect=force_fallback):
        assert_error_code(lambda: invoke(writable_service, "apply_patch", patch=patch), "patch")

    assert fallback_calls == []
