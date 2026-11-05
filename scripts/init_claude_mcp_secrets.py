"""Initialize separate, owner-only credentials for the Claude remote MCP endpoint.

Never prints, accepts as CLI arguments, or stores credentials in Git.
Re-running preserves the existing credentials and connector URL.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import secrets

from gateway_muse_http import _read_secret_file


PUBLIC_ORIGIN = "https://nk-h01.f5.si"
PREFIX = "/claude-mcp"


def _owner_only_acl(path: Path, *, directory: bool) -> None:
    import win32api
    import win32security
    import ntsecuritycon

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    try:
        sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    finally:
        win32api.CloseHandle(token)

    acl = win32security.ACL()
    flags = (
        win32security.OBJECT_INHERIT_ACE | win32security.CONTAINER_INHERIT_ACE
        if directory else 0
    )
    acl.AddAccessAllowedAceEx(
        win32security.ACL_REVISION, flags, ntsecuritycon.FILE_ALL_ACCESS, sid
    )
    win32security.SetNamedSecurityInfo(
        str(path),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION
        | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
        None,
        None,
        acl,
        None,
    )


def _write_new_private_file(path: Path, contents: str) -> None:
    with path.open("x", encoding="ascii", newline="") as handle:
        handle.write(contents)
    _owner_only_acl(path, directory=False)


def initialize(state: Path) -> Path:
    if not state.is_absolute():
        raise ValueError("state directory must be an absolute path")
    state.mkdir(parents=True, exist_ok=True)
    directory = state / "secrets"
    if directory.is_symlink():
        raise ValueError("secret directory cannot be a symlink")
    if not directory.exists():
        directory.mkdir()
    if not directory.is_dir():
        raise ValueError("secret directory is not a directory")
    _owner_only_acl(directory, directory=True)

    for name, value in (
        ("path.secret", lambda: secrets.token_hex(32)),
        ("token.secret", lambda: secrets.token_urlsafe(48)),
    ):
        path = directory / name
        if not path.exists() and not path.is_symlink():
            _write_new_private_file(path, value())

    # Both reads check the secret directory and file owner, ACL, and reparse status
    # before accessing any credentials.
    url_path = _read_secret_file(directory / "path.secret")
    bearer = _read_secret_file(directory / "token.secret", token=True)
    url = f"{PUBLIC_ORIGIN}{PREFIX}/{url_path}/mcp"
    handoff = directory / "connector.txt"
    text = (
        "Claude Remote MCP Connector - Home MCP Gateway\n"
        "Connector name: Home MCP Gateway (Claude)\n"
        f"URL: {url}\n"
        "Authentication setting: No sign in\n"
        "Request header name: Authorization\n"
        f"Request header value: Bearer {bearer}\n"
        "\n"
        "This grants unrestricted access to the signed-in Windows user's PC.\n"
        "Keep this file private. Never paste it into a public conversation.\n"
    )
    if handoff.is_symlink():
        raise ValueError("connector handoff cannot be a symlink")
    if not handoff.exists():
        _write_new_private_file(handoff, text)
    else:
        _owner_only_acl(handoff, directory=False)
        handoff.write_text(text, encoding="utf-8")
    return handoff


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    args = parser.parse_args()
    handoff = initialize(args.state_root)
    print(f"Private connector instructions created at: {handoff}")
    print("Credentials are not displayed.")


if __name__ == "__main__":
    main()
