"""Filesystem boundary helpers for the Muse workspace service.

The functions in this module intentionally do more checking than
``Path.resolve``.  A path is accepted only when every existing component is a
regular directory or regular file with exact directory-entry spelling.  This
keeps Windows case aliases, short (8.3) names, junctions, symlinks, device
names, and alternate data streams out of the service boundary.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat
from typing import Iterable

from .errors import WorkspaceError


MAX_PATH_CHARS = 300
MAX_READ_BYTES = 4_000_000
MAX_READ_CHARS = 16 * 1024
MAX_READ_LINES = 200
MAX_WRITE_BYTES = 256 * 1024
MAX_LIST_ENTRIES = 300
MAX_GREP_FILES = 4_000
MAX_GREP_MATCHES = 60
MAX_GREP_BYTES = 4 * 1024 * 1024
MAX_GREP_FILE_BYTES = 2_000_000
ALLOW_EXT = frozenset(
    {
        "",
        ".cfg",
        ".css",
        ".html",
        ".ini",
        ".js",
        ".json",
        ".mjs",
        ".md",
        ".patch",
        ".diff",
        ".py",
        ".sample",
        ".sql",
        ".ts",
        ".tsx",
        ".toml",
        ".txt",
        ".yaml",
        ".yml",
        ".example",
    }
)


# Names which are commonly credential stores or source-control internals.
# Matching is case-insensitive because the service is primarily used on
# Windows.  We intentionally deny the directories themselves as well as all
# descendants.
DENY_NAMES = frozenset(
    {
        ".git",
        ".gitmodules",
        ".git-credentials",
        ".ssh",
        ".aws",
        ".azure",
        ".kube",
        ".gnupg",
        ".config",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "_netrc",
        "credentials",
        "credential",
        "credentials.json",
        "secrets",
        "secret",
        "secrets.json",
        "tokens.json",
        "token.json",
        "token",
        "key.json",
        "id_rsa",
        "id_dsa",
        "id_ecdsa",
        "id_ed25519",
    }
)
DENY_SUFFIXES = (
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".keystore",
    ".jks",
    ".kdbx",
    ".secret",
    ".secrets",
)
DENY_PATTERN = re.compile(
    r"(?i)^(?:secrets?\.(?:json|ya?ml|toml|ini|env)|id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?)$"
)

_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")
_SHORT_NAME = re.compile(r"(?i)~[0-9]+")
_RESERVED_DEVICE = re.compile(
    r"(?i)^(?:con|prn|aux|nul|clock\$|com[0-9]+|lpt[0-9]+)(?:\..*)?$"
)
_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x0400)


def _error(code: str) -> WorkspaceError:
    return WorkspaceError(code)


def _raw_string(value: object) -> str:
    if isinstance(value, os.PathLike):
        value = os.fspath(value)
    if not isinstance(value, str) or not value or len(value) > MAX_PATH_CHARS or "\x00" in value:
        raise _error("invalid_path")
    return value


def _looks_like_device_or_unc(value: str) -> bool:
    slash = chr(92)
    folded = value.casefold().replace("/", slash)
    return (
        folded.startswith(slash * 2 + "?" + slash)
        or folded.startswith(slash * 2 + "." + slash)
        or folded.startswith(slash * 2 + "??" + slash)
        or "globalroot" in folded
        or folded.startswith(slash * 2)
    )


def _components(raw: str) -> list[str]:
    """Return lexical components while handling both Windows separators."""
    normalized = raw.replace("\\", "/")
    drive = _DRIVE_PREFIX.match(normalized)
    if drive:
        normalized = normalized[2:]
    result: list[str] = []
    for component in normalized.split("/"):
        if component in ("", "."):
            continue
        result.append(component)
    return result


def _check_component_name(name: str) -> None:
    if not name or name in {".", ".."}:
        raise _error("path_denied")
    if ":" in name:
        # A drive colon is handled before components are inspected.  Any colon
        # left here is an NTFS alternate data stream separator.
        raise _error("path_denied")
    if name.endswith((".", " ")):
        raise _error("path_denied")
    if _SHORT_NAME.search(name):
        raise _error("path_denied")
    if _RESERVED_DEVICE.fullmatch(name):
        raise _error("path_denied")


def validate_lexical_path(value: object, *, relative: bool = False) -> str:
    """Validate path syntax without touching the filesystem.

    The returned string is the original value; this helper is used for Git
    path names where a deleted file may no longer exist.
    """
    raw = _raw_string(value)
    if _looks_like_device_or_unc(raw):
        raise _error("path_denied")
    drive = _DRIVE_PREFIX.match(raw)
    if drive and not re.match(r"^[A-Za-z]:[\\/]", raw):
        # ``C:foo`` is a drive-relative path and has surprising semantics.
        raise _error("path_denied")
    if relative and (raw.startswith(("/", "\\")) or drive):
        raise _error("path_denied")
    for component in _components(raw):
        if component == "..":
            raise _error("path_denied")
        _check_component_name(component)
    return raw


def is_denied_name(name: str) -> bool:
    """Return whether one path component is protected by the denylist."""
    folded = name.casefold()
    return (
        folded in DENY_NAMES
        or folded.startswith(".env")
        or folded in {"private", ".claude", ".codex"}
        or folded.endswith(DENY_SUFFIXES)
        or folded.endswith((".sqlite3", ".sqlite", ".db"))
        or any(token in folded for token in ("secret", "cookie", "credential", "token"))
        or folded.startswith("id_rsa")
        or bool(DENY_PATTERN.fullmatch(name))
    )


def is_allowed_extension(path: Path) -> bool:
    return path.suffix.casefold() in ALLOW_EXT


def denied_components(parts: Iterable[str]) -> bool:
    return any(is_denied_name(part) for part in parts)


def _file_attributes(info: os.stat_result) -> int:
    return int(getattr(info, "st_file_attributes", 0) or 0)


def _reject_link_or_reparse(info: os.stat_result) -> None:
    if stat.S_ISLNK(info.st_mode) or _file_attributes(info) & _REPARSE_POINT:
        raise _error("path_denied")


def _absolute_path(raw: str) -> Path:
    """Make an absolute path without resolving symlinks."""
    try:
        path = Path(raw)
        if not path.is_absolute():
            path = Path(os.path.abspath(os.fspath(path)))
        return Path(os.path.normpath(os.fspath(path)))
    except (OSError, ValueError, TypeError):
        raise _error("invalid_path") from None


def _same_path(left: Path, right: Path) -> bool:
    # ``normcase`` is a no-op on POSIX and gives the required case-folded
    # comparison on Windows.  Comparing normalized absolute paths also avoids
    # treating a trailing separator as a different root.
    try:
        return os.path.normcase(os.path.normpath(os.fspath(left))) == os.path.normcase(
            os.path.normpath(os.fspath(right))
        )
    except (OSError, TypeError):
        return False


def _within(root: Path, candidate: Path) -> bool:
    try:
        common = os.path.commonpath((os.fspath(root), os.fspath(candidate)))
    except (OSError, ValueError):
        return False
    return _same_path(Path(common), root)


def _find_exact_child(parent: Path, requested: str) -> Path | None:
    """Find a child with exact spelling, rejecting Windows case aliases."""
    try:
        with os.scandir(parent) as entries:
            for entry in entries:
                if entry.name == requested:
                    return Path(entry.path)
                # NTFS is case-insensitive.  A different spelling is rejected
                # rather than silently normalized to the requested string.
                if os.name == "nt" and entry.name.casefold() == requested.casefold():
                    raise _error("path_denied")
    except WorkspaceError:
        raise
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        raise _error("path_denied") from None
    return None


def _canonical_components(path: Path, *, allow_missing_final: bool) -> Path:
    """Walk an existing path one directory entry at a time.

    ``Path.resolve`` alone can hide a junction or a case-alias.  The explicit
    walk checks lstat metadata on every ancestor and returns the actual entry
    spelling.  A missing final component is accepted only for an atomic write
    target; its parent still has to exist and pass every check.
    """
    path = _absolute_path(os.fspath(path))
    anchor = Path(path.anchor) if path.anchor else Path(os.path.abspath(os.sep))
    # On POSIX, ``Path('/tmp/x').parts`` starts with '/'.  On Windows the drive
    # anchor is one part and the remaining parts are ordinary names.
    current = anchor
    parts = list(path.parts)
    if parts and parts[0] == path.anchor:
        parts = parts[1:]
    if not path.anchor and parts and parts[0] == os.sep:
        parts = parts[1:]
    for index, requested in enumerate(parts):
        _check_component_name(requested)
        child = _find_exact_child(current, requested)
        is_final = index == len(parts) - 1
        if child is None:
            if is_final and allow_missing_final:
                # A final write target can be absent.  Parent has already been
                # checked in prior iterations.
                return current / requested
            raise _error("path_not_found")
        try:
            info = os.lstat(child)
        except OSError:
            raise _error("path_denied") from None
        _reject_link_or_reparse(info)
        if not stat.S_ISDIR(info.st_mode) and not is_final:
            raise _error("path_denied")
        current = child
    try:
        info = os.lstat(current)
    except OSError:
        if allow_missing_final:
            return current
        raise _error("path_not_found") from None
    _reject_link_or_reparse(info)
    return current


def canonical_root(value: object) -> Path:
    """Validate and return an existing, non-link directory root."""
    raw = _raw_string(value)
    validate_lexical_path(raw)
    if not Path(raw).is_absolute():
        raise _error("invalid_path")
    path = _canonical_components(_absolute_path(raw), allow_missing_final=False)
    try:
        info = os.lstat(path)
    except OSError:
        raise _error("path_not_found") from None
    if not stat.S_ISDIR(info.st_mode):
        raise _error("invalid_path")
    try:
        real = Path(os.path.realpath(os.fspath(path)))
    except OSError:
        raise _error("path_denied") from None
    if not _same_path(real, path):
        raise _error("path_denied")
    return path


def safe_path(
    root: Path,
    value: object,
    *,
    allow_missing: bool = False,
    allow_directory: bool = True,
    allow_file: bool = True,
    deny: bool = True,
    allow_absolute: bool = False,
) -> Path:
    """Validate a path under ``root`` and return its canonical spelling."""
    raw = validate_lexical_path(value, relative=not allow_absolute)
    if not allow_absolute and Path(raw).is_absolute():
        raise _error("path_denied")
    root = canonical_root(root)
    supplied = _absolute_path(raw) if Path(raw).is_absolute() else root / raw
    candidate = _absolute_path(supplied)
    if not _within(root, candidate):
        raise _error("path_denied")

    # Check lexical denylist before touching a final target.  This also protects
    # a deleted path and a path which will be created by a write.
    try:
        relative = candidate.relative_to(root)
        rel_parts = tuple(relative.parts)
    except ValueError:
        raise _error("path_denied") from None
    if deny and denied_components(rel_parts):
        raise _error("path_denied")

    result = _canonical_components(candidate, allow_missing_final=allow_missing)
    if not _within(root, result):
        raise _error("path_denied")
    try:
        root_real = Path(os.path.realpath(os.fspath(root)))
        result_real = Path(os.path.realpath(os.fspath(result)))
    except OSError:
        raise _error("path_denied") from None
    if not _within(root_real, result_real):
        raise _error("path_denied")
    try:
        info = os.lstat(result)
    except OSError:
        if allow_missing:
            # The parent is guaranteed to have been walked and checked.
            return result
        raise _error("path_not_found") from None
    _reject_link_or_reparse(info)
    is_dir = stat.S_ISDIR(info.st_mode)
    is_file = stat.S_ISREG(info.st_mode)
    if not is_dir and not is_file:
        raise _error("path_denied")
    if is_file and info.st_nlink > 1:
        raise _error("path_denied")
    if is_dir and not allow_directory:
        raise _error("is_directory")
    if is_file and not allow_file:
        raise _error("invalid_path")
    return result


def safe_relative_name(value: object) -> str:
    """Validate a Git relative path, including deleted paths."""
    raw = validate_lexical_path(value, relative=True)
    # Git always uses '/'.  Backslashes are accepted by Git as ordinary
    # characters only on POSIX, but treating them as separators here keeps the
    # Windows and POSIX boundary identical.
    parts = [part for part in raw.replace("\\", "/").split("/") if part not in ("", ".")]
    if not parts or denied_components(parts):
        raise _error("path_denied")
    return "/".join(parts)


def parent_for_write(root: Path, value: object) -> tuple[Path, Path]:
    """Return ``(parent, target)`` for an atomic write target."""
    target = safe_path(root, value, allow_missing=True, allow_directory=False, allow_file=True)
    try:
        target_info = os.lstat(target)
    except FileNotFoundError:
        target_info = None
    except OSError:
        raise _error("path_denied") from None
    if target_info is not None:
        _reject_link_or_reparse(target_info)
        if stat.S_ISDIR(target_info.st_mode):
            raise _error("is_directory")
        if not stat.S_ISREG(target_info.st_mode) or target_info.st_nlink > 1:
            raise _error("path_denied")
    parent = target.parent
    parent = safe_path(root, str(parent), allow_directory=True, allow_file=False, allow_absolute=True)
    return parent, target


__all__ = [
    "DENY_NAMES",
    "ALLOW_EXT",
    "MAX_GREP_BYTES",
    "MAX_GREP_FILE_BYTES",
    "MAX_GREP_FILES",
    "MAX_GREP_MATCHES",
    "MAX_LIST_ENTRIES",
    "MAX_PATH_CHARS",
    "MAX_READ_BYTES",
    "MAX_READ_CHARS",
    "MAX_READ_LINES",
    "MAX_WRITE_BYTES",
    "canonical_root",
    "denied_components",
    "is_denied_name",
    "is_allowed_extension",
    "parent_for_write",
    "safe_path",
    "safe_relative_name",
    "validate_lexical_path",
]
