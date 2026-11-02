"""Log the broker's dedicated Edge profile in to the local AoiTalk without a password.

Runs the app's own issuer (frontend/scripts/issue-local-browser-session.mjs),
keeps the returned session token only in memory, and stores it as a persistent
cookie in the dedicated profile through the broker's bound Playwright endpoint.
Nothing secret is printed or logged. The cookie lives 7 days (the app's session
lifetime); run this again to renew it.

    python scripts/seed_dedicated_edge_login.py --user-id <uuid>
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from home_mcp_gateway import edge_cdp_broker as broker  # noqa: E402

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_DEFAULT_FRONTEND = Path(r"D:\Dev\41_AoiTalk\frontend")
_ORIGIN = "http://127.0.0.1:3002"


def _issue_cookie(frontend: Path, user_id: str) -> dict:
    result = subprocess.run(
        ["node", "scripts/issue-local-browser-session.mjs", "--user-id", user_id],
        cwd=frontend, capture_output=True, timeout=60, check=False,
    )
    if result.returncode != 0:
        code = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] or ["issue_failed"]
        raise SystemExit(f"issuer_failed: {code[0][:80]}")
    payload = json.loads(result.stdout.decode("utf-8"))
    if not isinstance(payload, dict) or not all(k in payload for k in ("name", "value", "path", "expires")):
        raise SystemExit("issuer_failed: unexpected_output")
    return payload


async def _seed(cookie: dict, verify_path: str) -> str:
    from playwright.async_api import async_playwright

    endpoint, _version = await broker.get_browser_endpoint(timeout_sec=45)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.connect(endpoint, timeout=20_000)
        try:
            context = browser.contexts[0] if browser.contexts else await browser.new_context()
            await context.add_cookies([{
                "name": cookie["name"], "value": cookie["value"],
                "domain": "127.0.0.1", "path": cookie["path"],
                "httpOnly": True, "secure": False, "sameSite": "Lax",
                "expires": int(cookie["expires"]),
            }])
            page = await context.new_page()
            try:
                await page.goto(f"{_ORIGIN}{verify_path}", wait_until="domcontentloaded", timeout=30_000)
                final_path = page.url.split("?", 1)[0].split("#", 1)[0]
            finally:
                await page.close()
        finally:
            await browser.close()
    return final_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--frontend", default=str(_DEFAULT_FRONTEND))
    parser.add_argument("--verify-path", default="/at/chat")
    args = parser.parse_args()
    if not _UUID.fullmatch(args.user_id):
        raise SystemExit("invalid --user-id")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]{1,64}", args.verify_path):
        raise SystemExit("invalid --verify-path")
    cookie = _issue_cookie(Path(args.frontend), args.user_id.lower())
    final_path = asyncio.run(_seed(cookie, args.verify_path))
    logged_in = "/login" not in final_path
    print(json.dumps({"seeded": True, "logged_in": logged_in, "landing_path": final_path,
                      "cookie_name": cookie["name"], "cookie_path": cookie["path"],
                      "expires": cookie["expires"]}))
    if not logged_in:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
