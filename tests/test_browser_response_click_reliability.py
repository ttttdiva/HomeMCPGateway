from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
import unittest

from home_mcp_gateway import browser_qa
from home_mcp_gateway.browser_agent import execute_browser_plan, safe_exception_summary


JSON_BODY = b'{"ok":true,"fixture":"json"}'


class SafeBrowserErrorTests(unittest.TestCase):
    def test_navigation_exception_summary_keeps_reason_and_drops_url_secrets(self):
        summary = safe_exception_summary(RuntimeError(
            "net::ERR_CONNECTION_RESET https://user:pass@example.test/path?token=canary#fragment cookie=secret"
        ))
        self.assertEqual(summary["type"], "RuntimeError")
        self.assertIn("net::ERR_CONNECTION_RESET", summary["message"])
        for private in ("user", "pass", "token", "canary", "fragment", "secret"):
            self.assertNotIn(private, summary["message"])


@contextmanager
def response_fixture_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            if self.path == "/json":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                body = JSON_BODY
            elif self.path == "/text":
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                body = b"plain-fixture-ok"
            elif self.path == "/redirect-json":
                self.send_response(302)
                self.send_header("Location", "/json")
                self.end_headers()
                return
            elif self.path == "/attachment":
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition", 'attachment; filename="gateway-fixture.txt"')
                body = b"discard-this-local-fixture"
            else:
                self.send_response(404)
                body = b"not found"
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


class FixtureJev:
    model = "jev-browser-fixture"
    input_tokens = 0
    output_tokens = 0

    def __init__(self):
        self.attempts = 0

    async def choose(self, *, instruction, candidates, **_kwargs):
        self.attempts += 1
        for candidate_id, candidate in candidates.items():
            label = " ".join(str(candidate.get(key, "")) for key in ("label", "text", "name"))
            if candidate.get("tag") == "button" and "Continue" in label:
                return SimpleNamespace(
                    choice=candidate_id,
                    confidence=0.99,
                    probabilities={candidate_id: 0.99, "none": 0.01},
                    model=self.model,
                )
        return SimpleNamespace(
            choice="none", confidence=0.99, probabilities={"none": 0.99}, model=self.model
        )


class BrowserNavigationResponseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.started = await browser_qa.browser_start(
                headless=True, browser_type="chromium", channel="msedge", width=1280, height=800
            )
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install Microsoft Edge to run browser navigation fixtures")
            raise
        self.session_id = self.started["session_id"]

    async def asyncTearDown(self):
        if getattr(self, "session_id", None):
            await browser_qa.browser_close(self.session_id)

    async def test_json_text_redirect_and_attachment_navigation_results(self):
        with response_fixture_server() as base:
            json_result = await browser_qa.browser_navigate(self.session_id, base + "/json")
            self.assertEqual(json_result["status"], 200)
            self.assertEqual(json_result["url"], base + "/json")

            text_result = await browser_qa.browser_navigate(self.session_id, base + "/text")
            self.assertEqual(text_result["status"], 200)
            self.assertEqual(text_result["url"], base + "/text")

            redirect_result = await browser_qa.browser_navigate(self.session_id, base + "/redirect-json")
            self.assertEqual(redirect_result["status"], 200)
            self.assertEqual(redirect_result["url"], base + "/json")

            attachment = await browser_qa.browser_navigate(self.session_id, base + "/attachment")
            self.assertEqual(attachment["code"], "browser_navigation_download")
            self.assertTrue(attachment["download_started"])

    async def test_run_plan_start_url_handles_json_redirect_and_download(self):
        with response_fixture_server() as base:
            for path in ("/json", "/text", "/redirect-json"):
                result = await browser_qa.browser_run_plan(
                    session_id=self.session_id,
                    start_url=base + path,
                    steps=[{"action": "done"}],
                    max_steps=1,
                )
                self.assertEqual(result["status"], "steps_completed", result)
                navigation = next(event for event in result["trace"] if event["event"] == "navigation_completed")
                self.assertEqual(navigation["status"], 200)

            attachment = await browser_qa.browser_run_plan(
                session_id=self.session_id,
                start_url=base + "/attachment",
                steps=[{"action": "done"}],
                max_steps=1,
            )
            self.assertEqual(attachment["status"], "failed")
            self.assertEqual(attachment["reason"], "browser_navigation_download")


class BrowserClickReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        try:
            self.started = await browser_qa.browser_start(
                headless=True, browser_type="chromium", channel="msedge", width=1280, height=800
            )
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install Microsoft Edge to run click fixtures")
            raise
        self.session_id = self.started["session_id"]

    async def asyncTearDown(self):
        if getattr(self, "session_id", None):
            await browser_qa.browser_close(self.session_id)

    async def execute_click(self, html, *, action_timeout_seconds=2):
        fake_jev = FixtureJev()
        async with browser_qa._session(self.session_id) as session:
            await session["page"].set_content(html)
            result = await execute_browser_plan(
                session,
                [{"action": "click", "instruction": "Continue", "expected_text": "clicked"}],
                completion_text="clicked",
                action_timeout_seconds=action_timeout_seconds,
                decision_client=fake_jev,
            )
            marker = await session["page"].locator("#result").inner_text()
        return result, marker, fake_jev

    async def test_click_waits_for_late_rendered_target(self):
        html = """<!doctype html><meta charset=utf-8><div id=root></div><output id=result>waiting</output>
        <script>setTimeout(()=>{const b=document.createElement('button');b.textContent='Continue';
        b.onclick=()=>document.querySelector('#result').textContent='clicked';document.querySelector('#root').appendChild(b)},350)</script>"""
        result, marker, fake_jev = await self.execute_click(html)
        self.assertEqual(result["status"], "completed", result)
        self.assertTrue(result["completion_verified"])
        self.assertEqual(marker, "clicked")
        self.assertGreaterEqual(fake_jev.attempts, 1)

    async def test_click_scrolls_offscreen_target_and_waits_for_transient_overlay(self):
        html = """<!doctype html><meta charset=utf-8><style>
        #spacer{height:1100px}.cover{position:fixed;inset:0;background:#888;z-index:2}
        </style><div id=spacer></div><button id=continue>Continue</button><output id=result>waiting</output>
        <div class=cover></div><script>setTimeout(()=>document.querySelector('.cover').remove(),250);
        document.querySelector('#continue').onclick=()=>document.querySelector('#result').textContent='clicked';</script>"""
        result, marker, _fake_jev = await self.execute_click(html)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(marker, "clicked")

    async def test_permanent_obstruction_returns_reason_without_repeating_click(self):
        html = """<!doctype html><meta charset=utf-8><style>
        .cover{position:fixed;inset:0;background:#888;z-index:2}
        </style><button id=continue>Continue</button><output id=result>waiting</output><div class=cover></div>
        <script>document.querySelector('#continue').onclick=()=>document.querySelector('#result').textContent='clicked';</script>"""
        result, marker, fake_jev = await self.execute_click(html, action_timeout_seconds=0.5)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "browser_element_obscured")
        self.assertEqual(marker, "waiting")
        self.assertEqual(fake_jev.attempts, 1)
        self.assertEqual(result["reason_detail"]["type"], "TimeoutError")
        self.assertNotIn("cover", result["reason_detail"]["message"])


if __name__ == "__main__":
    unittest.main()
