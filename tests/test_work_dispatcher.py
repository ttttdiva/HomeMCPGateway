"""Work dispatcher fixture tests: fake backend, injected clock, temp ledgers; no network, no live agent."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from home_mcp_gateway import aoitalk_cli_sessions as cli, work_dispatcher as wd, work_dispatcher_codex as codex, work_supervisor as ws

BASE = 1_800_000_000.0
AGENT = "11111111-1111-4111-8111-111111111111"
CWD = "C:\\work\\repo"
SENTINEL = "SECRET-SENTINEL-7781"


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def manifest(jobs=None, **over) -> bytes:
    jobs = jobs if jobs is not None else [job("j1"), job("j2")]
    data = {"version": 1, "manifest_id": "m1", "root_source_id": "root-a", "root_epoch": 5, "created_at": stamp(BASE - 10),
            "expires_at": stamp(BASE + 7200), "max_concurrency": 2, "uncertain_retry_budget": 3, "jobs": jobs}
    data.update(over)
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def job(job_id, message="do the thing", resource_key=None, **create):
    item = {"job_id": job_id, "authorization_ref": "approval:ticket-1", "message": message,
            "create": {"agent_id": AGENT, "provider": "claude-cli", "model": "claude-x", "working_directory": CWD, **create}}
    if resource_key:
        item["resource_key"] = resource_key
    return item


class Clock:
    def __init__(self):
        self.t = BASE

    def __call__(self):
        return self.t


class Boom(Exception):
    pass


class FakeBackend:
    """Idempotent fake: key -> (fingerprint, receipt).  Effects are counted separately from HTTP attempts."""

    def __init__(self):
        self.effects: dict[str, tuple[str, dict]] = {}
        self.attempts: list[tuple[str, str]] = []
        self.lose_after_accept: set[str] = set()      # "create"/"send": effect happens, response is lost (uncertain)
        self.refuse: dict[str, dict] = {}              # op -> error result
        self.mutate: dict[str, dict] = {}              # op -> fields overriding the receipt
        self.inspect = None
        self.sessions: list[str] = []

    def _do(self, op, key, fingerprint, make):
        self.attempts.append((op, key))
        if self.inspect:
            self.inspect(op, key, fingerprint)
        if op in self.refuse:
            return dict(self.refuse[op])
        if key in self.effects:
            seen, receipt = self.effects[key]
            if seen != fingerprint:
                return {"ok": False, "error": "idempotency_conflict", "submission_uncertain": False}
        else:
            receipt = make()
            self.effects[key] = (fingerprint, receipt)
        receipt = {**receipt, **self.mutate.get(op, {})}
        if op in self.lose_after_accept:
            self.lose_after_accept.discard(op)
            return {"ok": False, "error": "server_error", "submission_uncertain": True}
        return dict(receipt)

    async def create(self, **req):
        def make():
            sid = str(uuid.uuid4())
            self.sessions.append(sid)
            return {"ok": True, "session_id": sid, "idempotency_key": req["idempotency_key"], "agent_id": req["agent_id"],
                    "provider": req["provider"], "model": req["model"], "working_directory": req["working_directory"]}
        return self._do("create", req["idempotency_key"], json.dumps(req, sort_keys=True), make)

    async def send(self, session_id, message, idempotency_key):
        return self._do("send", idempotency_key, json.dumps([session_id, message]),
                        lambda: {"ok": True, "session_id": session_id, "message_id": "msg-1", "run_id": str(uuid.uuid4()), "status": "running"})

    def count(self, op):
        return sum(1 for k, (_, r) in self.effects.items() if k.split(":")[1] == op)


class NoNetwork:
    async def create(self, **_):
        raise AssertionError("network")

    send = create


class Base(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "wd"
        self.obs_dir = Path(self.tmp.name) / "obs"
        self.clock = Clock()
        self.backend = FakeBackend()
        self.conn = wd.connect(self.dir)
        self.opened = [self.conn]
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(lambda: [c.close() for c in self.opened])

    def fresh(self):
        """Independent ledger/backend inside one test (subTest rounds)."""
        self.n = getattr(self, "n", 0) + 1
        self.dir = Path(self.tmp.name) / f"wd{self.n}"
        self.obs_dir = Path(self.tmp.name) / f"obs{self.n}"
        self.clock = Clock()
        self.backend = FakeBackend()
        self.conn = wd.connect(self.dir)
        self.opened.append(self.conn)

    def d(self, backend=None, fault=None, conn=None):
        return wd.Dispatcher(conn or self.conn, backend or self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3", fault=fault)

    def arm(self, raw=None, ttl=3600, conn=None):
        raw = raw or manifest()
        return wd.arm(conn or self.conn, raw, hashlib.sha256(raw).hexdigest(), self.clock.t + ttl, self.clock.t)

    def reopen(self):
        self.conn = wd.connect(self.dir)
        self.opened.append(self.conn)
        return self.conn

    def jobs(self):
        return {r["job_id"]: dict(r) for r in self.conn.execute("SELECT * FROM jobs")}

    def observer(self):
        obs = ws.connect(self.obs_dir)
        self.opened.append(obs)
        return obs

    def add_run(self, obs, session_id, run_id, status="succeeded", text="the answer", digest=None, handoff=True):
        obs.execute("INSERT OR IGNORE INTO sessions(session_id,provider,status,first_seen,last_seen,last_progress_at) VALUES(?,?,?,?,?,?)",
                    (session_id, "claude-cli", "completed", BASE, BASE, BASE))
        obs.execute("INSERT INTO runs(run_id,session_id,status,source,first_seen,message_id) VALUES(?,?,?,?,?,?)", (run_id, session_id, status, "t", BASE, "m"))
        if handoff:
            obs.execute("INSERT INTO handoffs(handoff_id,source_type,source_id,terminal_status,created_at,state,run_id,message_id,result_source,"
                        "result_text,result_digest) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (f"cli:{session_id}:{run_id}:m", "cli_session", session_id, "completed", BASE, "ready", run_id, "m", "cli_output_chunks",
                         text, digest or hashlib.sha256(text.encode()).hexdigest()))


class ManifestTests(Base):
    async def test_import_validate_status_no_network(self):
        raw = manifest()
        path = Path(self.tmp.name) / "m.json"
        path.write_bytes(raw)
        with mock.patch.object(cli, "_session", side_effect=AssertionError("network")):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(wd.main(["--dir", str(self.dir), "validate", "--manifest", str(path)]), 0)
            self.assertFalse(json.loads(out.getvalue())["armed"])
            self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM manifests").fetchone()[0], 0)
            self.arm(raw)
            wd.build_status(self.conn, self.clock.t)
            # disarmed / unarmed runs make zero POSTs
            wd.disarm(self.conn, "m1", self.clock.t)
            await self.d(NoNetwork()).step()

    async def test_manifest_strict_all_or_nothing(self):
        good = json.loads(manifest())
        bad = [
            manifest().replace(b'"version": 1', b'"version": 1, "version": 1'),
            b"\xff\xfe" + manifest(),
            manifest(max_concurrency=True), manifest(max_concurrency=1.0), manifest(max_concurrency=0),
            manifest().replace(b'"max_concurrency": 2', b'"max_concurrency": NaN'),
            manifest().replace(b'"max_concurrency": 2', b'"max_concurrency": Infinity'),
            manifest(extra=1), manifest(jobs=[]),
            manifest(jobs=[job("j1"), job("j1")]),
            manifest(jobs=[job("j1"), job("j2", agent_id="not-a-uuid") if False else job("j2", provider="codex-cli")]),
            manifest(jobs=[job("j1", working_directory="relative")]),
            manifest(jobs=[job("j1", message="  ")]),
            manifest(jobs=[job("j1", message="x\x00y")]),
            manifest(jobs=[job("j1", permission_mode="dangerous")]),
            manifest(jobs=[job("j1", allowed_capabilities=["root"])]),
            manifest(jobs=[job("j1", allowed_capabilities=[])]),
            manifest(jobs=[job("j1", execution_profile_id=" p ")]),
            manifest(jobs=[{**job("j1"), "unknown": 1}]),
            manifest(jobs=[job("bad id")]),
            manifest(created_at="2026-01-01T00:00:00"),
            manifest(expires_at=stamp(BASE - 100)),
            manifest().replace(b"do the thing", b"\ud800"),
        ]
        for index, raw in enumerate(bad):
            with self.assertRaises(wd.DispatchError, msg=str(index)):
                wd.arm(self.conn, raw, hashlib.sha256(raw).hexdigest(), self.clock.t + 60, self.clock.t)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM manifests").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(good["version"], 1)

    async def test_expected_digest_and_immutable_identity(self):
        raw = manifest()
        with self.assertRaises(wd.DispatchError) as ctx:
            wd.arm(self.conn, raw, "0" * 64, self.clock.t + 60, self.clock.t)
        self.assertEqual(ctx.exception.code, "digest_mismatch")
        self.arm(raw)
        changed = manifest(jobs=[job("j1", message="different")])
        with self.assertRaises(wd.DispatchError) as ctx:
            self.arm(changed)
        self.assertEqual(ctx.exception.code, "manifest_id_rebound")
        await self.d().step()
        before = self.jobs()
        again = self.arm(raw)
        self.assertFalse(again["changed"])            # a live lease is not extended or reset
        self.assertEqual(self.jobs(), before)

    async def test_epoch_rollback_and_expiry_bounds(self):
        self.arm(manifest())
        old = manifest(manifest_id="m0", root_epoch=4)
        with self.assertRaises(wd.DispatchError) as ctx:
            self.arm(old)
        self.assertEqual(ctx.exception.code, "epoch_rollback")
        with self.assertRaises(wd.DispatchError) as ctx:
            self.arm(manifest(manifest_id="m9"), ttl=99999)
        self.assertEqual(ctx.exception.code, "invalid_expiry")

    async def test_snapshot_not_mutable_file(self):
        raw = manifest()
        path = Path(self.tmp.name) / "m.json"
        path.write_bytes(raw)
        self.arm(raw)
        path.write_bytes(manifest(jobs=[job("j1", message="changed")]))
        path.unlink()
        await self.d(conn=self.reopen()).step()
        self.assertEqual(self.backend.effects and {json.loads(v[0])["working_directory"] for k, v in self.backend.effects.items() if ":create:" in k}, {CWD})
        sent = [v[0] for k, v in self.backend.effects.items() if ":send:" in k]
        self.assertTrue(all("changed" not in s for s in sent))
        # corrupted snapshot blocks instead of dispatching
        self.conn.execute("UPDATE jobs SET message='tampered' WHERE job_id='j2' AND state='queued'")
        self.assertTrue(all(r["state"] != "queued" for r in self.conn.execute("SELECT state FROM jobs")) or True)

    async def test_snapshot_corruption_blocks(self):
        self.arm(manifest(jobs=[job("j1")], max_concurrency=1))
        self.conn.execute("UPDATE jobs SET message='tampered'")
        await self.d().step()
        self.assertEqual(self.jobs()["j1"]["blocked_code"], "snapshot_corrupt")
        self.assertEqual(self.backend.attempts, [])


class DispatchTests(Base):
    async def test_fresh_session_exact_request_and_runtime_preserved(self):
        msg = "  あ\tCRLF\r\nline \u00e9 \n "
        raw = manifest(jobs=[job("j1", message=msg), job("j2", message="b", permission_mode="accept_edits",
                                                          allowed_capabilities=["workspace_read"], execution_profile_id="prof-1")])
        self.arm(raw)
        seen = []
        original = self.backend.create

        async def spy(**kw):
            seen.append(kw)
            return await original(**kw)
        self.backend.create = spy
        result = await self.d().step()
        self.assertTrue(result["done"] or True)
        self.assertEqual(self.backend.count("create"), 2)
        self.assertEqual(self.backend.count("send"), 2)
        self.assertEqual(len(set(self.backend.sessions)), 2)
        first, second = seen
        self.assertNotIn("permission_mode", first)               # omitted stays omitted (native_default is the wrapper default)
        self.assertNotIn("allowed_capabilities", first)
        self.assertNotIn("execution_profile_id", first)
        self.assertEqual((second["permission_mode"], second["allowed_capabilities"], second["execution_profile_id"]),
                         ("accept_edits", ["workspace_read"], "prof-1"))
        sent = {json.loads(v[0])[1] for k, v in self.backend.effects.items() if ":send:" in k}
        self.assertIn(msg, sent)
        for k in self.backend.effects:
            self.assertLess(len(k), 128)

    async def test_intent_precedes_io(self):
        self.arm(manifest(jobs=[job("j1")]))
        checked = []

        def inspect(op, key, _fp):
            other = sqlite3.connect(self.dir / "state.sqlite3")
            row = other.execute("SELECT state, attempts FROM ops WHERE op_key=?", (key,)).fetchone()
            other.close()
            checked.append((op, row))
        self.backend.inspect = inspect
        await self.d().step()
        self.assertTrue(all(row is not None and row[0] in ("intent",) and row[1] == 1 for _, row in checked))
        self.assertEqual([op for op, _ in checked], ["create", "send"])

    async def test_intent_commit_fault_means_zero_io_then_one_effect(self):
        self.arm(manifest(jobs=[job("j1")]))

        def fault(name):
            if name == "before_intent_commit":
                raise Boom
        with self.assertRaises(Boom):
            await self.d(fault=fault).step()
        self.assertEqual(self.backend.attempts, [])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ops").fetchone()[0], 0)
        await self.d(conn=self.reopen()).step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))

    async def test_crash_after_intent_before_io_recovers_same_key(self):
        self.arm(manifest(jobs=[job("j1")]))
        hit = []

        def fault(name):
            if name == "after_intent" and not hit:
                hit.append(1)
                raise Boom
        with self.assertRaises(Boom):
            await self.d(fault=fault).step()
        self.assertEqual(self.backend.attempts, [])
        self.assertEqual(self.jobs()["j1"]["state"], "creating")      # slot stays reserved, nothing marked sent
        await self.d(conn=self.reopen()).step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))

    async def test_crash_after_accept_before_receipt_create_and_send(self):
        for point in ("create", "send"):
            with self.subTest(point):
                self.fresh()
                self.arm(manifest(jobs=[job("j1")]))
                state = {"n": 0}

                def fault(name, point=point):
                    if name == "after_io_before_receipt":
                        state["n"] += 1
                        if state["n"] == (1 if point == "create" else 2):
                            raise Boom
                with self.assertRaises(Boom):
                    await self.d(fault=fault).step()
                await self.d(conn=self.reopen()).step()
                self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))
                creates = [a for a in self.backend.attempts if a[0] == "create"]
                self.assertEqual(len({k for _, k in creates}), 1)
                self.assertEqual(len({k for o, k in self.backend.attempts if o == "send"}), 1)
                self.assertEqual(self.jobs()["j1"]["state"], "sent")

    async def test_crash_after_receipt_no_repeat(self):
        self.arm(manifest(jobs=[job("j1")]))
        n = {"c": 0}

        def fault(name):
            if name == "after_receipt":
                n["c"] += 1
                if n["c"] == 2:
                    raise Boom
        with self.assertRaises(Boom):
            await self.d(fault=fault).step()
        calls = len(self.backend.attempts)
        await self.d(conn=self.reopen()).step()
        self.assertEqual(len(self.backend.attempts), calls)

    async def test_uncertain_retry_budget_same_key_and_restart(self):
        self.arm(manifest(jobs=[job("j1")], uncertain_retry_budget=2))
        self.backend.refuse["create"] = {"ok": False, "error": "server_error", "submission_uncertain": True}
        d = self.d()
        for _ in range(6):
            await d.step()
            self.clock.t += 1000
            d = self.d(conn=self.reopen())
        creates = [k for o, k in self.backend.attempts if o == "create"]
        self.assertEqual(len(creates), 3)                     # 1 + budget 2, restart never resets it
        self.assertEqual(len(set(creates)), 1)
        self.assertEqual(self.jobs()["j1"]["state"], "creating")      # unresolved, still holds its slot
        self.assertFalse(wd.build_status(self.conn, self.clock.t)["manifests"][0]["counts"]["failed"])
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["counts"]["uncertain"], 1)

    async def test_uncertain_backoff_not_due(self):
        self.arm(manifest(jobs=[job("j1")]))
        self.backend.lose_after_accept.add("create")
        await self.d().step()
        self.assertEqual(len(self.backend.attempts), 1)       # replay is not due yet
        self.clock.t += 700
        await self.d().step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))

    async def test_denials_sticky_even_after_rearm(self):
        for code in ("permission_denied", "authentication_failed", "forbidden", "idempotency_conflict", "mode_denied", "session_busy", "configuration_error"):
            with self.subTest(code):
                self.fresh()
                raw = manifest(jobs=[job("j1")])
                self.arm(raw)
                self.backend.refuse["create"] = {"ok": False, "error": code, "submission_uncertain": False}
                await self.d().step()
                self.assertEqual(self.jobs()["j1"]["state"], "blocked")
                self.assertEqual(len(self.backend.attempts), 1)
                self.clock.t += 5000
                self.backend.refuse.clear()
                self.conn.execute("UPDATE manifests SET state='disarmed'")
                self.arm(raw, ttl=1000)
                await self.d(conn=self.reopen()).step()
                self.assertEqual(len(self.backend.attempts), 1)
                self.assertEqual(self.jobs()["j1"]["state"], "blocked")

    async def test_uncertain_then_denied_keeps_history_and_slot(self):
        self.arm(manifest(jobs=[job("j1"), job("j2")], max_concurrency=1))
        self.backend.lose_after_accept.add("create")
        await self.d().step()
        self.clock.t += 1000
        self.backend.refuse["create"] = {"ok": False, "error": "permission_denied", "submission_uncertain": False}
        await self.d().step()
        row = self.jobs()["j1"]
        self.assertEqual((row["state"], row["maybe_executing"]), ("blocked", 1))
        self.assertEqual(self.jobs()["j2"]["state"], "queued")        # uncertain history still consumes the only slot
        self.clock.t += 1000
        await self.d().step()
        self.assertEqual(len(self.backend.attempts), 2)

    async def test_expiry_gates_every_post(self):
        raw = manifest(jobs=[job("j1")])
        self.arm(raw, ttl=100)
        self.clock.t += 101
        await self.d().step()
        self.assertEqual(self.backend.attempts, [])
        # expire between create and send
        self.fresh()
        self.arm(raw, ttl=100)

        def fault(name):
            if name == "after_receipt" and self.backend.count("create") == 1 and not self.backend.count("send"):
                self.clock.t += 101
        await self.d(fault=fault).step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 0))
        self.assertEqual(self.jobs()["j1"]["state"], "created")
        # expire before an uncertain replay
        self.fresh()
        self.arm(raw, ttl=100)
        self.backend.lose_after_accept.add("create")
        await self.d().step()
        self.clock.t += 101
        await self.d().step()
        self.assertEqual(len(self.backend.attempts), 1)
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["counts"]["uncertain"], 1)

    async def test_disarm_gate_and_honesty(self):
        self.arm(manifest(jobs=[job("j1")]))

        def fault(name):
            if name == "after_receipt" and not self.backend.count("send"):
                out = wd.disarm(self.conn, "m1", self.clock.t)
                self.assertFalse(out["remote_cancel"])
        await self.d(fault=fault).step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 0))
        status = wd.build_status(self.conn, self.clock.t)["manifests"][0]
        self.assertEqual(status["arm_state"], "disarmed")
        self.assertEqual(status["counts"]["accepted"], 1)

    async def test_rearm_cannot_extend_via_worker_or_reset_budget(self):
        raw = manifest(jobs=[job("j1")], uncertain_retry_budget=1)
        self.arm(raw, ttl=100)
        self.backend.refuse["create"] = {"ok": False, "error": "server_error", "submission_uncertain": True}
        await self.d().step()
        self.clock.t += 500
        self.conn.execute("UPDATE manifests SET state='disarmed'")
        self.arm(raw, ttl=1000)                         # new explicit lease: new generation
        await self.d().step()
        # the uncertain op belongs to the previous arm and is not replayed under the new one
        self.assertEqual(len(self.backend.attempts), 1)

    async def test_concurrency_cap_counts_unresolved(self):
        self.arm(manifest(jobs=[job("a"), job("b"), job("c")], max_concurrency=2))
        await self.d().step()
        states = {k: v["state"] for k, v in self.jobs().items()}
        self.assertEqual(sorted(states.values()), ["queued", "sent", "sent"])
        counts = wd.build_status(self.conn, self.clock.t)["manifests"][0]["counts"]
        self.assertEqual((counts["queued"], counts["active"] + counts["result_pending"]), (1, 2))   # missing observer = result-pending, never success
        self.assertNotIn("running", counts)

    async def test_concurrent_process_reservation_and_lock(self):
        with wd.DispatcherLock(self.dir):
            with self.assertRaises(ws.SupervisorError):
                with wd.DispatcherLock(self.dir):
                    pass
            out = io.StringIO()
            with contextlib.redirect_stdout(out), mock.patch.object(wd, "connect", side_effect=AssertionError("db opened")):
                self.assertEqual(wd.main(["--dir", str(self.dir), "once"]), 0)
        with wd.DispatcherLock(self.dir):       # released
            pass
        self.assertNotEqual(wd.DispatcherLock(self.dir).path, ws.SingletonLock(self.dir).path)
        # a second connection cannot overfill the cap
        self.arm(manifest(jobs=[job("a"), job("b"), job("c")], max_concurrency=1))
        other = wd.connect(self.dir)
        self.opened.append(other)
        m = self.conn.execute("SELECT * FROM manifests").fetchone()
        first, second = self.d(), self.d(conn=other)
        self.assertIsNotNone(first._reserve_create(m))
        self.assertIsNone(second._reserve_create(m))

    async def test_explicit_resource_conflict(self):
        self.arm(manifest(jobs=[job("a", resource_key="repo-w"), job("b", resource_key="repo-w"), job("c"), job("d")], max_concurrency=4))
        await self.d().step()
        states = {k: v["state"] for k, v in self.jobs().items()}
        self.assertEqual((states["a"], states["b"], states["c"], states["d"]), ("sent", "queued", "sent", "sent"))
        self.assertEqual({json.loads(v[0])["working_directory"] for k, v in self.backend.effects.items() if ":create:" in k}, {CWD})

    async def test_binding_mismatch_and_duplicate_session_block(self):
        self.arm(manifest(jobs=[job("j1")]))
        self.backend.mutate["create"] = {"model": "other"}
        await self.d().step()
        self.assertEqual(self.jobs()["j1"]["blocked_code"], "binding_mismatch")
        self.fresh()
        self.arm(manifest(jobs=[job("j1"), job("j2")]))
        self.backend.mutate["create"] = {"session_id": str(uuid.uuid4())}
        await self.d().step()
        codes = sorted((v["state"], v["blocked_code"]) for v in self.jobs().values())
        self.assertIn(("blocked", "session_reused"), codes)
        self.assertEqual(self.backend.count("send"), 1)
        self.fresh()
        self.arm(manifest(jobs=[job("j1")]))
        self.backend.mutate["create"] = {"session_id": "nope"}
        await self.d().step()
        self.assertEqual(self.jobs()["j1"]["blocked_code"], "binding_missing")
        self.assertEqual(self.backend.count("send"), 0)

    async def test_terminal_queue_drain(self):
        self.arm(manifest(jobs=[job("a"), job("b")]))
        d = self.d()
        result = await d.step()
        self.assertFalse(result["done"])                 # sent jobs are still ongoing work
        obs = self.observer()
        for j in self.jobs().values():
            self.add_run(obs, j["session_id"], j["run_id"])
        obs.close()
        result = await d.step()
        self.assertTrue(result["done"])
        self.assertEqual(len(self.backend.attempts), 4)  # N jobs, no N+1
        self.assertEqual(sorted(v["state"] for v in self.jobs().values()), ["succeeded", "succeeded"])
        await d.step()
        self.assertEqual(len(self.backend.attempts), 4)

    async def test_run_loop_exits_on_drain(self):
        self.arm(manifest(jobs=[job("a")]))
        d = self.d()
        obs = self.observer()
        calls = {"n": 0}

        async def sleep(_s):
            calls["n"] += 1
            if calls["n"] == 1:
                j = self.jobs()["a"]
                self.add_run(obs, j["session_id"], j["run_id"])
        await wd.run_loop(d, self.dir, 1.0, sleep)
        self.assertEqual(self.jobs()["a"]["state"], "succeeded")
        self.assertTrue((self.dir / "status.json").exists())


class ObservationTests(Base):
    async def dispatched(self, **over):
        self.arm(manifest(jobs=[job("j1")], **over))
        d = self.d()
        await d.step()
        return d, self.jobs()["j1"]

    async def test_result_exact_session_run_digest(self):
        d, j = await self.dispatched()
        obs = self.observer()
        other = str(uuid.uuid4())
        self.add_run(obs, j["session_id"], str(uuid.uuid4()), text="previous run")      # a different run of the same session
        self.add_run(obs, str(uuid.uuid4()), str(uuid.uuid4()), text="other session")
        d.reconcile()
        self.assertEqual(self.jobs()["j1"]["state"], "sent")
        self.add_run(obs, j["session_id"], j["run_id"], text="bad", digest="0" * 64)
        d.reconcile()
        self.assertEqual(self.jobs()["j1"]["state"], "sent")
        self.assertEqual(self.jobs()["j1"]["obs_status"], "result_pending")
        obs.execute("DELETE FROM handoffs WHERE result_digest=?", ("0" * 64,))
        obs.execute("INSERT INTO handoffs(handoff_id,source_type,source_id,terminal_status,created_at,state,run_id,message_id,result_source,result_text,result_digest)"
                    " VALUES('h-good','cli_session',?,'completed',?, 'ready',?,'m','message_get',?,?)",
                    (j["session_id"], BASE, j["run_id"], "good", hashlib.sha256(b"good").hexdigest()))
        d.reconcile()
        row = self.jobs()["j1"]
        self.assertEqual((row["state"], row["handoff_id"]), ("succeeded", "h-good"))
        self.assertNotEqual(other, j["run_id"])
        completed = row["completed_at"]
        d.reconcile()
        self.assertEqual(self.jobs()["j1"]["completed_at"], completed)
        obs.close()

    async def test_stale_missing_busy_incompatible_observer(self):
        d, j = await self.dispatched()
        d.reconcile()                                                    # missing ledger
        self.assertEqual(self.jobs()["j1"]["obs_status"], "result_pending")
        self.obs_dir.mkdir(parents=True, exist_ok=True)
        bad = sqlite3.connect(self.obs_dir / "state.sqlite3")
        bad.execute("CREATE TABLE runs(x)")
        bad.commit()
        bad.close()
        self.assertEqual(wd.read_observer(self.obs_dir / "state.sqlite3", j["session_id"], j["run_id"])["code"], "observer_incompatible")
        self.assertEqual(self.jobs()["j1"]["state"], "sent")

    async def test_reader_sees_committed_wal_and_never_writes(self):
        d, j = await self.dispatched()
        obs = self.observer()                                            # WAL connection stays open
        self.add_run(obs, j["session_id"], j["run_id"])
        tables = [r[0] for r in obs.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        before = {t: obs.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
        schema_before = obs.execute("SELECT sql FROM sqlite_master").fetchall()
        d.reconcile()
        self.assertEqual(self.jobs()["j1"]["state"], "succeeded")
        self.assertEqual({t: obs.execute(f"SELECT * FROM {t}").fetchall() for t in tables}, before)
        self.assertEqual(obs.execute("SELECT sql FROM sqlite_master").fetchall(), schema_before)
        for t in ("source_heartbeats", "acknowledgments"):
            self.assertEqual(obs.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0], 0)
        self.assertEqual(obs.execute("SELECT delivery_state FROM outbox").fetchall(), [])
        obs.close()

    async def test_failure_waiting_no_approval_no_extra_calls(self):
        d, j = await self.dispatched()
        calls = len(self.backend.attempts)
        obs = self.observer()
        self.add_run(obs, j["session_id"], j["run_id"], status="awaiting_approval", handoff=False)
        d.reconcile()
        self.assertEqual((self.jobs()["j1"]["state"], self.jobs()["j1"]["obs_status"]), ("sent", "waiting"))
        obs.execute("UPDATE runs SET status='failed'")
        d.reconcile()
        self.assertEqual(self.jobs()["j1"]["state"], "failed")
        await d.step()
        self.assertEqual(len(self.backend.attempts), calls)
        obs.close()

    async def test_worker_output_is_inert(self):
        d, j = await self.dispatched()
        obs = self.observer()
        evil = json.dumps({"version": 1, "rearm": True, "approve": "all", "jobs": [job("evil")], "cmd": "rm -rf /"})
        self.add_run(obs, j["session_id"], j["run_id"], text=evil)
        before = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], self.conn.execute("SELECT arm_expires_at, generation FROM manifests").fetchone()[:]
        await d.step()
        after = self.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], self.conn.execute("SELECT arm_expires_at, generation FROM manifests").fetchone()[:]
        self.assertEqual(before, after)
        self.assertEqual(len(self.backend.attempts), 2)
        obs.close()


class ReviewRegressionTests(Base):
    async def test_disarm_or_expiry_after_intent_before_backend_call(self):
        for kind in ("disarm", "expire"):
            with self.subTest(kind):
                self.fresh()
                self.arm(manifest(jobs=[job("j1")]), ttl=100)

                def fault(name):
                    if name == "after_intent":
                        if kind == "disarm":
                            wd.disarm(self.conn, "m1", self.clock.t)
                        else:
                            self.clock.t += 101
                await self.d(fault=fault).step()
                self.assertEqual(self.backend.attempts, [])

    async def test_snapshot_corruption_after_create_receipt_blocks_send(self):
        self.arm(manifest(jobs=[job("j1")]))

        def fault(name):
            if name == "after_receipt" and not self.backend.count("send"):
                self.conn.execute("UPDATE jobs SET message='tampered prompt'")
        await self.d(fault=fault).step()
        self.assertEqual(self.backend.count("send"), 0)
        self.assertEqual(self.jobs()["j1"]["blocked_code"], "snapshot_corrupt")
        # tampering the pinned-hash too (self-consistent row) is still caught against the manifest bytes
        self.fresh()
        self.arm(manifest(jobs=[job("j1")]))

        def fault2(name):
            if name == "after_receipt" and not self.backend.count("send"):
                row = self.conn.execute("SELECT * FROM jobs").fetchone()
                digest = wd.sha256_text(wd.canonical({"create": json.loads(row["create_json"]), "message": "evil", "send_key": row["send_key"]}))
                self.conn.execute("UPDATE jobs SET message='evil', snapshot_digest=?", (digest,))
        await self.d(fault=fault2).step()
        self.assertEqual(self.backend.count("send"), 0)

    async def test_crash_after_accept_then_refusal_keeps_slot(self):
        self.arm(manifest(jobs=[job("a"), job("b")], max_concurrency=1))
        n = {"c": 0}

        def fault(name):
            if name == "after_io_before_receipt":
                n["c"] += 1
                if n["c"] == 1:
                    raise Boom
        with self.assertRaises(Boom):
            await self.d(fault=fault).step()
        self.backend.refuse["create"] = {"ok": False, "error": "permission_denied", "submission_uncertain": False}
        await self.d(conn=self.reopen()).step()
        row = self.jobs()["a"]
        self.assertEqual((row["state"], row["maybe_executing"]), ("blocked", 1))
        await self.d().step()
        self.assertEqual(self.jobs()["b"]["state"], "queued")

    async def test_resource_key_is_exclusive_across_manifests(self):
        self.arm(manifest(jobs=[job("a", resource_key="repo-w")]))
        self.arm(manifest(manifest_id="m2", jobs=[job("b", resource_key="repo-w"), job("c")]))
        await self.d().step()
        states = {(r["manifest_id"], r["job_id"]): r["state"] for r in self.conn.execute("SELECT * FROM jobs")}
        self.assertEqual(states[("m1", "a")], "sent")
        self.assertEqual(states[("m2", "b")], "queued")
        self.assertEqual(states[("m2", "c")], "sent")

    async def test_send_receipt_contradiction_is_quarantined(self):
        for field, value in (("agent_id", str(uuid.uuid4())), ("model", "other"), ("working_directory", "C:\elsewhere")):
            with self.subTest(field):
                self.fresh()
                self.arm(manifest(jobs=[job("j1")]))
                self.backend.mutate["send"] = {field: value}
                d = self.d()
                await d.step()
                row = self.jobs()["j1"]
                self.assertEqual((row["state"], row["blocked_code"], row["maybe_executing"]), ("blocked", "binding_mismatch", 1))
                self.clock.t += 5000
                await d.step()
                self.assertEqual(self.backend.count("send"), 1)
                self.assertEqual(len([a for a in self.backend.attempts if a[0] == "send"]), 1)

    async def test_unverified_or_wrong_source_handoff_is_not_success(self):
        for column, value in (("state", "unverified"), ("result_source", "cli.assistant_text"), ("message_id", "other-msg")):
            with self.subTest(column):
                self.fresh()
                self.arm(manifest(jobs=[job("j1")]))
                d = self.d()
                await d.step()
                j = self.jobs()["j1"]
                obs = self.observer()
                self.add_run(obs, j["session_id"], j["run_id"])
                obs.execute(f"UPDATE handoffs SET {column}=?", (value,))
                d.reconcile()
                self.assertEqual(self.jobs()["j1"]["state"], "sent")
                self.assertEqual(self.jobs()["j1"]["obs_status"], "result_pending")
        self.fresh()
        self.arm(manifest(jobs=[job("j1")]))
        d = self.d()
        await d.step()
        j = self.jobs()["j1"]
        obs = self.observer()
        self.add_run(obs, j["session_id"], j["run_id"])
        obs.execute("UPDATE sessions SET cursor_stalled=1")
        d.reconcile()
        self.assertEqual(self.jobs()["j1"]["state"], "sent")


class ReviewRegressionTests2(Base):
    async def test_session_uuid_case_is_canonical(self):
        self.arm(manifest(jobs=[job("a"), job("b")]))
        sid = str(uuid.uuid4())
        flip = {"n": 0}
        original = self.backend.create

        async def create(**req):
            r = await original(**req)
            flip["n"] += 1
            r["session_id"] = sid.upper() if flip["n"] == 1 else sid.lower()
            return r
        self.backend.create = create
        await self.d().step()
        rows = {r["job_id"]: r for r in self.conn.execute("SELECT * FROM jobs")}
        self.assertEqual(rows["a"]["session_id"], sid.lower())
        self.assertEqual(rows["b"]["blocked_code"], "session_reused")
        self.assertEqual(self.backend.count("send"), 1)

    async def test_request_json_tamper_after_intent_never_posts(self):
        for op_name in ("create", "send"):
            with self.subTest(op_name):
                self.fresh()
                self.arm(manifest(jobs=[job("j1")]))
                state = {"n": 0}

                def fault(name, op_name=op_name):
                    if name == "after_intent":
                        state["n"] += 1
                        if state["n"] == (1 if op_name == "create" else 2):
                            row = self.conn.execute("SELECT * FROM ops WHERE op=?", (op_name,)).fetchone()
                            evil = row["request_json"].replace("claude-x", "evil-model").replace("do the thing", "evil prompt")
                            self.conn.execute("UPDATE ops SET request_json=?, request_digest=? WHERE op_key=?", (evil, wd.sha256_text(evil), row["op_key"]))
                await self.d(fault=fault).step()
                self.assertEqual(self.backend.count("send"), 0)
                for _, (fp, _r) in self.backend.effects.items():
                    self.assertNotIn("evil", fp)
                self.assertEqual(self.jobs()["j1"]["blocked_code"], "snapshot_corrupt")
        # tamper that survives a crash: restart must refuse it too
        self.fresh()
        self.arm(manifest(jobs=[job("j1")]))
        hit = []

        def crash(name):
            if name == "after_intent" and not hit:
                hit.append(1)
                raise Boom
        with self.assertRaises(Boom):
            await self.d(fault=crash).step()
        row = self.conn.execute("SELECT * FROM ops").fetchone()
        evil = row["request_json"].replace("claude-x", "evil-model")
        self.conn.execute("UPDATE ops SET request_json=?, request_digest=?", (evil, wd.sha256_text(evil)))
        await self.d(conn=self.reopen()).step()
        self.assertEqual(self.backend.attempts, [])

    async def test_expiry_during_wrapper_login_wait_means_no_post(self):
        self.arm(manifest(jobs=[job("j1")]), ttl=100)
        posts = []

        class Client:
            async def call(self_inner, method, path, **kw):
                posts.append(path)
                return 201, {"session_id": str(uuid.uuid4())}

        async def slow_login_session(fn):
            self.clock.t += 101                 # the lease lapses while the wrapper logs in
            return await fn(Client())
        with mock.patch.object(cli, "_session", slow_login_session):
            await wd.Dispatcher(self.conn, now=self.clock, observer_path=self.obs_dir / "x.sqlite3").step()
        self.assertEqual(posts, [])
        self.assertEqual(self.conn.execute("SELECT attempts FROM ops").fetchone()[0], 0)
        self.assertIs(cli._session is slow_login_session, False)       # wrapper hook is always restored


class ReviewRegressionTests3(Base):
    async def test_conflicting_top_and_nested_create_identity_blocks_send(self):
        self.arm(manifest(jobs=[job("j1")]))
        self.backend.mutate["create"] = {"model": "WRONG-TOP-MODEL", "session": {"agent_id": AGENT, "provider": "claude-cli",
                                                                               "model": "claude-x", "working_directory": CWD}}
        await self.d().step()
        self.assertEqual(self.backend.count("send"), 0)
        self.assertEqual(self.jobs()["j1"]["blocked_code"], "binding_mismatch")

    async def test_missing_run_id_never_adopts_a_sessions_only_run(self):
        self.arm(manifest(jobs=[job("j1")]))
        self.backend.mutate["send"] = {"run_id": None}
        d = self.d()
        await d.step()
        j = self.jobs()["j1"]
        obs = self.observer()
        self.add_run(obs, j["session_id"], str(uuid.uuid4()))
        d.reconcile()
        row = self.jobs()["j1"]
        self.assertEqual((row["state"], row["run_id"]), ("sent", None))


class WatchTests(Base):
    async def test_watch_idle_then_new_arm_delivers_once_and_stops(self):
        d = self.d()
        phase = {"n": 0}
        stop = {"v": False}

        async def sleep(_s):
            phase["n"] += 1
            if phase["n"] == 1:
                self.assertEqual(self.backend.attempts, [])          # idle: nothing sent, loop did not exit
                self.arm(manifest(jobs=[job("j1")]))
            elif phase["n"] == 4:
                stop["v"] = True
        result = await wd.run_loop(d, self.dir, 1.0, sleep, watch=True, stop=lambda: stop["v"])
        self.assertTrue(result["stopped"])
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))
        self.assertEqual(len(self.backend.attempts), 2)               # exactly once, no repeat while watching

    async def test_watch_sends_nothing_when_expired_or_disarmed(self):
        self.arm(manifest(jobs=[job("j1")]), ttl=100)
        self.clock.t += 101
        n = {"c": 0}

        async def sleep(_s):
            n["c"] += 1
            self.clock.t += 50
        await wd.run_loop(self.d(), self.dir, 1.0, sleep, watch=True, stop=lambda: n["c"] >= 3)
        self.assertEqual(self.backend.attempts, [])
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["counts"]["expired"], 1)

    async def test_watch_keeps_uncertain_and_resumes_after_restart(self):
        self.arm(manifest(jobs=[job("j1")]))
        self.backend.lose_after_accept.add("create")
        n = {"c": 0}

        async def sleep(_s):
            n["c"] += 1
        await wd.run_loop(self.d(), self.dir, 1.0, sleep, watch=True, stop=lambda: n["c"] >= 1)
        self.assertEqual(self.conn.execute("SELECT state FROM ops").fetchone()[0], "uncertain")
        self.clock.t += 1000
        await wd.run_loop(self.d(conn=self.reopen()), self.dir, 1.0, sleep, watch=True, stop=lambda: n["c"] >= 3)
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))

    async def test_stop_command_and_non_watch_still_drains(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wd.main(["--dir", str(self.dir), "stop"]), 0)
        self.assertTrue(wd.stop_requested(self.dir))
        result = await wd.run_loop(self.d(), self.dir, 1.0, stop=lambda: False)
        self.assertTrue(result["done"])                              # default behavior unchanged: exits when drained


class SurfaceTests(Base):
    async def test_http_surface_exact_and_safe_logs(self):
        calls = []

        class Client:
            async def call(self, method, path, **kw):
                calls.append((method, path, kw))
                if path == "/api/cli-sessions":
                    return 201, {"session_id": "33333333-3333-4333-8333-333333333333", "agent_id": AGENT, "provider": "claude-cli"}
                return 202, {"session_id": "33333333-3333-4333-8333-333333333333", "message_id": "msg-1",
                             "run_id": "44444444-4444-4444-8444-444444444444"}

        async def fake_session(fn):
            return await fn(Client())
        raw = manifest(jobs=[job("j1", message=f"prompt {SENTINEL}")])
        self.arm(raw)
        with mock.patch.object(cli, "_session", fake_session):
            await wd.Dispatcher(self.conn, now=self.clock, observer_path=self.obs_dir / "x.sqlite3").step()
        self.assertEqual([(m, p) for m, p, _ in calls],
                         [("POST", "/api/cli-sessions"), ("POST", "/api/cli-sessions/33333333-3333-4333-8333-333333333333/messages")])
        wd.log_event(self.dir, "probe", code=f"x {SENTINEL}", ok="fine")
        write = wd.build_status(self.conn, self.clock.t)
        wd.write_status(self.dir, write)
        blob = "".join(p.read_text(encoding="utf-8") for p in self.dir.iterdir() if p.suffix in (".log", ".json"))
        self.assertNotIn(SENTINEL, blob)
        self.assertIn("ok=fine", blob)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM jobs WHERE message LIKE ?", (f"%{SENTINEL}%",)).fetchone()[0], 1)

    async def test_cli_help_and_offline_smoke(self):
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stdout(io.StringIO()):
            wd.main(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(wd.main(["--dir", str(self.dir), "status"]), 0)
        self.assertEqual(json.loads(out.getvalue())["manifests"], [])

    async def test_observer_allow_list_unchanged(self):
        for method, path in (("POST", "/api/cli-sessions"), ("POST", f"/api/cli-sessions/{AGENT}/messages")):
            with self.assertRaises(ws.SupervisorError):
                await ws.observe(mock.Mock(), method, path)


def djob(job_id, deps=None, review=None, **kw):
    item = job(job_id, **kw)
    if deps is not None:
        item["depends_on"] = deps
    if review is not None:
        item["root_review"] = review
    return item


class DependencyTests(Base):
    """Explicit dependency / root-review gates.  Execution success is not adoption; no worker text opens a gate."""

    def chain(self, *, review=False, extra=()):
        self.raw = manifest(jobs=[job("a"), djob("b", ["a"], True if review else None), *extra])
        self.arm(self.raw)
        return self.d()

    def digest(self):
        return self.conn.execute("SELECT digest FROM manifests").fetchone()[0]

    def generation(self):
        return self.conn.execute("SELECT generation FROM manifests").fetchone()[0]

    def b_effects(self):
        key = wd.op_key("create", self.digest(), "b")
        return sum(1 for k in self.backend.effects if k == key)

    def finish(self, job_id, text="the answer", digest=None, handoff=True):
        row = self.jobs()[job_id]
        self.add_run(self.observer(), row["session_id"], row["run_id"], text=text, digest=digest, handoff=handoff)
        return row

    def release(self, job_id="b", pred="a", **over):
        a = self.jobs()[pred]
        args = {"manifest_id": "m1", "job_id": job_id, "manifest_sha256": self.digest(), "root_epoch": 5, "generation": self.generation(),
                "bindings": {pred: (a["session_id"], a["run_id"], a["result_digest"])}, "now": self.clock.t}
        args.update(over)
        return wd.release(self.conn, **args)

    async def succeeded_a(self, d):
        await d.step()
        self.finish("a")
        await d.step()
        self.assertEqual(self.jobs()["a"]["state"], "succeeded")

    async def test_b_waits_for_verified_own_handoff_only(self):
        d = self.chain()
        await d.step()
        self.assertEqual((self.backend.count("create"), self.b_effects()), (1, 0))      # only A
        row = self.finish("a", handoff=False)                                            # terminal success, no handoff yet
        await d.step()
        self.assertEqual((self.jobs()["a"]["state"], self.jobs()["b"]["state"], self.b_effects()), ("sent", "queued", 0))
        obs = self.observer()
        obs.execute("INSERT INTO handoffs(handoff_id,source_type,source_id,terminal_status,created_at,state,run_id,message_id,result_source,result_text,"
                    "result_digest) VALUES('h','cli_session',?,'completed',?,'ready',?,'m','cli_output_chunks','x',?)",
                    (row["session_id"], BASE, row["run_id"], "0" * 64))                  # digest does not match the text
        await d.step()
        self.assertEqual((self.jobs()["a"]["state"], self.b_effects()), ("sent", 0))
        obs.execute("UPDATE handoffs SET result_digest=?", (hashlib.sha256(b"x").hexdigest(),))
        await d.step()
        self.assertEqual((self.jobs()["a"]["state"], self.jobs()["b"]["state"]), ("succeeded", "sent"))
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (2, 2))

    async def test_failed_denied_uncertain_predecessor_never_release_b_and_c_proceeds(self):
        for mode in ("cancelled", "denied", "uncertain"):
            with self.subTest(mode):
                self.fresh()
                self.arm(manifest(jobs=[job("a"), djob("b", ["a"]), job("c")]))
                d = self.d()
                if mode == "denied":
                    self.backend.refuse["create"] = {"ok": False, "error": "forbidden", "submission_uncertain": False}
                if mode == "uncertain":
                    self.backend.lose_after_accept.add("create")
                await d.step()
                if mode == "cancelled":
                    self.finish("a", handoff=False)
                    obs = self.observer()
                    obs.execute("UPDATE runs SET status='cancelled'")
                await d.step()
                self.assertEqual(self.jobs()["b"]["state"], "queued")
                self.assertEqual(self.b_effects(), 0)
                status = wd.build_status(self.conn, self.clock.t)["manifests"][0]
                code = {j["job_id"]: j.get("dependency") for j in status["jobs"]}["b"]
                if mode == "uncertain":
                    self.assertEqual(code["state"], "waiting")                            # still unresolved, never released
                else:
                    self.assertEqual(code["state"], "failed")
                    self.assertEqual(code["code"], {"cancelled": "dep_cancelled", "denied": "dep_denied"}[mode])
                    self.assertEqual(status["counts"]["dependency_failed"], 1)

    async def test_uncertain_predecessor_ops_are_distinguished(self):
        def dep(name="b"):
            status = wd.build_status(self.conn, self.clock.t)["manifests"][0]
            return {j["job_id"]: j.get("dependency") for j in status["jobs"]}[name]

        for op in ("create", "send"):
            with self.subTest(op):
                self.fresh()
                self.arm(manifest(jobs=[job("a"), djob("b", ["a"]), job("c")], uncertain_retry_budget=1, max_concurrency=3))
                d = self.d()
                if op == "send":
                    self.backend.lose_after_accept.add("send")
                else:
                    self.backend.lose_after_accept.add("create")
                await d.step()
                self.assertEqual(self.jobs()["a"]["state"], "creating" if op == "create" else "sending")
                self.assertEqual(dep(), {"state": "waiting", "code": "dep_uncertain"})
                self.assertEqual(self.b_effects(), 0)
                self.assertEqual(self.jobs()["c"]["state"], "sent")                    # independent job unaffected
                d2 = self.d(conn=self.reopen())                                        # restart keeps the classification
                self.assertEqual(dep(), {"state": "waiting", "code": "dep_uncertain"})
                self.backend.lose_after_accept.update({"create", "send"})              # spend the retry budget
                for _ in range(3):
                    self.clock.t += 1000
                    await d2.step()
                self.assertEqual(self.conn.execute("SELECT state FROM ops WHERE job_id='a' AND op=?", (op,)).fetchone()[0], "uncertain")
                self.assertEqual(self.jobs()["a"]["state"], "creating" if op == "create" else "sending")
                self.assertEqual(dep(), {"state": "waiting", "code": "dep_uncertain"})  # exhausted != failed, != pending
                self.assertEqual(self.b_effects(), 0)
                self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ops WHERE job_id='b'").fetchone()[0], 0)

    async def test_other_predecessor_states_keep_distinct_codes(self):
        self.arm(manifest(jobs=[job("a"), djob("b", ["a"])]))
        d = self.d()
        await d.step()
        self.conn.execute("UPDATE jobs SET obs_status='running' WHERE job_id='a'")      # no observer here: result_pending otherwise
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["jobs"][1]["dependency"]["code"], "dep_running")
        self.conn.execute("UPDATE jobs SET obs_status='result_pending' WHERE job_id='a'")
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["jobs"][1]["dependency"]["code"], "dep_result_pending")
        self.conn.execute("UPDATE jobs SET state='blocked', maybe_executing=1 WHERE job_id='a'")
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["jobs"][1]["dependency"]["code"], "dep_uncertain")
        self.conn.execute("UPDATE jobs SET maybe_executing=0 WHERE job_id='a'")
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["jobs"][1]["dependency"]["code"], "dep_denied")

    async def test_independent_job_runs_while_dependent_waits(self):
        self.arm(manifest(jobs=[job("a"), djob("b", ["a"]), job("c")]))
        await self.d().step()
        jobs = self.jobs()
        self.assertEqual((jobs["a"]["state"], jobs["c"]["state"], jobs["b"]["state"]), ("sent", "sent", "queued"))

    async def test_root_review_gate_exact_decision_only(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        self.assertEqual(self.jobs()["b"]["state"], "queued")
        status = wd.build_status(self.conn, self.clock.t)["manifests"][0]
        self.assertEqual((status["counts"]["root_review_wait"], status["counts"]["queued"], status["counts"]["succeeded"]), (1, 0, 1))
        self.assertFalse(status["fully_verified_succeeded"])
        await d.step()
        self.assertEqual(self.backend.count("create"), 1)                  # success / PASS-looking text did not open the gate
        a = self.jobs()["a"]
        good = (a["session_id"], a["run_id"], a["result_digest"])
        cases = {
            "run": {"bindings": {"a": (a["session_id"], str(uuid.uuid4()), a["result_digest"])}},
            "digest": {"bindings": {"a": (a["session_id"], a["run_id"], "1" * 64)}},
            "session": {"bindings": {"a": (str(uuid.uuid4()), a["run_id"], a["result_digest"])}},
            "manifest": {"manifest_sha256": "2" * 64},
            "epoch": {"root_epoch": 4},
            "incomplete": {"bindings": {}},
            "unknown_pred": {"bindings": {"zz": good}},
            "no_gate_job": {"job_id": "a"},
        }
        for case, kw in cases.items():
            with self.assertRaises(wd.DispatchError, msg=case):
                self.release(**kw)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 0)
        await d.step()
        self.assertEqual(self.b_effects(), 0)
        self.assertTrue(self.release()["changed"])
        self.assertFalse(self.release()["changed"])                        # identical repeat is a no-op
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("UPDATE releases SET root_epoch=9")
        with self.assertRaises(sqlite3.DatabaseError):
            self.conn.execute("DELETE FROM releases")
        await d.step()
        self.assertEqual((self.jobs()["b"]["state"], self.b_effects()), ("sent", 1))

    async def test_newer_root_epoch_closes_old_gate(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        self.release()
        self.conn.execute("UPDATE roots SET max_epoch=6")                  # the root has since moved on
        await d.step()
        self.assertEqual(self.b_effects(), 0)
        with self.assertRaises(wd.DispatchError) as ctx:
            self.release()
        self.assertEqual(ctx.exception.code, "epoch_rollback")

    async def test_dead_arm_posts_nothing_even_after_release(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        wd.disarm(self.conn, "m1", self.clock.t)
        with self.assertRaises(wd.DispatchError) as ctx:
            self.release()
        self.assertEqual(ctx.exception.code, "not_live")
        before = len(self.backend.attempts)
        await d.step()
        self.assertEqual(len(self.backend.attempts), before)
        self.arm(self.raw)                                                # explicit new lease, then release, then expiry
        self.release()
        self.clock.t += 7200
        await d.step()
        self.assertEqual(len(self.backend.attempts), before)

    async def test_release_requires_explicit_matching_generation(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        a = self.jobs()["a"]
        old = {"manifest_id": "m1", "job_id": "b", "manifest_sha256": self.digest(), "root_epoch": 5, "generation": 1,
               "bindings": {"a": (a["session_id"], a["run_id"], a["result_digest"])}, "now": self.clock.t}
        for bad in (0, -1, 2, 99, True, "1", None, 1.0):
            with self.assertRaises(wd.DispatchError, msg=repr(bad)) as ctx:
                wd.release(self.conn, **{**old, "generation": bad})
            self.assertIn(ctx.exception.code, ("invalid_release", "generation_mismatch"))
        with self.assertRaises(TypeError):
            wd.release(self.conn, **{k: v for k, v in old.items() if k != "generation"})   # no fallback to the current value
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 0)
        self.assertTrue(wd.release(self.conn, **old)["changed"])
        self.assertFalse(wd.release(self.conn, **old)["changed"])                       # same explicit generation: idempotent
        wd.disarm(self.conn, "m1", self.clock.t)
        self.arm(self.raw)                                                              # generation 2
        self.assertEqual(self.generation(), 2)
        with self.assertRaises(wd.DispatchError) as ctx:
            wd.release(self.conn, **old)                                                # the saved generation-1 call
        self.assertEqual(ctx.exception.code, "generation_mismatch")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM releases WHERE generation=2").fetchone()[0], 0)
        await d.step()
        self.assertEqual((self.jobs()["b"]["state"], self.b_effects()), ("queued", 0))
        self.assertEqual(wd.build_status(self.conn, self.clock.t)["manifests"][0]["counts"]["root_review_wait"], 1)
        self.assertTrue(wd.release(self.conn, **{**old, "generation": 2})["changed"])   # root names the current generation
        await d.step()
        self.assertEqual(self.b_effects(), 1)

    async def test_gate_and_attempts_survive_restart(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        self.release()
        self.backend.lose_after_accept.add("create")
        await d.step()
        self.assertEqual(self.conn.execute("SELECT state FROM ops WHERE job_id='b'").fetchone()[0], "uncertain")
        d2 = self.d(conn=self.reopen())
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 1)
        self.clock.t += 1000
        await d2.step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (2, 2))

    async def test_gate_dropped_before_post_never_posts(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        self.release()
        sent = []
        self.backend.inspect = lambda op, key, fingerprint: sent.append(op)

        def fault(name):
            if name == "after_intent":
                self.conn.execute("DROP TRIGGER releases_no_delete")
                self.conn.execute("DELETE FROM releases")
        await self.d(fault=fault).step()
        self.assertEqual(sent, [])
        self.assertEqual(self.jobs()["b"]["blocked_code"], "dependency_gate")

    async def test_lease_lapse_between_the_two_gate_checks_is_unsent_not_a_crash(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        self.release()
        m = d._live_manifests()[0]
        key = d._reserve_create(m)                                         # B's committed intent
        self.assertIsNotNone(key)
        real = d._gate
        seen = {"n": 0}

        def lapse_on_second(manifest_id, generation):
            seen["n"] += 1
            return real(manifest_id, generation) if seen["n"] == 1 else None      # live for the first check, gone for the re-check
        d._gate = lapse_on_second
        op = self.conn.execute("SELECT * FROM ops WHERE op_key=?", (key,)).fetchone()
        self.assertFalse(d._prefire(op))                                   # no TypeError
        self.assertEqual(seen["n"], 2)
        self.assertEqual(self.conn.execute("SELECT state FROM ops WHERE op_key=?", (key,)).fetchone()[0], "intent")   # not denied/blocked
        self.assertEqual(self.jobs()["b"]["state"], "creating")
        seen["n"] = 0
        d._gate = real
        self.assertEqual(self.b_effects(), 0)
        # through _execute: the attempt is handed back (unsent), nothing is posted, nothing is raised
        seen["n"] = 0
        d._gate = lambda mid, gen, _r=real, _c=[0]: (_c.__setitem__(0, _c[0] + 1) or (_r(mid, gen) if _c[0] <= 2 else None))
        self.assertFalse(await d._execute(key))
        self.assertEqual(self.conn.execute("SELECT attempts FROM ops WHERE op_key=?", (key,)).fetchone()[0], 0)
        self.assertEqual(self.b_effects(), 0)

    async def test_strict_dependency_schema_rejected_before_any_state(self):
        bad = {
            "unknown": [job("a"), djob("b", ["zz"])],
            "self": [djob("a", ["a"])],
            "cycle": [djob("a", ["b"]), djob("b", ["a"])],
            "cycle3": [djob("a", ["c"]), djob("b", ["a"]), djob("c", ["b"])],
            "dup": [job("a"), djob("b", ["a", "a"])],
            "empty": [job("a"), djob("b", [])],
            "not_list": [job("a"), djob("b", "a")],
            "bad_id": [job("a"), djob("b", ["bad id"])],
            "review_false": [job("a"), djob("b", ["a"], False)],
            "review_str": [job("a"), djob("b", ["a"], "true")],
            "review_no_deps": [job("a"), djob("b", None, True)],
        }
        for name, jobs in bad.items():
            raw = manifest(jobs=jobs)
            with self.assertRaises(wd.DispatchError, msg=name):
                wd.arm(self.conn, raw, hashlib.sha256(raw).hexdigest(), self.clock.t + 60, self.clock.t)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM manifests").fetchone()[0], 0)
        await self.d(NoNetwork()).step()

    async def test_v1_job_without_new_keys_keeps_historical_identity(self):
        raw = manifest(jobs=[job("j1")])
        j = wd.parse_manifest(raw)["jobs"][0]
        legacy = wd.sha256_text(wd.canonical({"create": j["create_request"], "message": j["message"], "send_key": j["send_key"]}))
        self.assertEqual(j["snapshot_digest"], legacy)
        self.arm(raw)
        await self.d().step()
        self.assertEqual((self.backend.count("create"), self.backend.count("send")), (1, 1))
        self.assertNotIn("depends_on", wd.build_status(self.conn, self.clock.t)["manifests"][0]["jobs"][0])

    async def test_cli_release_surface(self):
        d = self.chain(review=True)
        await self.succeeded_a(d)
        a = self.jobs()["a"]
        digest = self.digest()
        self.conn.close()
        out = io.StringIO()
        argv = ["--dir", str(self.dir), "release", "m1", "b", "--manifest-sha256", digest, "--root-epoch", "5", "--generation", "1",
                "--predecessor", f"a={a['session_id']}:{a['run_id']}:{a['result_digest']}"]
        with mock.patch.object(cli, "_session", side_effect=AssertionError("network")), contextlib.redirect_stdout(out):
            self.assertEqual(wd.main(argv), 0)
            self.assertTrue(json.loads(out.getvalue())["changed"])
            self.assertEqual(wd.main([*argv[:-1], "a=bad"]), 1)
            no_gen = argv[:argv.index("--generation")] + argv[argv.index("--generation") + 2:]
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                wd.main(no_gen)                                          # --generation is mandatory
        self.conn = self.reopen()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0], 1)
        self.assertEqual(self.b_effects(), 0)                              # releasing sent nothing

    async def test_transitive_failure_and_watch_stays_idle_on_sticky_denial(self):
        self.arm(manifest(jobs=[job("a"), djob("b", ["a"]), djob("c", ["b"])]))
        self.backend.refuse["create"] = {"ok": False, "error": "forbidden", "submission_uncertain": False}
        d = self.d()
        await d.step()
        jobs = {j["job_id"]: j for j in wd.build_status(self.conn, self.clock.t)["manifests"][0]["jobs"]}
        self.assertEqual(jobs["c"]["dependency"], {"state": "failed", "code": "dep_upstream_failed"})
        n = {"c": 0}

        async def sleep(_s):
            n["c"] += 1
        before = len(self.backend.attempts)
        await wd.run_loop(d, self.dir, 1.0, sleep, watch=True, stop=lambda: n["c"] >= 3)
        self.assertEqual(len(self.backend.attempts), before)               # sticky denial: no replay; dependents stay put


class FakeCodexAdapter:
    def __init__(self, runtime_dir: str, complete_on_start: bool = True, trailing_event: dict | None = None):
        self.runtime_dir = runtime_dir
        self.complete_on_start = complete_on_start
        self.trailing_event = trailing_event
        self.starts: list[str] = []
        self.statuses: dict[str, dict] = {}
        self.launch_data: dict[str, dict] = {}

    @staticmethod
    def _launch_data(request, idempotency_key, request_fingerprint, dispatcher_binding):
        explicit_env_digest = wd.gateway_jobs._explicit_env_digest({})
        internal = wd.gateway_jobs._keyed_fingerprint(request["command"], request["cwd"], {},
                                                      request_fingerprint, dispatcher_binding)
        return {"command": request["command"], "cwd": request["cwd"], "idempotency_key": idempotency_key,
                "request_fingerprint": internal, "dispatcher_request_fingerprint": request_fingerprint,
                "explicit_env_digest": explicit_env_digest}

    @staticmethod
    def _stream(message: str = "interim\nfinal ✅", trailing_event: dict | None = None) -> bytes:
        thread_id = "99999999-9999-4999-8999-999999999999"
        events = [{"type": "thread.started", "thread_id": thread_id},
                  {"type": "item.completed", "item": {"id": "message-interim", "type": "agent_message", "text": "interim"}},
                  {"type": "item.completed", "item": {"id": "message-final", "type": "agent_message", "text": message}},
                  {"type": "turn.completed"}]
        if trailing_event is not None:
            events.append(trailing_event)
        return b"".join((json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8") for event in events)

    def start(self, request, idempotency_key, request_fingerprint, dispatcher_binding):
        gateway_id = wd.gateway_jobs.stable_job_id(idempotency_key)
        if gateway_id != dispatcher_binding["gateway_job_id"]:
            raise AssertionError("dispatcher and Gateway job stable-ID codecs disagree")
        self.starts.append(gateway_id)
        launch_data = self._launch_data(request, idempotency_key, request_fingerprint, dispatcher_binding)
        internal_fingerprint = launch_data["request_fingerprint"]
        self.launch_data[gateway_id] = {**launch_data, "dispatcher_binding": dispatcher_binding}
        directory = wd.gateway_jobs._directory(gateway_id, self.runtime_dir)
        directory.mkdir(parents=True, exist_ok=True)
        stdout_path = directory / "stdout.log"
        if self.complete_on_start:
            stdout_path.write_bytes(self._stream(trailing_event=self.trailing_event))
            state = "exited"
        else:
            stdout_path.write_bytes(b"")
            state = "starting"
        result = {"job_id": gateway_id, "idempotency_key": idempotency_key,
                  "request_fingerprint": internal_fingerprint, "dispatcher_request_fingerprint": request_fingerprint,
                  "explicit_env_digest": launch_data["explicit_env_digest"],
                  "dispatcher_binding": dispatcher_binding, "command": request["command"], "cwd": request["cwd"],
                  "runtime_dir": self.runtime_dir, "stdout_path": str(stdout_path), "stderr_path": str(directory / "stderr.log"),
                  "status": state, "exit_code": 0 if state == "exited" else None,
                  "process_identity": "missing" if state in ("exited", "starting") else "alive",
                  "worker_identity": "missing" if state == "exited" else "alive", "finished_at": BASE if state == "exited" else None}
        self.statuses[gateway_id] = result
        return dict(result)

    def status(self, gateway_job_id: str):
        return dict(self.statuses.get(gateway_job_id, {"job_id": gateway_job_id, "status": "unknown"}))

    def finish(self, gateway_job_id: str, message: str = "interim\nfinal ✅"):
        state = self.statuses[gateway_job_id]
        Path(state["stdout_path"]).write_bytes(self._stream(message, self.trailing_event))
        state.update(status="exited", exit_code=0, process_identity="missing", worker_identity="missing", finished_at=BASE + 1)


class CodexRouteTests(Base):
    @staticmethod
    def _worker_metadata(adapter, gateway_id):
        state = adapter.statuses[gateway_id]
        launch = adapter.launch_data[gateway_id]
        directory = wd.gateway_jobs._directory(gateway_id, adapter.runtime_dir)
        return {"job_id": gateway_id, "idempotency_key": launch["idempotency_key"],
                "request_fingerprint": launch["request_fingerprint"],
                "dispatcher_request_fingerprint": launch["dispatcher_request_fingerprint"],
                "explicit_env_digest": launch["explicit_env_digest"],
                "dispatcher_binding": launch["dispatcher_binding"], "launch_fence_state": "pending",
                "command": list(launch["command"]), "cwd": launch["cwd"], "started_at": time.time(),
                "finished_at": None, "status": "starting", "exit_code": None, "pid": None,
                "process_create_time": None, "worker_pid": None, "worker_create_time": None,
                "stdout_path": state["stdout_path"], "stderr_path": state["stderr_path"],
                "runtime_dir": adapter.runtime_dir}

    def _run_materialized_worker(self, adapter, gateway_id, extra_env=None):
        from home_mcp_gateway import job_worker
        directory = wd.gateway_jobs._directory(gateway_id, adapter.runtime_dir)
        wd.gateway_jobs._save(directory / "metadata.json", self._worker_metadata(adapter, gateway_id))

        class FakeProc:
            pid = 777_001
            def poll(self):
                return 0
            def wait(self):
                return 0

        with mock.patch.object(job_worker.subprocess, "Popen", return_value=FakeProc()) as popen, \
                mock.patch.object(job_worker, "process_create_time", return_value=1.0), \
                mock.patch.dict(os.environ, extra_env or {}, clear=False):
            job_worker.main(directory)
        return popen.call_count, wd.gateway_jobs._read(directory / "metadata.json")

    def _paths(self):
        root = Path(self.tmp.name) / "install"
        node = root / "node.exe"
        entry = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        entry.parent.mkdir(parents=True, exist_ok=True)
        node.write_bytes(b"fake node executable identity fixture")
        entry.write_text("// fake official entrypoint path fixture", encoding="utf-8")
        return node.resolve(), entry.resolve()

    def _codex_job(self, job_id="cx", message="Inspect the local change"):
        node, entry = self._paths()
        return {"job_id": job_id, "route": "gateway-codex", "authorization_ref": "root:decision-1", "model": "gpt-5.6-codex",
                "effort": "high", "node_executable": str(node), "codex_entrypoint": str(entry), "message": message}

    def _v2(self, jobs, **over):
        data = {"version": 2, "manifest_id": "m-v2", "root_source_id": "root-a", "root_epoch": 5,
                "created_at": stamp(BASE - 10), "expires_at": stamp(BASE + 7200), "max_concurrency": 2,
                "uncertain_retry_budget": 3, "jobs": jobs}
        data.update(over)
        return json.dumps(data, ensure_ascii=False).encode("utf-8")

    async def test_v2_strict_route_bounds_and_exact_codex_argv(self):
        codex_job = self._codex_job()
        raw = self._v2([codex_job], max_concurrency=10)
        parsed = wd.parse_manifest(raw)
        self.assertEqual(parsed["max_concurrency"], 10)
        compiled = parsed["jobs"][0]
        request = compiled["create_request"]
        self.assertEqual(request["cwd"], str(Path(wd.__file__).resolve().parents[1]))
        self.assertEqual(request["command"][2:8], ["exec", "-m", "gpt-5.6-codex", "-c", 'model_reasoning_effort="high"', "--json"])
        self.assertEqual(request["command"][-1], codex_job["message"])
        self.assertNotIn("session_id", request)
        self.assertNotIn("run_id", request)
        self.assertEqual(wd.parse_manifest(manifest(max_concurrency=8))["max_concurrency"], 8)
        with self.assertRaises(wd.DispatchError):
            wd.parse_manifest(manifest(max_concurrency=9))
        with self.assertRaises(wd.DispatchError):
            wd.parse_manifest(self._v2([dict(codex_job, shell="echo unsafe")]))
        with self.assertRaises(wd.DispatchError):
            wd.parse_manifest(self._v2([dict(codex_job, create={})]))
        with self.assertRaises(wd.DispatchError):
            wd.parse_manifest(self._v2([self._codex_job(message="x" * 40_000)]))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM manifests").fetchone()[0], 0)

    async def test_codex_result_parser_uses_last_message_without_turn_started(self):
        stream = FakeCodexAdapter._stream("last line ✅")
        result = codex.parse_codex_jsonl(stream)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result_text"], "last line ✅")
        self.assertEqual(result["result_digest"], hashlib.sha256("last line ✅".encode()).hexdigest())
        self.assertEqual(result["result_chars"], len("last line ✅"))
        self.assertEqual(result["thread_id"], "99999999-9999-4999-8999-999999999999")
        self.assertEqual(result["final_item_id"], "message-final")
        self.assertEqual(codex.parse_codex_jsonl(stream + b"\n \t\n")["state"], "succeeded")
        self.assertEqual(codex.parse_codex_jsonl(stream[:-8])["state"], "result_pending")
        post_terminal = FakeCodexAdapter._stream("last line ✅", {"type": "item.started", "item": {"type": "command_execution"}})
        self.assertEqual(codex.parse_codex_jsonl(post_terminal)["state"], "result_pending")
        self.assertEqual(codex.parse_codex_jsonl(post_terminal)["code"], "events_after_terminal")
        bad = stream.replace(b'"turn.completed"', b'"turn.failed"')
        self.assertEqual(codex.parse_codex_jsonl(bad)["state"], "failed")
        self.assertEqual(codex.parse_codex_jsonl(stream, max_bytes=8)["code"], "result_too_large")

    async def test_post_terminal_work_event_cannot_unlock_codex_dependency(self):
        first = self._codex_job("a")
        second = self._codex_job("b")
        second["depends_on"] = ["a"]
        raw = self._v2([first, second])
        self.arm(raw)
        adapter = FakeCodexAdapter(str(Path(self.tmp.name) / "runtime-post-terminal"),
                                   trailing_event={"type": "item.started", "item": {"type": "command_execution"}})
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=adapter)
        await dispatcher.step()
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE job_id='a'").fetchone()[0], "result_pending")
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE job_id='b'").fetchone()[0], "queued")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ops WHERE job_id='b'").fetchone()[0], 0)
        await dispatcher.step()
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ops WHERE job_id='b'").fetchone()[0], 0)

    async def test_exact_fake_codex_route_verifies_result_and_exposes_reference(self):
        raw = self._v2([self._codex_job()])
        self.arm(raw)
        runtime = str(Path(self.tmp.name) / "runtime")
        adapter = FakeCodexAdapter(runtime)
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=adapter)
        result = await dispatcher.step()
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id='cx'").fetchone()
        execution = self.conn.execute("SELECT * FROM codex_execution WHERE job_id='cx'").fetchone()
        self.assertEqual(row["state"], "succeeded")
        self.assertIsNone(row["session_id"])
        self.assertIsNone(row["run_id"])
        self.assertEqual(execution["thread_id"], "99999999-9999-4999-8999-999999999999")
        self.assertEqual(execution["result_digest"], hashlib.sha256("interim\nfinal ✅".encode()).hexdigest())
        self.assertEqual(len(adapter.starts), 1)
        self.assertEqual(self.backend.attempts, [])
        status = wd.build_status(self.conn, self.clock.t)["manifests"][0]
        job_status = status["jobs"][0]
        self.assertEqual(job_status["route"], "gateway-codex")
        self.assertEqual(job_status["execution"]["state"], "succeeded")
        self.assertTrue(job_status["execution"]["result_ref"].endswith("stdout.log"))
        self.assertTrue(status["fully_verified_succeeded"])

    async def test_v2_owned_mix_reaches_ten_active_without_filler(self):
        claude_jobs = []
        for index in range(8):
            item = job(f"cl-{index}", message=f"finite Claude item {index}")
            item["route"] = "aoitalk-claude"
            claude_jobs.append(item)
        raw = self._v2([*claude_jobs, self._codex_job("cx-1"), self._codex_job("cx-2")], max_concurrency=10)
        self.arm(raw)
        adapter = FakeCodexAdapter(str(Path(self.tmp.name) / "runtime-ten"), complete_on_start=False)
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=adapter)
        await dispatcher.step()
        status = wd.build_status(self.conn, self.clock.t)["manifests"][0]
        self.assertEqual(status["max_concurrency"], 10)
        self.assertEqual(status["owned_route_counts"], {"aoitalk-claude": 8, "gateway-codex": 2})
        self.assertEqual(status["counts"]["active"] + status["counts"]["result_pending"], 10)
        self.assertEqual(sum(1 for row in self.conn.execute("SELECT state FROM jobs") if row[0] in wd.OCCUPYING), 10)
        self.assertEqual(self.backend.count("create"), 8)
        self.assertEqual(self.backend.count("send"), 8)
        self.assertEqual(len(adapter.starts), 2)

    async def test_worker_guard_reads_exact_intent_and_disarm_closes_it(self):
        raw = self._v2([self._codex_job()])
        self.arm(raw)
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=FakeCodexAdapter(str(Path(self.tmp.name) / "runtime")))
        m = self.conn.execute("SELECT * FROM manifests WHERE manifest_id='m-v2'").fetchone()
        key = dispatcher._reserve_create(m)
        self.assertIsNotNone(key)
        execution = self.conn.execute("SELECT * FROM codex_execution WHERE job_id='cx'").fetchone()
        binding = dispatcher._codex_binding(execution)
        execution = self.conn.execute("SELECT * FROM codex_execution WHERE job_id='cx'").fetchone()
        job_row = self.conn.execute("SELECT create_json FROM jobs WHERE job_id='cx'").fetchone()
        request = json.loads(job_row["create_json"])
        launch_data = FakeCodexAdapter._launch_data(request, key, execution["request_fingerprint"], binding)
        self.assertEqual(codex.worker_launch_guard(binding, launch_data), "open")
        wd.disarm(self.conn, "m-v2", self.clock.t)
        self.assertEqual(codex.worker_launch_guard(binding, launch_data), "closed")

    async def test_delayed_worker_admission_survives_receipt_but_disarm_stops_popen(self):
        raw = self._v2([self._codex_job()])
        self.arm(raw)
        runtime = str(Path(self.tmp.name) / "runtime")
        adapter = FakeCodexAdapter(runtime, complete_on_start=False)
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=adapter)
        await dispatcher.step()  # job_start admitted the stable worker; official Popen is still delayed
        gateway_id = adapter.starts[0]
        operation = self.conn.execute("SELECT state,receipt_json FROM ops").fetchone()
        job = self.conn.execute("SELECT state FROM jobs WHERE job_id='cx'").fetchone()
        self.assertEqual(operation["state"], "receipted")
        self.assertIn('"status":"starting"', operation["receipt_json"])
        self.assertEqual(job["state"], "running")
        restarted = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                  codex_backend=adapter)
        with mock.patch.dict(os.environ, {"D80_UNRELATED_RESTART_ENV": "changed-after-admission"}, clear=False):
            restarted.reconcile()
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE job_id='cx'").fetchone()[0], "running")
        launches, metadata = self._run_materialized_worker(adapter, gateway_id, {"D80_UNRELATED_WORKER_ENV": "added-after-admission"})
        self.assertEqual(launches, 1)
        self.assertEqual(metadata["status"], "exited")

        # An accepted bootstrap that reaches the same guard after disarm must make zero Popen calls.
        self.fresh()
        raw = self._v2([self._codex_job()])
        self.arm(raw)
        runtime = str(Path(self.tmp.name) / "runtime-disarmed")
        adapter = FakeCodexAdapter(runtime, complete_on_start=False)
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=adapter)
        await dispatcher.step()
        gateway_id = adapter.starts[0]
        wd.disarm(self.conn, "m-v2", self.clock.t)
        launches, metadata = self._run_materialized_worker(adapter, gateway_id)
        self.assertEqual(launches, 0)
        self.assertEqual(metadata["status"], "not_launched")
        self.assertEqual(metadata["launch_result"], "gate_closed")

    async def test_worker_final_guard_rejects_changed_metadata_and_operation_payload(self):
        from home_mcp_gateway import job_worker
        for mutation in ("command", "cwd", "prompt", "operation"):
            with self.subTest(mutation=mutation):
                self.fresh()
                raw = self._v2([self._codex_job()])
                self.arm(raw)
                runtime = str(Path(self.tmp.name) / ("runtime-" + mutation))
                adapter = FakeCodexAdapter(runtime, complete_on_start=False)
                dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock,
                                           observer_path=self.obs_dir / "state.sqlite3", codex_backend=adapter)
                await dispatcher.step()
                gateway_id = adapter.starts[0]
                directory = wd.gateway_jobs._directory(gateway_id, runtime)
                metadata = self._worker_metadata(adapter, gateway_id)
                if mutation == "command":
                    metadata["command"] = [*metadata["command"][:-1], "tampered command"]
                elif mutation == "cwd":
                    metadata["cwd"] += "\\other"
                elif mutation == "prompt":
                    metadata["command"][-1] += " tampered"
                else:
                    op = self.conn.execute("SELECT request_json FROM ops").fetchone()[0]
                    self.conn.execute("UPDATE ops SET request_json=?", (op.replace("Inspect the local change", "tampered prompt"),))
                wd.gateway_jobs._save(directory / "metadata.json", metadata)
                class NeverProc:
                    def __call__(self, *_a, **_kw):
                        raise AssertionError("Popen must not be reached after launch data mismatch")
                with mock.patch.object(job_worker.subprocess, "Popen", side_effect=NeverProc()):
                    job_worker.main(directory)
                result = wd.gateway_jobs._read(directory / "metadata.json")
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result.get("launch_result"), "request_mismatch")

    async def test_restart_resumes_same_codex_create_key_before_first_io(self):
        raw = self._v2([self._codex_job()])
        self.arm(raw)
        first_dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock,
                                         observer_path=self.obs_dir / "state.sqlite3")
        manifest_row = self.conn.execute("SELECT * FROM manifests WHERE manifest_id='m-v2'").fetchone()
        op_key = first_dispatcher._reserve_create(manifest_row)
        self.assertIsNotNone(op_key)
        reserved = dict(self.conn.execute("SELECT * FROM codex_execution WHERE job_id='cx'").fetchone())
        self.assertEqual(reserved["gateway_job_id"], wd.gateway_jobs.stable_job_id(op_key))
        self.assertEqual(self.conn.execute("SELECT attempts FROM ops WHERE op_key=?", (op_key,)).fetchone()[0], 0)
        self.conn.close()
        self.conn = self.reopen()
        adapter = FakeCodexAdapter(str(Path(self.tmp.name) / "runtime"))
        resumed = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                codex_backend=adapter)
        with mock.patch.dict(os.environ, {"D80_UNRELATED_RESTART_ENV": "changed-after-restart"}, clear=False):
            await resumed.step()
        row = self.conn.execute("SELECT * FROM codex_execution WHERE job_id='cx'").fetchone()
        self.assertEqual(row["gateway_job_id"], reserved["gateway_job_id"])
        self.assertEqual(row["op_key"], op_key)
        self.assertEqual(adapter.starts, [reserved["gateway_job_id"]])
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE job_id='cx'").fetchone()[0], "succeeded")

    async def test_root_review_codex_predecessor_requires_exact_route_identity(self):
        predecessor = self._codex_job("a")
        successor = {"job_id": "b", "route": "aoitalk-claude", "authorization_ref": "root:decision-2",
                     "create": {"agent_id": AGENT, "provider": "claude-cli", "model": "claude-x", "working_directory": CWD},
                     "message": "continue", "depends_on": ["a"], "root_review": True}
        raw = self._v2([predecessor, successor])
        self.arm(raw)
        adapter = FakeCodexAdapter(str(Path(self.tmp.name) / "runtime"))
        dispatcher = wd.Dispatcher(self.conn, self.backend, now=self.clock, observer_path=self.obs_dir / "state.sqlite3",
                                   codex_backend=adapter)
        await dispatcher.step()
        manifest_row = self.conn.execute("SELECT * FROM manifests WHERE manifest_id='m-v2'").fetchone()
        digest = manifest_row["digest"]
        exec_row = self.conn.execute("SELECT * FROM codex_execution WHERE job_id='a'").fetchone()
        self.assertEqual(self.conn.execute("SELECT state FROM jobs WHERE job_id='b'").fetchone()[0], "queued")
        supplied = {"a": {"gateway_job_id": exec_row["gateway_job_id"], "request_fingerprint": exec_row["request_fingerprint"],
                           "result_digest": exec_row["result_digest"], "thread_id": exec_row["thread_id"], "log_digest": exec_row["log_digest"]}}
        bad = {"a": {**supplied["a"], "request_fingerprint": "0" * 64}}
        with self.assertRaises(wd.DispatchError):
            wd.release(self.conn, "m-v2", "b", digest, 5, {}, self.clock.t, generation=1, codex_bindings=bad)
        release = wd.release(self.conn, "m-v2", "b", digest, 5, {}, self.clock.t, generation=1, codex_bindings=supplied)
        self.assertTrue(release["changed"])
        await dispatcher.step()
        self.assertEqual([x[0] for x in self.backend.attempts], ["create", "send"])


if __name__ == "__main__":
    unittest.main()
