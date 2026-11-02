from __future__ import annotations

import asyncio
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

pytest.importorskip("mcp")
httpx = pytest.importorskip("httpx")
uvicorn = pytest.importorskip("uvicorn")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402


def _build_app(*, registry: Path, audit: Path, secret: str, port: int, token: str):
    from muse_workspace_mcp import server as server_module

    build_app = server_module.build_app

    try:
        return build_app(
            workspaces_path=registry,
            audit_path=audit,
            secret=secret,
            port=port,
            prefix="/muse-mcp",
            token=token,
            checks_enabled=False,
        )
    except TypeError:
        # The implementation may expose an explicit two-stage adapter while
        # retaining the same public endpoint. Build the FastMCP object first,
        # then apply its bearer guard.
        server = server_module.build_from_paths(
            registry_path=registry,
            audit_path=audit,
            secret=secret,
            checks_enabled=False,
            port=port,
            prefix="/muse-mcp",
        )
        return build_app(server, token=token, audit_path=audit)


def _asgi(app_obj: Any):
    return app_obj.streamable_http_app() if hasattr(app_obj, "streamable_http_app") else app_obj


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _running_server(*, registry: Path, audit: Path, secret: str, token: str) -> Iterator[str]:
    port = _free_port()
    app = _asgi(_build_app(registry=registry, audit=audit, secret=secret, port=port, token=token))
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="muse-mcp-test", daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{port}/muse-mcp/{secret}/mcp"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if server.started:
            break
        time.sleep(0.02)
    else:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("test MCP server did not start")
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def _run(coro):
    return asyncio.run(coro)


def test_streamable_http_client_can_list_and_call_tools_on_separate_port(snapshot_env):
    secret = "a" * 32
    token = "b" * 32
    with _running_server(
        registry=snapshot_env["registry"],
        audit=snapshot_env["audit"],
        secret=secret,
        token=token,
    ) as url:

        async def client_call():
            async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as streams:
                read_stream, write_stream, _ = streams
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    names = {tool.name for tool in tools.tools}
                    assert names == {
                        "list_workspaces",
                        "use_workspace",
                        "list_dir",
                        "read_file",
                        "grep_text",
                        "write_file",
                        "apply_patch",
                        "git_status",
                        "git_diff",
                        "run_check",
                    }
                    result = await session.call_tool("read_file", {"path": "README.md"})
                    content = "\n".join(block.text for block in result.content if hasattr(block, "text"))
                    assert "hello" in content

        _run(client_call())


def test_authentication_rejects_missing_and_wrong_bearer_and_wrong_path(snapshot_env):
    secret = "c" * 32
    token = "d" * 32
    with _running_server(
        registry=snapshot_env["registry"],
        audit=snapshot_env["audit"],
        secret=secret,
        token=token,
    ) as url:
        with httpx.Client(timeout=5) as client:
            missing = client.get(url)
            assert missing.status_code == 401
            wrong = client.get(url, headers={"Authorization": "Bearer wrong-token"})
            assert wrong.status_code == 401
            wrong_path = client.get(
                url.replace(f"/{secret}/", "/wrong-path/"),
                headers={"Authorization": f"Bearer {token}"},
            )
            assert wrong_path.status_code == 404

    raw = snapshot_env["audit"].read_text(encoding="utf-8")
    assert "wrong-token" not in raw
    assert token not in raw
    assert secret not in raw
    assert '"tool": "_auth"' in raw or '"tool":"_auth"' in raw


def test_server_refuses_an_empty_bearer_token(snapshot_env):
    from muse_workspace_mcp import server as server_module

    secret = "e" * 32
    server = server_module.build_from_paths(
        registry_path=snapshot_env["registry"],
        audit_path=snapshot_env["audit"],
        secret=secret,
        checks_enabled=False,
        port=_free_port(),
        prefix="/muse-mcp",
    )
    with pytest.raises(Exception) as caught:
        server_module.build_app(server, token="", audit_path=snapshot_env["audit"])
    assert "token" in str(caught.value).lower()
