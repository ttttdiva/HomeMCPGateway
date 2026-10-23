"""Gateway-lifespan AoiTalk auth reuse tests. httpx.MockTransport only; no live AoiTalk."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import unittest
from unittest.mock import patch

import httpx

from home_mcp_gateway import aoitalk, aoitalk_agent_tasks as agent_tasks, aoitalk_auth, aoitalk_cli_sessions as cli_sessions
from home_mcp_gateway import aoitalk_tasks as tasks

REAL_CLIENT = httpx.AsyncClient
SETTINGS = aoitalk.Settings("http://127.0.0.1:3000", "fixture-user", "fixture-password")


class Fake:
    def __init__(self):
        self.logins = 0
        self.reads = 0
        self.login_status = 200
        self.login_headers = {}
        self.read_status = 200
        self.read_headers = {}
        self.read_body = {"detail": "x"}
        self.clients = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/login":
            self.logins += 1
            if self.login_status != 200:
                return httpx.Response(self.login_status, json={"detail": "no"}, headers=self.login_headers)
            return httpx.Response(200, json={"authenticated": True, "user": {"password_reset_required": False}},
                                  headers={"Set-Cookie": "session=fixture-cookie; Path=/; HttpOnly"})
        if "session=fixture-cookie" not in request.headers.get("cookie", ""):
            return httpx.Response(401, json={"detail": "login required"})
        self.reads += 1
        if self.read_status != 200:
            return httpx.Response(self.read_status, json=self.read_body, headers=self.read_headers)
        return httpx.Response(200, json={"enabled": True, "tasks": []})

    def factory(self, **kwargs):
        client = REAL_CLIENT(transport=httpx.MockTransport(self.handler), **kwargs)
        self.clients.append(client)
        return client


class AuthReuseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = Fake()
        self.settings = SETTINGS
        for p in (patch.object(aoitalk.httpx, "AsyncClient", side_effect=self.fake.factory),
                  patch.object(aoitalk, "load_settings", side_effect=lambda: self.settings)):
            p.start()
            self.addCleanup(p.stop)
        real_login = aoitalk.Client.login

        async def slow_login(client):  # MockTransport never yields; make logins really overlap
            await asyncio.sleep(0.02)
            await real_login(client)

        patcher = patch.object(aoitalk.Client, "login", slow_login)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.owner = await aoitalk_auth.activate()
        self.addAsyncCleanup(aoitalk_auth.deactivate)

    async def read(self):
        return await tasks.aoitalk_local_task_list()

    def expire_cooldowns(self):
        # Move the owner's own expiry into the past; never patch the shared monotonic clock
        # (asyncio timers, including slow_login's sleep, depend on it).
        for key in self.owner._cooldowns:
            self.owner._cooldowns[key] = (aoitalk_auth.time.monotonic() - 1, False)

    async def test_sequential_reads_log_in_once_and_read_fresh(self):
        for _ in range(3):
            self.assertTrue((await self.read())["ok"])
        self.assertEqual((self.fake.logins, self.fake.reads), (1, 6))  # list = status + tasks
        self.assertEqual(len(self.fake.clients), 1)

    async def test_concurrent_calls_single_flight(self):
        results = await asyncio.gather(*[self.read() for _ in range(12)])
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual((self.fake.logins, self.fake.reads), (1, 24))

    async def test_changed_identity_never_reuses_cookie_and_old_client_closes(self):
        await self.read()
        self.settings = aoitalk.Settings(SETTINGS.base_url, SETTINGS.username, "other-password")
        await self.read()
        self.assertEqual(self.fake.logins, 2)
        self.assertTrue(self.fake.clients[0].is_closed)
        self.assertFalse(self.fake.clients[1].is_closed)

    async def test_shutdown_closes_everything_and_falls_back_to_per_call(self):
        await self.read()
        await aoitalk_auth.deactivate()
        self.assertTrue(all(c.is_closed for c in self.fake.clients))
        await self.read()
        await self.read()
        self.assertEqual(self.fake.logins, 3)  # per-call mode: no owner, no reuse
        self.assertTrue(all(c.is_closed for c in self.fake.clients))

    async def test_other_event_loop_does_not_use_owner(self):
        result = await asyncio.to_thread(lambda: asyncio.run(self.read()))
        self.assertTrue(result["ok"])
        self.assertEqual(self.fake.logins, 1)
        self.assertIsNone(self.owner._entry)
        self.assertTrue(all(c.is_closed for c in self.fake.clients))

    async def test_401_invalidates_without_replay_then_next_call_logs_in(self):
        await self.read()
        self.fake.read_status = 401
        result = await self.read()
        self.assertEqual((result["error"], result["http_status"]), ("authentication_failed", 401))
        self.assertEqual(self.fake.reads, 3)  # first read of the call fails; nothing replayed
        self.assertEqual(self.fake.logins, 1)
        self.fake.read_status = 200
        self.assertTrue((await self.read())["ok"])
        self.assertEqual(self.fake.logins, 2)
        self.assertTrue(self.fake.clients[0].is_closed)

    async def test_403_stays_visible_and_keeps_session(self):
        await self.read()
        self.fake.read_status = 403
        self.assertEqual((await self.read())["error"], "permission_denied")
        self.fake.read_status = 200
        await self.read()
        self.assertEqual(self.fake.logins, 1)

    async def test_login_429_sets_cooldown_and_blocks_other_callers(self):
        self.fake.login_status, self.fake.login_headers = 429, {"Retry-After": "30"}
        first = await self.read()
        self.assertEqual((first["error"], first["http_status"], first["retry_after_seconds"]),
                         ("rate_limited", 429, 30))
        blocked = await asyncio.gather(*[self.read() for _ in range(5)])
        self.assertEqual(self.fake.logins, 1)
        for r in blocked:
            self.assertEqual(r["error"], "rate_limited")
            self.assertTrue(1 <= r["retry_after_seconds"] <= 30)
        self.expire_cooldowns()
        self.fake.login_status = 200
        self.assertTrue((await self.read())["ok"])
        self.assertEqual(self.fake.logins, 2)

    async def test_login_429_http_date_retry_after(self):
        when = datetime.now(timezone.utc) + timedelta(seconds=45)
        self.fake.login_status, self.fake.login_headers = 429, {"Retry-After": format_datetime(when, usegmt=True)}
        result = await self.read()
        self.assertTrue(40 <= result["retry_after_seconds"] <= 46)

    async def test_login_429_missing_or_malformed_header_holds_bounded_cooldown(self):
        for headers in ({}, {"Retry-After": "soon"}, {"Retry-After": "-5"}, {"Retry-After": "999999"}):
            await aoitalk_auth.activate()  # fresh owner: no cooldown carried between cases
            self.fake.login_status, self.fake.login_headers = 429, headers
            before = self.fake.logins
            first = await self.read()
            self.assertEqual(first["error"], "rate_limited")
            self.assertNotIn("retry_after_seconds", first)  # server gave no usable value
            for _ in range(3):  # consecutive calls must not log in again
                held = await self.read()
                self.assertEqual(held["error"], "rate_limited")
                # Internal hold, not a server delay: must not be reported as retry_after_seconds.
                self.assertNotIn("retry_after_seconds", held)
                self.assertTrue(1 <= held["local_cooldown_seconds"] <= aoitalk_auth.UNKNOWN_RETRY_AFTER_SECONDS)
            self.assertEqual(self.fake.logins, before + 1, headers)

    async def test_concurrent_calls_after_config_change_login_once_and_close_old(self):
        await self.read()
        self.settings = aoitalk.Settings(SETTINGS.base_url, SETTINGS.username, "other-password")
        results = await asyncio.gather(*[self.read() for _ in range(3)])
        self.assertTrue(all(r["ok"] for r in results))
        self.assertEqual(self.fake.logins, 2)  # initial + exactly one for the new identity
        self.assertEqual(len(self.fake.clients), 2)
        self.assertTrue(self.fake.clients[0].is_closed)
        await aoitalk_auth.deactivate()
        self.assertTrue(all(c.is_closed for c in self.fake.clients))

    async def test_429_cooldown_holds_even_when_body_overrides_error_code(self):
        await self.read()
        self.fake.read_status, self.fake.read_headers = 429, {"Retry-After": "30"}
        self.fake.read_body = {"error": "permission_denied"}
        first = await self.read()
        self.assertEqual(first["error"], "permission_denied")  # code meaning is not rewritten
        self.assertEqual((first["http_status"], first["retry_after_seconds"]), (429, 30))
        reads = self.fake.reads
        second = await self.read()
        self.assertEqual(second["error"], "rate_limited")
        self.assertEqual(self.fake.reads, reads)  # blocked without another HTTP request

    async def test_out_of_order_429_never_shortens_existing_hold(self):
        settings = SETTINGS
        self.owner.note_rate_limited(settings, 60)
        self.owner.note_rate_limited(settings, 1)  # late response with a shorter delay
        until, advertised = self.owner._cooldowns[(settings.base_url, settings.username)]
        self.assertTrue(advertised)
        self.assertGreater(until - aoitalk_auth.time.monotonic(), 58)
        self.owner.note_rate_limited(settings, 120)  # a longer one extends it
        self.assertGreater(self.owner._cooldowns[(settings.base_url, settings.username)][0] - until, 50)
        blocked = await self.read()
        self.assertEqual(blocked["error"], "rate_limited")
        self.assertGreater(blocked["retry_after_seconds"], 100)
        self.assertEqual(self.fake.logins, 0)

    async def test_shutdown_while_retiring_old_client_sends_no_new_login(self):
        await self.read()
        old = self.fake.clients[0]
        real_aclose = old.aclose

        async def slow_aclose():
            await asyncio.sleep(0.02)
            await real_aclose()

        old.aclose = slow_aclose
        self.settings = aoitalk.Settings(SETTINGS.base_url, SETTINGS.username, "other-password")
        task = asyncio.ensure_future(self.read())
        await asyncio.sleep(0.005)  # old client is being retired
        await aoitalk_auth.deactivate()
        result = await task
        self.assertFalse(result["ok"])
        self.assertEqual(self.fake.logins, 1)  # no login after shutdown began
        self.assertEqual(len(self.fake.clients), 1)
        self.assertTrue(old.is_closed)

    async def test_shutdown_during_login_never_installs_session(self):
        task = asyncio.ensure_future(self.read())
        await asyncio.sleep(0.005)  # login is in progress
        await aoitalk_auth.deactivate()
        result = await task
        self.assertFalse(result["ok"])
        self.assertIsNone(self.owner._entry)
        self.assertTrue(self.fake.clients and all(c.is_closed for c in self.fake.clients))
        self.assertEqual(self.fake.reads, 0)

    async def test_request_429_sets_cooldown_for_all_following_calls_without_requests(self):
        await self.read()
        self.fake.read_status, self.fake.read_headers = 429, {"Retry-After": "30"}
        result = await self.read()
        self.assertEqual((result["error"], result["retry_after_seconds"]), ("rate_limited", 30))
        reads = self.fake.reads
        self.assertEqual(reads, 3)  # failed status read stops the call; nothing replayed
        blocked = await asyncio.gather(*[self.read() for _ in range(4)])
        for r in blocked:
            self.assertEqual(r["error"], "rate_limited")
            self.assertTrue(1 <= r["retry_after_seconds"] <= 30)
        self.assertEqual((self.fake.reads, self.fake.logins), (reads, 1))  # no request, no login
        self.expire_cooldowns()
        self.fake.read_status = 200
        self.assertTrue((await self.read())["ok"])
        self.assertEqual(self.fake.logins, 1)  # session itself was kept

    async def test_request_429_without_usable_header_holds_bounded_cooldown(self):
        await self.read()
        self.fake.read_status, self.fake.read_headers = 429, {"Retry-After": "nope"}
        first = await self.read()
        self.assertNotIn("retry_after_seconds", first)
        reads = self.fake.reads
        second = await self.read()
        self.assertNotIn("retry_after_seconds", second)
        self.assertTrue(1 <= second["local_cooldown_seconds"] <= aoitalk_auth.UNKNOWN_RETRY_AFTER_SECONDS)
        self.assertEqual(self.fake.reads, reads)

    async def test_login_failure_shared_by_concurrent_waiters(self):
        self.fake.login_status = 401
        results = await asyncio.gather(*[self.read() for _ in range(6)])
        self.assertTrue(all(r["error"] == "authentication_failed" for r in results))
        self.assertEqual(self.fake.logins, 1)
        self.assertTrue(all(c.is_closed for c in self.fake.clients))

    async def test_no_secrets_in_results(self):
        self.fake.login_status, self.fake.login_headers = 429, {"Retry-After": "5"}
        text = repr(await self.read()) + repr(self.owner._entry) + repr(self.settings)
        for secret in ("fixture-password", "fixture-cookie"):
            self.assertNotIn(secret, text)


class AdapterRetryAfterTests(unittest.IsolatedAsyncioTestCase):
    """Every adapter's call() keeps a validated Retry-After on API 429 (no NameError)."""

    async def call_429(self, cls, error_cls, headers):
        transport = httpx.MockTransport(lambda request: httpx.Response(429, json={"detail": "slow"}, headers=headers))
        with patch.object(aoitalk.httpx, "AsyncClient",
                          side_effect=lambda **kw: REAL_CLIENT(transport=transport, **kw)):
            client = cls(SETTINGS)
            try:
                with self.assertRaises(error_cls) as ctx:
                    await client.call("GET", "/api/x")
            finally:
                await client.aclose()
        return ctx.exception

    async def test_429_retry_after_in_all_adapters(self):
        for cls, error_cls in ((tasks.TaskClient, tasks.TaskError),
                               (agent_tasks.AgentTaskClient, agent_tasks.AgentTaskError),
                               (cli_sessions.CliSessionClient, cli_sessions.CliSessionError)):
            exc = await self.call_429(cls, error_cls, {"Retry-After": "7"})
            self.assertEqual((exc.code, exc.http_status, exc.retry_after), ("rate_limited", 429, 7), cls)
            exc = await self.call_429(cls, error_cls, {})
            self.assertEqual((exc.code, exc.retry_after), ("rate_limited", None), cls)

    async def test_agent_tool_returns_structured_rate_limited(self):
        transport = httpx.MockTransport(lambda r: httpx.Response(
            200 if r.url.path == "/api/auth/login" else 429,
            json={"authenticated": True, "user": {}} if r.url.path == "/api/auth/login" else {"detail": "slow"},
            headers={"Set-Cookie": "session=c; Path=/"} if r.url.path == "/api/auth/login" else {"Retry-After": "9"}))
        with patch.object(aoitalk.httpx, "AsyncClient",
                          side_effect=lambda **kw: REAL_CLIENT(transport=transport, **kw)),                 patch.object(aoitalk, "load_settings", return_value=SETTINGS):
            try:
                await agent_tasks._session(lambda c: c.call("GET", "/api/x"))
            except agent_tasks.AgentTaskError as exc:
                result = agent_tasks._error(exc)
        self.assertEqual((result["error"], result["http_status"], result["retry_after_seconds"]),
                         ("rate_limited", 429, 9))


class RetryAfterParserTests(unittest.TestCase):
    def test_parse(self):
        p = aoitalk.parse_retry_after
        self.assertEqual(p("0"), 0)
        self.assertEqual(p(" 12 "), 12)
        for bad in (None, "", "1.5", "-1", "abc", "86401", "٣", "Mon, 01 Jan 2001 00:00:00"):
            self.assertIsNone(p(bad), bad)
        self.assertEqual(p("Thu, 01 Jan 1970 00:00:00 GMT"), 0)


if __name__ == "__main__":
    unittest.main()
