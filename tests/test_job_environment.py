import json
import ntpath
import stat
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from home_mcp_gateway.job_environment import (
    CodexWindowsAppsPolicyError,
    _is_codex_launch,
    _is_system32_powershell,
    _is_windowsapps_entry,
    _packaged_path,
    _qualified_path_entry,
    _shell_file_status,
    apply_codex_temp_default,
    apply_codex_windowsapps_policy,
    build_job_environment,
    is_direct_official_codex_command,
)


@pytest.mark.parametrize("command", [
    ["C:/install/node.exe", "C:/install/node_modules/@openai/codex/bin/codex.js", "exec"],
    ["C:/install/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe", "exec"],
])
def test_codex_temp_default_is_per_job_and_preserves_explicit_values(tmp_path, command):
    (tmp_path / ".git").mkdir()
    cwd = tmp_path / "src"
    cwd.mkdir()
    child = {"Temp": "shared", "TMP": "shared", "tmpdir": "explicit"}
    metadata = apply_codex_temp_default(command, str(cwd), "job-1", {"tmpdir": "explicit"}, child)
    target = tmp_path / ".runtime" / "codex-job-temp" / "job-1"
    assert target.is_dir()
    assert child == {"TEMP": str(target), "TMP": str(target), "tmpdir": "explicit"}
    assert metadata == {"codex_temp_default": str(target)}


def test_codex_temp_default_leaves_other_jobs_and_owns_nonrepo_temp(tmp_path, monkeypatch):
    from pathlib import Path
    import home_mcp_gateway.job_environment as environment
    gateway_root = tmp_path / "gateway"
    monkeypatch.setattr(environment, "_GATEWAY_REPO_ROOT", gateway_root)
    original_exists = Path.exists
    monkeypatch.setattr(Path, "exists", lambda path: False if path.name == ".git" else original_exists(path))
    child = {"TEMP": "shared"}
    assert apply_codex_temp_default(["other.exe"], str(tmp_path), "job", {}, child) == {}
    assert apply_codex_temp_default("codex exec", str(tmp_path), "job", {}, child) == {}
    result = apply_codex_temp_default(["node.exe", "x/node_modules/@openai/codex/bin/codex.js"], str(tmp_path), "job", {}, child)
    target = gateway_root / ".runtime" / "codex-job-temp" / "job"
    assert result == {"codex_temp_default": str(target)}
    assert child == {key: str(target) for key in ("TEMP", "TMP", "TMPDIR")}
    assert target.is_dir()
    assert not (tmp_path / ".runtime").exists()


def test_codex_temp_default_falls_back_when_repository_temp_is_not_writable(tmp_path, monkeypatch):
    from pathlib import Path
    import home_mcp_gateway.job_environment as environment
    repo = tmp_path / "readonly-repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: worktree")
    gateway_root = tmp_path / "gateway"
    monkeypatch.setattr(environment, "_GATEWAY_REPO_ROOT", gateway_root)
    original_mkdir = Path.mkdir
    def mkdir(path, *args, **kwargs):
        if repo in path.parents:
            raise PermissionError("readonly repository")
        return original_mkdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    child = {}
    result = apply_codex_temp_default(["node.exe", "x/node_modules/@openai/codex/bin/codex.js"], str(repo), "fallback-job", {}, child)
    target = gateway_root / ".runtime" / "codex-job-temp" / "fallback-job"
    assert result == {"codex_temp_default": str(target)}
    assert all(child[key] == str(target) for key in ("TEMP", "TMP", "TMPDIR"))
    assert target.is_dir()


def test_full_parent_environment_is_inherited_including_credentials_and_tool_settings():
    parent = {
        "Path": "original-path",
        "OPENAI_API_KEY": "sensitive-fixture",
        "GIT_ASKPASS": "helper-path",
        "UNLISTED_CUSTOM_VARIABLE": "custom-value",
        "NODE_OPTIONS": "--no-warnings",
    }
    child, metadata = build_job_environment(parent)
    assert child == parent
    assert metadata == {
        "env_policy": "inherit-all",
        "env_inherited_count": len(parent),
        "env_dropped_count": 0,
        "env_dropped_secret_like_names": [],
    }
    assert "sensitive-fixture" not in json.dumps(metadata)


def test_explicit_values_override_inherited_variables_without_filtering():
    child, metadata = build_job_environment(
        {"PATH": "old", "OPENAI_API_KEY": "old-token", "CUSTOM": "preserve"},
        {"Path": "new", "OPENAI_API_KEY": "override"},
    )
    assert child == {
        "Path": "new",
        "OPENAI_API_KEY": "override",
        "CUSTOM": "preserve",
    }
    assert metadata["env_dropped_count"] == 0
    assert metadata["env_inherited_count"] == 3


@pytest.mark.parametrize("command", [
    ["codex.cmd", "exec", "--sandbox", "workspace-write", "--approve-for-me", "--json", "prompt"],
    ["codex.exe", "exec", "-s", "read-only", "-c", 'sandbox_mode="workspace-write"', "prompt"],
    ["codex", "exec", "--sandbox=read-only", "--config", 'approval_policy="on-request"', "prompt"],
])
def test_gateway_codex_exec_forces_full_access_over_restrictive_caller_args(command):
    from home_mcp_gateway.job_environment import enforce_codex_full_access
    result, metadata = enforce_codex_full_access(command, {"APPDATA": "C:/Users/test/AppData/Roaming"}, is_windows=True)
    assert result[0:2] == command[0:2]
    assert result[2] == "--dangerously-bypass-approvals-and-sandbox"
    assert result[-1] == "prompt"
    assert "--approve-for-me" not in result
    assert "--sandbox" not in result
    assert "workspace-write" not in result
    assert 'shell_environment_policy.inherit="all"' in result
    assert metadata["codex_full_access"] is True
    assert metadata["codex_sandbox_effective"] == "danger-full-access"


def test_gateway_codex_exec_preserves_resume_args_and_deduplicates_full_access():
    from home_mcp_gateway.job_environment import enforce_codex_full_access
    command = ["codex.cmd", "exec", "--dangerously-bypass-approvals-and-sandbox", "resume", "id", "prompt"]
    result, metadata = enforce_codex_full_access(command, is_windows=True)
    assert result.count("--dangerously-bypass-approvals-and-sandbox") == 1
    assert result[-3:] == ["resume", "id", "prompt"]
    assert metadata["codex_full_access"]


def test_gateway_codex_exec_supports_native_and_node_entrypoints_without_affecting_others():
    from home_mcp_gateway.job_environment import enforce_codex_full_access
    native = ["C:/install/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
              "exec", "--approve-for-me", "prompt"]
    node = ["C:/install/node.exe", "C:/install/node_modules/@openai/codex/bin/codex.js",
            "exec", "--sandbox", "workspace-write", "prompt"]
    for command in [native, node]:
        result, metadata = enforce_codex_full_access(command, is_windows=True)
        assert "--dangerously-bypass-approvals-and-sandbox" in result
        assert metadata["codex_full_access"] is True
    for command in [["claude.exe", "exec", "prompt"], ["codex.cmd", "app-server"],
                    ["python.exe", "-c", "print(1)"]]:
        assert enforce_codex_full_access(command, is_windows=True) == (command, {})

NATIVE_CODEX_COMMAND = [
    "C:/install/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe",
    "exec",
]
NODE_CODEX_COMMAND = [
    "C:/install/node.exe",
    "C:/install/node_modules/@openai/codex/bin/codex.js",
    "exec",
]
POLICY_ENV = {"HOME_MCP_CODEX_WINDOWSAPPS_POLICY": "exclude_alias"}


def _windowsapps_alias_dir(tmp_path):
    alias_dir = tmp_path / "Users" / "qa" / "AppData" / "Local" / "Microsoft" / "WindowsApps"
    alias_dir.mkdir(parents=True)
    (alias_dir / "pwsh.exe").write_bytes(b"alias-stub")
    return alias_dir


def _powershell_dir(tmp_path, executable):
    shell_dir = tmp_path / "Program Files" / "PowerShell" / "7"
    shell_dir.mkdir(parents=True)
    (shell_dir / executable).write_bytes(b"real-shell")
    return shell_dir


@pytest.mark.parametrize("command", [NATIVE_CODEX_COMMAND, NODE_CODEX_COMMAND])
def test_direct_official_codex_detection_accepts_existing_shapes(command):
    assert is_direct_official_codex_command(command)


@pytest.mark.parametrize("command", [
    ["C:/Python312/python.exe", "C:/install/node_modules/@openai/codex/bin/codex.js", "exec"],
    ["C:/install/claude.exe", "exec"],
    ["codex", "exec"],
    ["C:/install/node.exe", "C:/install/other/bin/tool.js", "exec"],
    "codex exec",
    [],
])
def test_direct_official_codex_detection_rejects_wrappers_and_other_clis(command):
    assert not is_direct_official_codex_command(command)


@pytest.mark.parametrize("command", [NATIVE_CODEX_COMMAND, NODE_CODEX_COMMAND])
def test_windowsapps_default_prefers_real_pwsh_and_preserves_alias(tmp_path, command):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    shell_dir = _powershell_dir(tmp_path, "pwsh.exe")
    original_path = ";".join([str(alias_dir), str(shell_dir)])
    child = {"Path": original_path}
    metadata = apply_codex_windowsapps_policy(command, {}, child, is_windows=True)
    assert metadata == {
        "codex_windowsapps_alias_present": True,
        "codex_windowsapps_unsafe_entries_present": True,
        "codex_windowsapps_policy": "prefer_pwsh",
        "codex_windowsapps_powershell_candidate": "pwsh",
    }
    assert child == {"Path": ";".join([str(shell_dir), str(alias_dir)])}
    assert str(alias_dir) not in json.dumps(metadata)
    assert original_path not in json.dumps(metadata)


def test_windowsapps_default_reports_missing_alias(tmp_path):
    shell_dir = _powershell_dir(tmp_path, "pwsh.exe")
    child = {"PATH": str(shell_dir)}
    metadata = apply_codex_windowsapps_policy(NODE_CODEX_COMMAND, {}, child, is_windows=True)
    assert metadata == {
        "codex_windowsapps_alias_present": False,
        "codex_windowsapps_unsafe_entries_present": False,
        "codex_windowsapps_policy": "automatic",
    }
    assert child == {"PATH": str(shell_dir)}


@pytest.mark.parametrize("command", [
    ["C:/Python312/python.exe", "-c", "pass"],
    ["C:/install/claude.exe", "exec"],
    ["C:/install/grok.exe", "codex"],
    ["C:/install/agy.exe", "codex"],
    "claude codex",
    "codex exec & claude",
])
def test_windowsapps_policy_never_targets_other_commands_or_app_server(tmp_path, command):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    child = {"PATH": str(alias_dir)}
    assert apply_codex_windowsapps_policy(command, POLICY_ENV, child, is_windows=True) == {}
    assert child == {"PATH": str(alias_dir)}


def test_windowsapps_policy_is_windows_only(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    child = {"PATH": str(alias_dir)}
    assert apply_codex_windowsapps_policy(NATIVE_CODEX_COMMAND, POLICY_ENV, child, is_windows=False) == {}
    assert child == {"PATH": str(alias_dir)}


def test_windowsapps_exclude_alias_removes_alias_entries_and_duplicates(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    shell_dir = _powershell_dir(tmp_path, "pwsh.exe")
    alias_variant = str(alias_dir) + "\\"
    original_path = ";".join([str(alias_dir), str(shell_dir), alias_variant, str(shell_dir)])
    child = {"Path": original_path, "OTHER": "kept"}
    explicit = dict(POLICY_ENV)
    metadata = apply_codex_windowsapps_policy(NATIVE_CODEX_COMMAND, explicit, child, is_windows=True)
    assert child == {"Path": ";".join([str(shell_dir), str(shell_dir)]), "OTHER": "kept"}
    assert metadata == {
        "codex_windowsapps_alias_present": True,
        "codex_windowsapps_unsafe_entries_present": True,
        "codex_windowsapps_policy": "exclude_alias",
        "codex_windowsapps_alias_entries_removed": 2,
        "codex_windowsapps_powershell_candidate": "pwsh",
    }
    assert explicit == POLICY_ENV
    assert str(alias_dir) not in json.dumps(metadata)
    assert original_path not in json.dumps(metadata)


def test_windowsapps_exclude_alias_name_matching_is_case_insensitive(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    shell_dir = _powershell_dir(tmp_path, "pwsh.exe")
    child = {"PATH": ";".join([str(alias_dir), str(shell_dir)])}
    metadata = apply_codex_windowsapps_policy(
        NODE_CODEX_COMMAND,
        {"home_mcp_codex_windowsapps_policy": "exclude_alias"},
        child,
        is_windows=True,
    )
    assert metadata["codex_windowsapps_policy"] == "exclude_alias"
    assert child == {"PATH": str(shell_dir)}


def test_windowsapps_exclude_alias_falls_back_to_windows_powershell_51(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    shell_dir = _powershell_dir(tmp_path, "powershell.exe")
    child = {"PATH": ";".join([str(alias_dir), str(shell_dir)])}
    metadata = apply_codex_windowsapps_policy(NATIVE_CODEX_COMMAND, POLICY_ENV, child, is_windows=True)
    assert metadata["codex_windowsapps_powershell_candidate"] == "powershell"
    assert child == {"PATH": str(shell_dir)}


def test_windowsapps_exclude_alias_prefers_powershell_7_over_51(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    legacy_dir = _powershell_dir(tmp_path, "powershell.exe")
    shell_dir = tmp_path / "PowerShell7"
    shell_dir.mkdir()
    (shell_dir / "pwsh.exe").write_bytes(b"real-shell")
    child = {"PATH": ";".join([str(alias_dir), str(legacy_dir), str(shell_dir)])}
    metadata = apply_codex_windowsapps_policy(NATIVE_CODEX_COMMAND, POLICY_ENV, child, is_windows=True)
    assert metadata["codex_windowsapps_powershell_candidate"] == "pwsh"


def test_windowsapps_exclude_alias_with_alias_only_path_fails_explicitly(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    child = {"PATH": str(alias_dir)}
    with pytest.raises(CodexWindowsAppsPolicyError, match="codex_windowsapps_powershell_unavailable"):
        apply_codex_windowsapps_policy(NATIVE_CODEX_COMMAND, POLICY_ENV, child, is_windows=True)


def test_windowsapps_exclude_alias_without_any_candidate_fails_explicitly(tmp_path):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    child = {"PATH": str(empty_dir)}
    with pytest.raises(CodexWindowsAppsPolicyError, match="codex_windowsapps_powershell_unavailable"):
        apply_codex_windowsapps_policy(NODE_CODEX_COMMAND, POLICY_ENV, child, is_windows=True)


def test_windowsapps_policy_inherited_value_does_not_override_automatic(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    shell_dir = _powershell_dir(tmp_path, "pwsh.exe")
    original_path = ";".join([str(alias_dir), str(shell_dir)])
    child = {"PATH": original_path, "HOME_MCP_CODEX_WINDOWSAPPS_POLICY": "exclude_alias"}
    metadata = apply_codex_windowsapps_policy(NATIVE_CODEX_COMMAND, {}, child, is_windows=True)
    assert metadata == {
        "codex_windowsapps_alias_present": True,
        "codex_windowsapps_unsafe_entries_present": True,
        "codex_windowsapps_policy": "prefer_pwsh",
        "codex_windowsapps_powershell_candidate": "pwsh",
    }
    assert child["PATH"] == ";".join([str(shell_dir), str(alias_dir)])


def test_windowsapps_unknown_explicit_value_only_diagnoses(tmp_path):
    alias_dir = _windowsapps_alias_dir(tmp_path)
    child = {"PATH": str(alias_dir)}
    metadata = apply_codex_windowsapps_policy(
        NATIVE_CODEX_COMMAND,
        {"HOME_MCP_CODEX_WINDOWSAPPS_POLICY": "keep"},
        child,
        is_windows=True,
    )
    assert metadata["codex_windowsapps_policy"] == "diagnose"
    assert child == {"PATH": str(alias_dir)}


@pytest.mark.parametrize(
    "entry, expected",
    [
        (r"C:\Users\qa\AppData\Local\Microsoft\WindowsApps", True),
        (r"C:\Users\qa\AppData\Local\MICROSOFT\WINDOWSAPPS", True),
        (r"C:/Users/qa/AppData/Local/Microsoft/WindowsApps", True),
        (r'"C:\Users\qa\AppData\Local\Microsoft\WindowsApps"', True),
        (r"C:\Users\qa\AppData\Local\Microsoft\WindowsApps\\", True),
        (r"C:/Users/qa/AppData/Local/Microsoft/WindowsApps/", True),
        (r'"C:/Users/qa/AppData/Local/Microsoft/WindowsApps/"', True),
        (r"Microsoft\WindowsApps", True),
        (r"microsoft/windowsapps", True),
        (r"D:\NotMicrosoft\WindowsApps", False),
        (r"D:\OtherMicrosoft\WindowsApps", False),
        (r"D:\Microsoft\WindowsAppsOther", False),
        (r"D:/NotMicrosoft/WindowsApps", False),
        (r"D:/OtherMicrosoft/WindowsApps", False),
        (r"D:/Microsoft/WindowsAppsOther", False),
        (r'"D:\NotMicrosoft\WindowsApps"', False),
        (r"WindowsApps", False),
    ],
)
def test_windowsapps_entry_boundary_distinction(entry, expected):
    assert _is_windowsapps_entry(entry) is expected


def test_windowsapps_exclude_alias_ignores_notmicrosoft_windowsapps(tmp_path):
    not_alias_dir = tmp_path / "NotMicrosoft" / "WindowsApps"
    not_alias_dir.mkdir(parents=True)
    (not_alias_dir / "pwsh.exe").write_bytes(b"not-alias-stub")
    original_path = str(not_alias_dir)
    child = {"PATH": original_path}
    explicit = dict(POLICY_ENV)
    metadata = apply_codex_windowsapps_policy(
        NATIVE_CODEX_COMMAND,
        explicit,
        child,
        is_windows=True,
    )
    assert child == {"PATH": original_path}
    assert metadata == {
        "codex_windowsapps_alias_present": False,
        "codex_windowsapps_unsafe_entries_present": False,
        "codex_windowsapps_policy": "exclude_alias",
        "codex_windowsapps_alias_entries_removed": 0,
        "codex_windowsapps_powershell_candidate": "pwsh",
    }


@pytest.mark.parametrize("command", [
    ["codex", "exec"], ["CODEX.CMD", "exec"],
    ["C:/profile/npm/codex.cmd", "exec"],
    'codex exec "a & b"', '"C:/profile/npm/codex.cmd" exec',
    '"C:/install/node.exe" "C:/install/node_modules/@openai/codex/bin/codex.js" app-server',
    NODE_CODEX_COMMAND[:2] + ["app-server"],
    NATIVE_CODEX_COMMAND[:1] + ["app-server"],
])
def test_automatic_policy_covers_direct_routes_and_app_server(tmp_path, command):
    alias = _windowsapps_alias_dir(tmp_path)
    root = tmp_path / "Windows"
    shell = root / "System32" / "WindowsPowerShell" / "v1.0"
    shell.mkdir(parents=True)
    (shell / "powershell.exe").write_bytes(b"fixture")
    child = {"PATH": f'{alias};"{alias}\\";{shell};{shell}', "APPDATA": "C:/profile", "SYSTEMROOT": str(root)}
    result = apply_codex_windowsapps_policy(command, {}, child, is_windows=True)
    assert result["codex_windowsapps_powershell_candidate"] == "powershell"
    assert result["codex_windowsapps_alias_entries_removed"] == 2
    assert child["PATH"] == f"{shell};{shell}"


def test_automatic_policy_fails_if_no_verified_shell(tmp_path):
    alias = _windowsapps_alias_dir(tmp_path)
    with pytest.raises(CodexWindowsAppsPolicyError):
        apply_codex_windowsapps_policy(["codex"], {}, {"PATH": str(alias)}, is_windows=True)
    child = {"PATH": ""}
    with pytest.raises(CodexWindowsAppsPolicyError):
        apply_codex_windowsapps_policy(["codex"], {}, child, is_windows=True)


def test_automatic_policy_parallel_jobs_do_not_modify_parent_or_each_other(tmp_path):
    alias = _windowsapps_alias_dir(tmp_path)
    root = tmp_path / "Windows"
    shell = root / "System32" / "WindowsPowerShell" / "v1.0"
    shell.mkdir(parents=True)
    (shell / "powershell.exe").write_bytes(b"fixture")
    parent = {"PATH": f"{alias};{shell}", "SYSTEMROOT": str(root), "OTHER": "preserved"}
    def run(index):
        child, _ = build_job_environment(parent, {"HOME_MCP_TEST_INDEX": str(index)})
        apply_codex_windowsapps_policy(["codex"], {}, child, is_windows=True)
        return child
    with ThreadPoolExecutor(max_workers=8) as pool:
        children = list(pool.map(run, range(32)))
    assert parent == {"PATH": f"{alias};{shell}", "SYSTEMROOT": str(root), "OTHER": "preserved"}
    assert all(item["PATH"] == str(shell) for item in children)
    assert {item["HOME_MCP_TEST_INDEX"] for item in children} == {str(i) for i in range(32)}


@pytest.mark.parametrize("path", [
    r'C:\Program Files\WindowsApps\Microsoft.PowerShell_7.5.0.0_x64__8wekyb3d8bbwe',
    r'"c:/PROGRAM FILES/WindowsApps/Microsoft.PowerShell_7.5.0.0_x64__8wekyb3d8bbwe/"',
    r'C:\Users\u\AppData\Local\Microsoft\WindowsApps',
])
def test_packaged_paths_with_observed_file_are_unsafe(path):
    assert _packaged_path(path)
    with patch('home_mcp_gateway.job_environment.Path') as file_path:
        assert _shell_file_status(path, "pwsh.exe", {}) == "unsafe"
        file_path.return_value.__truediv__.return_value.lstat.assert_called_once()


def test_symlink_target_and_zero_size_are_not_verified_pwsh():
    with patch("home_mcp_gateway.job_environment.Path") as path:
        directory = path.return_value.resolve.return_value
        directory.__str__.return_value = r'C:\links'
        file = directory.__truediv__.return_value
        resolved = file.resolve.return_value
        resolved.__str__.return_value = r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1\pwsh.exe'
        assert _shell_file_status(r'C:\links', 'pwsh.exe', {}) == 'unsafe'
        resolved.__str__.return_value = r'C:\unpacked\pwsh.exe'
        resolved.stat.return_value.st_size = 0
        assert _shell_file_status(r'C:\links', 'pwsh.exe', {}) == 'unsafe'
        resolved.stat.return_value.st_size = 100
        resolved.stat.return_value.st_mode = stat.S_IFREG
        assert _shell_file_status(r'C:\links', 'pwsh.exe', {}) == 'safe'
        directory.__str__.return_value = r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1'
        assert _shell_file_status(r'C:\links', 'pwsh.exe', {}) == 'unsafe'


def test_child_expansion_and_duplicate_path_keys_use_explicit_values():
    legacy = r'C:\Windows\System32\WindowsPowerShell\v1.0'
    child = {"PATH": r'C:\parent', "Path": r'%SHELLROOT%', "SHELLROOT": r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1', 'SYSTEMROOT': r'C:\Windows'}
    explicit = {"pAtH": r'%SHELLROOT%;' + legacy, "SHELLROOT": child["SHELLROOT"]}
    original = dict(child)
    # Simulate the existence of a packaged shell and a legacy candidate; the
    # packaged-file classification itself is covered separately.
    real_status = _shell_file_status
    def status(entry, name, env):
        if entry == legacy:
            return 'safe' if name == 'powershell.exe' else 'missing'
        if _packaged_path(_qualified_path_entry(entry, env) or ''):
            return 'unsafe'  # Synthetic existing package for the policy test.
        return real_status(entry, name, env)
    with patch('home_mcp_gateway.job_environment._shell_file_status', side_effect=status), \
         patch('home_mcp_gateway.job_environment._is_system32_powershell', side_effect=lambda entry, env: entry == legacy):
        meta = apply_codex_windowsapps_policy(["codex"], explicit, child, is_windows=True)
    assert meta['codex_windowsapps_powershell_candidate'] == 'powershell'
    assert [v for k, v in child.items() if k.upper() == 'PATH'] == [legacy]
    assert original['PATH'] == r'C:\parent'


@pytest.mark.parametrize('value', ['off', 'diagnose', 'keep'])
def test_explicit_off_or_diagnose_keeps_path_without_filesystem_fixtures(value):
    child = {'Path': r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1'}
    before = dict(child)
    with patch('home_mcp_gateway.job_environment.Path'):
        meta = apply_codex_windowsapps_policy(['codex'], {'HOME_MCP_CODEX_WINDOWSAPPS_POLICY': value}, child, is_windows=True)
    assert meta['codex_windowsapps_policy'] == 'diagnose'
    assert child == before


def test_recognizes_only_documented_direct_launch_shapes():
    env = {'APPDATA': r'C:\profile'}
    assert _is_codex_launch(['codex', 'exec', 'resume', 'thread'], env)
    assert _is_codex_launch([r'C:\profile\npm\codex.cmd', 'exec'], env)
    assert _is_codex_launch(NATIVE_CODEX_COMMAND, env)
    assert _is_codex_launch(NODE_CODEX_COMMAND[:2] + ['app-server'], env)
    assert not _is_codex_launch([r'C:\other\codex.exe', 'exec'], env)
    assert not _is_codex_launch([r'C:\other\codex.cmd', 'exec'], env)
    assert not _is_codex_launch('codex exec & claude', env)


def test_packaged_only_path_fails_without_safe_fallback():
    child = {'PATH': r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1'}
    before = dict(child)
    with patch('home_mcp_gateway.job_environment.Path'):
        with pytest.raises(CodexWindowsAppsPolicyError, match='powershell_unavailable'):
            apply_codex_windowsapps_policy(['codex'], {}, child, is_windows=True)
    assert child == before


@pytest.mark.parametrize('child', [{}, {'PATH': ''}])
def test_absent_or_empty_path_has_no_verified_shell(child):
    before = dict(child)
    with pytest.raises(CodexWindowsAppsPolicyError, match='powershell_unavailable'):
        apply_codex_windowsapps_policy(['codex'], {}, child, is_windows=True)
    assert child == before


def test_parallel_policy_choices_with_synthetic_file_statuses():
    packaged = r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1'
    path = f'{packaged};C:\\PowerShell7;{packaged}'
    parent = {'PATH': path}
    real_status = _shell_file_status
    def status(entry, name, env):
        if entry == r'C:\PowerShell7':
            return 'safe' if name == 'pwsh.exe' else 'missing'
        if _packaged_path(entry):
            return 'unsafe'  # Synthetic existing package for the policy test.
        return real_status(entry, name, env)
    def run(index):
        child = dict(parent, HOME_MCP_INDEX=str(index))
        metadata = apply_codex_windowsapps_policy(['codex'], {}, child, is_windows=True)
        return child, metadata
    with patch('home_mcp_gateway.job_environment._shell_file_status', side_effect=status):
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(run, range(32)))
    assert parent == {'PATH': path}
    assert all(child['PATH'] == f'C:\\PowerShell7;{packaged};{packaged}' for child, _ in results)
    assert all(meta['codex_windowsapps_policy'] == 'prefer_pwsh' for _, meta in results)


def test_non_codex_and_non_windows_do_not_modify_path_keys():
    child = {'PATH': 'one', 'Path': 'two'}
    before = dict(child)
    assert apply_codex_windowsapps_policy(['claude', 'codex'], {}, child, is_windows=True) == {}
    assert apply_codex_windowsapps_policy(['codex'], {}, child, is_windows=False) == {}
    assert child == before


def test_app_server_job_forwards_policy_failure_before_launch():
    # The transport validators are stubbed solely to isolate forwarding; the
    # policy error is deliberately propagated and no process is launched.
    from home_mcp_gateway import jobs, work_dispatcher_codex
    key, fingerprint = 'test-app-server', 'a' * 64
    binding = {'db_path': 'offline', 'manifest_id': 'm', 'manifest_digest': fingerprint,
               'root_source_id': 'root', 'root_epoch': 1, 'generation': 1,
               'job_id': 'j', 'op_key': key, 'request_digest': fingerprint,
               'gateway_job_id': jobs.stable_job_id(key), 'request_fingerprint': fingerprint}
    command = NODE_CODEX_COMMAND[:2] + ['app-server']
    with patch.object(work_dispatcher_codex, 'app_server_enabled', return_value=True), \
         patch.object(work_dispatcher_codex, 'validate_app_server_spec', return_value={}), \
         patch.object(work_dispatcher_codex, 'validate_app_server_command', return_value=command), \
         patch.object(jobs, 'apply_codex_temp_default', return_value={}), \
         patch.object(jobs, 'apply_codex_windowsapps_policy', side_effect=CodexWindowsAppsPolicyError('shell unavailable')) as policy:
        result = jobs.job_start(command, cwd='.', idempotency_key=key, request_fingerprint=fingerprint,
                                dispatcher_binding=binding, transport='codex_app_server', app_server={})
    assert result['ok'] is False and result['error'] == 'shell unavailable'
    assert policy.call_args.args[0] == command


@pytest.mark.parametrize('command', [
    'codex exec "Explain?"',
    '"C:/install/node_modules/@openai/codex-win32-x64/vendor/x86_64-pc-windows-msvc/bin/codex.exe" exec "Why?"',
    '"C:/install/node.exe" "C:/install/node_modules/@openai/codex/bin/codex.js" exec "What?"',
])
def test_v3_quoted_question_is_direct_codex(command):
    assert _is_codex_launch(command)


@pytest.mark.parametrize('command', ['codex exec & claude', 'codex exec %PROMPT%',
                                     'codex exec !PROMPT!', 'codex exec ^& echo'])
def test_v3_unsupported_shell_syntax_is_not_direct(command):
    assert not _is_codex_launch(command)


@pytest.mark.parametrize('entry', ['', '.', 'shell', r'C:shell', r'\shell', '/shell', '%MISSING%', '%TWO%'])
def test_v3_unqualified_path_is_never_inspected(entry):
    with patch('home_mcp_gateway.job_environment.Path') as path:
        assert _shell_file_status(entry, 'pwsh.exe', {'TWO': r'C:\first;C:\second'}) == 'missing'
        path.assert_not_called()


@pytest.mark.parametrize('entry,expected', [
    (r'C:\absolute', r'C:\absolute'),
    ('"C:/Program Files/PowerShell/7/"', 'C:/Program Files/PowerShell/7/'),
    (r'\\server\share\tools', r'\\server\share\tools'),
    (r'%WINDIR%\System32', r'D:\child\System32'),
])
def test_v3_qualified_path_uses_only_child_values(entry, expected):
    assert _qualified_path_entry(entry, {'WINDIR': r'D:\child'}) == expected


def test_v3_expanded_inspected_path_is_emitted_with_relative_entries_unchanged():
    original_path = r'%WINDIR%\PowerShell;relative;C:relative;\root;%UNRESOLVED%;;'
    child = {'PATH': original_path, 'WINDIR': r'D:\child'}
    with patch('home_mcp_gateway.job_environment.Path') as path:
        directory = path.return_value.resolve.return_value
        directory.__str__.return_value = r'D:\child\PowerShell'
        resolved = directory.__truediv__.return_value.resolve.return_value
        resolved.__str__.return_value = r'D:\child\PowerShell\pwsh.exe'
        resolved.stat.return_value.st_mode = stat.S_IFREG
        resolved.stat.return_value.st_size = 100
        meta = apply_codex_windowsapps_policy('codex exec "Why?"', {}, child, is_windows=True)
        assert path.call_args_list[0].args == (r'D:\child\PowerShell',)
    assert meta['codex_windowsapps_policy'] == 'automatic'
    assert child['PATH'] == r'D:\child\PowerShell;relative;C:relative;\root;%UNRESOLVED%;;'
    assert original_path == r'%WINDIR%\PowerShell;relative;C:relative;\root;%UNRESOLVED%;;'


@pytest.mark.parametrize('explicit', [{}, POLICY_ENV])
def test_v3_expanded_candidate_is_emitted_when_reordering_or_excluding(explicit):
    packaged = r'C:\Program Files\WindowsApps\Microsoft.PowerShell_1'
    child = {'PATH': packaged + r';%SAFE%;relative', 'SAFE': r'D:\shell'}
    real_status = _shell_file_status
    def status(entry, filename, env):
        if _qualified_path_entry(entry, env) == r'D:\shell':
            return 'safe' if filename == 'pwsh.exe' else 'missing'
        if _packaged_path(_qualified_path_entry(entry, env) or ''):
            return 'unsafe'  # Synthetic existing package for the policy test.
        return real_status(entry, filename, env)
    with patch('home_mcp_gateway.job_environment._shell_file_status', side_effect=status):
        meta = apply_codex_windowsapps_policy(['codex', 'exec'], explicit, child, is_windows=True)
    expected = r'D:\shell;relative' if explicit else r'D:\shell;' + packaged + ';relative'
    assert child['PATH'] == expected
    assert meta['codex_windowsapps_policy'] == ('exclude_alias' if explicit else 'prefer_pwsh')


@pytest.mark.parametrize('outcome,expected_policy', [
    ('missing', 'automatic'), ('present', 'exclude_alias'), ('denied', 'exclude_alias'),
])
def test_v4_alias_absence_preserves_other_apps_and_unknown_is_unsafe(outcome, expected_policy):
    alias = r'C:\Users\u\AppData\Local\Microsoft\WindowsApps'
    legacy = r'C:\Windows\System32\WindowsPowerShell\v1.0'
    original = f'{alias};{legacy}'
    child = {'PATH': original, 'SYSTEMROOT': r'C:\Windows'}
    real_status = _shell_file_status
    def status(entry, filename, env):
        if entry == legacy:
            return 'safe' if filename == 'powershell.exe' else 'missing'
        with patch('home_mcp_gateway.job_environment.Path') as path:
            lstat = path.return_value.__truediv__.return_value.lstat
            if outcome == 'missing':
                lstat.side_effect = FileNotFoundError()
            elif outcome == 'denied':
                lstat.side_effect = PermissionError()
            return real_status(entry, filename, env)
    with patch('home_mcp_gateway.job_environment._shell_file_status', side_effect=status), \
         patch('home_mcp_gateway.job_environment._is_system32_powershell', side_effect=lambda entry, env: entry == legacy), \
         patch('home_mcp_gateway.job_environment._alias_file_observed', return_value=outcome == 'present'):
        metadata = apply_codex_windowsapps_policy(['codex', 'exec'], {}, child, is_windows=True)
    assert metadata['codex_windowsapps_policy'] == expected_policy
    assert child['PATH'] == (original if outcome == 'missing' else legacy)


def test_v4_failed_resolution_after_existing_file_is_not_missing():
    with patch('home_mcp_gateway.job_environment.Path') as path:
        path.return_value.resolve.side_effect = FileNotFoundError()
        assert _shell_file_status(r'C:\links', 'pwsh.exe', {}) == 'unsafe'


def test_v5_explicit_lowercase_appdata_wins_for_shim_recognition():
    child = {'APPDATA': r'C:\parent', 'appdata': r'D:\child'}
    before = dict(child)
    assert _is_codex_launch([r'D:\child\npm\codex.cmd', 'exec'], child)
    assert not _is_codex_launch([r'C:\parent\npm\codex.cmd', 'exec'], child)
    assert child == before


def test_v5_explicit_lowercase_systemroot_wins_for_powershell():
    child = {'SYSTEMROOT': r'C:\parent', 'systemroot': r'D:\child'}
    before = dict(child)
    class SyntheticPath:
        def __init__(self, *parts):
            self.value = ntpath.join(*parts)
        def resolve(self, *, strict):
            assert strict
            return self
        def __str__(self):
            return self.value
    with patch('home_mcp_gateway.job_environment.Path', SyntheticPath):
        assert _is_system32_powershell(r'D:\child\System32\WindowsPowerShell\v1.0', child)
        assert not _is_system32_powershell(r'C:\parent\System32\WindowsPowerShell\v1.0', child)
    assert child == before
