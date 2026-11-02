"""Find POSIX /dev/null use in Windows shell contexts.

POSIX shell scripts intentionally use /dev/null and are not examined here.
The scanner targets PowerShell/cmd source and Python/Node command builders.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path


WINDOWS_SHELL_EXTENSIONS = {".ps1", ".psm1", ".psd1", ".bat", ".cmd"}
COMMAND_SOURCE_EXTENSIONS = {".py", ".pyw", ".js", ".mjs", ".cjs", ".ts", ".tsx"}
YAML_EXTENSIONS = {".yml", ".yaml"}
SCANNED_EXTENSIONS = WINDOWS_SHELL_EXTENSIONS | COMMAND_SOURCE_EXTENSIONS | YAML_EXTENSIONS
EXCLUDED_DIRECTORIES = {
    ".git",
    ".local",
    ".runtime",
    ".venv",
    ".worktrees",
    "__pycache__",
    "node_modules",
    "repo.local",
}
EXCLUDED_REPOSITORY = "92_Anime_Frame_Picker_full_quality_staging_20261021"
EXCLUDED_FILES = {
    "scripts/check_windows_null_redirects.py",
    "tests/test_windows_null_redirect_lint.py",
}

DEV_NULL = re.compile(r"/dev/null", re.IGNORECASE)
WINDOWS_SHELL = re.compile(r"\b(?:powershell|pwsh|cmd(?:\.exe)?|comspec)\b", re.IGNORECASE)
POSIX_SHELL = re.compile(
    r"(?:\b(?:bash|sh|wsl(?:\.exe)?|ssh)\b|/bin/(?:ba)?sh|git\s+bash)",
    re.IGNORECASE,
)
PYTHON_SHELL_CALL = re.compile(
    r"\bsubprocess\.(?:run|popen|call|check_call|check_output)\s*\(|\bos\.system\s*\(",
    re.IGNORECASE,
)
SHELL_TRUE = re.compile(r"\bshell\s*=\s*(?:true|1)\b", re.IGNORECASE)
NODE_CHILD_PROCESS = re.compile(
    r"child_process\s*\.\s*exec(?:sync)?\s*\(|"
    r"require\s*\(\s*['\"](?:node:)?child_process['\"]\s*\)",
    re.IGNORECASE,
)
GENERIC_EXEC = re.compile(r"\bexec(?:sync)?\s*\(", re.IGNORECASE)
DIRECT_PATH_WRITE = re.compile(
    r"\b(?:open|os\.open)\s*\([^\n]{0,120}/dev/null[^\n]{0,100}"
    r"(?:['\"](?:w|a|x|r\+|wb|ab|xb|w\+|a\+|r\+b)['\"]|"
    r"O_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND))|"
    r"\b(?:stdout|stderr)\s*=\s*(?:open|os\.open)\s*\([^\n]{0,120}/dev/null",
    re.IGNORECASE,
)
POSIX_RUNTIME_GUARD = re.compile(
    r"(?:os\.name\s*==\s*['\"]posix|sys\.platform\s*!=\s*['\"]win32|"
    r"platform\.system\s*\(\s*\)\s*!=\s*['\"]windows)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str


def _comment_only(line: str, suffix: str) -> bool:
    stripped = line.lstrip()
    if suffix in {".ps1", ".psm1", ".psd1", ".py", ".pyw"}:
        return stripped.startswith("#")
    if suffix in {".bat", ".cmd"}:
        return stripped.startswith("::") or stripped.lower().startswith("rem ")
    if suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx"}:
        return stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*")
    return False


def _window(lines: list[str], index: int, radius: int = 12) -> str:
    start = max(0, index - radius)
    end = min(len(lines), index + radius + 1)
    return "\n".join(lines[start:end])


def _yaml_shell_mode(lines: list[str], index: int) -> str | None:
    """Return the nearest step shell setting preceding this YAML line."""
    start = max(0, index - 40)
    mode: str | None = None
    for line in lines[start : index + 1]:
        if re.match(r"^\s*-\s*(?:name|uses)\s*:", line, re.IGNORECASE):
            mode = None
        match = re.match(r"^\s*shell\s*:\s*([^\s#]+)", line, re.IGNORECASE)
        if match:
            mode = match.group(1).strip("'\"").lower()
    return mode


def analyze_text(path: str | Path, text: str) -> list[Finding]:
    """Return possible Windows-context /dev/null uses in one source file."""
    source_path = Path(path)
    suffix = source_path.suffix.lower()
    if suffix not in SCANNED_EXTENSIONS:
        return []

    lines = text.splitlines()
    findings: list[Finding] = []
    whole_text = text
    for index, line in enumerate(lines):
        if not DEV_NULL.search(line) or _comment_only(line, suffix):
            continue

        reason: str | None = None
        if suffix in WINDOWS_SHELL_EXTENSIONS:
            reason = "literal appears in PowerShell/cmd source"
        elif suffix in YAML_EXTENSIONS:
            mode = _yaml_shell_mode(lines, index)
            prior = "\n".join(lines[max(0, index - 40) : index + 1])
            if mode in {"pwsh", "powershell", "cmd", "cmd.exe"}:
                reason = f"literal appears in a {mode} step"
            elif mode is None and re.search(r"(?im)^\s*runs-on\s*:\s*.*windows", prior):
                reason = "literal appears in a Windows workflow step with its default shell"
        else:
            context = _window(lines, index)
            explicit_posix = bool(POSIX_SHELL.search(context))
            explicit_windows = bool(WINDOWS_SHELL.search(context))
            if DIRECT_PATH_WRITE.search(context) and not POSIX_RUNTIME_GUARD.search(context):
                reason = "literal is used as a filesystem sink on an unguarded Windows-capable path"
            elif PYTHON_SHELL_CALL.search(context) and (SHELL_TRUE.search(context) or explicit_windows):
                if explicit_windows or not explicit_posix:
                    reason = "literal is in a Python shell command that may use cmd/PowerShell"
            elif NODE_CHILD_PROCESS.search(whole_text) and GENERIC_EXEC.search(context):
                if explicit_windows or not explicit_posix:
                    reason = "literal is in a Node child_process.exec command that may use cmd/PowerShell"

        if reason:
            findings.append(Finding(path=str(source_path), line=index + 1, reason=reason))
    return findings


def _repository_roots(root: Path) -> list[Path]:
    root = root.resolve()
    if (root / ".git").exists():
        return [root]
    if not root.is_dir():
        raise NotADirectoryError(root)
    return sorted(
        child
        for child in root.iterdir()
        if child.is_dir()
        and child.name != EXCLUDED_REPOSITORY
        and (child / ".git").exists()
    )


def scan_root(root: str | Path) -> tuple[int, list[Finding]]:
    """Scan one repository or all direct-child repositories below root."""
    root_path = Path(root).resolve()
    repositories = _repository_roots(root_path)
    findings: list[Finding] = []
    scanned = 0

    for repository in repositories:
        for directory, dirnames, filenames in os.walk(repository):
            dirnames[:] = [name for name in dirnames if name not in EXCLUDED_DIRECTORIES]
            for filename in filenames:
                path = Path(directory) / filename
                relative = path.relative_to(repository).as_posix()
                if path.suffix.lower() not in SCANNED_EXTENSIONS or relative in EXCLUDED_FILES:
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                scanned += 1
                display_path = f"{repository.name}/{relative}" if len(repositories) > 1 else relative
                findings.extend(analyze_text(display_path, text))
    return scanned, findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="repository root or D:\\Dev-style root containing repositories",
    )
    args = parser.parse_args(argv)

    try:
        scanned, findings = scan_root(args.root)
    except (NotADirectoryError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if findings:
        for finding in findings:
            print(f"{finding.path}:{finding.line}: {finding.reason}")
        print(f"FAIL: {len(findings)} Windows-context /dev/null use(s) in {scanned} source file(s)")
        return 1

    print(f"PASS: no Windows-context /dev/null use in {scanned} source file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
