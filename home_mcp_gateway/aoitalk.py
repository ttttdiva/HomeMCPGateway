"""Authenticated ClipIngest adapter; credentials never cross the MCP boundary.

Uses AoiTalk's existing durable job API, not a second writer or direct SQL.
All configuration comes from operator-owned local environment/files, never a
URL, command, or credential supplied by clip content or a model tool argument.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any
from uuid import UUID

from dotenv import dotenv_values
import httpx

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT = 5.0
POLL_INTERVAL = 1.0
STATES = {"queued", "running", "succeeded", "failed"}
ACTIONS = {"create", "append", "duplicate_skip"}
MESSAGES = {
    "configuration_error": "AoiTalkの接続設定・認証情報をローカルの.envで確認してください。",
    "invalid_input": "source・UUID・idempotency_key・wait_secondsの指定を確認してください。",
    "authentication_failed": ".envの認証情報でログインできません。現在のパスワードと一致するか確認してください。",
    "password_reset_required": "AoiTalk側で初回パスワード変更を完了してから再実行してください。",
    "permission_denied": "AoiTalk側のアクセス権限を確認してください。権限の回避は行いません。",
    "not_found": "指定したジョブ・ノード・APIが見つかりません。再登録前にIDと接続先を確認してください。",
    "conflict": "AoiTalkが競合を返しました。保存先や既存ジョブを確認してください。",
    "rate_limited": "AoiTalkがリクエストを制限しています。時間を置いて同じキーで確認してください。",
    "server_error": "AoiTalkのAPIがエラーを返しました。AoiTalk側のログを確認してください。",
    "connection_error": "AoiTalkへ接続できません。起動状態と接続先を確認してください。",
    "invalid_response": "AoiTalkの応答を検証できません。保存完了とは扱いません。",
    "idempotency_conflict": "同じキーの既存ジョブと送信内容が一致しません。上書き・再登録はしていません。",
    "job_failed": "クリップ取り込みが失敗しました。AoiTalkのジョブ詳細で原因を確認してください。",
    "verification_failed": "ジョブは成功を返しましたが、保存ノード・receiptの再読取を確認できません。再登録せず状態を確認してください。",
}


class ClipError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(MESSAGES[code])
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True)
class Settings:
    base_url: str
    username: str = field(repr=False)
    password: str = field(repr=False)
    target_node_id: str | None = None


def _uuid(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    try:
        return str(UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        raise ClipError("invalid_input") from None


def _key(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", value):
        raise ClipError("invalid_input")
    return value


def _wait(value: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ClipError("invalid_input")
    if not math.isfinite(value) or not 0 <= value <= 30:
        raise ClipError("invalid_input")
    return float(value)


def _env_file(path: Path, *, required: bool = False) -> dict[str, str]:
    try:
        if not path.is_file():
            if required:
                raise ClipError("configuration_error")
            return {}
        return {k: v for k, v in dotenv_values(path, encoding="utf-8-sig", interpolate=False).items()
                if v is not None}
    except (OSError, UnicodeError):
        raise ClipError("configuration_error") from None


def load_settings() -> Settings:
    # dotenv_values neither mutates os.environ nor expands ${...} in passwords.
    gateway = _env_file(GATEWAY_ROOT / ".env")

    def option(name: str, default: str = "") -> str:
        return os.environ.get(name, gateway.get(name, default))

    path = Path(option("HOME_MCP_AOITALK_ENV_FILE", str(GATEWAY_ROOT.parent / "41_AoiTalk" / ".env"))).expanduser()
    if not path.is_absolute():
        path = GATEWAY_ROOT / path
    app = _env_file(path)
    username = option("AOITALK_BOOTSTRAP_ADMIN_USERNAME", app.get("AOITALK_BOOTSTRAP_ADMIN_USERNAME", ""))
    password = option("AOITALK_BOOTSTRAP_ADMIN_PASSWORD", app.get("AOITALK_BOOTSTRAP_ADMIN_PASSWORD", ""))
    if not username.strip() or not password:
        raise ClipError("configuration_error")
    raw_url = option("HOME_MCP_AOITALK_URL", "http://127.0.0.1:3000").strip().rstrip("/")
    try:
        url = httpx.URL(raw_url)
        loopback = url.host == "localhost"
        try:
            loopback = loopback or ipaddress.ip_address(url.host).is_loopback
        except ValueError:
            pass
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or url.query or url.fragment or url.path not in {"", "/"}
                or (url.scheme == "http" and not loopback)):
            raise ValueError("Invalid endpoint")
    except (ValueError, httpx.InvalidURL):
        raise ClipError("configuration_error") from None
    target = option("HOME_MCP_AOITALK_TARGET_NODE_ID").strip()
    try:
        target_id = _uuid(target) if target else None
    except ClipError:
        raise ClipError("configuration_error") from None
    return Settings(str(url).rstrip("/"), username.strip(), password, target_id)


def _error(exc: ClipError, **state: Any) -> dict[str, Any]:
    result = {"ok": False, "saved": False, "error": exc.code, "message": MESSAGES[exc.code], **state}
    if exc.http_status is not None:
        result["http_status"] = exc.http_status
    return result


def _safe_text(value: Any, settings: Settings, limit: int = 300) -> str:
    if not isinstance(value, str):
        return ""
    # Only selected display fields are returned; never echo raw HTTP/error bodies.
    for secret in (settings.password, settings.username):
        if secret:
            value = value.replace(secret, "[redacted]")
    value = re.sub(r"(?i)\b(?:bearer\s+|aoitpat_|sk-)[^\s]+", "[redacted]", value)
    return "".join(c for c in value if c >= " ")[:limit]


class Client:
    def __init__(self, settings: Settings):
        self.settings = settings
        # Do not forward local passwords through proxy env variables or redirects.
        self.http = httpx.AsyncClient(base_url=settings.base_url, trust_env=False,
                                      follow_redirects=False, timeout=REQUEST_TIMEOUT)

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with self.http.stream(method, path, **kwargs) as response:
                status = response.status_code
                if status not in {200, 202}:
                    code = {401: "authentication_failed", 403: "permission_denied", 404: "not_found",
                            409: "conflict", 429: "rate_limited"}.get(status, "server_error")
                    if 300 <= status < 400:
                        code = "configuration_error"
                    raise ClipError(code, status)
                parts: list[bytes] = []
                size = 0
                async for part in response.aiter_bytes():
                    size += len(part)
                    if size > MAX_RESPONSE_BYTES:
                        raise ClipError("invalid_response")
                    parts.append(part)
                data = json.loads(b"".join(parts))
                if not isinstance(data, dict):
                    raise ClipError("invalid_response")
                return data
        except (httpx.HTTPError, OSError):
            raise ClipError("connection_error") from None
        except (ValueError, UnicodeError):
            raise ClipError("invalid_response") from None

    async def login(self) -> None:
        data = await self.request("POST", "/api/auth/login", json={
            "username": self.settings.username, "password": self.settings.password,
            "credential_source": "local",
        })
        user = data.get("user")
        if data.get("authenticated") is not True or not isinstance(user, dict) or not self.http.cookies:
            raise ClipError("authentication_failed")
        if user.get("password_reset_required") is True:
            raise ClipError("password_reset_required")
        self._rescope_session_cookies()

    def _rescope_session_cookies(self) -> None:
        """Send the session cookie to the loopback API root.

        With ``AOITALK_PUBLIC_BASE_PATH`` (e.g. ``/at``) AoiTalk scopes its
        cookie to the public browser prefix, while this client talks to the
        FastAPI root directly. The value is unchanged and stays in memory.
        """
        jar = self.http.cookies.jar
        for cookie in list(jar):
            if cookie.path in ("", "/"):
                continue
            jar.clear(cookie.domain, cookie.path, cookie.name)
            self.http.cookies.set(cookie.name, cookie.value, domain=cookie.domain, path="/")


@asynccontextmanager
async def _client():
    client = Client(load_settings())
    try:
        await client.login()
        yield client
    finally:
        await client.http.aclose()


def _job(data: dict[str, Any], *, expected_id: str | None = None,
         expected_key: str | None = None, expected_source: str | None = None,
         expected_target: str | None = None) -> dict[str, Any]:
    try:
        job_id = _uuid(data.get("job_id") or data.get("id"))
    except ClipError:
        raise ClipError("invalid_response") from None
    if expected_id is not None and job_id != expected_id:
        raise ClipError("invalid_response")
    if data.get("status") not in STATES:
        raise ClipError("invalid_response")
    source_hash = data.get("source_sha256")
    if not isinstance(source_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", source_hash):
        raise ClipError("invalid_response")
    if expected_key is not None and data.get("idempotency_key") != expected_key:
        raise ClipError("idempotency_conflict")
    if expected_source is not None and source_hash != expected_source:
        raise ClipError("idempotency_conflict")
    if expected_target is not None and data.get("target_node_id") != expected_target:
        raise ClipError("idempotency_conflict")
    return {**data, "job_id": job_id}


async def _finish(client: Client, initial: dict[str, Any], wait_seconds: float,
                  *, expected_key: str | None = None,
                  expected_source: str | None = None, expected_target: str | None = None) -> dict[str, Any]:
    job = _job(initial, expected_key=expected_key, expected_source=expected_source, expected_target=expected_target)
    state: dict[str, Any] = {"job_id": job["job_id"], "status": job["status"],
                             "source_sha256": job["source_sha256"]}
    key = job.get("idempotency_key")
    if isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", key):
        state["idempotency_key"] = key
    deadline = time.monotonic() + wait_seconds
    try:
        while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
            await asyncio.sleep(min(POLL_INTERVAL, max(0, deadline - time.monotonic())))
            if time.monotonic() >= deadline:
                break
            data = await client.request("GET", f'/api/docs/ingest/jobs/{job["job_id"]}')
            job = _job(data, expected_id=state["job_id"], expected_key=expected_key,
                       expected_source=state["source_sha256"], expected_target=expected_target)
            state["status"] = job["status"]
        if job["status"] == "failed":
            # Errors can contain provider-specific data. Report only bounded identifiers.
            error = job.get("error") or job.get("error_json") or {}
            if isinstance(error, dict):
                code = _safe_text(error.get("code"), client.settings, 100)
                if re.fullmatch(r"[A-Za-z0-9._:-]{1,100}", code):
                    state["server_error_code"] = code
            return _error(ClipError("job_failed"), **state)
        if job["status"] != "succeeded":
            return {"ok": True, "saved": False, **state, "message": "受付済み・処理中です。aoitalk_clip_statusで確認してください。"}
        # Success of enqueue (202) is NOT success of persistence. Read both the
        # durable receipt and the node through the same authenticated ACL boundary.
        result = job.get("result") or job.get("result_json") or {}
        if not isinstance(result, dict):
            raise ClipError("verification_failed")
        try:
            node_id = _uuid(result.get("open_node_id"))
            receipt_id = _uuid(job.get("receipt_id") or result.get("receipt_id")
                               or result.get("clip_ingest_receipt_id"))
        except ClipError:
            raise ClipError("verification_failed") from None
        action = result.get("action")
        if action not in ACTIONS:
            raise ClipError("verification_failed")
        try:
            node_data = await client.request("GET", f"/api/docs/nodes/{node_id}")
            receipt_data = await client.request("GET", f"/api/docs/clip-ingest-receipts/{receipt_id}")
        except ClipError:
            raise ClipError("verification_failed") from None
        node, receipt = node_data.get("node"), receipt_data.get("receipt")
        if (not isinstance(node, dict) or node.get("id") != node_id
                or not isinstance(receipt, dict) or receipt.get("id") != receipt_id
                or receipt.get("topic_node_id") != node_id
                or receipt.get("source_sha256") != state["source_sha256"]
                or receipt.get("action") != action):
            raise ClipError("verification_failed")
        return {"ok": True, "saved": True, "verified": True, **state,
                "action": action, "node_id": node_id, "receipt_id": receipt_id,
                "node_title": _safe_text(node.get("title"), client.settings),
                "message": "既存クリップとの重複を確認しました。" if action == "duplicate_skip" else "AoiTalkへの保存と再読取を確認しました。"}
    except ClipError as exc:
        return _error(exc, **state)


async def aoitalk_clip_connection() -> dict[str, Any]:
    """Check the configured AoiTalk login and ClipIngest job read access; do not create clips.

    Uses operator-configured .env credentials internally; never return credentials or existing
    clip content. A successful check does not prove that the LLM worker works.
    """
    try:
        async with _client() as client:
            data = await client.request("GET", "/api/docs/ingest/jobs", params={"limit": 1})
            if not isinstance(data.get("jobs"), list):
                raise ClipError("invalid_response")
            if client.settings.target_node_id:
                data = await client.request("GET", f"/api/docs/nodes/{client.settings.target_node_id}")
                if not isinstance(data.get("node"), dict) or data["node"].get("id") != client.settings.target_node_id:
                    raise ClipError("invalid_response")
            return {"ok": True, "authenticated": True, "jobs_api_accessible": True,
                    "base_url": client.settings.base_url, "default_target_node_id": client.settings.target_node_id,
                    "external_research_default": False, "worker_write_tested": False}
    except ClipError as exc:
        return _error(exc)


async def aoitalk_clip_ingest(source: str, target_node_id: str | None = None,
                               enable_external_research: bool = False,
                               idempotency_key: str | None = None,
                               wait_seconds: float = 20) -> dict[str, Any]:
    """Save a prepared Markdown clip using AoiTalk's existing ClipIngest job API.

    This writes Docs and can invoke AoiTalk's configured LLM (not verbatim storage).
    Pass Markdown without the outer code fence. External research defaults OFF.
    Omit target_node_id to use the configured AoiTalk target. Identical source,
    target and flags reuse a deterministic key, including after lost responses.
    Keep source and key unchanged on retries; do not silently rephrase/resubmit.
    saved=true means job success AND receipt/node read-back; queued/running only
    mean accepted. Continue with aoitalk_clip_status, not another create call.
    """
    state: dict[str, Any] = {}
    post_attempted = False
    try:
        wait_seconds = _wait(wait_seconds)
        if (not isinstance(source, str) or not source.strip() or len(source) > 100_000
                or not isinstance(enable_external_research, bool)):
            raise ClipError("invalid_input")
        # Same newline canonicalization as AoiTalk; preserve all other characters.
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        settings = load_settings()
        target = _uuid(target_node_id) if target_node_id is not None else settings.target_node_id
        body = {"source": source, "upload_ids": [], "skip_image_recognition": True,
                "enable_external_research": enable_external_research, "target_node_id": target}
        fingerprint = json.dumps({"endpoint": settings.base_url, **body}, ensure_ascii=False,
                                 sort_keys=True, separators=(",", ":"))
        key = _key(idempotency_key) if idempotency_key is not None else "chatgpt-clip-v1-" + hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
        state = {"idempotency_key": key, "source_sha256": source_hash}
        client = Client(settings)
        try:
            await client.login()
            # Recover before POST. A timeout/5xx here must not fall through to a write.
            try:
                data = await client.request("GET", f"/api/docs/ingest/jobs/by-idempotency-key/{key}")
            except ClipError as exc:
                if exc.code != "not_found":
                    raise
                post_attempted = True
                data = await client.request("POST", "/api/docs/ingest/jobs",
                                            json=body, headers={"Idempotency-Key": key})
            return await _finish(client, data, wait_seconds, expected_key=key,
                                  expected_source=source_hash, expected_target=target)
        finally:
            await client.http.aclose()
    except ClipError as exc:
        return _error(exc, **state, submission_uncertain=post_attempted,
                      next_step="aoitalk_clip_status with the same idempotency_key" if post_attempted else "resolve_error")


async def aoitalk_clip_status(job_id: str | None = None, idempotency_key: str | None = None,
                               wait_seconds: float = 20) -> dict[str, Any]:
    """Read/wait for one ClipIngest job and verify its saved node/receipt; never create.

    Supply exactly one of job_id or idempotency_key. Use the key when the enqueue
    response was lost. Waiting is bounded (0-30s); queued/running are not saved.
    A failed job is not automatically retried or replaced with a new job.
    """
    state: dict[str, Any] = {}
    try:
        wait_seconds = _wait(wait_seconds)
        if (job_id is None) == (idempotency_key is None):
            raise ClipError("invalid_input")
        if job_id is not None:
            job_id = _uuid(job_id)
            state["job_id"] = job_id
            path = f"/api/docs/ingest/jobs/{job_id}"
        else:
            idempotency_key = _key(idempotency_key)
            state["idempotency_key"] = idempotency_key
            path = f"/api/docs/ingest/jobs/by-idempotency-key/{idempotency_key}"
        async with _client() as client:
            data = await client.request("GET", path)
            data = _job(data, expected_id=job_id, expected_key=idempotency_key)
            return await _finish(client, data, wait_seconds, expected_key=idempotency_key)
    except ClipError as exc:
        return _error(exc, **state)


TOOLS = [aoitalk_clip_connection, aoitalk_clip_ingest, aoitalk_clip_status]
