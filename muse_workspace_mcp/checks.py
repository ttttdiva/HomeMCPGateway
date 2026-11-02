"""Bounded, injectable check execution for the workspace service."""
from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Any, Sequence

from .errors import WorkspaceError
from .paths import safe_relative_name


CHECK_TIMEOUT_SECONDS = 120.0
CHECK_OUTPUT_BYTES = 64 * 1024
MAX_CHECK_ARGS = 32
MAX_CHECK_ARG_CHARS = 256

# The command vectors are fixed.  User input can select one of these checks
# and provide conservative arguments, but cannot choose an executable or a
# shell string.
CHECK_COMMANDS: dict[str, tuple[str, ...]] = {
    "pytest": ("python", "-m", "pytest"),
    "ruff": ("ruff", "check"),
    "tsc": ("tsc",),
}
ALLOWED_CHECKS = frozenset(CHECK_COMMANDS)

_SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:=+,~\\-]+$")
_PYTEST_OPTION = re.compile(r"^(?:--maxfail|--timeout)=[0-9]{1,3}$")


def _scoped_path(value: str, prefix: str | None = None) -> str:
    normalized = value.replace("\\", "/")
    if normalized.startswith(("/", "\\")) or (len(normalized) >= 2 and normalized[1] == ":"):
        raise WorkspaceError("check_not_allowed")
    parts = normalized.split("/")
    if any(part in {"..", ""} for part in parts):
        raise WorkspaceError("check_not_allowed")
    if prefix is not None and not (normalized == prefix or normalized.startswith(prefix + "/")):
        raise WorkspaceError("check_not_allowed")
    try:
        safe_relative_name(normalized)
    except WorkspaceError:
        raise WorkspaceError("check_not_allowed") from None
    return normalized


@dataclass(frozen=True)
class CheckResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "timed_out": self.timed_out,
            "truncated": self.truncated,
        }


class Runner:
    """Unconfigured runner seam.

    Production must inject a launcher owned by the host policy.  The default
    implementation intentionally never starts a process.
    """

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        timeout: float = CHECK_TIMEOUT_SECONDS,
        max_output_bytes: int = CHECK_OUTPUT_BYTES,
    ) -> CheckResult:
        raise WorkspaceError("check_runner_unavailable")


def validate_check(name: object, args: object = None) -> tuple[str, list[str]]:
    """Validate a check name and its bounded argument list."""
    if not isinstance(name, str) or name.casefold() not in ALLOWED_CHECKS:
        raise WorkspaceError("check_not_allowed")
    selected = name.casefold()
    if args is None:
        values: list[object] = []
    elif isinstance(args, (list, tuple)):
        values = list(args)
    else:
        raise WorkspaceError("invalid_input")
    if len(values) > MAX_CHECK_ARGS:
        raise WorkspaceError("limit_exceeded")
    result: list[str] = []
    pytest_has_path = False
    for value in values:
        if not isinstance(value, str) or not value or len(value) > MAX_CHECK_ARG_CHARS or "\x00" in value:
            raise WorkspaceError("invalid_input")
        if value.startswith(("@", ";", "|", "&", ">", "<", "-c")) or not _SAFE_TOKEN.fullmatch(value):
            raise WorkspaceError("check_not_allowed")
        if selected == "pytest":
            if value not in {"-q", "-x"} and not _PYTEST_OPTION.fullmatch(value):
                _scoped_path(value, "tests")
                pytest_has_path = True
        elif selected == "ruff":
            # The command vector already supplies the only permitted subcommand.
            if value.startswith("-") or value in {"check", "format", "config"}:
                raise WorkspaceError("check_not_allowed")
            _scoped_path(value)
        else:  # tsc
            if value != "--noEmit":
                _scoped_path(value, "frontend")
        result.append(value)
    if selected == "pytest" and not pytest_has_path:
        raise WorkspaceError("check_not_allowed")
    if selected == "ruff" and not result:
        raise WorkspaceError("check_not_allowed")
    if selected == "tsc" and ("--noEmit" not in result or not any(
        item == "frontend" or item.replace("\\", "/").startswith("frontend/") for item in result
    )):
        raise WorkspaceError("check_not_allowed")
    return selected, result


def command_for(name: str, args: Sequence[str]) -> list[str]:
    selected, checked = validate_check(name, args)
    return [*CHECK_COMMANDS[selected], *checked]


__all__ = [
    "ALLOWED_CHECKS",
    "CHECK_COMMANDS",
    "CHECK_OUTPUT_BYTES",
    "CHECK_TIMEOUT_SECONDS",
    "CheckResult",
    "Runner",
    "command_for",
    "validate_check",
]
