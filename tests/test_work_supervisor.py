"""Work supervisor fixture tests: no live AoiTalk, no network, no provider process.

The multi-chunk fixtures follow the real backend (41 cli_development_sessions._bounded_text_chunk): each
chunk's ``text_end`` is ITS OWN ``end``, not the end of the whole message."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from home_mcp_gateway import aoitalk, work_supervisor as ws

SETTINGS = aoitalk.Settings("http://127.0.0.1:3000", "fixture-user", "fixture-password")
S1 = "11111111-1111-4111-8111-111111111111"
S2 = "22222222-2222-4222-8222-222222222222"
T1 = "a" * 32
RUN_A = "aaaaaaaa-0000-4000-8000-00000000000a"
RUN_B = "bbbbbbbb-0000-4000-8000-00000000000b"
RUN_C = "cccccccc-0000-4000-8000-00000000000c"
M1, M2, M3 = ("a1111111-0000-4000-8000-000000000001", "a2222222-0000-4000-8000-000000000002",
              "a3333333-0000-4000-8000-000000000003")


class FakeClient:
    """Routes the read-only API surface; records every call for observer-only assertions."""

    def __init__(self, sessions=None, details=None, events=None, tasks=None, task_details=None, collect_hook=None,
                 runs=None, messages=None):
        self.settings = SETTINGS
        self.sessions = sessions or {}       # sid -> {provider,status}  (the latest-run PROJECTION)
        self.details = details or {}         # sid -> confirmed status
        self.events = events or {}           # sid -> list of events
        self.tasks = tasks or []
        self.task_details = task_details or {}
        self.collect_hook = collect_hook
        self.runs = runs or {}               # run_id -> {session_id,status,message_id,persisted}
        self.messages = messages or {}       # sid -> list of message dicts
        self.run_errors: dict[str, int] = {}     # run_id -> http status for GET /api/agent-runs/{id}
        self.list_errors: dict[str, int] = {}    # sid -> http status for the run list
        self.calls: list[tuple[str, str]] = []
        self.collect_limits: list[int] = []

    @staticmethod
    def run_dict(run_id, run):
        out = {"id": run_id, "session_id": run["session_id"], "status": run["status"],
               "created_at": "2026-10-22T14:00:00+00:00", "updated_at": "2026-10-22T14:51:57+00:00",
               "result": {"assistant_message_id": run["message_id"]} if run.get("message_id") else {},
               "validation": {"provider_output_persisted": True} if run.get("persisted") else {}}
        return out

    async def call(self, method, path, **kwargs):
        self.calls.append((method, path))
        params = kwargs.get("params") or {}
        if path == "/api/cli-sessions":
            items = [{"session_id": sid, **v} for sid, v in self.sessions.items() if v["status"] == params["status"]]
            return 200, {"sessions": items[: params["limit"]]}
        if path == "/api/cli-sessions/collect":
            body = kwargs["json"]
            self.collect_limits.append(body["limit_per_session"])
            if self.collect_hook:
                return 200, self.collect_hook(body)
            pages = []
            for sid in body["session_ids"]:
                evs = self.events.get(sid, [])
                cursor = body["cursors"].get(sid)
                start = int(cursor[1:]) if cursor else 0
                chunk = evs[start:start + body["limit_per_session"]]
                pages.append({"session_id": sid, "events": chunk, "next_cursor": f"c{start + len(chunk)}"})
            return 200, {"sessions": pages}
        m = re.fullmatch(r"/api/cli-sessions/([0-9a-f-]{36})", path)
        if m:
            if m[1] not in self.sessions:
                raise ws.cli.CliSessionError("not_found", 404)
            return 200, {"session_id": m[1], "status": self.details.get(m[1], self.sessions[m[1]]["status"])}
        m = re.fullmatch(r"/api/agent-runs/([0-9a-f-]{36})", path)
        if m:
            assert params == ws.RUN_GET_PARAMS
            if m[1] in self.run_errors:
                raise ws.cli.CliSessionError({403: "permission_denied", 404: "not_found"}.get(self.run_errors[m[1]], "server_error"), self.run_errors[m[1]])
            if m[1] not in self.runs:
                raise ws.cli.CliSessionError("not_found", 404)
            return 200, {"success": True, "agent_run": self.run_dict(m[1], self.runs[m[1]])}
        m = re.fullmatch(r"/api/conversations/([0-9a-f-]{36})/agent-runs", path)
        if m:
            if m[1] in self.list_errors:
                raise ws.cli.CliSessionError("permission_denied", 403)
            found = [self.run_dict(r, v) for r, v in self.runs.items() if v["session_id"] == m[1] and v["status"] == params["status"]]
            return 200, {"success": True, "session_id": m[1], "agent_runs": found[: params["limit"]]}
        m = re.fullmatch(r"/api/conversations/([0-9a-f-]{36})/messages", path)
        if m:
            return 200, {"success": True, "messages": self.messages.get(m[1], [])[: params["limit"]],
                         "server_time": "2026-10-22T14:52:00+00:00"}
        if path == "/api/local-agent/tasks":
            items = [t for t in self.tasks if t["status"] == params["status"]]
            return 200, {"tasks": items}
        m = re.fullmatch(r"/api/local-agent/tasks/([a-f0-9]{32})", path)
        if m:
            return 200, {"task": self.task_details.get(m[1]) or next(t for t in self.tasks if t["task_id"] == m[1])}
        raise AssertionError(f"unexpected call {method} {path}")


def event(n, kind="assistant_text", text="x"):
    return {"event_id": f"e{n}", "event_type": kind, "message": text}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.conn = ws.connect(self.dir)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(lambda: self.conn.close())
        patcher = mock.patch.object(ws, "RECOVERY_GRACE_SECONDS", 0.0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def reset(self):
        self.conn.close()
        self._tmp.cleanup()
        self.setUp()

    def fetcher(self, client, task_client=None, now=None):
        async def fetch(plan):
            with mock.patch("home_mcp_gateway.work_supervisor._fetch_jobs", new=_no_jobs):
                return await ws.fetch_all(plan, client, task_client or client, now or time.time())
        return fetch

    def poll(self, client, task_client=None, now=None, conn=None, directory=None):
        return asyncio.run(ws.poll_once(conn or self.conn, directory or self.dir, self.fetcher(client, task_client, now)))

    def count(self, table, where="1"):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]

    def codes(self, status):
        return [a["code"] for a in status["alerts"]]

    def fresh(self, name):
        return ws.connect(self.dir / name)


async def _no_jobs(out):
    out.jobs = {"active": 0, "errors": 0}
    out.source("jobs", ok=True)


_seq = [0]


def run_event(run, event_type, status=None, **payload):
    _seq[0] += 1
    return {"event_id": f"run:{_seq[0]}", "source": "run", "run_id": run, "work_item_id": None,
            "sequence": _seq[0], "event_type": event_type, "status": status, "message": None,
            "payload": {"cli_session_sequence": _seq[0], "error_code": None, **payload}}


def chunk(run, msg, index, count, start, text, *, end=None, text_end=None, truncated=False, status="completed"):
    """Real backend form: text_end is this chunk's own end."""
    end = start + len(text) if end is None else end
    return run_event(run, "cli.output", status, assistant_message_id=msg, chunk_index=index, chunk_count=count,
                     start=start, end=end, text=text, text_end=(end if text_end is None else text_end), text_truncated=truncated)


def done(run, msg=None):
    return run_event(run, "cli.completed", "completed", assistant_message_id=msg)


def handoff_ids(status):
    return sorted(h["handoff_id"] for h in status["ready_handoffs"])


def hid(run=RUN_A, msg=M1, sid=S1):
    return f"cli:{sid}:{run}:{msg}"


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def message(msg, sid, run, content, role="assistant"):
    return {"id": msg, "session_id": sid, "role": role, "content": content, "metadata": {"cli_development": True, "run_id": run},
            "deleted_at": None}


def receive(conn, *ids):
    rows = [conn.execute("SELECT handoff_id, result_digest FROM outbox WHERE handoff_id=?", (h,)).fetchone() for h in ids]
    return ws.record_receipt(conn, [f"{r[0]}={r[1]}" for r in rows], root_source_id="root-1", root_epoch=1)


class CrashAndReplay(Base):
    def client(self):
        return FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}},
                          events={S1: [event(1), event(2)]})

    def test_crash_between_fetch_and_commit_loses_nothing_and_replays_once(self):
        client = self.client()
        plan = ws.make_plan(self.conn)
        asyncio.run(self.fetcher(client)(plan))          # fetched, process "dies" before commit
        self.assertEqual(self.count("inbox"), 0)
        self.assertEqual(self.count("snapshots"), 0)
        self.poll(client)                                 # restart: same page is fetched again
        self.assertEqual(self.count("inbox"), 2)
        self.assertEqual(self.conn.execute("SELECT cursor FROM sessions").fetchone()[0], "c2")

    def test_crash_inside_commit_rolls_back_events_cursor_and_snapshot(self):
        client = self.client()
        with mock.patch.object(ws, "_create_handoffs", side_effect=sqlite3.OperationalError("boom")):
            with self.assertRaises(sqlite3.OperationalError):
                self.poll(client)
        self.assertEqual((self.count("inbox"), self.count("snapshots"), self.count("sessions")), (0, 0, 0))
        self.poll(client)
        self.assertEqual(self.count("inbox"), 2)

    def test_duplicate_replay_is_exactly_once_insertion(self):
        client = self.client()
        fetched = asyncio.run(self.fetcher(client)(ws.make_plan(self.conn)))
        ws.commit(self.conn, fetched, {})
        again = ws.commit(self.conn, fetched, {})
        self.assertEqual(again["new_events"], 0)
        self.assertEqual(self.count("inbox"), 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("INSERT INTO inbox(session_id,event_id,kind,body_json,received_at) VALUES(?,?,?,?,?)",
                              (S1, "e1", "info", "{}", 0))

    def test_handoff_and_outbox_are_created_together_and_replay_adds_nothing(self):
        client = FakeClient(sessions={S1: {"provider": "codex-cli", "status": "completed"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "done"), done(RUN_A, M1)]})
        fetched = asyncio.run(self.fetcher(client)(ws.make_plan(self.conn)))
        ws.commit(self.conn, fetched, {})
        self.assertEqual((self.count("handoffs"), self.count("outbox")), (1, 1))
        ws.commit(self.conn, fetched, {})
        self.assertEqual((self.count("handoffs"), self.count("outbox")), (1, 1))
        with mock.patch.object(ws, "_settle_runs", side_effect=sqlite3.OperationalError("boom")):
            sub = self.fresh("crash")
            self.addCleanup(sub.close)
            with self.assertRaises(sqlite3.OperationalError):
                ws.commit(sub, fetched, {})
        self.assertEqual((sub.execute("SELECT COUNT(*) FROM handoffs").fetchone()[0], sub.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]), (0, 0))


class TruncatedPages(Base):
    def hook(self, huge_always):
        big = event(2, text="あ" * 30_000)
        small = event(1)

        def collect(body):
            limit = body["limit_per_session"]
            if body["cursors"].get(S1) == "c1":
                chunk_ = [big] if huge_always else []
            else:
                chunk_ = [small, big][:limit]
            return {"sessions": [{"session_id": S1, "events": chunk_, "next_cursor": f"c{len(chunk_)}"}]}
        return collect

    def test_smaller_pages_deliver_without_losing_cursor(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}}, collect_hook=self.hook(False))
        self.poll(client)
        self.assertEqual(client.collect_limits[0], 100)
        self.assertLess(min(client.collect_limits), 100)
        self.assertEqual(self.count("inbox"), 1)           # only the complete event was persisted
        self.assertEqual(self.conn.execute("SELECT cursor FROM sessions").fetchone()[0], "c1")

    def test_permanent_truncation_never_moves_cursor_or_stores_clipped_text(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}}, collect_hook=self.hook(True))
        self.poll(client)                                  # reaches c1 via the small page
        status = self.poll(client)                         # now only the oversized event is next
        row = self.conn.execute("SELECT cursor, cursor_stalled, drained FROM sessions").fetchone()
        self.assertEqual((row[0], row[1], row[2]), ("c1", 1, 0))
        self.assertEqual(self.count("inbox"), 1)
        alert = next(a for a in status["alerts"] if a["code"] == "cursor_stalled_truncated_page")
        self.assertEqual(alert["reason"], "page_truncated")
        self.assertFalse(status["source_completeness"]["cli_collect"]["complete"])


class CollectPageIntegrity(Base):
    """Item errors, missing pages and malformed pages are never successful pages."""

    def prime(self, hook):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}}, events={S1: [event(1)]})
        self.poll(client)                                   # cursor c1
        self.assertEqual(self.conn.execute("SELECT cursor FROM sessions").fetchone()[0], "c1")
        client.collect_hook = hook
        return client

    def assert_not_advanced(self, status, code):
        row = self.conn.execute("SELECT cursor, drained, collect_error FROM sessions").fetchone()
        self.assertEqual((row["cursor"], row["drained"]), ("c1", 0))
        self.assertEqual(self.count("inbox"), 1)
        self.assertIn(code, self.codes(status))
        self.assertFalse(status["source_completeness"]["cli_collect"]["complete"])

    def test_item_level_error_is_incomplete_per_item(self):
        client = self.prime(lambda body: {"sessions": [{"session_id": S1, "events": [], "error": {"code": "permission_denied"}}]})
        status = self.poll(client)
        self.assert_not_advanced(status, "collect_error:permission_denied")
        self.assertEqual(status["source_completeness"]["cli_collect"]["error"], "permission_denied")
        alert = next(a for a in status["alerts"] if a["code"] == "collect_error:permission_denied")
        self.assertEqual(alert["subject"], f"cli:{S1}")

    def test_unsafe_item_error_text_is_replaced_by_a_fixed_code(self):
        client = self.prime(lambda body: {"sessions": [{"session_id": S1, "events": [], "error": {"code": "Bad Code: password=fixture-password"}}]})
        status = self.poll(client)
        self.assertNotIn("fixture-password", json.dumps(status))
        self.assertIn("collect_error:item_error", self.codes(status))

    def test_missing_requested_page(self):
        client = self.prime(lambda body: {"sessions": []})
        self.assert_not_advanced(self.poll(client), "collect_error:page_missing")

    def test_malformed_pages_never_move_the_cursor(self):
        cases = {
            "events_not_list": {"session_id": S1, "events": "oops", "next_cursor": "c9"},
            "event_not_object": {"session_id": S1, "events": ["x"], "next_cursor": "c9"},
            "event_identity_missing": {"session_id": S1, "events": [{"event_type": "cli.output"}], "next_cursor": "c9"},
            "events_without_cursor": {"session_id": S1, "events": [event(5)]},
        }
        for name, page in cases.items():
            with self.subTest(name):
                self.reset()
                client = self.prime(lambda body, page=page: {"sessions": [page]})
                status = self.poll(client)
                self.assert_not_advanced(status, "cursor_stalled_truncated_page")
                self.assertEqual(next(a for a in status["alerts"] if a["code"] == "cursor_stalled_truncated_page")["reason"], "page_malformed")

    def test_whole_response_malformed(self):
        client = self.prime(lambda body: {"sessions": "nope"})
        self.assert_not_advanced(self.poll(client), "collect_error:malformed_response")

    def test_recovery_after_the_error_clears_the_alert_and_advances(self):
        client = self.prime(lambda body: {"sessions": [{"session_id": S1, "events": [], "error": {"code": "permission_denied"}}]})
        self.poll(client)
        client.collect_hook = None
        client.events[S1].append(event(2))
        status = self.poll(client)
        self.assertEqual(self.conn.execute("SELECT cursor, collect_error FROM sessions").fetchone()[:], ("c2", None))
        self.assertNotIn("collect_error:permission_denied", self.codes(status))


class ResponseBudget(Base):
    def test_listing_is_sanitized_per_item_not_cut_by_the_shared_budget(self):
        ids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(8)]
        client = FakeClient(sessions={sid: {"provider": "claude-cli", "status": "running", "b1": "x" * 10_000, "b2": "y" * 10_000} for sid in ids})
        data = asyncio.run(ws.observe(client, "GET", "/api/cli-sessions", params={"limit": 200, "status": "running"}))
        self.assertEqual(len(data["sessions"]), 8)                  # 8 x 20 KB > 64 KB, none dropped
        self.assertNotIn("output_truncated", data)
        status = self.poll(client)
        self.assertTrue(status["source_completeness"]["cli_list:running"]["complete"])
        self.assertEqual(status["providers"]["claude-cli"]["session_projection"]["running"], 8)

    def test_a_clipped_listing_item_still_marks_the_list_incomplete_and_secrets_stay_redacted(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running", "blob": "x" * 70_000,
                                           "note": "password=fixture-password fixture-user"}})
        data = asyncio.run(ws.observe(client, "GET", "/api/cli-sessions", params={"limit": 200, "status": "running"}))
        self.assertTrue(data["output_truncated"])
        self.assertNotIn("fixture-password", json.dumps(data))
        status = self.poll(client)
        self.assertFalse(status["source_completeness"]["cli_list:running"]["complete"])

    def test_terminal_lists_use_a_smaller_limit(self):
        client = FakeClient()
        self.poll(client)
        self.assertEqual(ws.TERMINAL_LIST_LIMIT, 100)

    def test_collect_pages_dropped_by_the_response_budget_are_retried_smaller_not_errors(self):
        def collect(body):
            group = body["session_ids"]
            if len(group) > 1:                                      # the big response was clipped: pages silently dropped
                return {"sessions": [], "output_truncated": True}
            return {"sessions": [{"session_id": group[0], "events": [event(1)], "next_cursor": "c1"}]}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"},
                                      S2: {"provider": "claude-cli", "status": "running"}}, collect_hook=collect)
        status = self.poll(client)
        self.assertEqual(self.count("inbox"), 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM sessions WHERE collect_error IS NOT NULL").fetchone()[0], 0)
        self.assertNotIn("collect_error:page_missing", self.codes(status))


class BacklogRotation(Base):
    def test_terminal_history_is_collected_in_bounded_fair_rotation_while_active_sessions_always_are(self):
        done_ids = [f"{i:08d}-2222-4222-8222-222222222222" for i in range(7)]
        sessions = {sid: {"provider": "codex-cli", "status": "completed"} for sid in done_ids}
        sessions[S1] = {"provider": "claude-cli", "status": "running"}
        client = FakeClient(sessions=sessions, events={sid: [event(1)] for sid in done_ids + [S1]})
        seen: list[set[str]] = []
        with mock.patch.object(ws, "BACKLOG_PER_POLL", 3):
            for _ in range(9):
                self.poll(client)
                got = {sid for sid, in self.conn.execute("SELECT session_id FROM inbox")}
                seen.append(got)
        self.assertIn(S1, seen[0])                                       # active: collected from the first poll
        self.assertEqual(len(seen[0]) - 1, 3)                            # only 3 history sessions in the first poll
        self.assertTrue(set(done_ids) <= seen[-1])                       # everything is drained within a few polls
        status = ws.build_status(self.conn, time.time())
        self.assertEqual(self.count("sessions", "drained=0 AND status='completed'"), 0)
        self.assertIsNotNone(status)


class CollectIsolation(Base):
    def test_one_failing_session_does_not_hide_the_others(self):
        def collect(body):
            if S2 in body["session_ids"]:
                raise ws.cli.CliSessionError("server_error", 500)
            return {"sessions": [{"session_id": sid, "events": [event(1)], "next_cursor": "c1"} for sid in body["session_ids"]]}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"},
                                      S2: {"provider": "claude-cli", "status": "running"}}, collect_hook=collect)
        status = self.poll(client)
        self.assertEqual(self.count("inbox", f"session_id='{S1}'"), 1)
        self.assertEqual(self.conn.execute("SELECT cursor FROM sessions WHERE session_id=?", (S2,)).fetchone()[0], None)
        self.assertFalse(status["source_completeness"]["cli_collect"]["complete"])
        self.assertIn("collect_error:server_error", self.codes(status))


class ChunkContract(Base):
    """The real backend chunk format and every incomplete variant."""

    def client(self, events, status="completed", runs=None):
        return FakeClient(sessions={S1: {"provider": "codex-cli", "status": status}}, events={S1: events}, runs=runs)

    def reason(self, events):
        sub = self.fresh(f"c{_seq[0]}")
        self.addCleanup(sub.close)
        status = asyncio.run(ws.poll_once(sub, self.dir, self.fetcher(self.client(events + [done(RUN_A, M1)]))))
        self.assertEqual(status["ready_handoffs"], [])
        alert = next(a for a in status["alerts"] if a["code"] == "result_provenance_incomplete")
        return alert["items"][0]["reason"]

    def test_real_two_chunk_message_assembles(self):
        status = self.poll(self.client([chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 2, "cd"), done(RUN_A, M1)]))
        self.assertEqual(handoff_ids(status), [hid()])
        self.assertEqual(self.conn.execute("SELECT result_text, result_source FROM handoffs").fetchone()[:], ("abcd", "cli_output_chunks"))
        self.assertEqual(status["delivery"]["pending"], 1)

    def test_out_of_order_arrival_and_late_output(self):
        a, b = chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 2, "cd")
        client = self.client([b, done(RUN_A, M1)])
        status = self.poll(client)
        self.assertEqual(status["ready_handoffs"], [])                      # chunk 0 missing: UNKNOWN
        self.assertIn("result_provenance_incomplete", self.codes(status))
        client.events[S1].append(a)                                          # late output
        status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid()])
        self.assertEqual(self.conn.execute("SELECT result_text FROM handoffs").fetchone()[0], "abcd")
        self.assertNotIn("result_provenance_incomplete", self.codes(status))

    def test_unicode_is_measured_in_code_points(self):
        text = "日本語😀ok𝔘"
        half = 3
        status = self.poll(self.client([chunk(RUN_A, M1, 0, 2, 0, text[:half]), chunk(RUN_A, M1, 1, 2, half, text[half:]), done(RUN_A, M1)]))
        self.assertEqual(handoff_ids(status), [hid()])
        self.assertEqual(self.conn.execute("SELECT result_text FROM handoffs").fetchone()[0], text)
        # a UTF-16 / byte based range is not accepted
        wrong = self.reason([chunk(RUN_A, M1, 0, 1, 0, "😀", end=2)])
        self.assertEqual(wrong, "chunk_range_invalid")

    def test_every_incomplete_variant_is_unknown(self):
        cases = {
            "chunks_missing": [chunk(RUN_A, M1, 0, 2, 0, "ab")],
            "chunk_count_invalid": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 3, 2, "cd")],
            "chunk_gap": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 3, "cd")],
            "chunk_gap_overlap": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 1, "bc")],
            "chunk_gap_reversed": [chunk(RUN_A, M1, 0, 2, 2, "cd"), chunk(RUN_A, M1, 1, 2, 0, "ab")],
            "text_end_mismatch": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 2, "cd", text_end=99)],
            "chunk_text_truncated": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 2, "cd", truncated=True)],
            "chunk_range_invalid": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 1, 2, 2, "cd", end=9, text_end=9)],
            "chunk_fields_missing": [chunk(RUN_A, M1, 0, 2, 0, "ab"), run_event(RUN_A, "cli.output", assistant_message_id=M1, chunk_index=1)],
            "chunk_index_invalid": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 5, 2, 2, "cd")],
            "chunk_conflict": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 0, 2, 0, "xy"), chunk(RUN_A, M1, 1, 2, 2, "cd")],
            "chunk_conflict_range": [chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 0, 3, 0, "ab"), chunk(RUN_A, M1, 1, 2, 2, "cd")],
            "legacy_total_text_end": [chunk(RUN_A, M1, 0, 2, 0, "ab", text_end=4), chunk(RUN_A, M1, 1, 2, 2, "cd", text_end=4)],
        }
        expected = {"chunk_gap_overlap": "chunk_gap", "chunk_gap_reversed": "chunk_gap", "chunk_conflict_range": "chunk_count_invalid",
                    "legacy_total_text_end": "text_end_mismatch"}
        for name, events in cases.items():
            with self.subTest(name):
                self.assertEqual(self.reason(events), expected.get(name, name))

    def test_redacted_text_no_longer_matches_its_range_and_is_reported_as_such(self):
        self.assertEqual(self.reason([chunk(RUN_A, M1, 0, 1, 0, "key [redacted] end", end=30)]), "chunk_text_redacted")

    def test_identical_duplicate_chunk_is_not_a_conflict(self):
        status = self.poll(self.client([chunk(RUN_A, M1, 0, 2, 0, "ab"), chunk(RUN_A, M1, 0, 2, 0, "ab"),
                                        chunk(RUN_A, M1, 1, 2, 2, "cd"), done(RUN_A, M1)]))
        self.assertEqual(handoff_ids(status), [hid()])

    def test_chunk_without_message_provenance_is_flagged(self):
        broken = run_event(RUN_A, "cli.output", "completed", chunk_index=0, chunk_count=1, start=0, end=1, text="x", text_end=1, text_truncated=False)
        self.assertEqual(self.reason([broken]), "no_message_provenance")

    def test_oversized_message_is_not_assumed_to_exceed_12000_chars(self):
        text = "x" * 4096                        # backend chunk max_chars is 4096, so 12000 is not a required size
        status = self.poll(self.client([chunk(RUN_A, M1, 0, 2, 0, text), chunk(RUN_A, M1, 1, 2, 4096, "tail"), done(RUN_A, M1)]))
        self.assertEqual(len(self.conn.execute("SELECT result_text FROM handoffs").fetchone()[0]), 4100)
        self.assertEqual(handoff_ids(status), [hid()])


class Telemetry(Base):
    def test_normal_telemetry_is_neither_output_nor_corruption(self):
        events = [run_event(RUN_A, "run.queued", "queued"), run_event(RUN_A, "cli.invocation"),
                  run_event(RUN_A, "cli.assistant_text", None, text="thinking aloud"),
                  run_event(RUN_A, "cli.tool_start"), run_event(RUN_A, "cli.tool_end", "completed"),
                  run_event(RUN_A, "cli.status_update", "running")]
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}}, events={S1: events},
                            runs={RUN_A: {"session_id": S1, "status": "running"}})
        status = self.poll(client)
        self.assertEqual(self.count("inbox", "kind='output'"), 0)
        self.assertEqual(status["ready_handoffs"], [])
        self.assertNotIn("result_provenance_incomplete", self.codes(status))
        self.assertNotIn("terminal_without_result", self.codes(status))
        runs = ws.analyze_session(self.conn, S1)
        self.assertEqual(runs[RUN_A]["state"], "running")             # a completed TOOL does not complete the run

    def test_telemetry_only_run_is_terminal_without_result_not_a_phantom_result(self):
        events = [run_event(RUN_A, "cli.assistant_text", None, text="phantom"), done(RUN_A)]
        status = self.poll(FakeClient(sessions={S1: {"provider": "codex-cli", "status": "completed"}}, events={S1: events}))
        self.assertEqual(status["ready_handoffs"], [])
        self.assertIn("terminal_without_result", self.codes(status))
        self.assertNotIn("result_provenance_incomplete", self.codes(status))


class RunReconciliation(Base):
    """The session projection shows only the latest run; known runs are reconciled with ACL'd GETs."""

    def two_runs(self):
        runs = {RUN_A: {"session_id": S1, "status": "running"}, RUN_B: {"session_id": S1, "status": "queued"}}
        return FakeClient(sessions={S1: {"provider": "claude-cli", "status": "queued"}},    # latest projection = B
                          events={S1: [run_event(RUN_A, "cli.tool_start"), run_event(RUN_B, "run.queued", "queued")]}, runs=runs)

    def test_old_running_run_and_new_queued_run_are_distinguished(self):
        client = self.two_runs()
        status = self.poll(client)
        mc = status["minClaude"]
        self.assertEqual(status["providers"]["claude-cli"]["session_projection"]["running"], 0)    # projection says 0 running
        self.assertEqual(status["providers"]["claude-cli"]["session_projection"]["queued"], 1)
        self.assertEqual((status["providers"]["claude-cli"]["reconciled_runs"]["running"],
                          status["providers"]["claude-cli"]["reconciled_runs"]["queued"]), (1, 1))
        self.assertEqual((mc["running"], mc["queued_not_counted"], mc["deficit"]), (1, 1, 7))
        self.assertTrue(mc["exact"], mc["not_exact_reasons"])
        self.assertEqual(status["queued_followups"], [{"session_id": S1, "provider": "claude-cli",
                                                       "queued_run_id": RUN_B, "behind_run_id": RUN_A}])
        self.assertTrue(any(c == ("GET", f"/api/conversations/{S1}/agent-runs") for c in client.calls))

    def test_run_get_reconciles_a_known_run_that_no_list_shows(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "queued"}},
                            events={S1: [run_event(RUN_A, "cli.tool_start")]},
                            runs={RUN_A: {"session_id": S1, "status": "succeeded"}})
        self.poll(client)
        self.poll(client)
        self.assertTrue(any(c == ("GET", f"/api/agent-runs/{RUN_A}") for c in client.calls))
        self.assertEqual(self.conn.execute("SELECT status, settled FROM runs").fetchone()[:], ("succeeded", 1))   # nothing persisted to recover

    def test_failed_reconciliation_is_unknown_not_exact_and_not_invented(self):
        client = self.two_runs()
        client.list_errors[S1] = 403
        status = self.poll(client)
        mc = status["minClaude"]
        self.assertFalse(mc["exact"])
        self.assertEqual(mc["running"], 0)                           # nothing proven: lower bound, not the projection
        self.assertTrue(mc["deficit_is_upper_bound"])
        self.assertIn("run_list_source_incomplete", mc["not_exact_reasons"])
        self.assertIn("source_error:permission_denied", self.codes(status))

    def test_terminal_list_error_alone_makes_exact_false(self):
        class Boom(FakeClient):
            async def call(self, method, path, **kw):
                if path == "/api/cli-sessions" and kw["params"]["status"] == "completed":
                    raise ws.cli.CliSessionError("server_error", 500)
                return await super().call(method, path, **kw)
        client = Boom(sessions={S1: {"provider": "claude-cli", "status": "queued"}})
        status = self.poll(client)
        self.assertTrue(status["source_completeness"]["cli_list:running"]["complete"])
        self.assertEqual(status["source_completeness"]["cli_list"]["error"], "server_error")
        self.assertFalse(status["minClaude"]["exact"])
        self.assertIn("cli_list_error", status["minClaude"]["not_exact_reasons"])
        self.assertTrue(status["minClaude"]["deficit_is_upper_bound"])

    def test_rejected_run_get_demotes_a_running_run_immediately(self):
        for code in (403, 404):
            with self.subTest(code):
                self.reset()
                client = self.two_runs()
                self.poll(client)
                self.assertEqual(self.poll(client)["minClaude"]["running"], 1)
                self.conn.execute("UPDATE runs SET observed_at=observed_at-100")     # due for a re-check
                client.run_errors[RUN_A] = code
                client.runs.pop(RUN_B)
                client.list_errors[S1] = 403                                          # the list cannot vouch for it either
                with mock.patch.object(ws, "RUN_RECHECK_SECONDS", 0.0):
                    status = self.poll(client)
                self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0], "unknown")
                self.assertEqual(status["minClaude"]["running"], 0)
                self.assertFalse(status["minClaude"]["exact"])
                self.assertGreaterEqual(status["minClaude"]["unknown_runs"], 1)

    def test_binding_mismatch_demotes_a_known_running_run(self):
        client = self.two_runs()
        self.poll(client)
        client.runs[RUN_A]["session_id"] = S2
        client.list_errors[S1] = 403
        with mock.patch.object(ws, "RUN_RECHECK_SECONDS", 0.0):
            status = self.poll(client)
        self.assertEqual(self.conn.execute("SELECT status, check_error FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[:], ("unknown", "binding_mismatch"))
        self.assertEqual(status["minClaude"]["running"], 0)

    def test_stale_run_state_becomes_unknown(self):
        client = self.two_runs()
        self.poll(client)
        later = ws.build_status(self.conn, time.time() + ws.RUN_FRESH_SECONDS + 5)
        mc = later["minClaude"]
        self.assertFalse(mc["exact"])
        self.assertEqual(mc["running"], 0)
        self.assertEqual(mc["unknown_runs"], 2)
        self.assertIn("unreconciled_runs", mc["not_exact_reasons"])

    def test_run_that_left_the_list_is_unknown_until_run_get_says_otherwise(self):
        client = self.two_runs()
        self.poll(client)
        client.runs[RUN_A]["status"] = "succeeded"
        self.poll(client)                                             # list no longer shows A -> unknown, then GET
        status = self.poll(client)
        self.assertEqual(self.conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0], "succeeded")
        self.assertEqual(status["minClaude"]["running"], 0)

    def test_denied_or_missing_run_get_stays_unknown_and_backs_off(self):
        for code in (403, 404):
            with self.subTest(code):
                self.reset()
                client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}},
                                    events={S1: [run_event(RUN_A, "cli.tool_start")]})
                client.run_errors[RUN_A] = code
                self.poll(client)
                status = self.poll(client)
                row = self.conn.execute("SELECT status, check_error, next_check_at FROM runs").fetchone()
                self.assertEqual(row["status"], "unknown")
                self.assertIn(row["check_error"], ("permission_denied", "not_found"))
                self.assertGreater(row["next_check_at"], time.time() + 1000)
                calls = [c for c in client.calls if c[1] == f"/api/agent-runs/{RUN_A}"]
                self.poll(client)
                self.assertEqual(len([c for c in client.calls if c[1] == f"/api/agent-runs/{RUN_A}"]), len(calls))   # backed off
                self.assertFalse(status["minClaude"]["exact"])
                self.assertEqual(status["ready_handoffs"], [])

    def test_binding_mismatch_is_rejected(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}},
                            events={S1: [run_event(RUN_A, "cli.tool_start")]},
                            runs={RUN_A: {"session_id": S2, "status": "succeeded"}})
        self.poll(client)
        self.poll(client)
        row = self.conn.execute("SELECT status, check_error FROM runs").fetchone()
        self.assertEqual((row["status"], row["check_error"]), ("unknown", "binding_mismatch"))

    def test_bounded_run_list_is_a_lower_bound(self):
        runs = {f"{i:08d}-0000-4000-8000-000000000000": {"session_id": S1, "status": "running"} for i in range(3)}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}}, runs=runs)
        with mock.patch.object(ws, "RUN_LIST_LIMIT", 3):
            status = self.poll(client)
        self.assertFalse(status["minClaude"]["exact"])
        self.assertIn("run_list_source_incomplete", status["minClaude"]["not_exact_reasons"])

    def test_check_budget_rotates_fairly(self):
        ids = [f"{i:08d}-0000-4000-8000-000000000000" for i in range(5)]
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed"}},
                            events={S1: [run_event(r, "cli.tool_start") for r in ids]},
                            runs={r: {"session_id": S1, "status": "failed"} for r in ids})
        with mock.patch.object(ws, "RUN_CHECKS_PER_POLL", 2):
            for _ in range(4):
                self.poll(client)
        checked = {c[1] for c in client.calls if c[1].startswith("/api/agent-runs/")}
        self.assertEqual(len(checked), 5)

    def test_known_run_ids_survive_restart(self):
        client = self.two_runs()
        self.poll(client)
        self.conn.close()
        self.conn = ws.connect(self.dir)
        self.assertEqual({r[0] for r in self.conn.execute("SELECT run_id FROM runs")}, {RUN_A, RUN_B})


class RunSpecificHandoff(Base):
    def test_completed_run_is_handed_off_while_a_newer_run_is_queued_or_running(self):
        for newer in ("queued", "running"):
            with self.subTest(newer):
                self.reset()
                events = [chunk(RUN_A, M1, 0, 1, 0, "A result"), done(RUN_A, M1), run_event(RUN_B, "run.queued", "queued")]
                if newer == "running":
                    events.append(run_event(RUN_B, "cli.tool_start"))
                runs = {RUN_A: {"session_id": S1, "status": "succeeded", "message_id": M1, "persisted": True},
                        RUN_B: {"session_id": S1, "status": newer}}
                client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": newer}}, events={S1: events}, runs=runs)
                status = self.poll(client)
                self.assertEqual(handoff_ids(status), [hid()])
                self.assertEqual(self.conn.execute("SELECT terminal_status, result_text FROM handoffs").fetchone()[:], ("completed", "A result"))
                states = {k: v["state"] for k, v in ws.analyze_session(self.conn, S1).items()}
                self.assertEqual(states, {RUN_A: "terminal", RUN_B: newer})
                self.assertEqual(self.count("handoffs", f"run_id='{RUN_B}'"), 0)    # B stays unfinished, nothing inherited

    def test_terminal_status_comes_from_the_runs_own_authority(self):
        events = [chunk(RUN_A, M1, 0, 1, 0, "x", status=None)]          # output without any terminal evidence
        runs = {RUN_A: {"session_id": S1, "status": "succeeded", "message_id": M1, "persisted": True}}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}}, events={S1: events}, runs=runs)
        self.poll(client)
        status = self.poll(client)                          # run GET learned in poll 1/2
        self.assertEqual(self.conn.execute("SELECT terminal_status FROM handoffs").fetchone()[0], "succeeded")
        self.assertEqual(handoff_ids(status), [hid()])

    def test_handoff_status_is_corrected_to_the_runs_own_authority_not_the_sessions(self):
        client = FakeClient(sessions={S1: {"provider": "codex-cli", "status": "failed"}},          # session projection: failed
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "A ok"), done(RUN_A, M1)]},
                            runs={RUN_A: {"session_id": S1, "status": "succeeded", "message_id": M1, "persisted": True}})
        self.poll(client)
        self.conn.execute("UPDATE handoffs SET terminal_status='failed'")                           # what a v3 ledger stored
        self.poll(client)
        self.poll(client)
        self.assertEqual(self.conn.execute("SELECT terminal_status FROM handoffs").fetchone()[0], "succeeded")

    def test_session_listing_alone_does_not_make_a_nonterminal_run_terminal(self):
        client = FakeClient(sessions={S1: {"provider": "codex-cli", "status": "completed"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "x", status=None)]})   # no terminal evidence, no run GET
        self.assertEqual(self.poll(client)["ready_handoffs"], [])

    def test_a_later_active_run_state_holds_a_contradicting_terminal_event(self):
        runs = {RUN_A: {"session_id": S1, "status": "running"}}
        client = FakeClient(sessions={S1: {"provider": "codex-cli", "status": "running"}},
                            events={S1: [run_event(RUN_A, "cli.tool_start")]}, runs=runs)
        self.poll(client)
        client.events[S1] += [chunk(RUN_A, M1, 0, 1, 0, "x"), done(RUN_A, M1)]
        status = self.poll(client)                      # the run list (same instant) still says running: contradiction
        self.assertEqual(status["ready_handoffs"], [])
        alert = next(a for a in status["alerts"] if a["code"] == "result_provenance_incomplete")
        self.assertEqual(alert["items"][0]["reason"], "run_state_conflict")
        client.runs[RUN_A].update(status="succeeded", message_id=M1, persisted=True)
        for _ in range(3):
            status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid()])

    def test_vanished_running_session_is_unconfirmed_not_completed(self):
        client = FakeClient(sessions={S1: {"provider": "codex-cli", "status": "running"}})
        self.poll(client)
        client.sessions = {}
        client.details = {}
        status = self.poll(client)
        self.assertEqual(status["ready_handoffs"], [])
        self.assertIn("state_unconfirmed_absent_from_lists", self.codes(status))


class MessageRecovery(Base):
    """A succeeded run whose events feed carries no cli.output / terminal: result via the ACL'd messages GET."""

    def p0(self, content="the full A body", **message_overrides):
        msg = {**message(M1, S1, RUN_A, content), **message_overrides}
        return FakeClient(sessions={S1: {"provider": "claude-cli", "status": "queued"}},
                          events={S1: [run_event(RUN_A, "cli.tool_start"), run_event(RUN_B, "run.queued", "queued")]},
                          runs={RUN_A: {"session_id": S1, "status": "succeeded", "message_id": M1, "persisted": True},
                                RUN_B: {"session_id": S1, "status": "queued"}},
                          messages={S1: [message(M3, S1, RUN_B, "other"), msg]})

    def test_verified_message_recovers_the_full_text_for_only_that_run(self):
        client = self.p0()
        for _ in range(4):
            status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid()])
        row = self.conn.execute("SELECT result_text, result_source, run_id, message_id FROM handoffs").fetchone()
        self.assertEqual(tuple(row), ("the full A body", "message_get", RUN_A, M1))
        self.assertEqual(self.count("handoffs", f"run_id='{RUN_B}'"), 0)
        self.assertEqual(self.count("runs", "settled=1"), 1)
        self.assertTrue(any(c == ("GET", f"/api/conversations/{S1}/messages") for c in client.calls))

    def test_pointer_alone_is_never_a_result(self):
        client = self.p0()
        client.messages = {S1: []}                                 # the message cannot be read
        for _ in range(4):
            status = self.poll(client)
        self.assertEqual(status["ready_handoffs"], [])
        alert = next(a for a in status["alerts"] if a["code"] == "result_provenance_incomplete")
        self.assertEqual(alert["items"][0]["reason"], "output_pointer_unrecovered")
        self.assertEqual(self.conn.execute("SELECT recover_reason FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0], "message_not_found")

    def test_unverifiable_messages_are_rejected(self):
        cases = {
            "wrong_run": dict(metadata={"run_id": RUN_B}),
            "wrong_session": dict(session_id=S2),
            "wrong_role": dict(role="user"),
            "deleted": dict(deleted_at="2026-10-22T00:00:00"),
            "clipped": dict(content="x" * 13_000),
        }
        for name, override in cases.items():
            with self.subTest(name):
                self.reset()
                client = self.p0(**override)
                for _ in range(4):
                    status = self.poll(client)
                self.assertEqual(status["ready_handoffs"], [], name)
                self.assertEqual(self.count("recovered_messages"), 0)
                self.assertEqual(self.count("handoffs"), 0)

    def test_message_beyond_the_bounded_page_is_unrecovered(self):
        client = self.p0()
        client.messages = {S1: [message(f"b{i:07d}-0000-4000-8000-000000000000", S1, RUN_B, "n") for i in range(25)] + [message(M1, S1, RUN_A, "late")]}
        for _ in range(4):
            status = self.poll(client)
        self.assertEqual(status["ready_handoffs"], [])
        self.assertEqual(self.conn.execute("SELECT recover_reason FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0], "message_not_in_bounded_page")

    def test_chunks_arriving_later_do_not_double_hand_off(self):
        client = self.p0()
        for _ in range(4):
            self.poll(client)
        client.events[S1] += [chunk(RUN_A, M1, 0, 1, 0, "the full A body"), done(RUN_A, M1)]
        status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid()])
        self.assertEqual(self.count("handoffs"), 1)

    def test_recovery_is_rate_limited_and_backs_off(self):
        client = self.p0()
        client.messages = {S1: []}
        for _ in range(6):
            self.poll(client)
        fetches = [c for c in client.calls if c[1].endswith("/messages")]
        self.assertLessEqual(len(fetches), 3)                       # one attempt, then exponential backoff

    def test_message_id_mismatch_with_the_run_is_rejected(self):
        client = self.p0()
        client.messages = {S1: []}
        for _ in range(2):
            self.poll(client)                                      # the run (and its message id) is now known
        client.events[S1] += [chunk(RUN_A, M2, 0, 1, 0, "impostor"), done(RUN_A, M2)]
        status = self.poll(client)
        self.assertEqual(self.count("handoffs", "message_id='%s'" % M2), 0)
        reasons = {i["reason"] for a in status["alerts"] if a["code"] == "result_provenance_incomplete" for i in a["items"]}
        self.assertIn("message_id_mismatch", reasons)


class HandoffLifecycle(Base):
    def client(self, status, events, provider="codex-cli"):
        return FakeClient(sessions={S1: {"provider": provider, "status": status}}, events={S1: events})

    def test_terminal_run_without_any_output_is_terminal_without_result(self):
        status = self.poll(self.client("completed", [done(RUN_A)]))
        self.assertEqual(status["ready_handoffs"], [])
        self.assertIn("terminal_without_result", self.codes(status))

    def test_ack_requires_a_receipt_then_new_turn_is_independent(self):
        client = self.client("completed", [chunk(RUN_A, M1, 0, 1, 0, "first"), done(RUN_A, M1)])
        self.poll(client)
        with self.assertRaises(ws.SupervisorError) as ctx:
            ws.acknowledge(self.conn, [f"handoff:{hid()}"])
        self.assertEqual(ctx.exception.code, "delivery_not_received")
        receive(self.conn, hid())
        ws.acknowledge(self.conn, [f"handoff:{hid()}"])
        self.assertEqual(self.conn.execute("SELECT retired FROM sessions").fetchone()[0], 0)
        client.sessions[S1]["status"] = "running"
        client.events[S1] += [run_event(RUN_B, "run.queued", "queued"), run_event(RUN_B, "cli.tool_start")]
        status = self.poll(client)
        self.assertEqual(status["ready_handoffs"], [])
        self.assertEqual(self.count("inbox", f"run_id='{RUN_B}'"), 2)
        client.sessions[S1]["status"] = "completed"
        client.events[S1] += [chunk(RUN_B, M2, 0, 1, 0, "second"), done(RUN_B, M2)]
        status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid(RUN_B, M2)])
        self.assertEqual(self.conn.execute("SELECT state FROM handoffs WHERE handoff_id=?", (hid(),)).fetchone()[0], "acked")
        self.assertEqual(self.count("inbox", f"run_id='{RUN_B}' AND acked_at IS NOT NULL"), 0)

    def test_late_second_message_and_old_run_reread_are_not_acked_or_duplicated(self):
        client = self.client("completed", [chunk(RUN_A, M1, 0, 1, 0, "first"), done(RUN_A, M1)])
        self.poll(client)
        receive(self.conn, hid())
        ws.acknowledge(self.conn, [f"handoff:{hid()}"])
        client.events[S1] += [chunk(RUN_A, M2, 0, 1, 0, "late other message")]      # late, different message in the same run
        status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid(RUN_A, M2)])
        self.assertEqual(self.count("inbox", "acked_at IS NOT NULL AND message_id='%s'" % M2), 0)
        # re-reading the whole old feed (cursor lost) neither re-offers the acked result nor duplicates anything
        self.conn.execute("UPDATE sessions SET cursor=NULL, drained=0")
        before = (self.count("inbox"), self.count("handoffs"), self.count("outbox"))
        status = self.poll(client)
        self.assertEqual((self.count("inbox"), self.count("handoffs"), self.count("outbox")), before)
        self.assertEqual(handoff_ids(status), [hid(RUN_A, M2)])

    def test_replay_of_the_same_page_and_restart_keep_unacked_handoffs(self):
        client = self.client("completed", [chunk(RUN_A, M1, 0, 1, 0, "r"), done(RUN_A, M1)])
        self.poll(client)
        self.poll(client)
        self.assertEqual(self.count("handoffs"), 1)
        self.conn.close()
        self.conn = ws.connect(self.dir)
        self.poll(client)
        self.assertEqual(len(ws.build_status(self.conn, time.time())["ready_handoffs"]), 1)
        self.assertEqual(len(ws.build_status(self.conn, time.time())["unacked_terminal_or_result"]), 1)
        with self.assertRaises(ws.SupervisorError):
            ws.acknowledge(self.conn, ["handoff:nope"])

    def test_legacy_task_handoff_without_a_body_is_backfilled_only_while_undelivered(self):
        task = {"task_id": T1, "status": "completed", "goal": "g", "summary_text": "finished"}
        self.poll(FakeClient(tasks=[task]))
        text = self.conn.execute("SELECT result_text FROM handoffs").fetchone()[0]
        self.conn.execute("UPDATE handoffs SET result_text=NULL, result_digest=?", (digest(""),))      # what a v3 ledger kept
        self.conn.execute("UPDATE outbox SET result_digest=?, result_chars=0", (digest(""),))
        self.poll(FakeClient(tasks=[task]))
        row = self.conn.execute("SELECT h.result_text, h.result_digest, o.result_digest AS od, o.result_chars FROM handoffs h JOIN outbox o USING(handoff_id)").fetchone()
        self.assertEqual((row["result_text"], row["result_digest"], row["od"], row["result_chars"]), (text, digest(text), digest(text), len(text)))
        # once the root has received it, the stored digest is never rewritten
        receive(self.conn, f"task:{T1}")
        self.conn.execute("UPDATE handoffs SET result_text=NULL")
        self.poll(FakeClient(tasks=[task]))
        self.assertIsNone(self.conn.execute("SELECT result_text FROM handoffs").fetchone()[0])

    def test_task_terminal_without_result_then_with_result_and_ack(self):
        task = {"task_id": T1, "status": "completed", "goal": "g"}
        client = FakeClient(tasks=[task])
        status = self.poll(client)
        self.assertIn("terminal_without_result", self.codes(status))
        client.task_details[T1] = {**task, "summary_text": "finished"}
        status = self.poll(client)
        self.assertEqual(handoff_ids(status), [f"task:{T1}"])
        receive(self.conn, f"task:{T1}")
        self.assertTrue(ws.acknowledge(self.conn, [f"handoff:task:{T1}"])["ok"])
        self.assertEqual(ws.build_status(self.conn, time.time())["ready_handoffs"], [])


class RootAbsentDrain(Base):
    """Root absent while A and B complete -> observer restart -> root drains with a new epoch."""

    def feed(self, *runs):
        events, runs_map = [], {}
        for run, msg, text in runs:
            events += [chunk(run, msg, 0, 1, 0, text), done(run, msg)]
        return FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed"}}, events={S1: events}, runs=runs_map)

    def cli(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = ws.main(["--dir", str(self.dir), *argv])
        return code, json.loads(buf.getvalue())

    def test_full_drain_scenario(self):
        client = self.feed((RUN_A, M1, "result A"), (RUN_B, M2, "result B"))
        self.poll(client)
        self.assertEqual(self.count("source_heartbeats", "source_type='root'"), 0)         # root never ran, monitor wrote nothing for it
        self.conn.close()                                                                  # observer restart
        self.conn = ws.connect(self.dir)
        self.poll(client)
        self.assertEqual(self.count("outbox"), 2)
        self.conn.close()
        code, page1 = self.cli("pending", "--limit", "1")
        self.assertEqual((code, page1["total_pending"], page1["returned"], page1["has_more"]), (0, 2, 1, True))
        code, page2 = self.cli("pending", "--limit", "1", "--cursor", str(page1["next_cursor"]))
        self.assertEqual((page2["returned"], page2["has_more"], page2["remaining_after_cursor"]), (1, False, 1))
        seen = {i["handoff_id"]: i for i in page1["items"] + page2["items"]}
        self.assertEqual(set(seen), {hid(RUN_A, M1), hid(RUN_B, M2)})
        self.assertIn("source_freshness", page1)
        self.assertIn("blockers", page1)
        again = self.cli("pending", "--limit", "5")[1]
        self.assertEqual(again["total_pending"], 2)                                         # reading marked nothing
        a, b = hid(RUN_A, M1), hid(RUN_B, M2)
        shown = self.cli("show", a)[1]
        self.assertEqual((shown["result_text"], shown["digest_verified"], shown["delivery_state"]), ("result A", True, "pending"))
        self.assertEqual(shown["result_digest"], digest("result A"))
        # a wrong digest, a regressed epoch and an early ack are all refused
        self.assertEqual(self.cli("receive", f"{a}={'0' * 64}", "--root-source-id", "root-2", "--root-epoch", "2")[1]["error"], "digest_mismatch")
        self.assertEqual(self.cli("ack", f"handoff:{a}")[1]["error"], "delivery_not_received")
        self.assertEqual(self.cli("pending")[1]["total_pending"], 2)
        # the root comes back with a new epoch and records ONLY A
        self.assertEqual(self.cli("heartbeat", "--source-id", "root-2", "--source-type", "root", "--root-epoch", "2", "--ttl", "300")[0], 0)
        code, out = self.cli("receive", f"{a}={digest('result A')}", "--root-source-id", "root-2", "--root-epoch", "2")
        self.assertEqual((code, out["received"]), (0, [a]))
        pending = self.cli("pending")[1]
        self.assertEqual([i["handoff_id"] for i in pending["items"]], [b])                  # B is still undelivered
        self.assertEqual(self.cli("receive", f"{a}={digest('result A')}", "--root-source-id", "root-2", "--root-epoch", "2")[1]["already_received"], [a])
        self.assertEqual(self.cli("receive", f"{b}={digest('result B')}", "--root-source-id", "root-1", "--root-epoch", "1")[1]["error"], "root_epoch_regressed")
        self.assertEqual(self.cli("ack", f"handoff:{a}")[0], 0)
        shown_a = self.cli("show", a)[1]
        self.assertEqual((shown_a["delivery_state"], shown_a["root_source_id"], shown_a["root_epoch"]), ("received", "root-2", 2))
        # a new run C (a continued session: active again, then terminal) is independent and pending next to B
        self.conn = ws.connect(self.dir)
        client.sessions[S1]["status"] = "running"
        self.poll(client)
        client.events[S1] += [chunk(RUN_C, M3, 0, 1, 0, "result C"), done(RUN_C, M3)]
        client.sessions[S1]["status"] = "completed"
        self.poll(client)
        self.poll(client)
        self.conn.close()
        pending = self.cli("pending")[1]
        self.assertEqual(sorted(i["handoff_id"] for i in pending["items"]), sorted([b, hid(RUN_C, M3)]))
        self.assertEqual(self.cli("show", b)[1]["state"], "ready")
        self.assertEqual(self.cli("show", a)[1]["state"], "acked")
        self.conn = ws.connect(self.dir)

    def test_expiry_or_absence_never_deletes_or_receipts(self):
        client = self.feed((RUN_A, M1, "result A"))
        self.poll(client)
        ws.record_heartbeat(self.conn, source_id="root-1", source_type="root", root_epoch=1, ttl=1, now=time.time() - 100)
        status = ws.build_status(self.conn, time.time())
        self.assertIn("root_heartbeat_expired", self.codes(status))
        view = ws.pending_view(self.conn, time.time())
        self.assertEqual(view["total_pending"], 1)
        self.assertEqual(self.count("outbox", "delivery_state='pending'"), 1)
        self.assertEqual(self.count("outbox", "received_at IS NOT NULL"), 0)

    def test_pending_does_not_change_the_ledger(self):
        self.poll(self.feed((RUN_A, M1, "result A")))
        before = [tuple(r) for r in self.conn.execute("SELECT * FROM outbox")], self.count("acknowledgments"), self.count("source_heartbeats")
        ws.pending_view(self.conn, time.time())
        self.assertEqual(([tuple(r) for r in self.conn.execute("SELECT * FROM outbox")], self.count("acknowledgments"), self.count("source_heartbeats")), before)

    def test_receive_is_all_or_nothing(self):
        self.poll(self.feed((RUN_A, M1, "result A"), (RUN_B, M2, "result B")))
        good = f"{hid(RUN_A, M1)}={digest('result A')}"
        with self.assertRaises(ws.SupervisorError):
            ws.record_receipt(self.conn, [good, f"{hid(RUN_B, M2)}={'1' * 64}"], root_source_id="root-1", root_epoch=1)
        self.assertEqual(self.count("outbox", "delivery_state='received'"), 0)


class CountsAndHeartbeats(Base):
    def running_claude(self, n, status="running"):
        ids = [f"{i:08d}-1111-4111-8111-111111111111" for i in range(n)]
        sessions = {sid: {"provider": "claude-cli", "status": status} for sid in ids}
        runs = {f"{i:08d}-0000-4000-8000-000000000000": {"session_id": sid, "status": status} for i, sid in enumerate(ids)}
        return FakeClient(sessions=sessions, runs=runs)

    def test_reconciled_running_count_drives_the_deficit(self):
        status = self.poll(self.running_claude(3))
        mc = status["minClaude"]
        self.assertEqual((mc["running"], mc["deficit"], mc["exact"]), (3, 5, True))
        self.assertEqual(status["providers"]["claude-cli"]["session_projection"]["running"], 3)

    def test_queued_sessions_are_not_running(self):
        status = self.poll(self.running_claude(4, status="queued"))
        mc = status["minClaude"]
        self.assertEqual((mc["running"], mc["queued_not_counted"], mc["deficit"]), (0, 4, 8))
        self.assertTrue(mc["exact"])

    def test_projection_running_without_a_running_run_is_not_exact(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}})
        status = self.poll(client)
        self.assertFalse(status["minClaude"]["exact"])
        self.assertIn("projection_without_running_run", status["minClaude"]["not_exact_reasons"])

    def test_full_list_page_reports_lower_bound(self):
        with mock.patch.object(ws, "LIST_LIMIT", 2):
            status = self.poll(self.running_claude(3))
        self.assertTrue(status["providers"]["claude-cli"]["session_projection"]["running_lower_bound"])
        self.assertFalse(status["minClaude"]["exact"])
        self.assertTrue(status["minClaude"]["deficit_is_upper_bound"])
        self.assertFalse(status["source_completeness"]["cli_list"]["complete"])
        self.assertFalse(status["source_completeness"]["cli_list:running"]["complete"])
        self.assertTrue(status["source_completeness"]["cli_list:completed"]["complete"])

    def test_source_error_only_is_never_exact(self):
        class Boom(FakeClient):
            async def call(self, method, path, **kw):
                if path == "/api/cli-sessions" and kw["params"]["status"] == "running":
                    raise ws.cli.CliSessionError("server_error", 500)
                return await super().call(method, path, **kw)
        client = Boom(sessions={S1: {"provider": "claude-cli", "status": "queued"}})
        status = self.poll(client)
        mc = status["minClaude"]
        self.assertFalse(mc["exact"])
        self.assertIn("cli_list:running", mc["not_exact_reasons"])
        self.assertIsNone(status["providers"]["claude-cli"]["session_projection"]["running"])      # UNKNOWN, not 0
        self.assertEqual(status["source_completeness"]["cli_list:running"]["error"], "server_error")

    def test_stale_snapshot_is_not_exact_and_cycle_failure_is_visible(self):
        self.poll(self.running_claude(1), now=time.time() - 5000)
        status = ws.build_status(self.conn, time.time())
        self.assertFalse(status["snapshot_fresh"])
        self.assertFalse(status["minClaude"]["exact"])
        self.assertIn("snapshot_stale", status["minClaude"]["not_exact_reasons"])
        self.assertIn("observer_stale", self.codes(status))

    def test_cycle_failure_is_in_status_and_distinguished_from_staleness(self):
        self.poll(self.running_claude(1))
        async def failing(plan):
            raise ws.cli.CliSessionError("connection_error")
        asyncio.run(ws.run_loop(self.conn, self.dir, 0.0, failing, cycles=1))
        written = json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(written["poll_cycle"]["state"], "failing")
        self.assertEqual(written["poll_cycle"]["last_failure"]["code"], "connection_error")
        self.assertIn("poll_cycle_failed:connection_error", self.codes(written))
        self.assertTrue(written["snapshot_fresh"])                                    # the snapshot itself is recent
        self.assertFalse(written["minClaude"]["exact"])                               # but a failing poll is never exact
        self.poll(self.running_claude(1))
        self.assertEqual(ws.build_status(self.conn, time.time())["poll_cycle"]["state"], "ok")

    def test_missing_root_and_aux_are_alerts_and_unknown_not_invented(self):
        status = self.poll(FakeClient())
        self.assertIn("root_heartbeat_missing", self.codes(status))
        self.assertEqual(status["aux"]["deficit"], "UNKNOWN")
        self.assertEqual(status["root_heartbeat"]["state"], "missing")
        self.assertIsNone(status["aux"]["provenance"])

    def test_monitor_never_writes_root_heartbeat(self):
        self.poll(FakeClient())
        self.assertEqual(self.count("source_heartbeats", "source_type IN ('root','aux')"), 0)

    def test_explicit_heartbeat_supplies_aux_then_expires(self):
        now = time.time()
        ws.record_heartbeat(self.conn, source_id="root-1", source_type="root", root_epoch=3, ttl=120, aux_count=4, now=now)
        status = ws.build_status(self.conn, now + 10)
        self.assertEqual((status["aux"]["deficit"], status["root_heartbeat"]["state"]), (2, "live"))
        self.assertEqual(status["aux"]["provenance"]["root_epoch"], 3)
        late = ws.build_status(self.conn, now + 500)
        self.assertIn("root_heartbeat_expired", self.codes(late))
        self.assertEqual(late["aux"]["deficit"], "UNKNOWN")

    def test_heartbeat_validation_and_epoch_regression(self):
        now = time.time()
        ws.record_heartbeat(self.conn, source_id="root-1", source_type="root", root_epoch=5, ttl=60, now=now)
        with self.assertRaises(ws.SupervisorError) as ctx:
            ws.record_heartbeat(self.conn, source_id="root-0", source_type="root", root_epoch=4, ttl=60, now=now)
        self.assertEqual(ctx.exception.code, "root_epoch_regressed")
        for bad in ({"ttl": None}, {"ttl": 99_999_999}, {"observed_at": now + 3600}):
            with self.assertRaises(ws.SupervisorError):
                ws.record_heartbeat(self.conn, source_id="root-2", source_type="root", root_epoch=6, **{"ttl": 60, **bad}, now=now)

    def test_stale_running_worker_alert(self):
        sessions = {S1: {"provider": "claude-cli", "status": "running"}}
        t0 = time.time() - 5000
        self.poll(FakeClient(sessions=sessions), now=t0)
        status = ws.build_status(self.conn, time.time())
        self.assertIn(("stale_worker", f"cli:{S1}"), [(a["code"], a["subject"]) for a in status["alerts"]])
        self.assertIn("observer_stale", self.codes(status))


class LedgerMigration(unittest.TestCase):
    V1 = """
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE sessions(session_id TEXT PRIMARY KEY, provider TEXT NOT NULL, status TEXT NOT NULL, cursor TEXT,
      first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_progress_at REAL NOT NULL, terminal_confirmed_at REAL,
      retired INTEGER NOT NULL DEFAULT 0, cursor_stalled INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE tasks(task_id TEXT PRIMARY KEY, status TEXT NOT NULL, executor TEXT, goal TEXT, first_seen REAL NOT NULL,
      last_seen REAL NOT NULL, last_progress_at REAL NOT NULL, stalled INTEGER NOT NULL DEFAULT 0, terminal_confirmed_at REAL, result_json TEXT);
    CREATE TABLE inbox(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_id TEXT NOT NULL, event_type TEXT,
      status TEXT, kind TEXT NOT NULL, summary TEXT, body_json TEXT NOT NULL, received_at REAL NOT NULL, acked_at REAL,
      UNIQUE(session_id, event_id));
    CREATE TABLE handoffs(handoff_id TEXT PRIMARY KEY, source_type TEXT NOT NULL, source_id TEXT NOT NULL, provider TEXT,
      terminal_status TEXT NOT NULL, created_at REAL NOT NULL, state TEXT NOT NULL, acked_at REAL);
    CREATE TABLE source_heartbeats(seq INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, source_type TEXT NOT NULL,
      observed_at REAL NOT NULL, expires_at REAL, root_epoch INTEGER, aux_count INTEGER, via TEXT NOT NULL, error_code TEXT);
    CREATE TABLE acknowledgments(item_kind TEXT NOT NULL, item_id TEXT NOT NULL, acked_at REAL NOT NULL, note TEXT, PRIMARY KEY(item_kind, item_id));
    CREATE TABLE snapshots(seq INTEGER PRIMARY KEY AUTOINCREMENT, observed_at REAL NOT NULL, data_json TEXT NOT NULL);
    """

    def build(self, directory):
        raw = sqlite3.connect(directory / "state.sqlite3")
        raw.executescript(self.V1)
        out = json.dumps({"run_id": RUN_A, "event_type": "cli.output", "status": "completed",
                          "payload": {"assistant_message_id": M1, "chunk_index": 0, "chunk_count": 1}})
        tele = json.dumps({"run_id": RUN_A, "event_type": "cli.assistant_text", "payload": {"text": "t"}})
        raw.execute("INSERT INTO sessions VALUES(?,?,?,?,1,1,1,1,1,0)", (S1, "codex-cli", "completed", "c9"))
        raw.execute("INSERT INTO inbox(session_id,event_id,event_type,status,kind,summary,body_json,received_at) VALUES(?,?,?,?,?,?,?,1)",
                    (S1, "e1", "cli.output", "completed", "info", "t", out))
        raw.execute("INSERT INTO inbox(session_id,event_id,event_type,status,kind,summary,body_json,received_at) VALUES(?,?,?,?,?,?,?,1)",
                    (S1, "e2", "cli.assistant_text", "", "output", "t", tele))
        raw.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,NULL)", (f"cli:{S1}", "cli_session", S1, "codex-cli", "completed", 1, "ready"))
        raw.execute("INSERT INTO handoffs VALUES(?,?,?,?,?,?,?,5)", (f"task:{T1}", "task", T1, None, "completed", 1, "acked"))
        raw.execute("INSERT INTO acknowledgments VALUES('handoff',?,5,'legacy')", (f"cli:{S1}",))
        raw.execute("INSERT INTO snapshots(observed_at,data_json) VALUES(1,'{}')")
        raw.commit()
        raw.close()

    def test_v1_ledger_is_backed_up_preserved_and_migrated(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.build(directory)
            conn = ws.connect(directory)
            backups = list(directory.glob("state.sqlite3.pre-v5-*.bak"))
            self.assertEqual(len(backups), 1)
            with contextlib.closing(sqlite3.connect(backups[0])) as old:       # the original observations survive untouched
                self.assertEqual(old.execute("SELECT retired FROM sessions").fetchone()[0], 1)
                self.assertEqual(old.execute("SELECT state FROM handoffs WHERE handoff_id=?", (f"cli:{S1}",)).fetchone()[0], "ready")
                self.assertEqual(old.execute("SELECT kind FROM inbox WHERE event_id='e1'").fetchone()[0], "info")
            self.assertEqual(tuple(conn.execute("SELECT run_id, message_id FROM inbox WHERE event_id='e1'").fetchone()), (RUN_A, M1))
            self.assertEqual(conn.execute("SELECT state FROM handoffs WHERE handoff_id=?", (f"cli:{S1}",)).fetchone()[0], "superseded_v1")
            self.assertEqual(conn.execute("SELECT kind FROM inbox WHERE event_id='e1'").fetchone()[0], "output")       # v1 stored cli.output as info
            self.assertEqual(conn.execute("SELECT kind FROM inbox WHERE event_id='e2'").fetchone()[0], "info")         # telemetry is not output
            self.assertEqual(tuple(conn.execute("SELECT retired, cursor FROM sessions").fetchone()), (0, "c9"))
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()[0], "5")
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0], "unknown")
            states = {r[0]: r[1] for r in conn.execute("SELECT handoff_id, delivery_state FROM outbox")}
            self.assertEqual(states, {f"cli:{S1}": "superseded", f"task:{T1}": "received"})   # nothing superseded is revived
            pending = ws.pending_view(conn, time.time())
            self.assertEqual(pending["total_pending"], 0)
            conn.close()
            ws.connect(directory).close()                                       # idempotent: no second backup
            self.assertEqual(len(list(directory.glob("state.sqlite3.pre-v5-*.bak"))), 1)

    def test_legacy_ack_does_not_swallow_a_new_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.build(directory)
            conn = ws.connect(directory)
            client = FakeClient(sessions={S1: {"provider": "codex-cli", "status": "completed"}},
                                events={S1: [chunk(RUN_B, M2, 0, 1, 0, "new run"), done(RUN_B, M2)]})
            conn.execute("UPDATE sessions SET cursor=NULL")
            async def fetch(plan):
                with mock.patch("home_mcp_gateway.work_supervisor._fetch_jobs", new=_no_jobs):
                    return await ws.fetch_all(plan, client, client, time.time())
            status = asyncio.run(ws.poll_once(conn, directory, fetch))
            self.assertEqual(handoff_ids(status), [hid(RUN_B, M2)])
            self.assertEqual(status["delivery"]["pending"], 1)
            self.assertEqual(conn.execute("SELECT retired FROM sessions").fetchone()[0], 0)
            conn.close()


class ObserverOnly(Base):
    def test_gate_rejects_every_mutating_route(self):
        client = FakeClient()
        for method, path in (("POST", "/api/cli-sessions"), ("POST", f"/api/cli-sessions/{S1}/messages"),
                             ("POST", f"/api/cli-sessions/{S1}/cancel"), ("POST", f"/api/cli-sessions/{S1}/reconnect"),
                             ("POST", f"/api/local-agent/tasks/{T1}/cancel"), ("POST", "/api/local-agent/tasks"),
                             ("GET", "/api/cli-sessions/collect"),
                             ("POST", f"/api/agent-runs/{RUN_A}"), ("DELETE", f"/api/agent-runs/{RUN_A}"),
                             ("POST", f"/api/agent-runs/{RUN_A}/cancel"), ("GET", f"/api/agent-runs/{RUN_A}/events"),
                             ("POST", f"/api/conversations/{S1}/messages"), ("PUT", f"/api/conversations/{S1}/messages"),
                             ("POST", f"/api/conversations/{S1}/agent-runs"), ("GET", "/api/conversations"),
                             ("GET", f"/api/conversations/{S1}/messages/{M1}/branches"),
                             ("GET", "/api/agent-runs/not-a-uuid")):
            with self.assertRaises(ws.SupervisorError, msg=f"{method} {path}") as ctx:
                asyncio.run(ws.observe(client, method, path))
            self.assertEqual(ctx.exception.code, "route_not_allowed")
        self.assertEqual(client.calls, [])

    def test_new_get_routes_only_accept_the_fixed_read_only_parameters(self):
        client = FakeClient(runs={RUN_A: {"session_id": S1, "status": "running"}})
        bad = [(f"/api/agent-runs/{RUN_A}", None), (f"/api/agent-runs/{RUN_A}", {**ws.RUN_GET_PARAMS, "include_events": "true"}),
               (f"/api/agent-runs/{RUN_A}", {**ws.RUN_GET_PARAMS, "extra": "1"}),
               (f"/api/conversations/{S1}/agent-runs", {"limit": 500, "status": "running"}),
               (f"/api/conversations/{S1}/agent-runs", {"limit": 200, "status": "bogus"}),
               (f"/api/conversations/{S1}/agent-runs", {"limit": 200}),
               (f"/api/conversations/{S1}/messages", {"limit": 500, "since": "2026-10-22T00:00:00Z"}),
               (f"/api/conversations/{S1}/messages", {"limit": 20, "since": "not a time"}),
               (f"/api/conversations/{S1}/messages", {"limit": 20})]
        for path, params in bad:
            with self.assertRaises(ws.SupervisorError, msg=str((path, params))) as ctx:
                asyncio.run(ws.observe(client, "GET", path, params=params))
            self.assertEqual(ctx.exception.code, "route_not_allowed")
        self.assertEqual(client.calls, [])
        asyncio.run(ws.observe(client, "GET", f"/api/agent-runs/{RUN_A}", params=dict(ws.RUN_GET_PARAMS)))
        asyncio.run(ws.observe(client, "GET", f"/api/conversations/{S1}/agent-runs", params={"limit": 200, "status": "running"}))
        asyncio.run(ws.observe(client, "GET", f"/api/conversations/{S1}/messages", params={"limit": 20, "since": "2026-10-22T00:00:00Z"}))

    def test_full_poll_issues_only_read_routes(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"}},
                            events={S1: [run_event(RUN_A, "cli.tool_start")]}, tasks=[{"task_id": T1, "status": "running", "goal": "g"}],
                            runs={RUN_A: {"session_id": S1, "status": "succeeded", "message_id": M1, "persisted": True}},
                            messages={S1: [message(M1, S1, RUN_A, "body")]})
        for _ in range(4):
            self.poll(client)
        self.assertTrue(any(c[1].endswith("/messages") for c in client.calls))
        for method, path in client.calls:
            self.assertTrue(any(method == m and rx.fullmatch(path) for m, rx in ws.READ_ONLY_ROUTES), (method, path))
            self.assertIn(method, ("GET", "POST"))
            if method == "POST":
                self.assertEqual(path, "/api/cli-sessions/collect")

    def test_source_error_persists_safe_code_only(self):
        class Boom(FakeClient):
            async def call(self, method, path, **kw):
                if "local-agent" in path:
                    raise ws.tasks.TaskError("authentication_failed", 401, detail="password=fixture-password")
                raise ws.cli.CliSessionError("authentication_failed", 401, detail="password=fixture-password")
        status = self.poll(Boom())
        blob = json.dumps(status) + json.dumps([dict(r) for r in self.conn.execute("SELECT * FROM source_heartbeats")])
        self.assertIn("authentication_failed", blob)
        self.assertNotIn("fixture-password", blob)


class Infrastructure(Base):
    def test_singleton_lock_excludes_second_holder(self):
        with ws.SingletonLock(self.dir):
            with self.assertRaises(ws.SupervisorError) as ctx:
                with ws.SingletonLock(self.dir):
                    pass
            self.assertEqual(ctx.exception.code, "already_running")
        with ws.SingletonLock(self.dir):
            pass

    def test_status_json_is_atomic_and_recovery_removes_partial_exports(self):
        (self.dir / "status.json.99.tmp").write_text("{partial")
        ws.recover(self.conn, self.dir)
        self.assertFalse(list(self.dir.glob("*.tmp")))
        data = json.loads((self.dir / "status.json").read_text(encoding="utf-8"))
        self.assertTrue(data["observer_only"])
        for key in ("observed_at", "minClaude", "aux", "source_completeness", "ready_handoffs", "alerts", "delivery", "poll_cycle"):
            self.assertIn(key, data)

    def test_cli_heartbeat_ack_status_commands(self):
        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = ws.main(["--dir", str(self.dir), *argv])
            return code, json.loads(buf.getvalue())
        code, out = run("heartbeat", "--source-id", "root-1", "--source-type", "root", "--root-epoch", "1", "--ttl", "300", "--aux-count", "6")
        self.assertEqual((code, out["ok"]), (0, True))
        code, out = run("status")
        self.assertEqual((out["aux"]["deficit"], out["root_heartbeat"]["state"]), (0, "live"))
        code, out = run("ack", "handoff:missing")
        self.assertEqual((code, out["error"]), (1, "not_found"))
        code, out = run("pending")
        self.assertEqual((code, out["total_pending"], out["has_more"]), (0, 0, False))
        code, out = run("heartbeat", "--source-id", "bad id", "--source-type", "root", "--root-epoch", "1", "--ttl", "5")
        self.assertEqual((code, out["error"]), (1, "invalid_input"))

    def test_run_loop_survives_cycle_failure_and_records_code(self):
        async def failing(plan):
            raise ws.cli.CliSessionError("connection_error")
        asyncio.run(ws.run_loop(self.conn, self.dir, 0.0, failing, cycles=2))
        rows = self.conn.execute("SELECT error_code FROM source_heartbeats WHERE source_id='poll_cycle'").fetchall()
        self.assertEqual([r[0] for r in rows], ["connection_error"] * 2)


class LogonRecovery(Base):
    PS = Path(__file__).resolve().parents[1] / "scripts" / "work_supervisor_autostart.ps1"

    def ps(self, *args):
        import subprocess
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-File", str(self.PS), *args],
                             capture_output=True, text=True, timeout=120)
        return out.returncode, json.loads(out.stdout)

    @unittest.skipUnless(__import__("os").name == "nt", "Windows only")
    def test_planned_task_definition_is_same_user_limited_interactive_logon_ignorenew_without_credentials(self):
        code, plan = self.ps("plan")
        d = plan["desired"]
        self.assertEqual(code, 0)
        self.assertEqual((d["LogonType"], d["RunLevel"], d["MultipleInstances"], d["Trigger"]), ("Interactive", "Limited", "IgnoreNew", "AtLogOn"))
        root = str(Path(__file__).resolve().parents[1])
        self.assertEqual(d["WorkingDirectory"].lower(), root.lower())
        self.assertEqual(d["Execute"].lower(), (root + r"\.venv\Scripts\pythonw.exe").lower())
        self.assertTrue(d["Arguments"].lower().endswith(r'scripts\work_supervisor.py" run'))
        self.assertRegex(d["Sid"], r"^S-1-5-21-")                      # a user SID, never SYSTEM / S-1-5-18
        blob = json.dumps(plan).lower()
        for word in ("password", "s4u", "highest", "system", "runas", "token", "secret", ".env"):
            self.assertNotIn(word, blob.replace("gatewaytaskuntouched", ""))
        self.assertEqual(plan["taskName"], "Home MCP Work Supervisor")
        self.assertNotEqual(plan["taskName"], plan["gatewayTaskUntouched"])

    @unittest.skipUnless(__import__("os").name == "nt", "Windows only")
    def test_a_task_that_is_not_exactly_ours_is_blocked_never_adopted(self):
        code, result = self.ps("selftest")
        self.assertEqual(code, 0)
        self.assertTrue(all(result.values()), result)
        self.assertEqual(len(result), 11)

    def test_duplicate_start_is_a_noop_that_does_not_open_or_migrate_the_ledger(self):
        fresh_dir = self.dir / "dup"
        with ws.SingletonLock(fresh_dir):
            with mock.patch.object(ws, "connect", side_effect=AssertionError("ledger touched")) as connect:
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = ws.main(["--dir", str(fresh_dir), "run"])
                connect.assert_not_called()
        self.assertEqual(code, 0)                                         # clean exit: the scheduler must not restart-loop
        self.assertEqual(json.loads(buf.getvalue())["error"], "already_running")
        self.assertFalse((fresh_dir / "state.sqlite3").exists())
        self.assertIn("already_running", (fresh_dir / ws.LOG_NAME).read_text(encoding="utf-8"))

    def test_run_without_stdout_like_pythonw_still_works(self):
        with mock.patch("sys.stdout", None):
            self.assertEqual(ws.main(["--dir", str(self.dir / "w"), "status"]), 0)

    def test_log_rotates_with_a_size_cap_and_contains_only_safe_facts(self):
        with mock.patch.object(ws, "LOG_MAX_BYTES", 400), mock.patch.object(ws, "LOG_BACKUPS", 3):
            for i in range(120):
                ws.log_event(self.dir, "cycle_failed", code="server_error", detail=r"password=fixture-password C:\Users\x\.env", n=i)
        files = sorted(p.name for p in self.dir.glob(ws.LOG_NAME + "*"))
        self.assertEqual(files, [ws.LOG_NAME, ws.LOG_NAME + ".1", ws.LOG_NAME + ".2", ws.LOG_NAME + ".3"])
        blob = "".join((self.dir / n).read_text(encoding="utf-8") for n in files)
        self.assertNotIn("fixture-password", blob)
        self.assertNotIn(".env", blob)
        self.assertIn("detail=redacted", blob)
        self.assertTrue(all((self.dir / n).stat().st_size < 800 for n in files))

    def test_loop_logs_start_failure_code_once_and_recovery_without_touching_the_ledger_data(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "r"), done(RUN_A, M1)]})
        self.poll(client)
        before = (self.count("handoffs"), self.count("outbox"), self.count("inbox"))
        calls = {"n": 0}

        async def flaky(plan):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ws.cli.CliSessionError("connection_error")
            return await self.fetcher(client)(plan)
        asyncio.run(ws.run_loop(self.conn, self.dir, 0.0, flaky, cycles=4))
        text = (self.dir / ws.LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("loop_start", text)
        self.assertEqual(text.count("cycle_failed code=connection_error"), 1)    # not once per retry
        self.assertIn("cycle_recovered", text)
        self.assertEqual((self.count("handoffs"), self.count("outbox"), self.count("inbox")), before)

    def test_restart_keeps_unreceived_and_received_state(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "a"), done(RUN_A, M1), chunk(RUN_B, M2, 0, 1, 0, "b"), done(RUN_B, M2)]})
        self.poll(client)
        receive(self.conn, hid())
        ws.acknowledge(self.conn, [f"handoff:{hid()}"])
        self.conn.close()
        self.conn = ws.connect(self.dir)
        self.poll(client)
        states = {r[0]: (r[1], r[2]) for r in self.conn.execute("SELECT o.handoff_id, o.delivery_state, h.state FROM outbox o JOIN handoffs h USING(handoff_id)")}
        self.assertEqual(states, {hid(): ("received", "acked"), hid(RUN_B, M2): ("pending", "ready")})


if __name__ == "__main__":
    unittest.main()


class SplitBeforeShrink(Base):
    def test_combined_clip_splits_sessions_without_shrinking_their_page(self):
        def collect(body):
            group = body["session_ids"]
            if len(group) > 1:
                return {"sessions": [], "output_truncated": True}
            return {"sessions": [{"session_id": group[0], "events": [event(n) for n in range(body["limit_per_session"])], "next_cursor": "c100"}]}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"},
                                      S2: {"provider": "claude-cli", "status": "running"}}, collect_hook=collect)
        self.poll(client)
        self.assertEqual(set(client.collect_limits), {100})
        self.assertEqual(self.count("inbox", f"session_id='{S1}'"), 100)
        self.assertEqual(self.count("inbox", f"session_id='{S2}'"), 100)
        self.assertLess(len(client.collect_limits), 10)

    def test_one_heavy_session_cannot_force_the_others_to_shrink_or_move(self):
        def collect(body):
            group = body["session_ids"]
            pages = []
            for sid in group:
                if sid == S2:
                    return {"sessions": [], "output_truncated": True}     # S2 is clipped even alone
                pages.append({"session_id": sid, "events": [event(n) for n in range(body["limit_per_session"])], "next_cursor": "c100"})
            return {"sessions": pages, "output_truncated": len(group) > 1}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"},
                                      S2: {"provider": "claude-cli", "status": "running"}}, collect_hook=collect)
        status = self.poll(client)
        self.assertEqual(self.count("inbox", f"session_id='{S1}'"), 100)
        self.assertEqual(self.count("inbox", f"session_id='{S2}'"), 0)
        self.assertIsNone(self.conn.execute("SELECT cursor FROM sessions WHERE session_id=?", (S2,)).fetchone()[0])
        self.assertIn("cursor_stalled_truncated_page", self.codes(status))
        self.assertLessEqual(len(client.collect_limits), 12)

    def test_one_blocked_session_retries_alone_and_never_sends_an_empty_batch(self):
        def collect(body):
            group = body["session_ids"]
            assert group, "empty collect batch"
            return {"sessions": [{"session_id": sid, "events": [event(1)], "next_cursor": "c1", "cursor_advance_blocked": True}
                                 if sid == S2 else {"session_id": sid, "events": [event(n) for n in range(3)], "next_cursor": "c3"}
                                 for sid in group]}
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "running"},
                                      S2: {"provider": "claude-cli", "status": "running"}}, collect_hook=collect)
        status = self.poll(client)
        self.assertEqual(self.count("inbox", f"session_id='{S1}'"), 3)      # the accepted page is kept
        self.assertEqual(self.count("inbox", f"session_id='{S2}'"), 0)
        self.assertIn("cursor_stalled_truncated_page", self.codes(status))


class FollowUpBetweenPolls(Base):
    def test_run_that_starts_and_ends_between_polls_is_collected_with_its_own_handoff(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "A body"), done(RUN_A, M1)]})
        status = None
        for _ in range(3):
            status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid()])
        first = self.conn.execute("SELECT result_text, result_digest FROM handoffs WHERE run_id=?", (RUN_A,)).fetchone()
        client.events[S1] += [chunk(RUN_B, M2, 0, 1, 0, "B body"), done(RUN_B, M2)]    # never observed active
        with mock.patch.object(ws, "TERMINAL_RECHECK_SECONDS", 0.0):
            for _ in range(3):
                status = self.poll(client)
        self.assertEqual(handoff_ids(status), sorted([hid(), hid(RUN_B, M2)]))
        self.assertEqual(self.conn.execute("SELECT result_text FROM handoffs WHERE run_id=?", (RUN_B,)).fetchone()[0], "B body")
        self.assertEqual(tuple(self.conn.execute("SELECT result_text, result_digest FROM handoffs WHERE run_id=?", (RUN_A,)).fetchone()), tuple(first))

    def test_listed_change_hint_triggers_collection_immediately(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed", "updated_at": "2026-10-22T10:00:00Z"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "A body"), done(RUN_A, M1)]})
        for _ in range(3):
            self.poll(client)
        client.events[S1] += [chunk(RUN_B, M2, 0, 1, 0, "B body"), done(RUN_B, M2)]
        client.sessions[S1]["updated_at"] = "2026-10-23T10:00:00Z"
        status = self.poll(client)                       # default 120s recheck window: only the hint can explain this
        self.assertIn(hid(RUN_B, M2), handoff_ids(status))

    def test_quiet_terminal_sessions_are_not_rechecked_every_poll(self):
        client = FakeClient(sessions={S1: {"provider": "claude-cli", "status": "completed"}},
                            events={S1: [chunk(RUN_A, M1, 0, 1, 0, "A body"), done(RUN_A, M1)]})
        for _ in range(3):
            self.poll(client)
        before = len(client.collect_limits)
        self.poll(client)
        self.assertEqual(len(client.collect_limits), before)


class TimedMessages(FakeClient):
    """Mirrors AoiTalk: ordered by created_at, filtered on coalesce(updated, created) > since-5s, server_time = page max."""

    async def call(self, method, path, **kwargs):
        m = re.fullmatch(r"/api/conversations/([0-9a-f-]{36})/messages", path)
        if not m:
            return await super().call(method, path, **kwargs)
        self.calls.append((method, path))
        params = kwargs["params"]
        since = ws.parse_time(params["since"])
        rows = sorted(self.messages[m[1]], key=lambda r: ws.parse_time(r["created_at"]))
        rows = [r for r in rows if ws.parse_time(r.get("updated_at") or r["created_at"]) > since - 5][: params["limit"]]
        latest = max([ws.parse_time(r.get("updated_at") or r["created_at"]) for r in rows] + [since])
        return 200, {"success": True, "messages": rows, "server_time": ws.iso(latest)}


class MessageCursor(MessageRecovery):
    def client(self, rows):
        base = self.p0()
        return TimedMessages(sessions=base.sessions, events=base.events, runs=base.runs, messages={S1: rows})

    def stamped(self, msg, run, second, **extra):
        return {**message(msg, S1, run, "n"), "created_at": f"2026-10-22T14:00:{second:02d}", **extra}

    def reason(self):
        return self.conn.execute("SELECT recover_reason FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0]

    def test_target_beyond_the_first_page_is_found_by_created_at_paging(self):
        rows = [self.stamped(f"b{i:07d}-0000-4000-8000-000000000000", RUN_B, i * 6) for i in range(8)]
        rows += [{**message(M1, S1, RUN_A, "late body"), "created_at": "2026-10-22T14:00:55"}]
        with mock.patch.object(ws, "MESSAGE_LIMITS", (5,)):
            for _ in range(4):
                status = self.poll(self.client(rows))
        self.assertEqual(handoff_ids(status), [hid()])

    def test_updated_at_of_an_early_row_cannot_skip_unread_rows(self):
        rows = [self.stamped(f"b{i:07d}-0000-4000-8000-000000000000", RUN_B, i * 6) for i in range(5)]
        rows[0]["updated_at"] = "2026-10-22T15:00:00"          # would drag server_time past the target
        rows += [{**message(M1, S1, RUN_A, "late body"), "created_at": "2026-10-22T14:00:40"}]
        client = self.client(rows)
        with mock.patch.object(ws, "MESSAGE_LIMITS", (5,)):
            for _ in range(4):
                status = self.poll(client)
        self.assertEqual(handoff_ids(status), [hid()])

    def test_equal_timestamps_beyond_a_page_stay_pending_without_skipping(self):
        rows = [self.stamped(f"b{i:07d}-0000-4000-8000-000000000000", RUN_B, 10) for i in range(6)]
        rows += [{**message(M1, S1, RUN_A, "late body"), "created_at": "2026-10-22T14:00:10"}]
        with mock.patch.object(ws, "MESSAGE_LIMITS", (5,)):
            for _ in range(4):
                status = self.poll(self.client(rows))
        self.assertEqual(status["ready_handoffs"], [])
        self.assertEqual(self.reason(), "message_not_in_bounded_page")


class GatewayRecovery(unittest.TestCase):
    THREAD = "33333333-3333-4333-8333-333333333333"
    THREAD_2 = "44444444-4444-4444-8444-444444444444"
    JOB_A = "00000000-0000-4000-8000-000000000001"
    JOB_B = "00000000-0000-4000-8000-000000000002"
    JOB_C = "00000000-0000-4000-8000-000000000003"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "D80-runtime"
        self.jobs = self.runtime / "jobs"
        self.jobs.mkdir(parents=True)
        self.node = self.root / "node.exe"
        self.node.write_bytes(b"node fixture")
        self.entrypoint = self.root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        self.entrypoint.parent.mkdir(parents=True)
        self.entrypoint.write_text("// official path fixture", encoding="utf-8")
        self.ledger_dir = self.root / "ledger"
        self.addCleanup(self.temp.cleanup)
        self.conn = ws.connect(self.ledger_dir)
        self.addCleanup(self.close_connection)

    def close_connection(self):
        connection = getattr(self, "conn", None)
        if connection is not None:
            connection.close()

    @staticmethod
    def jsonl(text="final", thread=None, *, terminal="turn.completed", extra_after=False, two_threads=False):
        thread = thread or GatewayRecovery.THREAD
        events = [{"type": "thread.started", "thread_id": thread}]
        if two_threads:
            events.append({"type": "thread.started", "thread_id": GatewayRecovery.THREAD_2})
        events.append({"type": "item.completed", "thread_id": thread,
                       "item": {"id": "item-final", "type": "agent_message", "text": text}})
        events.append({"type": terminal, "thread_id": thread})
        if extra_after:
            events.append({"type": "item.started", "thread_id": thread, "item": {"id": "after"}})
        return b"".join(json.dumps(event, ensure_ascii=False).encode("utf-8") + b"\n" for event in events)

    def create_job(self, job_id=None, *, text="final", status="exited", exit_code=0, data=None, stdout_ref=None):
        job_id = job_id or self.JOB_A
        directory = self.jobs / job_id
        directory.mkdir(parents=True, exist_ok=True)
        metadata = {"job_id": job_id, "status": status, "exit_code": exit_code, "started_at": 123.0,
                    "command": [str(self.node), str(self.entrypoint), "exec", "-m", "gpt-test", "--json", "private prompt"],
                    "cwd": str(self.root / "D41-checkout"), "runtime_dir": str(self.runtime),
                    "stdout_path": stdout_ref or str(directory / "stdout.log")}
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        (directory / "stdout.log").write_bytes(self.jsonl(text) if data is None else data)
        return directory, metadata

    def observe(self, now=None):
        now = time.time() if now is None else now
        plan = ws.make_plan(self.conn, now)
        fetched = ws.Fetched(now)
        fetched.gateway_cursor = plan.get("gateway_cursor", "")
        fetched.gateway_previous = plan.get("gateway_jobs", {})
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime):
            asyncio.run(ws._fetch_jobs(fetched))
        return fetched, plan

    def poll_gateway(self, now=None, conn=None):
        conn = conn or self.conn
        now = time.time() if now is None else now
        plan = ws.make_plan(conn, now)
        fetched = ws.Fetched(now)
        fetched.gateway_cursor = plan.get("gateway_cursor", "")
        fetched.gateway_previous = plan.get("gateway_jobs", {})
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime):
            asyncio.run(ws._fetch_jobs(fetched))
        return ws.commit(conn, fetched, plan), fetched

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_exited_zero_preserves_exact_final_text_and_atomic_handoff(self):
        original = "  final\nbody with spacing  "
        self.create_job(text=original)
        summary, fetched = self.poll_gateway(1000)
        row = self.conn.execute("SELECT * FROM handoffs").fetchone()
        self.assertEqual(row["result_text"], original)
        self.assertEqual(row["result_digest"], hashlib.sha256(original.encode("utf-8")).hexdigest())
        self.assertEqual((row["source_type"], row["source_id"], row["provider"], row["terminal_status"]),
                         ("gateway_codex", self.JOB_A, "codex-cli", "turn_completed"))
        self.assertEqual((self.count("handoffs"), self.count("outbox")), (1, 1))
        prov = ws.pending_view(self.conn, 1000)["items"][0]["gateway_provenance"]
        self.assertEqual((prov["job_id"], prov["thread_id"], prov["execution_status"], prov["result_digest"]),
                         (self.JOB_A, self.THREAD, "succeeded", hashlib.sha256(original.encode("utf-8")).hexdigest()))
        status_json = json.dumps(ws.build_status(self.conn, 1000))
        self.assertNotIn("private prompt", status_json)
        self.assertNotIn(original, status_json)
        self.assertEqual(fetched.gateway_summary["handoffs"], 1)

    def test_running_linger_saves_turn_without_claiming_execution_success(self):
        self.create_job(status="running", exit_code=None, text="completed turn")
        self.poll_gateway(1000)
        status = ws.build_status(self.conn, 1000)
        row = self.conn.execute("SELECT terminal_status FROM handoffs").fetchone()
        self.assertEqual(row[0], "turn_completed")
        self.assertEqual(status["gateway_codex"]["jobs"][0]["execution_status"], "unknown")
        self.assertEqual(status["gateway_codex"]["jobs"][0]["process_status"], "running")

    def test_normal_process_metadata_updates_do_not_change_launch_identity(self):
        directory, metadata = self.create_job(status="running", exit_code=None, text="linger")
        self.poll_gateway(1000)
        before = self.conn.execute("SELECT launch_identity,handoff_id FROM gateway_jobs").fetchone()
        metadata.update(status="exited", exit_code=0, finished_at=124.0, pid=1234, worker_pid=5678)
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.poll_gateway(1001)
        after = self.conn.execute("SELECT launch_identity,handoff_id,process_status,exit_code,execution_status FROM gateway_jobs").fetchone()
        self.assertEqual((after["launch_identity"], after["handoff_id"]), (before["launch_identity"], before["handoff_id"]))
        self.assertEqual((after["process_status"], after["exit_code"], after["execution_status"]), ("exited", 0, "succeeded"))
        self.assertEqual((self.count("handoffs"), self.count("outbox")), (1, 1))

    def test_nonzero_process_exit_is_reported_separately_from_completed_turn(self):
        self.create_job(status="exited", exit_code=17, text="turn text")
        self.poll_gateway(1000)
        row = self.conn.execute("SELECT terminal_status FROM handoffs").fetchone()
        self.assertEqual(row[0], "turn_completed")
        job = ws.build_status(self.conn, 1000)["gateway_codex"]["jobs"][0]
        self.assertEqual((job["turn_status"], job["execution_status"], job["error_code"]),
                         ("turn_completed", "failed", "nonzero_exit"))

    def test_same_payload_on_different_jobs_has_distinct_handoffs(self):
        self.create_job(self.JOB_A, text="same")
        self.create_job(self.JOB_B, text="same")
        self.poll_gateway(1000)
        self.poll_gateway(1001)
        self.assertEqual(self.count("handoffs"), 2)
        ids = {row[0] for row in self.conn.execute("SELECT handoff_id FROM handoffs")}
        self.assertEqual(ids, {f"gateway-codex:{self.JOB_A}:{self.THREAD}", f"gateway-codex:{self.JOB_B}:{self.THREAD}"})

    def test_replay_restart_and_received_acknowledged_state_are_exactly_once(self):
        self.create_job(text="persist")
        self.poll_gateway(1000)
        hid = f"gateway-codex:{self.JOB_A}:{self.THREAD}"
        digest = self.conn.execute("SELECT result_digest FROM outbox WHERE handoff_id=?", (hid,)).fetchone()[0]
        ws.record_receipt(self.conn, [f"{hid}={digest}"], root_source_id="root-test", root_epoch=1, now=1001)
        ws.acknowledge(self.conn, [f"handoff:{hid}"], now=1002)
        self.poll_gateway(1003)
        self.assertEqual(self.conn.execute("SELECT delivery_state FROM outbox WHERE handoff_id=?", (hid,)).fetchone()[0], "received")
        self.assertEqual(self.conn.execute("SELECT state FROM handoffs WHERE handoff_id=?", (hid,)).fetchone()[0], "acked")
        self.assertEqual(ws.pending_view(self.conn, 1003)["items"], [])
        self.conn.close()
        self.conn = ws.connect(self.ledger_dir)
        self.poll_gateway(1004)
        self.assertEqual((self.count("handoffs"), self.count("outbox")), (1, 1))
        self.assertEqual(self.conn.execute("SELECT state FROM handoffs WHERE handoff_id=?", (hid,)).fetchone()[0], "acked")

    def test_transaction_rollback_replays_without_duplicate(self):
        self.create_job(text="rollback")
        fetched, plan = self.observe(1000)
        with mock.patch.object(ws, "_insert_handoff", side_effect=sqlite3.OperationalError("fixture")):
            with self.assertRaises(sqlite3.OperationalError):
                ws.commit(self.conn, fetched, plan)
        self.assertEqual((self.count("gateway_jobs"), self.count("handoffs"), self.count("outbox")), (0, 0, 0))
        ws.commit(self.conn, fetched, plan)
        ws.commit(self.conn, fetched, plan)
        self.assertEqual((self.count("gateway_jobs"), self.count("handoffs"), self.count("outbox")), (1, 1, 1))

    def test_changed_result_for_a_pinned_job_is_quarantined_without_replacing_body(self):
        directory, _ = self.create_job(text="original")
        self.poll_gateway(1000)
        handoff = self.conn.execute("SELECT handoff_id,result_text,result_digest FROM handoffs").fetchone()
        changed = self.jsonl("rewritten", thread=self.THREAD_2)
        (directory / "stdout.log").write_bytes(changed)
        self.poll_gateway(1001)
        after = self.conn.execute("SELECT handoff_id,result_text,result_digest,state FROM handoffs").fetchone()
        self.assertEqual(tuple(after), (handoff["handoff_id"], handoff["result_text"], handoff["result_digest"], "ready"))
        self.assertEqual(self.conn.execute("SELECT result_state,error_code FROM gateway_jobs").fetchone()[:],
                         ("conflict", "result_identity_conflict"))
        self.assertEqual(self.count("outbox"), 1)

    def test_changed_launch_identity_for_a_pinned_job_is_quarantined(self):
        directory, metadata = self.create_job(text="identity")
        self.poll_gateway(1000)
        original = self.conn.execute("SELECT result_text,result_digest FROM handoffs").fetchone()
        metadata["command"][-1] = "changed private prompt"
        (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        self.poll_gateway(1001)
        self.assertEqual(tuple(self.conn.execute("SELECT result_text,result_digest FROM handoffs").fetchone()), tuple(original))
        self.assertEqual(self.conn.execute("SELECT result_state,error_code FROM gateway_jobs").fetchone()[:],
                         ("conflict", "launch_identity_conflict"))
        self.assertEqual(self.count("outbox"), 1)
        with (directory / "stdout.log").open("ab") as stream:
            stream.write(b"changed after conflict\n")
        self.poll_gateway(1002)
        self.assertEqual(self.conn.execute("SELECT result_state,error_code FROM gateway_jobs").fetchone()[:],
                         ("conflict", "launch_identity_conflict"))
        self.assertEqual(self.count("outbox"), 1)

    def test_changed_thread_identity_with_same_body_is_quarantined(self):
        directory, _ = self.create_job(text="same body")
        self.poll_gateway(1000)
        original = self.conn.execute("SELECT handoff_id,result_text,result_digest FROM handoffs").fetchone()
        (directory / "stdout.log").write_bytes(self.jsonl("same body", thread=self.THREAD_2))
        self.poll_gateway(1001)
        self.assertEqual(tuple(self.conn.execute("SELECT handoff_id,result_text,result_digest FROM handoffs").fetchone()),
                         tuple(original))
        self.assertEqual(self.conn.execute("SELECT result_state FROM gateway_jobs").fetchone()[0], "conflict")
        self.assertEqual(self.count("outbox"), 1)

    def test_path_identity_partial_invalid_terminal_and_multithread_rejects(self):
        bad = [
            (self.JOB_A, {"stdout_ref": str(self.root / "outside.log")}, self.jsonl("path")),
            (self.JOB_B, {}, self.jsonl("mismatch")),
            (self.JOB_C, {}, self.jsonl("partial")[:-1]),
        ]
        for job_id, options, data in bad:
            directory, metadata = self.create_job(job_id, data=data, **options)
            if job_id == self.JOB_B:
                metadata["job_id"] = self.JOB_A
                (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        invalid_utf = "55555555-5555-4555-8555-555555555555"
        self.create_job(invalid_utf, data=b"\xff\n")
        no_final = "66666666-6666-4666-8666-666666666666"
        self.create_job(no_final, data=self.jsonl(terminal="item.started"))
        multiple = "77777777-7777-4777-8777-777777777777"
        self.create_job(multiple, data=self.jsonl(two_threads=True))
        after_terminal = "88888888-8888-4888-8888-888888888888"
        self.create_job(after_terminal, data=self.jsonl(extra_after=True))
        multiple_terminal = "99999999-9999-4999-8999-999999999999"
        self.create_job(multiple_terminal, data=self.jsonl() + json.dumps(
            {"type": "turn.completed", "thread_id": self.THREAD}).encode("utf-8") + b"\n")
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 20):
            self.poll_gateway(1000)
        self.assertEqual(self.count("handoffs"), 0)
        errors = {row["job_id"]: row["error_code"] for row in self.conn.execute("SELECT job_id,error_code FROM gateway_jobs")}
        self.assertEqual(errors[self.JOB_A], "stdout_reference_mismatch")
        self.assertEqual(errors[self.JOB_B], "metadata_job_id_mismatch")
        self.assertIn(errors[self.JOB_C], {"partial_jsonl", "invalid_jsonl"})
        self.assertEqual(errors[invalid_utf], "invalid_jsonl")
        self.assertEqual(errors[no_final], "terminal_record_invalid")
        self.assertEqual(errors[multiple], "thread_identity_invalid")
        self.assertEqual(errors[after_terminal], "events_after_terminal")
        self.assertEqual(errors[multiple_terminal], "terminal_record_invalid")

    def test_oversized_job_and_rotation_do_not_starve_valid_jobs(self):
        self.create_job(self.JOB_A, data=self.jsonl("x" * 700))
        self.create_job(self.JOB_B, text="b")
        self.create_job(self.JOB_C, text="c")
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 1), \
             mock.patch.object(ws, "GATEWAY_MAX_FILE_BYTES", 600), \
             mock.patch.object(ws, "GATEWAY_MAX_BYTES_PER_POLL", 600):
            for t in range(4):
                self.poll_gateway(1000 + t)
        self.assertEqual(self.count("handoffs"), 2)
        self.assertEqual(self.conn.execute("SELECT result_state FROM gateway_jobs WHERE job_id=?", (self.JOB_A,)).fetchone()[0],
                         "pending")

    def test_aggregate_byte_budget_rotates_the_deferred_job_next_poll(self):
        self.create_job(self.JOB_A, text="a")
        self.create_job(self.JOB_B, text="b")
        budget = len(self.jsonl("a"))
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 1), \
             mock.patch.object(ws, "GATEWAY_MAX_FILE_BYTES", budget + 10), \
             mock.patch.object(ws, "GATEWAY_MAX_BYTES_PER_POLL", budget):
            self.poll_gateway(1000)
            self.assertEqual(self.count("handoffs"), 1)
            self.poll_gateway(1031)
        self.assertEqual(self.count("handoffs"), 2)

    def test_aggregate_byte_deferral_rotates_even_when_job_count_is_capped(self):
        self.create_job(self.JOB_A, text="a")
        self.create_job(self.JOB_B, text="b")
        self.create_job(self.JOB_C, text="c")
        budget = len(self.jsonl("a"))
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 2), \
             mock.patch.object(ws, "GATEWAY_MAX_FILE_BYTES", budget + 10), \
             mock.patch.object(ws, "GATEWAY_MAX_BYTES_PER_POLL", budget):
            self.poll_gateway(1000)
            self.assertEqual(self.count("handoffs"), 1)
            self.poll_gateway(1031)
            self.assertEqual(self.count("handoffs"), 2)
            self.poll_gateway(1062)
        self.assertEqual(self.count("handoffs"), 3)

    @staticmethod
    def _job_id(index):
        return f"{index:08x}-0000-4000-8000-{index:012x}"

    def test_new_result_is_collected_ahead_of_large_existing_history(self):
        old_ids = [self._job_id(index) for index in range(1, 31)]
        for job_id in old_ids:
            self.create_job(job_id, text="historical")
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 4):
            for t in range(30):
                self.poll_gateway(1000 + t)
            self.assertEqual(self.count("handoffs"), len(old_ids))
            new_id = self._job_id(1000)
            self.create_job(new_id, text="new result")
            summary, fetched = self.poll_gateway(1100)
        self.assertIn(new_id, {item["source_id"] for item in self.conn.execute("SELECT source_id FROM handoffs")})
        self.assertEqual(summary["gateway_handoffs"], 1)
        self.assertIn(new_id, {item["job_id"] for item in fetched.gateway_observations})

    def test_non_codex_jobs_do_not_occupy_new_result_priority_slots(self):
        for index in range(1, 7):
            directory, metadata = self.create_job(self._job_id(index), text="not codex")
            metadata["command"] = [str(self.node), "not-codex"]
            (directory / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
        new_id = self._job_id(1000)
        self.create_job(new_id, text="new codex result")
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 2):
            summary, fetched = self.poll_gateway(1000)
        self.assertIn(new_id, {item["job_id"] for item in fetched.gateway_observations})
        self.assertEqual(summary["gateway_handoffs"], 1)
        self.assertEqual(self.conn.execute("SELECT source_id FROM handoffs").fetchone()[0], new_id)

    def test_priority_hint_checks_directory_containment_before_metadata_read(self):
        directory, _ = self.create_job()
        outside = self.root / "outside" / self.JOB_A
        original_resolve = type(directory).resolve
        original_read = ws._read_stable_file
        metadata_read_attempts = 0
        containment_resolution_calls = 0

        def resolve(path, strict=False):
            nonlocal containment_resolution_calls
            if path == directory:
                containment_resolution_calls += 1
                return outside
            return original_resolve(path, strict=strict)

        def read_stable_file(path, max_bytes):
            nonlocal metadata_read_attempts
            if path.name == "metadata.json":
                metadata_read_attempts += 1
            return original_read(path, max_bytes)

        with mock.patch.object(type(directory), "resolve", autospec=True, side_effect=resolve), \
             mock.patch.object(ws, "_read_stable_file", side_effect=read_stable_file):
            hint = ws._gateway_priority_hint(self.JOB_A, directory, None, self.jobs)

        self.assertIsNone(hint)
        self.assertEqual(metadata_read_attempts, 0)
        self.assertEqual(containment_resolution_calls, 1)

    def test_completed_transition_outranks_streaming_running_jobs(self):
        done_id = self._job_id(1)
        running_ids = [self._job_id(index) for index in range(2, 8)]
        partial = self.jsonl()[:70]
        self.create_job(done_id, status="running", exit_code=None, data=partial)
        for job_id in running_ids:
            self.create_job(job_id, status="running", exit_code=None, data=partial)
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 8):
            self.poll_gateway(1000)
        self.create_job(done_id, text="finished")
        for job_id in running_ids:
            self.create_job(job_id, status="running", exit_code=None, data=partial + b" ")
        past = time.time_ns()
        os.utime(self.jobs / done_id / "stdout.log", ns=(past - 10**12, past - 10**12))
        os.utime(self.jobs / done_id / "metadata.json", ns=(past - 10**12, past - 10**12))
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 2):
            _, fetched = self.poll_gateway(1100)
        self.assertIn(done_id, {item["job_id"] for item in fetched.gateway_observations})

    def test_history_cursor_advances_while_new_results_keep_arriving(self):
        old_ids = [self._job_id(index) for index in range(1, 11)]
        partial = self.jsonl()[:70]
        for job_id in old_ids:
            self.create_job(job_id, status="running", exit_code=None, data=partial)
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 20):
            self.poll_gateway(1000)
        first_cursor = self.conn.execute("SELECT value FROM meta WHERE key='gateway_cursor'").fetchone()[0]
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 4):
            for offset in range(1, 4):
                self.create_job(self._job_id(1000 + offset), text=f"new-{offset}")
                self.poll_gateway(1000 + offset)
        final_cursor = self.conn.execute("SELECT value FROM meta WHERE key='gateway_cursor'").fetchone()[0]
        self.assertNotEqual(final_cursor, first_cursor)
        self.assertGreater(self.conn.execute("SELECT observed_at FROM gateway_jobs WHERE job_id=?", (old_ids[0],)).fetchone()[0], 1000)

    def test_priority_and_history_byte_budgets_rotate_without_duplicate_handoffs(self):
        job_ids = [self._job_id(index) for index in range(1, 5)]
        for job_id in job_ids:
            self.create_job(job_id, text="x")
        budget = 2 * len(self.jsonl("x"))
        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 4), \
             mock.patch.object(ws, "GATEWAY_MAX_FILE_BYTES", budget), \
             mock.patch.object(ws, "GATEWAY_MAX_BYTES_PER_POLL", budget):
            self.poll_gateway(1000)
            self.poll_gateway(1031)
        self.assertEqual(self.count("handoffs"), 4)
        self.assertEqual(self.count("outbox"), 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(DISTINCT handoff_id) FROM handoffs").fetchone()[0], 4)

    def test_growth_between_stat_and_read_cannot_exceed_poll_byte_limit(self):
        directory, _ = self.create_job(self.JOB_A, text="a")
        self.create_job(self.JOB_B, text="b")
        budget = len(self.jsonl("a"))
        reader = ws._read_stable_file
        grew = False

        def grow_then_read(path, max_bytes):
            nonlocal grew
            if path.name == "stdout.log" and path.parent.name == self.JOB_A and not grew:
                grew = True
                with path.open("ab") as stream:
                    stream.write(b"z" * 200)
            return reader(path, max_bytes)

        with mock.patch.object(ws, "GATEWAY_MAX_JOBS_PER_POLL", 1), \
             mock.patch.object(ws, "GATEWAY_MAX_FILE_BYTES", budget + 500), \
             mock.patch.object(ws, "GATEWAY_MAX_BYTES_PER_POLL", budget), \
             mock.patch.object(ws, "_read_stable_file", side_effect=grow_then_read):
            self.poll_gateway(1000)
            self.poll_gateway(1031)
        self.assertTrue(grew)
        self.assertEqual(self.count("handoffs"), 1)
        self.assertEqual(self.conn.execute("SELECT result_state,error_code FROM gateway_jobs WHERE job_id=?",
                                           (self.JOB_A,)).fetchone()[:], ("pending", "poll_byte_budget"))
        self.assertEqual(self.conn.execute("SELECT source_id FROM handoffs").fetchone()[0], self.JOB_B)

    def test_running_job_final_append_is_new_evidence_during_backoff(self):
        directory, _ = self.create_job(status="running", exit_code=None, data=self.jsonl()[:70])
        fetched, plan = self.observe(1000)
        ws.commit(self.conn, fetched, plan)
        log = directory / "stdout.log"
        with log.open("ab") as stream:
            stream.write(self.jsonl()[70:])
        plan = ws.make_plan(self.conn, 1001)
        fetched = ws.Fetched(1001)
        fetched.gateway_cursor = plan["gateway_cursor"]
        fetched.gateway_previous = plan["gateway_jobs"]
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime):
            asyncio.run(ws._fetch_jobs(fetched))
        self.assertEqual(fetched.gateway_observations[0]["result_state"], "succeeded")
        self.assertNotIn("reuse", fetched.gateway_observations[0])
        ws.commit(self.conn, fetched, plan)
        self.assertEqual(self.count("handoffs"), 1)
        row = self.conn.execute("SELECT process_status,execution_status FROM gateway_jobs").fetchone()
        self.assertEqual(tuple(row), ("running", "unknown"))

    def test_identity_conflict_does_not_reopen_backoff_on_stdout_stat_change(self):
        conflicting = self.jsonl() + json.dumps({
            "type": "item.started", "thread_id": self.THREAD_2,
        }).encode("utf-8") + b"\n"
        directory, _ = self.create_job(status="running", exit_code=None, data=conflicting)
        self.poll_gateway(1000)
        self.assertEqual(self.conn.execute("SELECT error_code FROM gateway_jobs").fetchone()[0], "thread_identity_conflict")
        with (directory / "stdout.log").open("ab") as stream:
            stream.write(self.jsonl()[0:20])
        plan = ws.make_plan(self.conn, 1001)
        fetched = ws.Fetched(1001)
        fetched.gateway_cursor = plan["gateway_cursor"]
        fetched.gateway_previous = plan["gateway_jobs"]
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime), \
             mock.patch.object(ws.work_dispatcher_codex, "parse_codex_jsonl", side_effect=AssertionError("identity conflict stays backed off")):
            asyncio.run(ws._fetch_jobs(fetched))
        self.assertTrue(fetched.gateway_observations[0]["reuse"])
        self.assertEqual(fetched.gateway_observations[0]["error_code"], "thread_identity_conflict")

    def test_gateway_collection_survives_aotalk_configuration_failure(self):
        self.create_job(text="offline local result")
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime), \
             mock.patch.object(aoitalk, "load_settings", side_effect=aoitalk.ClipError("connection_error")):
            fetched = asyncio.run(ws.default_fetch(ws.make_plan(self.conn, 1000)))
        self.assertIn("gateway_codex", fetched.sources)
        self.assertEqual(fetched.sources["aoitalk"]["error"], "configuration_error")
        ws.commit(self.conn, fetched, ws.make_plan(self.conn, 1000))
        self.assertEqual(self.count("handoffs"), 1)
        status = ws.build_status(self.conn, 1000)
        self.assertFalse(status["source_completeness"]["aoitalk"]["fresh"])
        self.assertTrue(status["source_completeness"]["gateway_codex"]["fresh"])

    def test_gateway_source_failure_is_not_reported_fresh(self):
        plan = ws.make_plan(self.conn, 1000)
        fetched = ws.Fetched(1000)
        with mock.patch.object(ws, "_scan_gateway_jobs", side_effect=ws.SupervisorError("runtime_unavailable")):
            asyncio.run(ws._fetch_jobs(fetched))
        ws.commit(self.conn, fetched, plan)
        status = ws.build_status(self.conn, 1000)
        self.assertFalse(status["source_completeness"]["jobs"]["fresh"])
        self.assertFalse(status["source_completeness"]["gateway_codex"]["fresh"])

    def test_gateway_collection_survives_aotalk_login_failure(self):
        self.create_job(text="offline after login error")
        failure = ws.cli.CliSessionError("authentication_failed", 401)
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime), \
             mock.patch.object(aoitalk, "load_settings", return_value=SETTINGS), \
             mock.patch.object(ws.cli.CliSessionClient, "login_cli_session", new=mock.AsyncMock(side_effect=failure)):
            fetched = asyncio.run(ws.default_fetch(ws.make_plan(self.conn, 1000)))
        self.assertEqual(fetched.sources["aoitalk"]["error"], "authentication_failed")
        ws.commit(self.conn, fetched, ws.make_plan(self.conn, 1000))
        self.assertEqual(self.count("handoffs"), 1)

    def test_show_includes_gateway_provenance_and_verified_digest(self):
        self.create_job(text="show me")
        self.poll_gateway(1000)
        handoff = self.conn.execute("SELECT handoff_id FROM handoffs").fetchone()[0]
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(ws.main(["--dir", str(self.ledger_dir), "show", handoff]), 0)
        shown = json.loads(output.getvalue())
        self.assertTrue(shown["digest_verified"])
        self.assertEqual(shown["gateway_provenance"]["thread_id"], self.THREAD)
        self.assertEqual(shown["result_text"], "show me")


class GatewayLedgerMigration(unittest.TestCase):
    def test_v4_to_v5_is_backup_backed_and_does_not_rewrite_delivery_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            conn = ws.connect(directory)
            ws._insert_handoff(conn, handoff_id="legacy:one", source_type="task", source_id="a" * 32,
                               provider=None, status="completed", now=10, text="existing result")
            digest = conn.execute("SELECT result_digest FROM outbox WHERE handoff_id='legacy:one'").fetchone()[0]
            ws.record_receipt(conn, [f"legacy:one={digest}"], root_source_id="root-test", root_epoch=1, now=11)
            ws.acknowledge(conn, ["handoff:legacy:one"], now=12)
            conn.execute("INSERT OR IGNORE INTO source_heartbeats(source_id,source_type,observed_at,via,error_code) VALUES('old','observer_source',1,'monitor',NULL)")
            before = {table: [tuple(row) for row in conn.execute(f"SELECT * FROM {table}")]
                      for table in ("handoffs", "outbox", "acknowledgments", "source_heartbeats")}
            conn.execute("DROP TABLE gateway_jobs")
            conn.execute("UPDATE meta SET value='4' WHERE key='ledger_version'")
            conn.close()
            conn = ws.connect(directory)
            try:
                self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()[0], "5")
                for table in ("handoffs", "outbox", "acknowledgments", "source_heartbeats"):
                    self.assertEqual([tuple(row) for row in conn.execute(f"SELECT * FROM {table}")], before[table])
                backup = list(directory.glob("state.sqlite3.pre-v5-*.bak"))
                self.assertEqual(len(backup), 1)
                with contextlib.closing(sqlite3.connect(backup[0])) as old:
                    self.assertIsNone(old.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gateway_jobs'").fetchone())
            finally:
                conn.close()
