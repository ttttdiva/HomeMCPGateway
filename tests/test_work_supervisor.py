"""Work supervisor fixture tests: no live AoiTalk, no network, no provider process.

The multi-chunk fixtures follow the real backend (41 cli_development_sessions._bounded_text_chunk): each
chunk's ``text_end`` is ITS OWN ``end``, not the end of the whole message."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
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
            backups = list(directory.glob("state.sqlite3.pre-v4-*.bak"))
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
            self.assertEqual(conn.execute("SELECT value FROM meta WHERE key='ledger_version'").fetchone()[0], "4")
            self.assertEqual(conn.execute("SELECT status FROM runs WHERE run_id=?", (RUN_A,)).fetchone()[0], "unknown")
            states = {r[0]: r[1] for r in conn.execute("SELECT handoff_id, delivery_state FROM outbox")}
            self.assertEqual(states, {f"cli:{S1}": "superseded", f"task:{T1}": "received"})   # nothing superseded is revived
            pending = ws.pending_view(conn, time.time())
            self.assertEqual(pending["total_pending"], 0)
            conn.close()
            ws.connect(directory).close()                                       # idempotent: no second backup
            self.assertEqual(len(list(directory.glob("state.sqlite3.pre-v4-*.bak"))), 1)

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
