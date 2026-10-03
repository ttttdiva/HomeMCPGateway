"""Git operations on arbitrary repositories and detached worktrees."""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Any


def _git(path: str, *args: str, input_data: bytes | None = None, timeout: float = 60,
         check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(Path(path).expanduser().resolve()), *args],
                            input=input_data, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip() or "Git command failed")
    return result


def _text(path: str, *args: str, **kwargs) -> str:
    return _git(path, *args, **kwargs).stdout.decode("utf-8", "replace").strip()


def git_resolve(repository_path: str) -> dict[str, Any]:
    """Resolve a repository, subdirectory, linked worktree or bare repository to its root and Git directory."""
    path = str(Path(repository_path).expanduser().resolve())
    if Path(path).is_file():
        path = str(Path(path).parent)
    bare = _text(path, "rev-parse", "--is-bare-repository") == "true"
    root = _text(path, "rev-parse", "--absolute-git-dir" if bare else "--show-toplevel")
    return {"repository_root": root, "git_dir": _text(path, "rev-parse", "--absolute-git-dir"),
            "common_dir": str((Path(path) / _text(path, "rev-parse", "--git-common-dir")).resolve()),
            "bare": bare}


def git_find(search_path: str, max_depth: int = 4) -> dict[str, Any]:
    """Search a supplied directory for repositories, including worktrees and bare repos. max_depth=0 checks only that directory."""
    root = Path(search_path).expanduser().resolve()
    if not root.is_dir() or max_depth < 0:
        raise ValueError("search_path must be a directory and max_depth must be nonnegative")
    found, errors = [], []
    def onerror(exc):
        errors.append(str(exc))
    for directory, dirs, files in os.walk(root, onerror=onerror):
        depth = len(Path(directory).relative_to(root).parts)
        if ".git" in dirs or ".git" in files or ("HEAD" in files and "objects" in dirs):
            try:
                found.append(git_resolve(directory))
            except ValueError as exc:
                errors.append({"path": directory, "error": str(exc)})
        dirs[:] = [name for name in dirs if name != ".git"] if depth < max_depth else []
    return {"repositories": found, "errors": errors}


def git_status(repository_path: str) -> dict[str, Any]:
    """Return root, full HEAD SHA, branch, remotes, upstream and porcelain status; supports unborn branches and bare repos."""
    result = git_resolve(repository_path)
    root = result["repository_root"]
    result.update(head=_text(root, "rev-parse", "--verify", "HEAD", check=False) or None,
                  branch=_text(root, "symbolic-ref", "--short", "-q", "HEAD", check=False) or None,
                  upstream=_text(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False) or None,
                  remotes=_text(root, "remote", "-v"))
    result["status"] = "" if result["bare"] else _git(root, "status", "--porcelain=v1", "--untracked-files=all").stdout.decode("utf-8", "replace")
    result["dirty"] = bool(result["status"])
    return result


def git_fetch(repository_path: str, remote: str = "origin", refspec: str = "", timeout_sec: float = 120) -> dict[str, Any]:
    """Fetch a remote/refspec without switching or modifying the checkout. Returns diagnostics and current full HEAD SHA."""
    args = ["fetch", "--", remote] + ([refspec] if refspec else [])
    result = _git(repository_path, *args, timeout=timeout_sec)
    return {"stdout": result.stdout.decode("utf-8", "replace"), "stderr": result.stderr.decode("utf-8", "replace"),
            "repository": git_status(repository_path)}


def git_remote_head(repository_path: str, remote: str = "origin", timeout_sec: float = 60) -> dict[str, Any]:
    """Query the remote's actual HEAD SHA and symbolic default branch using ls-remote (network operation)."""
    raw = _text(repository_path, "ls-remote", "--symref", "--", remote, "HEAD", timeout=timeout_sec)
    branch, sha = None, None
    for line in raw.splitlines():
        value, ref = line.split("\t", 1)
        if ref == "HEAD":
            if value.startswith("ref: "):
                branch = value[5:]
            else:
                sha = value
    return {"remote": remote, "head": sha, "ref": branch}


def git_worktree_create(repository_path: str, commit_sha: str, workspace_path: str) -> dict[str, Any]:
    """Create a detached isolated worktree at an explicit path from a full 40/64-character commit SHA. Original checkout is untouched."""
    if not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", commit_sha):
        raise ValueError("commit_sha must be a full commit SHA (fetch it first if absent)")
    resolved = _text(repository_path, "rev-parse", "--verify", f"{commit_sha}^{{commit}}")
    if resolved.lower() != commit_sha.lower():
        raise ValueError("SHA must identify a commit, not a tag object")
    dst = Path(workspace_path).expanduser().resolve()
    dst.parent.mkdir(parents=True, exist_ok=True)
    _git(repository_path, "worktree", "add", "--detach", "--", str(dst), resolved)
    return {"workspace_path": str(dst), "requested_commit_sha": commit_sha, "base_commit_sha": resolved,
            "repository": git_status(str(dst))}


def git_worktree_list(repository_path: str) -> dict[str, Any]:
    """List all registered worktrees with HEAD, branch, detached/locked/prunable state."""
    raw = _git(repository_path, "worktree", "list", "--porcelain", "-z").stdout.decode("utf-8", "replace")
    entries, current = [], {}
    for field in raw.split("\0"):
        if not field:
            if current:
                entries.append(current)
                current = {}
        else:
            key, sep, value = field.partition(" ")
            current[key] = value if sep else True
    return {"worktrees": entries}


def git_worktree_status(workspace_path: str) -> dict[str, Any]:
    """Return the isolated worktree's exact current HEAD SHA and tracked/untracked status."""
    return git_status(workspace_path)


def git_worktree_diff(workspace_path: str, base_commit: str = "HEAD", files: list[str] | None = None) -> dict[str, Any]:
    """Return binary-capable unified diff of tracked index/working files against a commit. Untracked files are listed separately."""
    base = _text(workspace_path, "rev-parse", "--verify", "--end-of-options", f"{base_commit}^{{commit}}")
    paths = files or []
    diff = _git(workspace_path, "diff", "--binary", "--no-ext-diff", base, "--", *paths).stdout.decode("utf-8", "replace")
    untracked = _git(workspace_path, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths).stdout.decode("utf-8", "replace")
    return {"workspace_path": str(Path(workspace_path).resolve()), "base_commit_sha": base,
            "head": _text(workspace_path, "rev-parse", "HEAD"), "diff": diff,
            "untracked_files": [p for p in untracked.split("\0") if p]}


def git_apply_patch(workspace_path: str, patch: str, check_only: bool = False, index: bool = False) -> dict[str, Any]:
    """Apply a unified diff with git apply, or validate with check_only. index=True also updates the worktree index."""
    args = ["apply", "--whitespace=nowarn"]
    if check_only:
        args.append("--check")
    if index:
        args.append("--index")
    _git(workspace_path, *args, "-", input_data=patch.encode("utf-8"))
    return {"applied": not check_only, "checked": True, "repository": git_status(workspace_path)}


def git_changed_files(workspace_path: str, files: list[str] | None = None, base_commit: str = "HEAD") -> dict[str, Any]:
    """Return per-file Git change codes versus a commit, including untracked files; files accepts Git pathspecs."""
    base = _text(workspace_path, "rev-parse", "--verify", "--end-of-options", f"{base_commit}^{{commit}}")
    paths = files or []
    raw = _git(workspace_path, "diff", "--no-renames", "--name-status", "-z", base, "--", *paths).stdout.decode("utf-8", "replace").split("\0")
    changed = [{"status": raw[i], "path": raw[i + 1]} for i in range(0, len(raw) - 1, 2)]
    untracked = _git(workspace_path, "ls-files", "--others", "--exclude-standard", "-z", "--", *paths).stdout.decode("utf-8", "replace")
    changed.extend({"status": "?", "path": p} for p in untracked.split("\0") if p)
    return {"base_commit_sha": base, "files": changed}


def git_worktree_cleanup(repository_path: str, workspace_path: str, force: bool = False) -> dict[str, Any]:
    """Remove a registered worktree with Git. force=True discards its worktree changes; Git's usual semantics apply."""
    dst = str(Path(workspace_path).expanduser().resolve())
    _git(repository_path, "worktree", "remove", *(["--force"] if force else []), "--", dst)
    return {"workspace_path": dst, "removed": True}


TOOLS = [git_find, git_resolve, git_status, git_fetch, git_remote_head, git_worktree_create,
         git_worktree_list, git_worktree_status, git_worktree_diff, git_apply_patch, git_changed_files, git_worktree_cleanup]
