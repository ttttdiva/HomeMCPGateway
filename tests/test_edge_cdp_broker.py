from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from home_mcp_gateway import edge_cdp_broker as broker


EXPECTED_PIPE_PREFIX = r"\\.\pipe\LOCAL\AoiTalk.BrowserBroker."
EXPECTED_APP_DIRECTORY = Path("AoiTalk") / "browser-broker"
BROWSER_WS = "ws://127.0.0.1:54123/123e4567e89b12d3a456426614174000"


def _write_control_files(local_app_data: Path, pipe: str | None = None, key: bytes = b"k" * 32):
    paths = broker._paths(local_app_data)
    paths.directory.mkdir(parents=True, exist_ok=True)
    paths.control_file.write_text(
        json.dumps({"version": 1, "pipe": pipe or broker._pipe_address()}),
        encoding="utf-8",
    )
    paths.authkey_file.write_bytes(key)
    return paths


def test_paths_match_aoi_talk_broker_location_and_filenames(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = broker._paths()

    assert paths.directory == tmp_path / EXPECTED_APP_DIRECTORY
    assert paths.control_file == tmp_path / EXPECTED_APP_DIRECTORY / "control.json"
    assert paths.authkey_file == tmp_path / EXPECTED_APP_DIRECTORY / "control.authkey"
    assert paths.state_file == tmp_path / EXPECTED_APP_DIRECTORY / "connection-state.json"


def test_pipe_suffix_and_name_match_aoi_talk_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("USERDOMAIN", "ExampleDomain")
    monkeypatch.setenv("USERNAME", "ExampleUser")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    identity = "\\".join(("ExampleDomain", "ExampleUser", str(tmp_path))).casefold()
    expected_suffix = hashlib.sha256(identity.encode("utf-8", "replace")).hexdigest()[:24]

    assert broker._user_pipe_suffix() == expected_suffix
    assert broker._pipe_address() == EXPECTED_PIPE_PREFIX + expected_suffix
    assert broker._validate_pipe_address(broker._pipe_address()) == broker._pipe_address()


@pytest.mark.parametrize(
    "value",
    [
        "ws://127.0.0.1:54123/123e4567e89b12d3a456426614174000",
        r"\\remote\pipe\LOCAL\AoiTalk.BrowserBroker.user",
        r"\\.\pipe\LOCAL\AoiTalk.BrowserBroker.other-user",
        r"\\.\pipe\OTHER\AoiTalk.BrowserBroker.user",
        "x" * 241,
    ],
    ids=["url", "remote_pipe", "other_user", "wrong_namespace", "oversized"],
)
def test_pipe_validator_rejects_nonlocal_or_unexpected_names(value: str):
    with pytest.raises(broker.EdgeBrokerError) as caught:
        broker._validate_pipe_address(value)
    assert str(caught.value) == "edge_broker_invalid_endpoint"


def test_control_config_accepts_only_expected_pipe_version_and_authkey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = _write_control_files(tmp_path)
    address, authkey = broker._read_control_config()
    assert address == broker._pipe_address()
    assert authkey == b"k" * 32

    paths.control_file.write_text(json.dumps({"version": 2, "pipe": address}), encoding="utf-8")
    with pytest.raises(broker.EdgeBrokerError) as bad_version:
        broker._read_control_config()
    assert str(bad_version.value) == "edge_broker_invalid_control"

    paths.control_file.write_text(json.dumps({"version": 1, "pipe": EXPECTED_PIPE_PREFIX + "wrong"}), encoding="utf-8")
    with pytest.raises(broker.EdgeBrokerError) as bad_pipe:
        broker._read_control_config()
    assert str(bad_pipe.value) == "edge_broker_invalid_endpoint"

    paths.control_file.write_text(json.dumps({"version": 1, "pipe": address}), encoding="utf-8")
    paths.authkey_file.write_bytes(b"short")
    with pytest.raises(broker.EdgeBrokerError) as bad_key:
        broker._read_control_config()
    assert str(bad_key.value) == "edge_broker_invalid_control"


def test_control_config_errors_are_fixed_and_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = _write_control_files(tmp_path)
    paths.control_file.write_bytes(b"{" * 2_049)
    with pytest.raises(broker.EdgeBrokerError) as oversized:
        broker._read_control_config()
    assert str(oversized.value) == "edge_broker_invalid_control"
    assert "{" not in str(oversized.value)


def test_control_ipc_uses_authenticated_pipe_and_ensure_browser_protocol():
    observed: dict[str, object] = {}

    class FakeConnection:
        def send(self, message: dict[str, object]) -> None:
            observed["message"] = message

        def recv(self) -> dict[str, str]:
            return {"phase": "connecting"}

        def poll(self, timeout: float) -> bool:
            observed["poll_timeout"] = timeout
            return True

        def close(self) -> None:
            observed["closed"] = True

    def client(**kwargs: object) -> FakeConnection:
        observed.update(kwargs)
        return FakeConnection()

    with patch.object(broker.multiprocessing.connection, "Client", client):
        response = broker._request_control_sync(broker._pipe_address(), b"a" * 32, 3.5)

    assert observed["address"] == broker._pipe_address()
    assert observed["family"] == "AF_PIPE"
    assert observed["authkey"] == b"a" * 32
    assert observed["message"] == {"op": "ensure_browser", "wait_sec": 3.5}
    assert observed["poll_timeout"] == 4.5
    assert observed["closed"] is True
    assert response == {"phase": "connecting"}


def test_status_ipc_uses_authenticated_status_only_protocol():
    observed: dict[str, object] = {}

    class FakeConnection:
        def send(self, message: dict[str, object]) -> None:
            observed["message"] = message

        def recv(self) -> dict[str, str]:
            return {"phase": "ready"}

        def poll(self, timeout: float) -> bool:
            observed["poll_timeout"] = timeout
            return True

        def close(self) -> None:
            observed["closed"] = True

    def client(**kwargs: object) -> FakeConnection:
        observed.update(kwargs)
        return FakeConnection()

    with patch.object(broker.multiprocessing.connection, "Client", client):
        response = broker._request_status_sync(broker._pipe_address(), b"s" * 32, 2.0)

    assert observed["family"] == "AF_PIPE"
    assert observed["authkey"] == b"s" * 32
    assert observed["message"] == {"op": "status"}
    assert observed["poll_timeout"] == 3.0
    assert observed["closed"] is True
    assert response == {"phase": "ready"}


def test_control_request_timeout_returns_fixed_code():
    class TimedOutConnection:
        def send(self, _message: object) -> None:
            pass

        def poll(self, _timeout: float) -> bool:
            return False

        def recv(self) -> object:
            raise AssertionError("recv must not run after poll timeout")

        def close(self) -> None:
            pass

    with patch.object(broker.multiprocessing.connection, "Client", lambda **_kwargs: TimedOutConnection()):
        with pytest.raises(broker.EdgeBrokerError) as caught:
            broker._request_control_sync(broker._pipe_address(), b"a" * 32, 0.01)
    assert str(caught.value) == "edge_broker_timeout"


def test_public_timeout_validation_returns_fixed_error_for_overflow():
    with pytest.raises(broker.EdgeBrokerError) as caught:
        asyncio.run(broker.get_browser_endpoint(10**10_000))
    assert str(caught.value) == "edge_broker_invalid_timeout"


def test_get_broker_status_accepts_dedicated_and_omitted_mode_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    paths = _write_control_files(tmp_path)
    response = {
        "phase": "ready",
        "error_code": None,
        "error_at_utc": None,
        "cdp_connected_success": True,
        "cdp_connected": True,
        "broker_pid": 123,
        "status_ipc_supported": True,
    }
    with patch.object(broker, "_request_status_sync", return_value=response):
        old_dedicated_response = asyncio.run(broker.get_broker_status(1))
    response["mode"] = "dedicated"
    with patch.object(broker, "_request_status_sync", return_value=response):
        current_response = asyncio.run(broker.get_broker_status(1))
    response["mode"] = "legacy"
    with patch.object(broker, "_request_status_sync", return_value=response):
        with pytest.raises(broker.EdgeBrokerError) as wrong_mode:
            asyncio.run(broker.get_broker_status(1))

    assert old_dedicated_response["mode"] == "dedicated"
    assert current_response["mode"] == "dedicated"
    assert str(wrong_mode.value) == "edge_broker_invalid_response"


def test_get_broker_status_rejects_malformed_phase_with_fixed_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write_control_files(tmp_path)
    response = {
        "phase": ["ready"],
        "mode": "dedicated",
        "error_code": None,
        "error_at_utc": None,
        "cdp_connected_success": None,
        "cdp_connected": False,
        "broker_pid": None,
        "status_ipc_supported": True,
    }
    with patch.object(broker, "_request_status_sync", return_value=response):
        with pytest.raises(broker.EdgeBrokerError) as caught:
            asyncio.run(broker.get_broker_status(1))
    assert str(caught.value) == "edge_broker_invalid_response"


def test_get_broker_status_rejects_untrusted_error_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write_control_files(tmp_path)
    response = {
        "phase": "failed",
        "mode": "dedicated",
        "error_code": "private token value",
        "error_at_utc": None,
        "cdp_connected_success": False,
        "cdp_connected": False,
        "broker_pid": 123,
        "status_ipc_supported": True,
    }
    with patch.object(broker, "_request_status_sync", return_value=response):
        with pytest.raises(broker.EdgeBrokerError) as caught:
            asyncio.run(broker.get_broker_status(1))
    assert str(caught.value) == "edge_broker_invalid_response"
    assert "private" not in str(caught.value)


def test_get_browser_endpoint_uses_control_files_and_returns_safe_result(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write_control_files(tmp_path)
    captured: dict[str, object] = {}

    def request(address: str, authkey: bytes, wait_sec: float) -> dict[str, str]:
        captured.update(address=address, authkey=authkey, wait_sec=wait_sec)
        return {
            "phase": "ready",
            "endpoint": BROWSER_WS,
            "browser_version": "Microsoft Edge/156.0.4314.42",
        }

    with patch.object(broker, "_request_control_sync", request):
        result = asyncio.run(broker.get_browser_endpoint(4))

    assert result == (BROWSER_WS, "Microsoft Edge/156.0.4314.42")
    assert captured == {"address": broker._pipe_address(), "authkey": b"k" * 32, "wait_sec": 4.0}


@pytest.mark.parametrize(
    ("response", "expected_code"),
    [
        ({"phase": "connecting"}, "edge_broker_connecting"),
        ({"phase": "failed", "error": "edge_broker_cdp_connect_failed"}, "edge_broker_cdp_connect_failed"),
        ({"phase": "failed", "error": "endpoint=ws://secret"}, "edge_broker_failed"),
        ({"phase": "ready", "endpoint": "ws://192.0.2.3:54123/123e4567e89b12d3a456426614174000", "browser_version": "Edge/1"}, "edge_broker_invalid_endpoint"),
        ({"phase": "ready", "endpoint": BROWSER_WS, "browser_version": "token=secret"}, "edge_broker_invalid_response"),
        ([], "edge_broker_invalid_response"),
    ],
    ids=["connecting", "fixed-failure", "unsafe-failure", "remote-endpoint", "unsafe-version", "malformed"],
)
def test_get_browser_endpoint_maps_responses_to_fixed_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    response: object,
    expected_code: str,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write_control_files(tmp_path)
    with patch.object(broker, "_request_control_sync", return_value=response):
        with pytest.raises(broker.EdgeBrokerError) as caught:
            asyncio.run(broker.get_browser_endpoint(1))
    assert str(caught.value) == expected_code
    assert "secret" not in str(caught.value)


def test_get_browser_endpoint_sanitizes_ipc_exception_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    _write_control_files(tmp_path)

    def fail(address: str, authkey: bytes, _wait_sec: float) -> object:
        raise OSError(f"pipe={address} token={authkey!r}")

    with patch.object(broker, "_request_control_sync", fail):
        with pytest.raises(broker.EdgeBrokerError) as caught:
            asyncio.run(broker.get_browser_endpoint(1))
    assert str(caught.value) == "edge_broker_unavailable"
    assert "token" not in str(caught.value)


@pytest.mark.parametrize(
    "endpoint",
    [
        "ws://192.168.1.44:54123/123e4567e89b12d3a456426614174000",
        "ws://localhost:54123/123e4567e89b12d3a456426614174000",
        "wss://127.0.0.1:54123/123e4567e89b12d3a456426614174000",
        "ws://user@127.0.0.1:54123/123e4567e89b12d3a456426614174000",
        "ws://127.0.0.1:0/123e4567e89b12d3a456426614174000",
        "ws://127.0.0.1:54123/123e4567e89b12d3a456426614174000?token=x",
        "ws://127.0.0.1:54123/123e4567-e89b-12d3-a456-426614174000",
    ],
    ids=["non-loopback", "hostname", "tls", "userinfo", "invalid-port", "query", "non-guid-path"],
)
def test_ws_validator_rejects_non_loopback_or_unexpected_endpoints(endpoint: str):
    with pytest.raises(broker.EdgeBrokerError) as caught:
        broker._validate_loopback_ws_endpoint(endpoint)
    assert str(caught.value) == "edge_broker_invalid_endpoint"


def test_ws_validator_accepts_browser_bind_loopback_endpoint():
    assert broker._validate_loopback_ws_endpoint(BROWSER_WS) == BROWSER_WS


def test_client_module_has_no_broker_server_or_edge_launcher():
    source = Path(broker.__file__).read_text(encoding="utf-8")
    for symbol in ("BrokerState", "DedicatedEdge", "EdgeBrokerCore", "serve", "main", "reset_state"):
        assert f"def {symbol}" not in source
        assert f"class {symbol}" not in source
    assert "multiprocessing.connection.Listener" not in source
    assert "subprocess" not in source
