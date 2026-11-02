"""Client for the dedicated Edge broker managed by AoiTalk.

This module only reads the broker's local control files and sends authenticated
named-pipe requests. It never starts Edge or serves broker requests.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import math
import multiprocessing.connection
import os
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit


_APP_DIRECTORY = "AoiTalk/browser-broker"
_CONTROL_FILE = "control.json"
_AUTHKEY_FILE = "control.authkey"
_STATE_FILE = "connection-state.json"
_CONTROL_PROTOCOL_VERSION = 1
_MAX_TIMEOUT_SEC = 60.0
_PHASES = {"idle", "connecting", "ready", "failed"}
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9 ._/-]{1,200}$")
_PIPE_SUFFIX = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_SAFE_ERROR_CODE = re.compile(r"^edge_broker_[a-z0-9_]{1,64}$")
_SAFE_UTC_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$")


class EdgeBrokerError(RuntimeError):
    """An error with a fixed, non-secret public code."""

    def __init__(self, code: str) -> None:
        if not _SAFE_ERROR_CODE.fullmatch(code):
            code = "edge_broker_unavailable"
        super().__init__(code)
        self.code = code


def _raise(code: str) -> None:
    raise EdgeBrokerError(code) from None


def _timeout_value(value: Any) -> float:
    if type(value) not in (int, float):
        _raise("edge_broker_invalid_timeout")
    try:
        timeout_sec = float(value)
    except (OverflowError, ValueError):
        _raise("edge_broker_invalid_timeout")
    if not math.isfinite(timeout_sec) or not 1 <= timeout_sec <= _MAX_TIMEOUT_SEC:
        _raise("edge_broker_invalid_timeout")
    return timeout_sec


@dataclass(frozen=True)
class BrokerPaths:
    directory: Path
    control_file: Path
    authkey_file: Path
    state_file: Path


def _paths(local_app_data: str | os.PathLike[str] | None = None) -> BrokerPaths:
    root = local_app_data if local_app_data is not None else os.environ.get("LOCALAPPDATA")
    if not root:
        _raise("edge_broker_storage_unavailable")
    base = Path(root)
    if not base.is_absolute():
        _raise("edge_broker_storage_unavailable")
    directory = base / _APP_DIRECTORY
    return BrokerPaths(
        directory=directory,
        control_file=directory / _CONTROL_FILE,
        authkey_file=directory / _AUTHKEY_FILE,
        state_file=directory / _STATE_FILE,
    )


def _user_pipe_suffix() -> str:
    """Return the same per-user suffix used by AoiTalk's broker."""
    identity = "\\".join(
        (
            os.environ.get("USERDOMAIN", ""),
            os.environ.get("USERNAME", ""),
            os.environ.get("LOCALAPPDATA", ""),
        )
    )
    return hashlib.sha256(identity.casefold().encode("utf-8", "replace")).hexdigest()[:24]


def _pipe_address() -> str:
    return rf"\\.\pipe\LOCAL\AoiTalk.BrowserBroker.{_user_pipe_suffix()}"


def _validate_pipe_address(value: Any) -> str:
    """Accept only this user's AoiTalk broker pipe."""
    if not isinstance(value, str) or len(value) > 240:
        _raise("edge_broker_invalid_endpoint")
    prefix = "\\\\.\\pipe\\LOCAL\\"
    if not value.casefold().startswith(prefix.casefold()):
        _raise("edge_broker_invalid_endpoint")
    suffix = value[len(prefix) :]
    if not _PIPE_SUFFIX.fullmatch(suffix) or value.casefold() != _pipe_address().casefold():
        _raise("edge_broker_invalid_endpoint")
    return value


def _read_bounded(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as stream:
            value = stream.read(limit + 1)
    except OSError:
        _raise("edge_broker_unavailable")
    if len(value) > limit:
        _raise("edge_broker_invalid_control")
    return value


def _read_control_config(
    local_app_data: str | os.PathLike[str] | None = None,
) -> tuple[str, bytes]:
    paths = _paths(local_app_data)
    raw = _read_bounded(paths.control_file, 2_048)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        _raise("edge_broker_invalid_control")
    if not isinstance(payload, dict) or payload.get("version") != _CONTROL_PROTOCOL_VERSION:
        _raise("edge_broker_invalid_control")
    address = _validate_pipe_address(payload.get("pipe"))
    if address.casefold() != _pipe_address().casefold():
        _raise("edge_broker_invalid_endpoint")
    authkey = _read_bounded(paths.authkey_file, 64)
    if len(authkey) != 32:
        _raise("edge_broker_invalid_control")
    return address, authkey


def _request_control_sync(address: str, authkey: bytes, wait_sec: float) -> Any:
    connection = None
    try:
        connection = multiprocessing.connection.Client(
            address=address,
            family="AF_PIPE",
            authkey=authkey,
        )
        connection.send({"op": "ensure_browser", "wait_sec": wait_sec})
        if not connection.poll(wait_sec + 1.0):
            _raise("edge_broker_timeout")
        return connection.recv()
    finally:
        if connection is not None:
            connection.close()


def _request_status_sync(address: str, authkey: bytes, wait_sec: float) -> Any:
    """Request diagnostics only; this operation never starts an Edge attach."""
    connection = None
    try:
        connection = multiprocessing.connection.Client(
            address=address,
            family="AF_PIPE",
            authkey=authkey,
        )
        connection.send({"op": "status"})
        if not connection.poll(wait_sec + 1.0):
            _raise("edge_broker_timeout")
        return connection.recv()
    finally:
        if connection is not None:
            connection.close()


async def get_broker_status(timeout_sec: float = 3.0) -> dict[str, Any]:
    """Read authenticated diagnostics from AoiTalk without attaching to Edge."""
    timeout = _timeout_value(timeout_sec)
    try:
        address, authkey = await asyncio.to_thread(_read_control_config)
        response = await asyncio.wait_for(
            asyncio.to_thread(_request_status_sync, address, authkey, timeout),
            timeout=timeout + 2.0,
        )
    except EdgeBrokerError:
        raise
    except asyncio.TimeoutError:
        _raise("edge_broker_timeout")
    except Exception:
        _raise("edge_broker_unavailable")
    if not isinstance(response, dict):
        _raise("edge_broker_invalid_response")

    phase = response.get("phase")
    error_code = response.get("error_code")
    error_at_utc = response.get("error_at_utc")
    cdp_success = response.get("cdp_connected_success")
    cdp_connected = response.get("cdp_connected")
    broker_pid = response.get("broker_pid")
    # Older dedicated broker responses omitted mode; treat that response as
    # dedicated while refusing every explicitly different mode.
    mode = response.get("mode", "dedicated")
    if (
        not isinstance(phase, str)
        or phase not in _PHASES
        or mode != "dedicated"
        or (error_code is not None and (not isinstance(error_code, str) or not _SAFE_ERROR_CODE.fullmatch(error_code)))
        or (error_at_utc is not None and (not isinstance(error_at_utc, str) or not _SAFE_UTC_TIMESTAMP.fullmatch(error_at_utc)))
        or (cdp_success is not None and type(cdp_success) is not bool)
        or type(cdp_connected) is not bool
        or (broker_pid is not None and (type(broker_pid) is not int or broker_pid <= 0))
        or response.get("status_ipc_supported") is not True
    ):
        _raise("edge_broker_invalid_response")
    return {
        "phase": phase,
        "mode": "dedicated",
        "error_code": error_code,
        "error_at_utc": error_at_utc,
        "cdp_connected_success": cdp_success,
        "cdp_connected": cdp_connected,
        "broker_pid": broker_pid,
        "status_ipc_supported": True,
    }


def _safe_browser_version(value: Any) -> str:
    text = str(value or "").strip()[:200]
    return text if _SAFE_VERSION.fullmatch(text) else ""


def _validate_loopback_ws_endpoint(value: Any) -> str:
    """Accept only the ephemeral loopback WebSocket emitted by Browser.bind."""
    if not isinstance(value, str) or len(value) > 512:
        _raise("edge_broker_invalid_endpoint")
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        _raise("edge_broker_invalid_endpoint")
    if (
        parsed.scheme != "ws"
        or parsed.hostname != "127.0.0.1"
        or port is None
        or not 1 <= port <= 65_535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/[A-Fa-f0-9]{32}", parsed.path)
    ):
        _raise("edge_broker_invalid_endpoint")
    return value


async def get_browser_endpoint(timeout_sec: float = 30) -> tuple[str, str]:
    """Ask the authenticated broker for its already-bound browser endpoint."""
    timeout = _timeout_value(timeout_sec)
    try:
        address, authkey = await asyncio.to_thread(_read_control_config)
        response = await asyncio.wait_for(
            asyncio.to_thread(_request_control_sync, address, authkey, timeout),
            timeout=timeout + 2.0,
        )
    except EdgeBrokerError:
        raise
    except asyncio.TimeoutError:
        _raise("edge_broker_timeout")
    except Exception:
        # IPC exceptions may contain the private pipe name or other local data.
        _raise("edge_broker_unavailable")
    if not isinstance(response, dict):
        _raise("edge_broker_invalid_response")
    phase = response.get("phase")
    if phase == "connecting":
        _raise("edge_broker_connecting")
    if phase == "failed":
        code = response.get("error")
        if isinstance(code, str) and _SAFE_ERROR_CODE.fullmatch(code):
            _raise(code)
        _raise("edge_broker_failed")
    if phase != "ready":
        _raise("edge_broker_invalid_response")
    endpoint = _validate_loopback_ws_endpoint(response.get("endpoint"))
    if endpoint.casefold() == address.casefold():
        _raise("edge_broker_invalid_endpoint")
    version = _safe_browser_version(response.get("browser_version"))
    if not version:
        _raise("edge_broker_invalid_response")
    return endpoint, version
