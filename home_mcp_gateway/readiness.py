"""Deadline-bounded readiness checks, optionally tied to a persisted job."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from .jobs import ACTIVE, job_status


async def _wait(probe, timeout_sec, interval_sec, job_id, runtime_dir):
    if timeout_sec < 0 or interval_sec <= 0:
        raise ValueError("timeout_sec must be nonnegative and interval_sec must be positive")
    start = time.monotonic()
    deadline = start + timeout_sec
    last = {}
    last_error = None
    while True:
        if job_id:
            job = await asyncio.to_thread(job_status, job_id, runtime_dir)
            if job["status"] not in ACTIVE:
                return {"ready": False, "reason": "job_not_running", "job": job, "elapsed_sec": time.monotonic() - start}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ready": False, "reason": "timeout", "last_response": last,
                    "last_error": last_error, "elapsed_sec": time.monotonic() - start}
        try:
            last = await asyncio.wait_for(probe(), timeout=min(2, remaining))
            if last["ready"]:
                if job_id:
                    job = await asyncio.to_thread(job_status, job_id, runtime_dir)
                    if job["status"] not in ACTIVE:
                        return {"ready": False, "reason": "job_not_running", "job": job}
                return {**last, "job_id": job_id, "elapsed_sec": time.monotonic() - start}
        except (OSError, asyncio.TimeoutError, httpx.HTTPError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        await asyncio.sleep(min(interval_sec, max(0, deadline - time.monotonic())))


async def wait_tcp(port: int, host: str = "127.0.0.1", timeout_sec: float = 30, interval_sec: float = .2,
                   job_id: str | None = None, runtime_dir: str | None = None) -> dict[str, Any]:
    """Wait for a TCP listener until a deadline; optionally fail early when the associated job exits. Does not prove listener ownership."""
    async def probe():
        _, writer = await asyncio.open_connection(host, port)
        writer.close()
        await writer.wait_closed()
        return {"ready": True, "host": host, "port": port}
    return await _wait(probe, timeout_sec, interval_sec, job_id, runtime_dir)


async def wait_http(url: str, expected_status: int = 200, body_contains: str = "", timeout_sec: float = 30,
                    interval_sec: float = .2, max_body_bytes: int = 16384, job_id: str | None = None,
                    runtime_dir: str | None = None) -> dict[str, Any]:
    """Wait for HTTP status and optional substring in the bounded UTF-8 body prefix. Reports last response on timeout; optional job must remain active."""
    if max_body_bytes <= 0:
        raise ValueError("max_body_bytes must be positive")
    async with httpx.AsyncClient(trust_env=False, follow_redirects=True) as client:
        async def probe():
            async with client.stream("GET", url) as response:
                raw = bytearray()
                async for chunk in response.aiter_bytes():
                    raw.extend(chunk[:max_body_bytes + 1 - len(raw)])
                    if len(raw) > max_body_bytes:
                        break
                body = bytes(raw[:max_body_bytes]).decode("utf-8", "replace")
                return {"ready": response.status_code == expected_status and body_contains in body,
                        "url": str(response.url), "status": response.status_code, "body_preview": body,
                        "body_truncated": len(raw) > max_body_bytes}
        return await _wait(probe, timeout_sec, interval_sec, job_id, runtime_dir)


TOOLS = [wait_tcp, wait_http]
