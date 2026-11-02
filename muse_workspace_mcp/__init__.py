"""Muse workspace MCP policy package."""

from .errors import WorkspaceError
from .service import WorkspaceService

__all__ = ["WorkspaceError", "WorkspaceService"]
