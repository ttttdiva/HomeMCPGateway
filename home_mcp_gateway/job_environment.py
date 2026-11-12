"""Build owner-managed Gateway job environments and Codex launch settings."""

from __future__ import annotations

from collections.abc import Mapping
import os
import ntpath
import re
import stat
from pathlib import Path
from typing import Any


_GATEWAY_REPO_ROOT = Path(__file__).resolve().parents[1]


def build_job_environment(
    parent_environment: Mapping[str, Any],
    explicit_environment: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inherit the full environment for owner-managed Gateway jobs.

    Explicit overrides replace inherited variable names case-insensitively.
    Environment values are never written to job metadata.
    """
    child: dict[str, Any] = dict(parent_environment)
    for raw_name, value in (explicit_environment or {}).items():
        name = str(raw_name)
        for existing in list(child):
            if existing != name and str(existing).casefold() == name.casefold():
                del child[existing]
        child[name] = value
    return child, {
        "env_policy": "inherit-all",
        "env_inherited_count": len(parent_environment),
        "env_dropped_count": 0,
        "env_dropped_secret_like_names": [],
    }


def _direct_official_codex_kind(command: str | list[str]) -> str | None:
    """Classify recognizable direct official Codex argv without widening it.

    Returns ``"native"`` for the packaged ``codex.exe`` under
    ``node_modules/@openai/codex-*/vendor/.../bin`` and ``"node"`` for
    ``node`` running ``node_modules/@openai/codex/bin/codex.js``. Anything
    else — Python wrappers, other CLIs, bare names, string commands — is
    not a direct official Codex launch.
    """
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return None
    parts = command[0].replace("\\", "/").lower().split("/")
    native = (len(parts) >= 7 and parts[-1] in {"codex.exe", "codex"}
              and parts[-2] == "bin" and parts[-4] == "vendor"
              and parts[-5].startswith("codex-") and parts[-6] == "@openai"
              and parts[-7] == "node_modules")
    if native:
        return "native"
    entry = command[1].replace("\\", "/").lower().split("/") if len(command) > 1 else []
    node = (parts[-1] in {"node.exe", "node"} and
            entry[-5:] == ["node_modules", "@openai", "codex", "bin", "codex.js"])
    return "node" if node else None


def is_direct_official_codex_command(command: str | list[str]) -> bool:
    """Return whether argv is a recognizable direct official Codex launch."""
    return _direct_official_codex_kind(command) is not None


def apply_codex_temp_default(command: str | list[str], cwd: str, job_id: str,
                             explicit: Mapping[str, Any] | None,
                             child: dict[str, Any]) -> dict[str, Any]:
    """Set per-job defaults only for recognizable direct official Codex argv."""
    if not is_direct_official_codex_command(command):
        return {}
    names = {str(k).upper() for k in (explicit or {})}
    missing = {"TEMP", "TMP", "TMPDIR"} - names
    if not missing:
        return {}
    working = Path(cwd)
    root = next((p for p in (working, *working.parents) if (p / ".git").exists()), None)
    # Non-repository work is legitimate. Keep its cwd unchanged and own its
    # temporary output in the Gateway repository instead of shared/home Temp.
    root = root if root is not None and working.is_dir() else _GATEWAY_REPO_ROOT
    destination = root / ".runtime" / "codex-job-temp" / job_id
    try:
        destination.mkdir(parents=True, exist_ok=True)
    except OSError:
        if root == _GATEWAY_REPO_ROOT:
            raise
        destination = _GATEWAY_REPO_ROOT / ".runtime" / "codex-job-temp" / job_id
        destination.mkdir(parents=True, exist_ok=True)
    for key in list(child):
        if str(key).upper() in missing:
            del child[key]
    child.update({name: str(destination) for name in missing})
    return {"codex_temp_default": str(destination)}


CODEX_WINDOWSAPPS_POLICY_ENV = "HOME_MCP_CODEX_WINDOWSAPPS_POLICY"
CODEX_WINDOWSAPPS_POLICY_EXCLUDE_ALIAS = "exclude_alias"


class CodexWindowsAppsPolicyError(RuntimeError):
    """An explicit Codex WindowsApps policy cannot be honoured safely."""


def _windowsapps_entry_key(entry: str) -> str:
    return ntpath.normpath(entry.strip().strip('"').replace("/", "\\")).casefold()


def _is_windowsapps_entry(entry: str) -> bool:
    parts = _windowsapps_entry_key(entry).split("\\")
    return len(parts) >= 2 and parts[-2:] == ["microsoft", "windowsapps"]


def _expand_child_vars(value: str, child: Mapping[str, Any]) -> str:
    values = {str(k).casefold(): str(v) for k, v in child.items()}
    return re.sub(r'%([^%]+)%', lambda m: values.get(m.group(1).casefold(), m.group()), value)


def _qualified_path_entry(entry: str, child: Mapping[str, Any]) -> str | None:
    """Return the exact expanded directory to inspect and emit, if absolute."""
    expanded = _expand_child_vars(entry.strip().strip('"'), child)
    if not expanded or '%' in expanded or ';' in expanded:
        return None
    drive, tail = ntpath.splitdrive(expanded)
    if not drive or not ntpath.isabs(expanded) or (not drive.startswith('\\\\') and not tail.startswith(('\\', '/'))):
        return None
    return expanded


def _packaged_path(path: str) -> bool:
    parts = _windowsapps_entry_key(path).split("\\")
    return any(part == "windowsapps" and (
        (i > 0 and parts[i - 1] in {"microsoft", "program files", "program files (x86)"})
        or (i + 1 < len(parts) and parts[i + 1].startswith("microsoft.powershell_"))
    ) for i, part in enumerate(parts))


def _shell_file_status(entry: str, filename: str, child: Mapping[str, Any]) -> str:
    """Return safe, unsafe or missing; unresolved existing files fail closed."""
    cleaned = _qualified_path_entry(entry, child)
    if cleaned is None:
        return "missing"
    try:
        # lstat observes an alias or reparse point without following it. Only
        # an affirmative not-found result means the PATH entry lacks this file.
        (Path(cleaned) / filename).lstat()
    except FileNotFoundError:
        return "missing"
    except (OSError, RuntimeError):
        return "unsafe"
    try:
        if _packaged_path(cleaned):
            return "unsafe"
        directory = Path(cleaned).resolve(strict=True)
        if _packaged_path(str(directory)):
            return "unsafe"
        file = directory / filename
        resolved = file.resolve(strict=True)
        info = resolved.stat()
        if _packaged_path(str(resolved)) or info.st_size == 0:
            return "unsafe"
        return "safe" if stat.S_ISREG(info.st_mode) else "missing"
    except (OSError, RuntimeError):
        return "unsafe"


def _is_system32_powershell(entry: str, child: Mapping[str, Any]) -> bool:
    root = {str(k).casefold(): str(v) for k, v in child.items()}.get("systemroot", "")
    root = _qualified_path_entry(root, child)
    cleaned = _qualified_path_entry(entry, child)
    if root is None or cleaned is None:
        return False
    try:
        actual = Path(cleaned).resolve(strict=True)
        expected = Path(root, "System32", "WindowsPowerShell", "v1.0").resolve(strict=True)
        return _windowsapps_entry_key(str(actual)) == _windowsapps_entry_key(str(expected))
    except (OSError, RuntimeError):
        return False


def _alias_file_observed(entry: str, child: Mapping[str, Any]) -> bool:
    cleaned = _qualified_path_entry(entry, child)
    if cleaned is None or not _is_windowsapps_entry(cleaned):
        return False
    try:
        return (Path(cleaned) / "pwsh.exe").is_file()
    except OSError:
        return False


def _is_codex_launch(command: str | list[str], child: Mapping[str, Any] | None = None) -> bool:
    """Recognize direct commands, not compound shell text or arbitrary wrappers."""
    if isinstance(command, str):
        if re.search(r'[\r\n%!^]', command):
            return False
        quoted = False
        for character in command:
            if character == '"':
                quoted = not quoted
            elif not quoted and character in '&|<>()':
                return False
        if quoted:
            return False
        tokens = re.findall(r'"[^"]*"|[^\s"]+', command.strip())
        if not tokens or ''.join(tokens).count('"') != command.count('"'):
            return False
        command = [token[1:-1] if token.startswith('"') else token for token in tokens]
    if not isinstance(command, list) or not command or not all(isinstance(x, str) for x in command):
        return False
    if is_direct_official_codex_command(command):
        return True
    executable = command[0].replace('\\', '/')
    name = executable.rsplit('/', 1)[-1].casefold()
    if '/' not in executable:
        return name in {'codex', 'codex.exe', 'codex.cmd', 'codex.bat'}
    # npm's Windows shim has a known location; an arbitrary codex.exe or
    # wrapper elsewhere is not evidence that the process is official Codex.
    appdata = {str(k).casefold(): str(v) for k, v in (child or {}).items()}.get('appdata', '')
    return (bool(appdata) and name in {'codex.cmd', 'codex'}
            and _windowsapps_entry_key(ntpath.dirname(executable))
            == _windowsapps_entry_key(ntpath.join(appdata, 'npm')))


def enforce_codex_full_access(command: str | list[str],
                              child: Mapping[str, Any] | None = None,
                              *, is_windows: bool | None = None
                              ) -> tuple[str | list[str], dict[str, Any]]:
    """Prevent Gateway Codex exec jobs from re-enabling the Windows sandbox.

    Explicit caller flags override the user Codex configuration. Normalize
    official direct Codex exec argv to use no sandbox and no approval prompts.
    This affects no non-Codex commands or app-server startup commands.
    """
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows or not isinstance(command, list) or not command:
        return command, {}
    if not _is_codex_launch(command, child):
        return command, {}

    kind = _direct_official_codex_kind(command)
    exec_at = 2 if kind == "node" else 1
    if len(command) <= exec_at or command[exec_at] != "exec":
        return command, {}

    retained: list[str] = []
    removed = 0
    tail = command[exec_at + 1:]
    i = 0
    while i < len(tail):
        item = tail[i]
        if item in {"--approve-for-me", "--dangerously-bypass-approvals-and-sandbox"}:
            removed += 1
            i += 1
            continue
        if item in {"--sandbox", "-s"} and i + 1 < len(tail):
            removed += 1
            i += 2
            continue
        if item.startswith("--sandbox="):
            removed += 1
            i += 1
            continue
        if item in {"-c", "--config"} and i + 1 < len(tail):
            key = tail[i + 1].split("=", 1)[0].strip().lower()
            if key in {"sandbox_mode", "approval_policy", "shell_environment_policy.inherit"}:
                removed += 1
                i += 2
                continue
        if item.startswith(("-c=", "--config=")):
            key = item.split("=", 1)[1].split("=", 1)[0].strip().lower()
            if key in {"sandbox_mode", "approval_policy", "shell_environment_policy.inherit"}:
                removed += 1
                i += 1
                continue
        retained.append(item)
        i += 1

    effective = [*command[:exec_at + 1],
                 "--dangerously-bypass-approvals-and-sandbox",
                 "-c", 'shell_environment_policy.inherit="all"',
                 *retained]
    return effective, {
        "codex_full_access": True,
        "codex_sandbox_effective": "danger-full-access",
        "codex_approval_effective": "never",
        "codex_restrictive_args_removed": removed,
    }


def apply_codex_windowsapps_policy(command: str | list[str],
                                   explicit: Mapping[str, Any] | None,
                                   child: dict[str, Any], *,
                                   is_windows: bool | None = None) -> dict[str, Any]:
    """Select a usable Codex shell in the child environment only.

    Explicit exclude_alias retains its legacy behavior; other explicit values
    retain diagnose-only behavior. Inherited values do not override the default.
    """
    if not _is_codex_launch(command, child):
        return {}
    if is_windows is None:
        is_windows = os.name == "nt"
    if not is_windows:
        return {}
    path_keys = [key for key in child if str(key).upper() == "PATH"]
    path_key = path_keys[-1] if path_keys else None
    explicit_paths = [(k, v) for k, v in (explicit or {}).items() if str(k).upper() == "PATH"]
    raw_path = explicit_paths[-1][1] if explicit_paths else child.get(path_key) if path_key is not None else None
    entries = raw_path.split(";") if isinstance(raw_path, str) and raw_path else []
    statuses = [_shell_file_status(entry, "pwsh.exe", child) for entry in entries]
    unsafe_indexes = [i for i, status in enumerate(statuses) if status == "unsafe"]
    alias_present = any(_alias_file_observed(entry, child) for entry in entries)
    metadata: dict[str, Any] = {"codex_windowsapps_alias_present": alias_present,
                                "codex_windowsapps_unsafe_entries_present": bool(unsafe_indexes),
                                "codex_windowsapps_policy": "diagnose"}
    explicit_policy = next(
        (value for name, value in (explicit or {}).items()
         if str(name).upper() == CODEX_WINDOWSAPPS_POLICY_ENV),
        None,
    )
    if explicit_policy is not None and not (isinstance(explicit_policy, str)
            and explicit_policy.strip() == CODEX_WINDOWSAPPS_POLICY_EXCLUDE_ALIAS):
        return metadata
    # Emit exactly the fully qualified directories inspected above. Relative
    # and unresolved entries remain untouched and are never shell candidates.
    entries = [_qualified_path_entry(entry, child) or entry for entry in entries]
    force_exclude = explicit_policy is not None
    if not unsafe_indexes and not force_exclude:
        if not any(status == "safe" for status in statuses) and not any(
            _is_system32_powershell(entry, child)
            and _shell_file_status(entry, "powershell.exe", child) == "safe" for entry in entries
        ):
            raise CodexWindowsAppsPolicyError("codex_windowsapps_powershell_unavailable: no verified PowerShell on the job PATH")
        if path_key is not None:
            for key in path_keys:
                del child[key]
            child[path_key] = ";".join(entries)
        metadata["codex_windowsapps_policy"] = "automatic"
        return metadata
    # A real pwsh ahead of the alias preserves all WindowsApps CLI aliases.
    if not force_exclude:
        safe_index = next((i for i, status in enumerate(statuses) if status == "safe"), None)
        if safe_index is not None:
            if safe_index > unsafe_indexes[0]:
                entries.insert(0, entries.pop(safe_index))
            for key in path_keys:
                del child[key]
            child[path_key] = ";".join(entries)
            metadata.update(codex_windowsapps_policy="prefer_pwsh",
                            codex_windowsapps_powershell_candidate="pwsh")
            return metadata
    kept = [entry for i, entry in enumerate(entries) if i not in unsafe_indexes]
    candidate = None
    for filename, label in (("pwsh.exe", "pwsh"), ("powershell.exe", "powershell")):
        if any((filename != "powershell.exe" or force_exclude or _is_system32_powershell(entry, child))
               and _shell_file_status(entry, filename, child) == "safe"
               for entry in kept):
            candidate = label
            break
    if candidate is None:
        raise CodexWindowsAppsPolicyError(
            "codex_windowsapps_powershell_unavailable: "
            "Codex requires a real "
            "pwsh.exe (PowerShell 7) or powershell.exe (Windows PowerShell 5.1) "
            "outside Microsoft\\WindowsApps on the job PATH")
    if path_key is not None:
        for key in path_keys:
            del child[key]
        child[path_key] = ";".join(kept)
    metadata.update(
        codex_windowsapps_policy=CODEX_WINDOWSAPPS_POLICY_EXCLUDE_ALIAS,
        codex_windowsapps_alias_entries_removed=len(entries) - len(kept),
        codex_windowsapps_powershell_candidate=candidate,
    )
    return metadata
