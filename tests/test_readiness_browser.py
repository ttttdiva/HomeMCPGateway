import asyncio
import base64
import io
import time
import unittest
from unittest.mock import patch

from PIL import Image
from home_mcp_gateway import browser_qa as browser, readiness
from qa_test_support import local_server


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_tcp_http_status_body_and_timeout(self):
        with local_server() as (url, port):
            self.assertTrue((await readiness.wait_tcp(port))["ready"])
            self.assertTrue((await readiness.wait_http(url, body_contains="Local QA"))["ready"])
            self.assertTrue((await readiness.wait_http(url + "/unavailable", expected_status=503))["ready"])
            failed = await readiness.wait_http(url, body_contains="absent marker", timeout_sec=.6, interval_sec=.05)
            self.assertFalse(failed["ready"])
            self.assertEqual(failed["last_response"]["status"], 200)
            started = time.monotonic()
            slow = await readiness.wait_http(url + "/slow", timeout_sec=.3)
            self.assertFalse(slow["ready"])
            self.assertLess(time.monotonic() - started, 1.5)
            prefix = await readiness.wait_http(url, max_body_bytes=8)
            self.assertTrue(prefix["body_truncated"])
            self.assertEqual(len(prefix["body_preview"]), 8)

    async def test_job_exit_and_closed_port(self):
        with patch.object(readiness, "job_status", return_value={"status": "exited", "exit_code": 1}):
            result = await readiness.wait_tcp(1, job_id="fixture")
            self.assertEqual(result["reason"], "job_not_running")
        import socket
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        try:
            result = await readiness.wait_tcp(sock.getsockname()[1], timeout_sec=.15, interval_sec=.05)
            self.assertFalse(result["ready"])
        finally:
            sock.close()


class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_ui_real_browser(self):
        try:
            started = await browser.browser_start()
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install the browser: python -m playwright install chromium")
            raise
        sid = started["session_id"]
        try:
            with local_server() as (url, _):
                self.assertEqual((await browser.browser_navigate(sid, url))["status"], 200)
                self.assertIn("Local QA", (await browser.browser_page_text(sid))["text"])
                self.assertEqual((await browser.browser_find(sid, "#name"))["count"], 1)
                await browser.browser_fill(sid, "#name", "Gateway")
                await browser.browser_keyboard(sid, text=" QA")
                await browser.browser_click(sid, "#go")
                self.assertEqual((await browser.browser_page_text(sid, "#result"))["text"], "Gateway QA")
                image = await browser.browser_screenshot(sid)
                raw = base64.b64decode(next(c for c in image.content if c.type == "image").data)
                with Image.open(io.BytesIO(raw)) as picture:
                    picture.load()
                    self.assertEqual(picture.size, (1280, 800))
                errors = await browser.browser_errors(sid)
                self.assertTrue(any(e["text"] == "qa console error" for e in errors["console"]))
                self.assertTrue(any("qa page error" in e for e in errors["page_errors"]))
                await browser.browser_errors(sid, clear=True)
                self.assertEqual((await browser.browser_errors(sid))["console"], [])
        finally:
            await browser.browser_close(sid)
        with self.assertRaises(ValueError):
            await browser.browser_page_text(sid)
