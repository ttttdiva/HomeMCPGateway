from __future__ import annotations

import importlib
import json
import os
import stat
import threading
from types import SimpleNamespace
from unittest import mock

import pytest

from conftest import assert_error_code, error_text, invoke, run_hidden


def test_write_file_is_utf8_nul_free_bounded_and_atomic(writable_service, writable_env):
    root = writable_env["root"]
    marker = "WRITE_CONTENT_MUST_NOT_BE_AUDITED"
    invoke(writable_service, "write_file", path="new.txt", content=marker + "\n")
    assert (root / "new.txt").read_text(encoding="utf-8") == marker + "\n"
    assert not list(root.glob(".muse-*"))
    assert not list(root.glob("*.tmp"))

    assert_error_code(
        lambda: invoke(writable_service, "write_file", path="new.txt", content="replacement", create_only=True),
        "exists",
    )
    assert (root / "new.txt").read_text(encoding="utf-8") == marker + "\n"

    audit = writable_env["audit"].read_text(encoding="utf-8")
    assert marker not in audit
    assert "content" not in audit.lower()

    assert_error_code(
        lambda: invoke(writable_service, "write_file", path="nul.txt", content="a\x00b"),
        "input",
    )
    assert_error_code(
        lambda: invoke(writable_service, "write_file", path="large.txt", content="x" * (256 * 1024 + 1)),
        "limit",
    )


def test_write_file_rejects_secrets_and_non_writable_workspace(writable_service, snapshot_env, service):
    for path in [".env", "private/x.txt", "id_rsa", "cert.pem", "database.db", "cookie.txt", ".git/config"]:
        assert_error_code(
            lambda path=path: invoke(writable_service, "write_file", path=path, content="x"),
            "path",
        )
    assert_error_code(
        lambda: invoke(service, "write_file", path="new.txt", content="x"),
        "read_only",
    )


def test_write_rate_limit_is_enforced(writable_service):
    # The public limit is intentionally small enough to exercise in one test;
    # a new service fixture gives this test an independent rate window.
    for index in range(60):
        invoke(writable_service, "write_file", path=f"rate-{index:02d}.txt", content="x")
    assert_error_code(
        lambda: invoke(writable_service, "write_file", path="rate-over.txt", content="x"),
        "limit",
    )


def test_workspace_file_count_limit_is_enforced(writable_service, writable_env, monkeypatch):
    module = importlib.import_module(writable_service.__class__.__module__)
    monkeypatch.setattr(module, "MAX_WORKSPACE_FILES", 4)
    # Protected files cannot be written through the service, but they still
    # occupy a worktree inode and must count against the workspace cap.
    (writable_env["root"] / ".env").write_text("protected", encoding="utf-8")
    assert_error_code(
        lambda: invoke(writable_service, "write_file", path="fourth.txt", content="x"),
        "limit",
    )


def test_workspace_file_count_exact_boundary_blocks_one_more_write(writable_service, monkeypatch):
    module = importlib.import_module(writable_service.__class__.__module__)
    # The fixture's README.md, plain.txt, and tests/example.py are exactly the
    # configured count before this operation starts.
    monkeypatch.setattr(module, "MAX_WORKSPACE_FILES", 3)
    assert_error_code(
        lambda: invoke(writable_service, "write_file", path="fourth.txt", content="x"),
        "limit",
    )


def test_apply_patch_rejects_multiple_new_files_over_exact_file_cap(
    writable_service, writable_env, monkeypatch
):
    module = importlib.import_module(writable_service.__class__.__module__)
    monkeypatch.setattr(module, "MAX_WORKSPACE_FILES", 3)
    patch = """diff --git a/new-a.txt b/new-a.txt
--- /dev/null
+++ b/new-a.txt
@@ -0,0 +1 @@
+a
diff --git a/new-b.txt b/new-b.txt
--- /dev/null
+++ b/new-b.txt
@@ -0,0 +1 @@
+b
"""
    assert_error_code(lambda: invoke(writable_service, "apply_patch", patch=patch), "limit")
    assert not (writable_env["root"] / "new-a.txt").exists()
    assert not (writable_env["root"] / "new-b.txt").exists()


def test_concurrent_new_files_cannot_cross_file_count_cap(writable_service, writable_env, monkeypatch):
    module = importlib.import_module(writable_service.__class__.__module__)
    monkeypatch.setattr(module, "MAX_WORKSPACE_FILES", 4)
    barrier = threading.Barrier(2)
    outcomes: list[object] = [None, None]

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            outcomes[index] = invoke(
                writable_service,
                "write_file",
                path=f"count-race-{index}.txt",
                content="x",
                create_only=True,
            )
        except BaseException as exc:
            outcomes[index] = exc

    threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)

    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "limit" in error_text(errors[0])
    root = writable_env["root"]
    assert sum((root / name).exists() for name in ("README.md", "plain.txt", "count-race-0.txt", "count-race-1.txt")) <= 3


@pytest.mark.parametrize(
    "patch_text,code",
    [
        (
            """diff --git a/../outside.txt b/../outside.txt\n--- a/../outside.txt\n+++ b/../outside.txt\n@@ -0,0 +1 @@\n+outside\n""",
            "patch",
        ),
        (
            """diff --git a/../outside.txt b/renamed.txt\nsimilarity index 100%\nrename from ../outside.txt\nrename to renamed.txt\n""",
            "patch",
        ),
        (
            """diff --git a/README.md b/README.md\nold mode 100644\nnew mode 100755\n""",
            "patch",
        ),
        (
            """diff --git a/README.md b/README.md\nGIT binary patch\nliteral 3\nabc\n""",
            "patch",
        ),
        (
            """diff --git a/README.md b/.git/config\n--- a/README.md\n+++ b/.git/config\n@@ -1 +1 @@\n-before\n+secret\n""",
            "patch",
        ),
        (
            """diff --git a/.env b/new.txt\ncopy from .env\ncopy to new.txt\n""",
            "patch",
        ),
    ],
)
def test_apply_patch_rejects_all_paths_and_unsafe_patch_features(writable_service, patch_text, code):
    assert_error_code(lambda: invoke(writable_service, "apply_patch", patch=patch_text), code)


def test_apply_patch_checks_then_applies_only_a_text_file(writable_service, writable_env):
    patch = """diff --git a/README.md b/README.md
--- a/README.md
+++ b/README.md
@@ -1 +1 @@
-before
+after
"""
    invoke(writable_service, "apply_patch", patch=patch)
    assert (writable_env["root"] / "README.md").read_text(encoding="utf-8") == "after\n"


def test_apply_patch_rejects_external_absolute_and_ads_paths(writable_service):
    for path in ["/tmp/outside", r"C:\outside", r"\\server\share\outside", "README.md:stream"]:
        patch = f"""diff --git a/{path} b/{path}
--- a/{path}
+++ b/{path}
@@ -0,0 +1 @@
+x
"""
        assert_error_code(lambda patch=patch: invoke(writable_service, "apply_patch", patch=patch), "patch")


def test_git_tools_use_read_only_fixed_arguments_and_cap_output(writable_service, writable_env):
    module = importlib.import_module(writable_service.__class__.__module__)
    subprocess_module = getattr(module, "subprocess", None)
    if subprocess_module is None:
        pytest.fail("git implementation must use the subprocess module with an argv list")

    original_popen = subprocess_module.Popen
    with mock.patch.object(subprocess_module, "Popen", wraps=original_popen) as popen:
        invoke(writable_service, "git_status")
        invoke(writable_service, "git_diff")

    calls = [call.args[0] for call in popen.call_args_list if call.args]
    calls = [argv for argv in calls if "status" in argv or "diff" in argv]
    assert calls
    for argv in calls:
        assert isinstance(argv, (list, tuple))
        assert "--no-pager" in argv
        assert any(str(item).startswith("core.fsmonitor=false") for item in argv)
        assert any(str(item).startswith("core.hooksPath=") for item in argv)
        assert "status" in argv or "diff" in argv
        if "diff" in argv:
            assert "--no-ext-diff" in argv
            assert "--no-textconv" in argv or "--textconv" not in argv
        assert all(";" not in str(item) and "|" not in str(item) for item in argv)

    # A fake command result verifies the service-side output ceiling without
    # creating thousands of files in the test worktree.
    def fake_git_run(worktree, args, *, input_data=None, max_output=0, timeout=0):
        del worktree, args, input_data, max_output, timeout
        return 0, b"x" * 200_000, b"", True

    with mock.patch.object(module, "_git_run", side_effect=fake_git_run):
        result = invoke(writable_service, "git_status")
    assert result["truncated"] is True
    assert len(result["status"]) <= 70_000

    def fake_diff_run(worktree, args, *, input_data=None, max_output=0, timeout=0):
        del worktree, input_data, max_output, timeout
        if "--name-only" in args:
            return 0, b"plain.txt\x00", b"", False
        payload = b"x" * 700_000
        return 0, payload[:524_288], b"", True

    with mock.patch.object(module, "_git_run", side_effect=fake_diff_run):
        diff_result = invoke(writable_service, "git_diff")
    assert diff_result["truncated"] is True
    assert len(diff_result["diff"]) <= 524_288


def test_git_diff_path_is_scoped_and_staged_is_boolean(writable_service):
    assert_error_code(lambda: invoke(writable_service, "git_diff", path="../outside"), "path")
    assert_error_code(lambda: invoke(writable_service, "git_diff", path=".git/config"), "path")
    assert_error_code(lambda: invoke(writable_service, "git_diff", staged="yes"), "input")


def test_git_diff_audit_keeps_a_relative_path_only(writable_service, writable_env):
    invoke(writable_service, "git_diff", path="README.md")
    records = [
        json.loads(line)
        for line in writable_env["audit"].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    record = next(item for item in reversed(records) if item.get("tool") == "git_diff")
    assert record["path"] == "README.md"
    assert set(record) <= {"tool", "result", "workspace", "path", "size"}
    assert "content" not in record
    assert "patch" not in record
    assert str(writable_env["root"]) not in json.dumps(record, ensure_ascii=False)


def test_git_diff_never_returns_denied_file_content(writable_service, writable_env):
    root = writable_env["root"]
    marker = "GIT_DIFF_SECRET_CONTENT_MUST_NOT_LEAK"
    denied = root / "secret-notes.txt"
    denied.write_text(marker, encoding="utf-8")
    run_hidden(["git", "-C", str(root), "add", "secret-notes.txt"], check=True, capture_output=True)
    output = invoke(writable_service, "git_diff", staged=True)
    assert marker not in str(output)
    assert_error_code(lambda: invoke(writable_service, "git_diff", path="secret-notes.txt"), "path")


def test_broad_git_diff_excludes_indexed_hardlink_and_modified_symlink(
    writable_service, writable_env
):
    root = writable_env["root"]
    marker = "LINKED_PATH_CONTENT_MUST_NOT_LEAK"

    # Git indexes hardlinks as ordinary files, but the filesystem boundary must
    # reject the path because its inode has more than one link. Modifying the
    # link also modifies the tracked source file, so the broad diff has both
    # paths available for the service to filter.
    hardlink = root / "hardlink.txt"
    try:
        os.link(root / "plain.txt", hardlink)
    except OSError as exc:
        pytest.fail(f"hardlink creation unexpectedly unavailable: {exc}")
    run_hidden(["git", "-C", str(root), "add", "hardlink.txt"], check=True, capture_output=True)
    hardlink.write_text(marker, encoding="utf-8")
    broad = invoke(writable_service, "git_diff")
    assert "hardlink.txt" not in broad["files"]
    assert "hardlink.txt" not in broad["diff"]
    assert marker not in broad["diff"]

    # A real symlink is checked when the Windows test process has the privilege
    # to create one. If the privilege is unavailable, exercise the same
    # reparse-point policy directly instead of silently losing coverage.
    symlink = root / "muse-link.txt"
    try:
        symlink.symlink_to(root / "plain.txt")
    except (OSError, NotImplementedError):
        from muse_workspace_mcp.paths import _reject_link_or_reparse

        fake_reparse = SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x0400)
        with pytest.raises(Exception) as caught:
            _reject_link_or_reparse(fake_reparse)
        assert "path" in str(caught.value).lower()
        return

    try:
        run_hidden(["git", "-C", str(root), "add", "muse-link.txt"], check=True, capture_output=True)
        symlink.unlink()
        symlink.symlink_to(root / "README.md")
        broad = invoke(writable_service, "git_diff")
        assert "muse-link.txt" not in broad["files"]
        assert "muse-link.txt" not in broad["diff"]
    finally:
        if symlink.is_symlink():
            symlink.unlink()


def test_git_diff_literalizes_bracket_pathspec_and_does_not_leak_secret_marker(
    writable_service, writable_env
):
    root = writable_env["root"]
    marker = "BRACKET_PATHSPEC_SECRET_MARKER"
    safe_path = root / "s[e]cret.txt"
    secret_path = root / "secret.txt"

    safe_path.write_text("base\n", encoding="utf-8")
    run_hidden(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "add",
            "--",
            "s[e]cret.txt",
        ],
        check=True,
        capture_output=True,
    )
    run_hidden(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "add bracket path",
        ],
        check=True,
        capture_output=True,
    )
    safe_path.write_text("changed\n", encoding="utf-8")
    secret_path.write_text(marker, encoding="utf-8")
    run_hidden(["git", "-C", str(root), "add", "--", "s[e]cret.txt", "secret.txt"], check=True, capture_output=True)

    result = invoke(writable_service, "git_diff", path="s[e]cret.txt", staged=True)
    assert result["files"] == ["s[e]cret.txt"]
    assert "secret.txt" not in result["diff"]
    assert marker not in result["diff"]


def test_create_only_same_target_has_one_atomic_winner(writable_service, writable_env):
    barrier = threading.Barrier(2)
    outcomes: list[object] = [None, None]

    def write(index: int) -> None:
        try:
            barrier.wait(timeout=5)
            outcomes[index] = invoke(
                writable_service,
                "write_file",
                path="create-only-race.txt",
                content=f"winner-{index}\n",
                create_only=True,
            )
        except BaseException as exc:  # keep both thread outcomes for assertions
            outcomes[index] = exc

    threads = [threading.Thread(target=write, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)

    successes = [outcome for outcome in outcomes if not isinstance(outcome, BaseException)]
    errors = [outcome for outcome in outcomes if isinstance(outcome, BaseException)]
    assert len(successes) == 1
    assert len(errors) == 1
    assert "already_exists" in error_text(errors[0])
    content = (writable_env["root"] / "create-only-race.txt").read_text(encoding="utf-8")
    assert content in {"winner-0\n", "winner-1\n"}
