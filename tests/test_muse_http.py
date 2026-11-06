from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import socket
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from typing import Iterator

import pytest

httpx = pytest.importorskip("httpx")
httpx2 = pytest.importorskip("httpx2")
pytest.importorskip("mcp")
uvicorn = pytest.importorskip("uvicorn")

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import gateway_muse_http as muse_http  # noqa: E402


PATH_SECRET = "a" * 48
BEARER_TOKEN = "b" * 48


def _run(coro):
    return asyncio.run(coro)


def _build_app(audit_path: Path):
    return muse_http.build_app(PATH_SECRET, BEARER_TOKEN, audit_path=audit_path)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _running_server(app) -> Iterator[str]:
    port = _free_port()
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="muse-gateway-http-test", daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=3)
        raise RuntimeError("test MCP server did not start")
    try:
        yield f"http://127.0.0.1:{port}/muse-mcp/{PATH_SECRET}/mcp"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_http_endpoint_rejects_missing_or_incorrect_credentials(tmp_path):
    app = _build_app(tmp_path / "audit.jsonl")
    url = f"http://testserver/muse-mcp/{PATH_SECRET}/mcp"

    async def check():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            missing = await client.get(url)
            wrong_token = await client.get(url, headers={"Authorization": "Bearer wrong-token"})
            wrong_path = await client.get(
                url.replace(f"/{PATH_SECRET}/", "/wrong-path/"),
                headers={"Authorization": f"Bearer {BEARER_TOKEN}"},
            )
        assert missing.status_code == 401
        assert wrong_token.status_code == 401
        assert wrong_path.status_code == 404

    _run(check())
    assert (tmp_path / "audit.jsonl").read_text(encoding="utf-8") == ""


def test_entrypoint_rejects_non_loopback_bind_before_reading_secrets(tmp_path):
    with pytest.raises(SystemExit, match="host must be 127.0.0.1"):
        muse_http.main(["--secret-dir", str(tmp_path), "--host", "0.0.0.0"])


def test_startup_rejects_unavailable_audit_sink(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()
    with pytest.raises(ValueError, match="audit_sink_unavailable"):
        muse_http.build_app(PATH_SECRET, BEARER_TOKEN, audit_path=audit_path)


def test_tool_dispatch_is_blocked_when_audit_sink_becomes_unavailable(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.mkdir()
    context = SimpleNamespace(method="tools/call", params={"name": "path_info"})
    called = False

    async def call_next(_context):
        nonlocal called
        called = True
        return SimpleNamespace(is_error=False)

    token = muse_http.AUDIT_PATH_CONTEXT.set(audit_path)
    try:
        with pytest.raises(MCPError) as caught:
            _run(muse_http._ToolAuditMiddleware()(context, call_next))
    finally:
        muse_http.AUDIT_PATH_CONTEXT.reset(token)
    assert caught.value.code == -32603
    assert caught.value.message == "audit_sink_unavailable"
    assert not called


def test_final_audit_append_failure_returns_fixed_server_error(monkeypatch, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    context = SimpleNamespace(method="tools/call", params={"name": "path_info"})
    called = False
    monkeypatch.setattr(muse_http, "_audit_sink_writable", lambda _path: True)
    monkeypatch.setattr(muse_http, "_append_tool_audit", lambda *_args: False)

    async def call_next(_context):
        nonlocal called
        called = True
        return SimpleNamespace(is_error=False)

    token = muse_http.AUDIT_PATH_CONTEXT.set(audit_path)
    try:
        with pytest.raises(MCPError) as caught:
            _run(muse_http._ToolAuditMiddleware()(context, call_next))
    finally:
        muse_http.AUDIT_PATH_CONTEXT.reset(token)
    assert caught.value.code == -32603
    assert caught.value.message == "audit_write_failed"
    assert called


class _FakeDacl:
    def __init__(self, aces):
        self.aces = aces

    def GetAceCount(self):
        return len(self.aces)

    def GetAce(self, index):
        return self.aces[index]


class _FakeDescriptor:
    def __init__(self, owner_sid, dacl):
        self.owner_sid = owner_sid
        self.dacl = dacl

    def GetSecurityDescriptorOwner(self):
        return self.owner_sid

    def GetSecurityDescriptorDacl(self):
        return self.dacl


class _FakeHandle:
    def __init__(self, kind):
        self.kind = kind


def _install_fake_win32_file_api(
    monkeypatch,
    tmp_path,
    *,
    directory_attributes=0x10,
    file_attributes=0,
    directory_owner="current-user",
    directory_allowed=("current-user",),
    file_owner="current-user",
    file_allowed=("current-user",),
):
    events = []
    descriptors = {
        "directory": _FakeDescriptor(
            directory_owner,
            _FakeDacl([((0, 0), 0x001F01FF, sid) for sid in directory_allowed]),
        ),
        "file": _FakeDescriptor(
            file_owner,
            _FakeDacl([((0, 0), 0x001F01FF, sid) for sid in file_allowed]),
        ),
    }

    security = ModuleType("win32security")
    security.TOKEN_QUERY = 8
    security.TokenUser = 1
    security.OWNER_SECURITY_INFORMATION = 1
    security.DACL_SECURITY_INFORMATION = 4
    security.SE_FILE_OBJECT = 1
    security.ACCESS_ALLOWED_ACE_TYPE = 0
    security.ACCESS_ALLOWED_OBJECT_ACE_TYPE = 5
    security.ACCESS_DENIED_ACE_TYPE = 1
    security.ACCESS_DENIED_OBJECT_ACE_TYPE = 6
    security.OpenProcessToken = lambda process, access: "process-token"
    security.GetTokenInformation = lambda token, info: ("current-user", None)
    security.GetSecurityInfo = lambda handle, object_type, info: (
        events.append(f"acl:{handle.kind}") or descriptors[handle.kind]
    )
    security.ConvertSidToStringSid = lambda sid: sid

    api = ModuleType("win32api")
    api.GetCurrentProcess = lambda: "process"
    api.CloseHandle = lambda handle: None

    con = ModuleType("win32con")
    con.READ_CONTROL = 0x00020000
    con.FILE_SHARE_READ = 1
    con.OPEN_EXISTING = 3
    con.GENERIC_READ = 0x80000000
    con.FILE_ATTRIBUTE_DIRECTORY = 0x10
    con.FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    con.FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

    file_api = ModuleType("win32file")
    file_api.FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

    def create_file(path, access, share, security_attributes, disposition, flags, template):
        kind = "directory" if Path(path) == tmp_path else "file"
        events.append(f"open:{kind}")
        return _FakeHandle(kind)

    def get_file_info(handle):
        attrs = directory_attributes if handle.kind == "directory" else file_attributes
        return (attrs,)

    def read_file(handle, count):
        events.append("read:file")
        return (0, ("c" * 48).encode("ascii"))

    file_api.CreateFile = create_file
    file_api.GetFileInformationByHandle = get_file_info
    file_api.ReadFile = read_file
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setitem(sys.modules, "win32api", api)
    monkeypatch.setitem(sys.modules, "win32con", con)
    monkeypatch.setitem(sys.modules, "win32file", file_api)
    return events


@pytest.mark.parametrize(
    ("owner_sid", "allowed_sids", "has_dacl", "rejected"),
    [
        ("current-user", ["current-user"], True, False),
        ("other-user", ["current-user"], True, True),
        ("current-user", ["current-user", "other-user"], True, True),
        ("current-user", [], True, True),
        ("current-user", ["current-user"], False, True),
    ],
)
def test_secret_acl_requires_current_owner_and_excludes_other_allowed_sids(
    monkeypatch, tmp_path, owner_sid, allowed_sids, has_dacl, rejected
):
    current_sid = "current-user"
    ace_type = 0
    dacl = _FakeDacl([((ace_type, 0), 0x001F01FF, sid) for sid in allowed_sids]) if has_dacl else None
    descriptor = _FakeDescriptor(owner_sid, dacl)

    security = ModuleType("win32security")
    security.TOKEN_QUERY = 8
    security.TokenUser = 1
    security.OWNER_SECURITY_INFORMATION = 1
    security.DACL_SECURITY_INFORMATION = 4
    security.ACCESS_ALLOWED_ACE_TYPE = 0
    security.ACCESS_ALLOWED_OBJECT_ACE_TYPE = 5
    security.ACCESS_DENIED_ACE_TYPE = 1
    security.ACCESS_DENIED_OBJECT_ACE_TYPE = 6
    security.SE_FILE_OBJECT = 1
    security.OpenProcessToken = lambda process, access: "process-token"
    security.GetTokenInformation = lambda token, info: (current_sid, None)
    security.GetSecurityInfo = lambda handle, object_type, info: descriptor
    security.ConvertSidToStringSid = lambda sid: sid

    api = ModuleType("win32api")
    api.GetCurrentProcess = lambda: "process"
    api.CloseHandle = lambda handle: None
    monkeypatch.setitem(sys.modules, "win32security", security)
    monkeypatch.setitem(sys.modules, "win32api", api)

    if rejected:
        with pytest.raises(ValueError, match="invalid_secret_acl"):
            muse_http._require_owner_only_acl(_FakeHandle("file"))
    else:
        muse_http._require_owner_only_acl(_FakeHandle("file"))


def test_secret_reader_checks_directory_and_file_acls_before_reading(monkeypatch, tmp_path):
    events = _install_fake_win32_file_api(monkeypatch, tmp_path)
    value = muse_http._read_secret_file(tmp_path / "path.secret")

    assert value == "c" * 48
    assert events.index("acl:directory") < events.index("open:file")
    assert events.index("acl:file") < events.index("read:file")


def test_secret_directory_acl_rejects_other_allowed_sid_before_opening_child(monkeypatch, tmp_path):
    events = _install_fake_win32_file_api(
        monkeypatch,
        tmp_path,
        directory_allowed=("current-user", "other-user"),
    )
    with pytest.raises(ValueError, match="invalid_secret_acl"):
        muse_http._read_secret_file(tmp_path / "path.secret")
    assert "open:file" not in events
    assert "read:file" not in events


def test_secret_file_acl_rejects_other_allowed_sid_before_reading(monkeypatch, tmp_path):
    events = _install_fake_win32_file_api(
        monkeypatch,
        tmp_path,
        file_allowed=("current-user", "other-user"),
    )
    with pytest.raises(ValueError, match="invalid_secret_acl"):
        muse_http._read_secret_file(tmp_path / "path.secret")
    assert "acl:file" in events
    assert "read:file" not in events


@pytest.mark.parametrize(
    ("directory_attributes", "file_attributes", "expected_error"),
    [
        (0x10 | 0x400, 0, "invalid_secret_directory"),
        (0x10, 0x400, "invalid_secret_file"),
    ],
)
def test_secret_reader_rejects_directory_and_file_reparse_points(
    monkeypatch, tmp_path, directory_attributes, file_attributes, expected_error
):
    events = _install_fake_win32_file_api(
        monkeypatch,
        tmp_path,
        directory_attributes=directory_attributes,
        file_attributes=file_attributes,
    )
    with pytest.raises(ValueError, match=expected_error):
        muse_http._read_secret_file(tmp_path / "path.secret")
    assert "read:file" not in events


def test_authenticated_http_lists_all_gateway_tools_and_audits_tool_calls(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    app = _build_app(audit_path)

    async def check(url):
        expected_count = len(await muse_http.gateway_mcp.list_tools())
        async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {BEARER_TOKEN}"}) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                read_stream, write_stream = streams[:2]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    assert len(listing.tools) == expected_count
                    result = await session.call_tool("path_info", {"path": str(tmp_path)})
                    assert not result.is_error

    with _running_server(app) as url:
        _run(check(url))

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert set(records[0]) == {"tool", "timestamp", "result"}
    assert records[0]["tool"] == "path_info"
    assert records[0]["result"] == "ok"
    datetime.fromisoformat(records[0]["timestamp"])
    encoded = json.dumps(records)
    assert PATH_SECRET not in encoded
    assert BEARER_TOKEN not in encoded
    assert str(tmp_path) not in encoded


@pytest.mark.parametrize("session_id", [None, "diagnostic-session"])
def test_raw_http_json_responses_are_untransformed_and_length_delimited(tmp_path, session_id):
    audit_path = tmp_path / ("audit-session.jsonl" if session_id else "audit-stateless.jsonl")
    app = _build_app(audit_path)

    async def check(url):
        headers = {
            "Authorization": f"Bearer {BEARER_TOKEN}",
            "User-Agent": "curl/8.10.1",
            "Accept": "application/json, text/event-stream",
            "Accept-Encoding": "gzip",
            "Content-Type": "application/json",
        }
        session_headers = {"Mcp-Session-Id": session_id} if session_id else {}
        async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
            async def post(payload, *, protocol_version=False):
                request_headers = dict(session_headers)
                if protocol_version:
                    request_headers["Mcp-Protocol-Version"] = "2025-03-26"
                response = await client.post(url, json=payload, headers=request_headers)
                assert response.headers.get("cache-control") == "no-store, no-transform"
                assert response.headers.get("content-encoding") is None
                assert response.headers.get("content-length") == str(len(response.content))
                return response

            initialize = await post(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "curl-probe", "version": "1"},
                    },
                }
            )
            assert initialize.status_code == 200
            assert initialize.headers.get("mcp-session-id") is None
            assert initialize.json()["result"]["protocolVersion"] == "2025-03-26"

            initialized = await post(
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                protocol_version=True,
            )
            assert initialized.status_code == 202
            assert initialized.content == b""

            listing = await post(
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                protocol_version=True,
            )
            assert listing.status_code == 200
            assert len(listing.json()["result"]["tools"]) == len(await muse_http.gateway_mcp.list_tools())

            called = await post(
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/call",
                    "params": {"name": "run_command", "arguments": {"command": "hostname"}},
                },
                protocol_version=True,
            )
            assert called.status_code == 200
            assert called.json()["result"]["isError"] is False
            assert called.json()["result"]["content"]

    with _running_server(app) as url:
        _run(check(url))

    records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["tool"] == "run_command"
    assert records[0]["result"] == "ok"


def test_muse_bearer_only_alias_preserves_old_two_factor_endpoint(tmp_path):
    audit_path = tmp_path / "alias-audit.jsonl"
    app = muse_http.build_app(
        PATH_SECRET,
        BEARER_TOKEN,
        audit_path=audit_path,
        enable_muse_bearer_only=True,
    )

    async def check(legacy_url):
        alias_url = legacy_url.replace(
            f"/muse-mcp/{PATH_SECRET}/mcp",
            muse_http.MUSE_BEARER_ONLY_PATH,
        )
        async with httpx.AsyncClient(timeout=10) as probe:
            alias_missing = await probe.get(alias_url)
            alias_invalid = await probe.get(
                alias_url, headers={"Authorization": "Bearer invalid"}
            )
            old_missing = await probe.get(legacy_url)
            old_invalid = await probe.get(
                legacy_url, headers={"Authorization": "Bearer invalid"}
            )
        assert alias_missing.status_code == 401
        assert alias_invalid.status_code == 401
        assert old_missing.status_code == 401
        assert old_invalid.status_code == 401

        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {BEARER_TOKEN}"}
        ) as http_client:
            async with streamable_http_client(
                alias_url, http_client=http_client
            ) as streams:
                read_stream, write_stream = streams[:2]
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    assert len(listing.tools) == len(
                        await muse_http.gateway_mcp.list_tools()
                    )
                    result = await session.call_tool(
                        "path_info", {"path": str(tmp_path)}
                    )
                    assert not result.is_error

    with _running_server(app) as legacy_url:
        _run(check(legacy_url))

    records = [
        json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(records) == 1
    assert records[0]["tool"] == "path_info"
    assert records[0]["result"] == "ok"
    audit_contents = audit_path.read_text(encoding="utf-8")
    assert BEARER_TOKEN not in audit_contents
    assert PATH_SECRET not in audit_contents


def test_muse_alias_is_opt_in_and_never_enabled_on_claude_prefix(tmp_path):
    app = muse_http.build_app(
        PATH_SECRET, BEARER_TOKEN, audit_path=tmp_path / "opt-in-audit.jsonl"
    )

    async def check(legacy_url):
        alias_url = legacy_url.replace(
            f"/muse-mcp/{PATH_SECRET}/mcp",
            muse_http.MUSE_BEARER_ONLY_PATH,
        )
        async with httpx.AsyncClient(timeout=10) as probe:
            response = await probe.get(
                alias_url, headers={"Authorization": f"Bearer {BEARER_TOKEN}"}
            )
        assert response.status_code == 404

    with _running_server(app) as legacy_url:
        _run(check(legacy_url))
    with pytest.raises(
        ValueError, match="muse_bearer_only_requires_muse_prefix"
    ):
        muse_http.build_app(
            PATH_SECRET,
            BEARER_TOKEN,
            audit_path=tmp_path / "claude-audit.jsonl",
            prefix="/claude-mcp",
            enable_muse_bearer_only=True,
        )
