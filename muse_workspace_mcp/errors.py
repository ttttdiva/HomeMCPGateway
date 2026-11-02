"""Public errors for the Muse workspace service.

The service is used as a tool boundary.  Error values therefore deliberately
contain a small, fixed vocabulary and never include an operating-system error
message, a path, or command output.
"""
from __future__ import annotations

from typing import Final


# Keep this set deliberately closed.  Callers can safely map these values to
# user-facing messages without parsing exception text.
ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        "invalid_input",
        "invalid_registry",
        "invalid_workspace",
        "workspace_not_found",
        "workspace_expired",
        "workspace_changed",
        "workspace_read_only",
        "invalid_path",
        "path_denied",
        "path_not_found",
        "not_found",
        "is_directory",
        "binary_file",
        "unsupported_encoding",
        "limit_exceeded",
        "write_failed",
        "already_exists",
        "invalid_patch",
        "patch_check_failed",
        "patch_failed",
        "git_error",
        "git_output_limit",
        "checks_disabled",
        "check_not_allowed",
        "check_runner_unavailable",
        "check_busy",
        "check_failed",
        "audit_failed",
        "unsupported_platform",
        "internal_error",
    }
)


class WorkspaceError(RuntimeError):
    """A fixed-code error raised at the workspace boundary.

    ``str(error)`` is always just the code.  This is intentional: paths,
    process stderr, and file content must not escape through error handling.
    """

    def __init__(self, code: str) -> None:
        if code not in ERROR_CODES:
            code = "internal_error"
        self.code = code
        self.error_code = code
        super().__init__(code)


def as_workspace_error(exc: BaseException, default: str = "internal_error") -> WorkspaceError:
    """Convert an arbitrary exception to a fixed public error.

    Existing :class:`WorkspaceError` instances are returned unchanged.  The
    helper is useful at filesystem and subprocess boundaries where Python and
    OS exceptions must never be reflected to a caller.
    """

    if isinstance(exc, WorkspaceError):
        return exc
    return WorkspaceError(default if default in ERROR_CODES else "internal_error")


__all__ = ["ERROR_CODES", "WorkspaceError", "as_workspace_error"]
