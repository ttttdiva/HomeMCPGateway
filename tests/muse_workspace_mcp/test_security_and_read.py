from __future__ import annotations

import json
import os
import re
import stat
from types import SimpleNamespace
from unittest import mock

import pytest

from conftest import assert_error_code, error_text, invoke, make_entry, make_service, result_text, write_registry


def test_list_read_and_grep_preserve_read_only_snapshot(service, snapshot_env):
    listing = result_text(invoke(service, "list_dir", path="."))
    assert "README.md" in listing
    assert "src/" in listing or "src" in listing

    read_result = invoke(service, "read_file", path="README.md")
    text = read_result.get("content", "") if isinstance(read_result, dict) else str(read_result)
    assert "hello" in text
    assert "second line" in text

    hits = result_text(invoke(service, "grep_text", pattern="needle", path="."))
    assert "plain.txt" in hits
    assert "needle" in hits

    # A snapshot entry is always immutable even when a caller supplies the
    # optional workspace id explicitly.
    assert_error_code(
        lambda: invoke(service, "write_file", path="new.txt", content="x", workspace="snap-one"),
        "read_only",
    )


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside.txt",
        r"..\outside.txt",
        "a/../../outside.txt",
        "/etc/passwd",
        r"\server\share\file.txt",
        r"\\?\C:\outside.txt",
        "C:/outside.txt",
        "C:outside.txt",
        "CON",
        "NUL",
        "PRN",
        "AUX",
        "COM1",
        "LPT1",
        "README.md:stream",
        "short~1/file.txt",
        "trailing.",
        "trailing ",
        ".envrc",
        ".git",
        ".git/HEAD",
        ".ENV",
        "config/private/settings.json",
        ".claude/settings.json",
        ".codex/config.toml",
        "private/notes.txt",
        "credentials.txt",
        "COOKIE.txt",
        "secret-notes.txt",
        "token.txt",
        "id_rsa",
        "certificate.pem",
        "database.sqlite3",
        "cache.db",
    ],
)
def test_read_path_policy_rejects_escape_devices_ads_short_names_and_secrets(service, bad_path):
    assert_error_code(lambda: invoke(service, "read_file", path=bad_path), "path")


def test_read_policy_is_case_insensitive_for_directory_walk(service, snapshot_env):
    root = snapshot_env["root"]
    (root / ".ENV.local").write_text("do not read", encoding="utf-8")
    (root / "PRIVATE").mkdir()
    (root / "PRIVATE" / "x.txt").write_text("do not read", encoding="utf-8")
    (root / "safe.txt").write_text("safe needle", encoding="utf-8")

    listing = result_text(invoke(service, "list_dir", path="."))
    assert ".ENV.local" not in listing
    assert "PRIVATE/" not in listing and "PRIVATE" not in listing

    hits = result_text(invoke(service, "grep_text", pattern="do not read", path="."))
    assert "do not read" not in hits
    safe_hits = result_text(invoke(service, "grep_text", pattern="safe needle", path="."))
    assert "safe.txt" in safe_hits and "safe needle" in safe_hits


def test_case_mismatch_does_not_alias_an_existing_file(service):
    assert_error_code(lambda: invoke(service, "read_file", path="readme.md"), "path")


@pytest.mark.parametrize("extension", [".ps1", ".bat"])
def test_script_extensions_are_not_readable_or_grep_scanned(service, snapshot_env, extension):
    marker = f"SCRIPT_EXTENSION_MUST_NOT_LEAK_{extension[1:].upper()}"
    script = snapshot_env["root"] / f"safe{extension}"
    script.write_text(marker, encoding="utf-8")

    with pytest.raises(Exception) as caught:
        invoke(service, "read_file", path=script.name)
    code = error_text(caught.value)
    assert re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code)
    assert marker not in code
    assert str(snapshot_env["root"]) not in code

    grep_result = result_text(invoke(service, "grep_text", pattern=marker, path="."))
    assert marker not in grep_result


def test_symlink_or_reparse_point_is_rejected_without_skipping(service, snapshot_env):
    root = snapshot_env["root"]
    outside = snapshot_env["tmp"] / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    link = root / "link.txt"
    linked = False
    try:
        link.symlink_to(outside)
        linked = True
    except (OSError, NotImplementedError):
        # Windows CI may lack SeCreateSymbolicLinkPrivilege. Exercise the
        # same policy decision through the path object used by the service.
        pass

    if linked:
        assert_error_code(lambda: invoke(service, "read_file", path="link.txt"), "path")
        return

    # The fallback is a unit-level reparse-point simulation, explicitly used
    # instead of silently skipping the security assertion.
    real_lstat = os.lstat

    def fake_lstat(value):
        if os.fspath(value).casefold().endswith("link.txt"):
            return os.stat_result((stat.S_IFLNK, 0, 0, 1, 0, 0, 0, 0, 0))
        return real_lstat(value)

    with mock.patch("os.lstat", side_effect=fake_lstat):
        assert_error_code(lambda: invoke(service, "read_file", path="link.txt"), "path")


def test_nested_symlink_is_rejected_before_final_containment(service, snapshot_env):
    root = snapshot_env["root"]
    outside = snapshot_env["tmp"] / "outside-dir"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside", encoding="utf-8")
    linkdir = root / "linkdir"
    try:
        linkdir.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        real_lstat = os.lstat

        def fake_lstat(value):
            if os.fspath(value).casefold().endswith("linkdir"):
                return os.stat_result((stat.S_IFLNK, 0, 0, 1, 0, 0, 0, 0, 0))
            return real_lstat(value)

        with mock.patch("os.lstat", side_effect=fake_lstat):
            assert_error_code(lambda: invoke(service, "read_file", path="linkdir/secret.txt"), "path")
    else:
        assert_error_code(lambda: invoke(service, "read_file", path="linkdir/secret.txt"), "path")


def test_reparse_point_attribute_is_rejected_by_path_boundary():
    from muse_workspace_mcp.paths import _reject_link_or_reparse

    fake_junction = SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x0400)
    with pytest.raises(Exception) as caught:
        _reject_link_or_reparse(fake_junction)
    assert "path" in str(caught.value).lower()


def test_read_line_and_character_caps(service, snapshot_env):
    lines = [f"{index:04d} " + ("x" * 180) for index in range(400)]
    (snapshot_env["root"] / "large.txt").write_text("\n".join(lines), encoding="utf-8")
    result = invoke(service, "read_file", path="large.txt", start_line=1, end_line=200)
    output = result.get("content", "") if isinstance(result, dict) else str(result)
    assert len(output) <= 17_000
    if isinstance(result, dict):
        assert result["truncated"] is True
    else:
        assert "cut" in output.lower()
    assert "0200" not in output


def test_read_file_supports_later_pages_beyond_the_200_line_page_size(service, snapshot_env):
    lines = [f"line-{index:03d}" for index in range(1, 401)]
    (snapshot_env["root"] / "paged.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = invoke(service, "read_file", path="paged.txt", start_line=201, end_line=220)
    output = result.get("content", "") if isinstance(result, dict) else str(result)
    assert "line-201" in output
    assert "line-220" in output
    assert "line-200" not in output


def test_grep_hit_and_line_caps(service, snapshot_env):
    # Use a single write so the test remains fast on Windows.
    (snapshot_env["root"] / "many.txt").write_text(
        "\n".join(f"needle line {index} " + ("z" * 250) for index in range(100)), encoding="utf-8"
    )
    result = invoke(service, "grep_text", pattern="needle", path="many.txt")
    output = result_text(result)
    assert output.count("many.txt:") <= 60
    if isinstance(result, dict):
        assert len(result["matches"]) <= 60
        assert all(len(item["text"]) <= 200 for item in result["matches"])
    else:
        assert all(len(line) <= 220 for line in output.splitlines())


def test_list_cap_and_git_metadata_are_not_exposed(service, snapshot_env):
    root = snapshot_env["root"]
    for index in range(305):
        (root / f"file-{index:03d}.txt").write_text("x", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret", encoding="utf-8")
    result = invoke(service, "list_dir", path=".")
    output = result_text(result)
    assert (result.get("truncated") if isinstance(result, dict) else "truncated" in output) is True
    assert ".git" not in output


def test_registry_reload_happens_for_each_call(snapshot_env):
    second = snapshot_env["snapshots"] / "snapshot-two"
    second.mkdir()
    (second / "two.txt").write_text("second", encoding="utf-8")
    entry_two = make_entry("snap-two", second)
    service = make_service(snapshot_env["registry"], snapshot_env["audit"])

    first = result_text(invoke(service, "list_dir", path="."))
    assert "README.md" in first
    write_registry(snapshot_env["registry"], [entry_two], "snap-two")
    second_listing = result_text(invoke(service, "list_dir", path="."))
    assert "two.txt" in second_listing
    assert "README.md" not in second_listing


def test_audit_log_contains_metadata_but_never_content(service, snapshot_env):
    marker = "UNIQUE_CONTENT_MUST_NOT_BE_AUDITED"
    (snapshot_env["root"] / "safe.txt").write_text(marker, encoding="utf-8")
    invoke(service, "read_file", path="safe.txt")
    invoke(service, "list_dir", path=".")
    raw = snapshot_env["audit"].read_text(encoding="utf-8")
    assert marker not in raw
    assert str(snapshot_env["root"]) not in raw
    records = [json.loads(line) for line in raw.splitlines() if line.strip()]
    assert records
    assert all("tool" in record for record in records)
    assert all("content" not in record and "patch" not in record for record in records)
    assert any(record.get("workspace", record.get("workspace_id")) == "snap-one" for record in records)
