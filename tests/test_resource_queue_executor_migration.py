"""Offline coverage for the Codex -> Claude executor provider migration.

Everything runs against a temporary ledger and a keyed-jobs seam that records
the argv it is given and never starts a process, model, phone or GPU.
"""
from __future__ import annotations

import argparse
import ast
from contextlib import closing, contextmanager
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock

from home_mcp_gateway import jobs
from home_mcp_gateway import resource_queue as rq
from home_mcp_gateway import resource_queue_cli as cli
from home_mcp_gateway import resource_queue_claude as claude
from home_mcp_gateway import resource_queue_gateway as gateway


OLD_THREAD = "77777777-7777-4777-8777-777777777777"
SESSION = "0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d"
MODEL = "claude-sonnet-5-5"
EFFORT = "xhigh"
PERMISSION = "acceptEdits"
DEVICE = "adb:offline-phone"
CODEX_EXE = r"C:\offline\codex.exe"
CLAUDE_EXE = r"C:\offline\claude.exe"
NOW = 1_900_000_000.0
TOOLS = ["Bash(D:/qa/task.py status)", "Bash(D:/qa/task.py screenshot)"]


def as_prepared(launch: dict) -> gateway.PreparedLaunch:
    return gateway.PreparedLaunch(**{k: tuple(v) if k == "owned_processes" else v for k, v in launch.items()})


def profile(cwd, **overrides) -> dict:
    return {"model": MODEL, "effort": EFFORT, "cwd": str(cwd), "permission_mode": PERMISSION, **overrides}


def stream(*, session=SESSION, model=MODEL, permission=PERMISSION, per_turn=True, explicit_effort=None,
           init_session=None, init_model=None, result_session=None, usage=None, message_model=None,
           is_error=False, extra=()) -> str:
    """A stream shaped like the real CLI: the init names session, model, permission mode and
    per_turn_effort_active but has no effort field unless ``explicit_effort`` adds one."""
    init = {"type": "system", "subtype": "init", "session_id": init_session or session, "model": init_model or model,
            "permissionMode": permission, "per_turn_effort_active": per_turn, "apiKeySource": "none", "tools": []}
    if explicit_effort is not None:
        init["effort"] = explicit_effort
    events = [
        init,
        {"type": "assistant", "session_id": session, "message": {"model": message_model or model, "content": []}},
        *extra,
        {"type": "result", "subtype": "error_during_execution" if is_error else "success", "is_error": is_error,
         "result": "offline result", "session_id": result_session or session,
         "modelUsage": {model: {}} if usage is None else usage},
    ]
    return "\n".join(json.dumps(event) for event in events) + "\n"


def sql(db: Path, statement: str) -> list[tuple]:
    """Run one statement on a short-lived connection (Windows cannot delete an open ledger)."""
    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
        return conn.execute(statement).fetchall()


class FakeJobs:
    """Keyed jobs seam: records launches, finishes them at once, serves a stdout log."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[dict[str, object]] = []
        self.rows: dict[str, dict[str, object]] = {}
        self.stdout = stream
        self.status = "exited"
        self._pid = 4000

    def job_start(self, command, *, cwd, env, runtime_dir, idempotency_key, request_fingerprint):
        self.calls.append({"command": copy.deepcopy(command), "env": dict(env), "idempotency_key": idempotency_key})
        job_id = jobs.stable_job_id(idempotency_key)
        self._pid += 2
        log = self.root / f"{job_id}.stdout.log"
        log.write_text(self.stdout(), encoding="utf-8")
        row = {
            "job_id": job_id, "idempotency_key": idempotency_key, "command": copy.deepcopy(command), "cwd": cwd,
            "env": dict(env), "explicit_env_digest": jobs._explicit_env_digest(env),
            "dispatcher_request_fingerprint": request_fingerprint,
            "request_fingerprint": jobs._keyed_fingerprint(command, cwd, env, request_fingerprint, None),
            "status": self.status, "exit_code": 0 if self.status == "exited" else None, "pid": self._pid, "process_create_time": self._pid / 100.0,
            "worker_pid": self._pid + 1, "worker_create_time": (self._pid + 1) / 100.0, "stdout_path": str(log),
        }
        self.rows[job_id] = row
        return copy.deepcopy(row)

    def job_status(self, job_id, runtime_dir=None):
        try:
            return copy.deepcopy(self.rows[job_id])
        except KeyError as exc:
            raise FileNotFoundError(job_id) from exc

    def identity(self, pid, create_time):
        return "missing"

    def _keyed_fingerprint(self, command, cwd, env, request_fingerprint, binding):
        return jobs._keyed_fingerprint(command, cwd, env, request_fingerprint, binding)


class RaisingRunner:
    """A runner whose start fails: the dispatch outcome is unknown."""

    def start(self, task):
        raise RuntimeError("response lost")

    def lookup(self, task):
        raise RuntimeError("still unknown")


class Harness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.runtime.mkdir()
        self.workdir = self.root / "work"
        self.workdir.mkdir()
        self.jobs = FakeJobs(self.root)
        self.runner = gateway.GatewayRunner(str(self.runtime), jobs_api=self.jobs, clock=lambda: NOW)
        self.queue = rq.Queue(str(self.root / "queue"), now=lambda: NOW)
        self.queue.register_resource(
            "phone-main", "phone", OLD_THREAD,
            initial_attestation={"resource_id": "phone-main", "kind": "phone", "state": "idle", "idle": True, "device_identity": DEVICE},
        )
        self.controller = rq.Controller(self.queue, self.runner, owner="offline-controller", lease_seconds=60)

    def close(self) -> None:
        self.temp.cleanup()

    def pump(self):
        return self.controller.pump()

    @staticmethod
    def op_key(operation_id: str) -> str:
        return "rq1:" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()

    def _enqueue(self, operation_id: str, prepared: gateway.PreparedLaunch):
        projection = self.queue.enqueue(
            operation_id, "phone-main", "offline", input={"op": operation_id}, procedure={"name": "p"},
            launch=prepared.as_dict(), review_ref=f"review:{operation_id}",
        )
        return self.queue.review(operation_id, projection["input_hash"], projection["procedure_hash"], f"review:{operation_id}")

    def codex_operation(self, operation_id: str) -> gateway.PreparedLaunch:
        prepared = self.runner.prepare_codex_resume(
            self.op_key(operation_id), CODEX_EXE, OLD_THREAD, f"prompt {operation_id}", str(self.root),
            receipt_path=str(self.root / f"{operation_id}.receipt.json"), task_id=operation_id,
        )
        self._enqueue(operation_id, prepared)
        return prepared

    def claude_launch(self, operation_id: str, *, mode: str = "start", model: str = MODEL, session: str = SESSION,
                      cwd: str | None = None, permission_mode: str = PERMISSION) -> gateway.PreparedLaunch:
        return self.runner.prepare_claude_session(
            self.op_key(operation_id), CLAUDE_EXE, session, f"prompt {operation_id}", cwd or str(self.workdir),
            model=model, effort=EFFORT, permission_mode=permission_mode, session_mode=mode,
            receipt_path=str(self.root / f"{operation_id}.receipt.json"), task_id=operation_id,
        )

    def claude_operation(self, operation_id: str, **kwargs) -> gateway.PreparedLaunch:
        prepared = self.claude_launch(operation_id, **kwargs)
        self._enqueue(operation_id, prepared)
        return prepared

    def rebind(self, **overrides):
        args = {
            "profile": profile(self.workdir),
            "reason": "rate-limited Codex executor replaced by Claude Code", "expected_generation": 1, "requested_by": "root",
        }
        args.update(overrides)
        return self.queue.rebind_executor("phone-main", "claude", SESSION, **args)

    def receipt(self, task: dict, prepared: gateway.PreparedLaunch, **overrides) -> dict:
        data = {
            "operation_id": task["operation_id"], "resource_id": task["resource_id"], "job_id": task["job_id"],
            "op_key": task["operation_key"], "thread_id": task["dedicated_thread_id"], "owner": task["owner"],
            "generation": task["generation"], "payload_hash": task["payload_hash"], "device_identity": task["device_identity"],
            "receipt_state": "released", "owned_identities": [], "capture_identities": [],
            "device": {"kind": "phone", "state": "idle", "device_idle": True, "phone_input_stopped": True,
                       "screen_state": "home", "screen_appropriate": True, "capture_stopped": True, "capture_flushed": True},
        }
        if prepared.route == gateway.CLAUDE_ROUTE:
            executor = task["executor"]
            data.update(executor_provider=executor["provider"], executor_session_id=executor["session_id"],
                        executor_generation=executor["generation"])
        else:
            data["stdout_jsonl"] = "\n".join((
                json.dumps({"type": "thread.started", "thread_id": prepared.thread_id}),
                json.dumps({"type": "item.completed", "item": {"id": "m", "type": "agent_message", "text": "ok"}}),
                json.dumps({"type": "turn.completed"}),
            )) + "\n"
        data.update(overrides)
        Path(prepared.receipt_path or "").write_text(json.dumps(data), encoding="utf-8")
        return data

    def finish_by_pump(self, operation_id: str, prepared: gateway.PreparedLaunch) -> dict:
        self.pump()
        task = self.queue.task(operation_id)
        assert task is not None and task["state"] == "running", task
        self.receipt(task, prepared)
        self.pump()
        return self.queue.task(operation_id)

    @contextmanager
    def cli_jobs(self):
        with mock.patch.object(gateway.jobs, "job_status", self.jobs.job_status), \
             mock.patch.object(gateway.jobs, "identity", self.jobs.identity):
            yield


class ExecutorMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def tearDown(self) -> None:
        self.h.close()

    def finished_codex_task(self, operation_id: str = "old-1") -> tuple[gateway.PreparedLaunch, dict, dict]:
        prepared = self.h.codex_operation(operation_id)
        task = self.h.finish_by_pump(operation_id, prepared)
        self.assertEqual((task["state"], task["outcome"]), ("terminal", "succeeded"))
        receipt = json.loads(Path(prepared.receipt_path or "").read_text(encoding="utf-8"))
        return prepared, task, receipt

    def test_idle_rebind_appends_receipt_and_keeps_history(self):
        _, old_task, _ = self.finished_codex_task()
        old_receipts = self.h.queue.receipts("old-1")
        moved = self.h.rebind()
        binding = moved["binding"]
        self.assertFalse(moved["idempotent"])
        self.assertEqual((binding["generation"], binding["provider"], binding["session_id"]), (2, "claude", SESSION))
        self.assertEqual((binding["previous_generation"], binding["previous_provider"], binding["previous_session_id"]),
                         (1, "codex", OLD_THREAD))
        self.assertEqual(binding["profile"], profile(self.h.workdir.resolve()))
        self.assertIn("rate-limited", binding["reason"])
        self.assertTrue(binding["created_at_utc"].startswith("2030-03-17T") and binding["created_at_utc"].endswith("Z"))
        resource = self.h.queue.status(resource_id="phone-main")
        self.assertEqual(resource["dedicated_thread_id"], SESSION)
        self.assertEqual(resource["registered_dedicated_thread_id"], OLD_THREAD)
        self.assertEqual([item["provider"] for item in resource["executor_history"]], ["codex", "claude"])
        self.assertEqual(resource["device_identity"], DEVICE)
        # Past terminal work keeps its own executor, thread and receipts.
        task = self.h.queue.task("old-1")
        self.assertEqual((task["executor"]["provider"], task["executor"]["generation"], task["dedicated_thread_id"]), ("codex", 1, OLD_THREAD))
        self.assertFalse(task["executor"]["binding_current"])
        self.assertEqual(task["outcome"], old_task["outcome"])
        self.assertEqual(self.h.queue.receipts("old-1"), old_receipts)
        # Repeating the request is a no-op; the ledger is append-only.
        again = self.h.rebind()
        self.assertTrue(again["idempotent"])
        self.assertEqual(len(self.h.queue.status(resource_id="phone-main")["executor_history"]), 2)
        for statement in ("UPDATE executor_bindings SET reason='x'", "DELETE FROM executor_bindings"):
            with self.assertRaises(sqlite3.IntegrityError):
                sql(self.h.queue.db_path, statement)
        # The original registration still reads as the same resource identity.
        self.h.queue.register_resource(
            "phone-main", "phone", OLD_THREAD,
            initial_attestation={"resource_id": "phone-main", "kind": "phone", "state": "idle", "idle": True, "device_identity": DEVICE},
        )

    def test_rebind_is_refused_for_busy_and_unknown_dispatch(self):
        self.h.jobs.status = "running"
        self.h.codex_operation("busy-1")
        self.h.pump()
        self.assertEqual((self.h.queue.task("busy-1")["state"], self.h.queue.task("busy-1")["substate"]), ("running", "running"))
        with self.assertRaises(rq.QueueError) as busy:
            self.h.rebind()
        self.assertEqual(str(busy.exception), "resource_busy")

        other = Harness()
        try:
            other.codex_operation("lost-1")
            rq.Controller(other.queue, RaisingRunner(), owner="offline-controller").pump()
            self.assertEqual(other.queue.task("lost-1")["substate"], "unknown")
            with self.assertRaises(rq.QueueError) as unknown:
                other.rebind()
            self.assertEqual(str(unknown.exception), "resource_dispatch_unknown")
            for harness in (self.h, other):
                self.assertEqual(len(harness.queue.status(resource_id="phone-main")["executor_history"]), 1)
        finally:
            other.close()

    def test_idle_return_to_codex_preserves_snapshots_and_release_checks(self):
        self.finished_codex_task()
        self.h.rebind()
        claude_launch = self.h.claude_operation("claude-before-return")
        self.h.finish_by_pump("claude-before-return", claude_launch)
        before = {op: self.h.queue.task(op) for op in ("old-1", "claude-before-return")}
        receipts = {op: self.h.queue.receipts(op) for op in before}
        session = "11111111-2222-4333-8444-555555555555"
        args = cli._parser().parse_args([
            "--queue-dir", str(self.h.root / "queue"), "rebind-executor", "phone-main",
            "--provider", "codex", "--session-id", session, "--model", "gpt-6.1-sol", "--effort", "xhigh",
            "--permission-mode", "default", "--cwd", str(self.h.workdir), "--reason", "Claude quota exhausted",
            "--expected-generation", "2", "--requested-by", "root",
        ])
        moved = cli._run(args)
        self.assertFalse(moved["idempotent"])
        resource = moved["resource"]
        self.assertEqual([b["provider"] for b in resource["executor_history"]], ["codex", "claude", "codex"])
        self.assertEqual((resource["executor"]["generation"], resource["dedicated_thread_id"]), (3, session))
        self.assertEqual((resource["state"], resource["owner"], resource["lease_generation"]), ("idle", None, None))
        self.assertEqual(resource["registered_dedicated_thread_id"], OLD_THREAD)
        self.assertTrue(cli._run(args)["idempotent"])
        for op, old in before.items():
            current = self.h.queue.task(op)
            for key in ("payload", "payload_hash", "dedicated_thread_id", "outcome", "generation"):
                self.assertEqual(current[key], old[key])
            for key in ("provider", "session_id", "generation", "profile"):
                self.assertEqual(current["executor"][key], old["executor"][key])
            self.assertEqual(self.h.queue.receipts(op), receipts[op])

        # Existing prepared Node/Codex route and JSONL parser remain unchanged.
        command = [r"C:\offline\node.exe", r"C:\offline\node_modules\@openai\codex\bin\codex.js",
                   "exec", "resume", session, "continue QA", "-m", "gpt-6.1-sol",
                   "-c", 'model_reasoning_effort="xhigh"', "--json"]
        prepared = self.h.runner.prepare(
            self.h.op_key("codex-return"), command, str(self.h.workdir), thread_id=session,
            task_id="codex-return", receipt_path=str(self.h.root / "return.receipt.json"),
        )
        self.h._enqueue("codex-return", prepared)
        self.h.pump()
        task = self.h.queue.task("codex-return")
        self.assertEqual((task["state"], task["executor"]["generation"], task["generation"]), ("running", 3, 3))
        self.assertEqual(self.h.jobs.calls[-1]["command"], command)
        receipt = self.h.receipt(task, prepared, executor_provider="codex", executor_session_id=session, executor_generation=3)
        self.assertTrue(self.h.runner.verify_release(task, receipt)["ok"])
        row = self.h.jobs.rows[task["job_id"]]
        fingerprint = row["dispatcher_request_fingerprint"]
        row["dispatcher_request_fingerprint"] = "f" * 64
        self.assertEqual(self.h.runner.verify_release(task, receipt)["error"], "job_metadata_mismatch")
        row["dispatcher_request_fingerprint"] = fingerprint
        self.h.pump()
        self.assertEqual(self.h.queue.task("codex-return")["state"], "terminal")
        self.assertEqual(self.h.queue.status(resource_id="phone-main")["state"], "idle")

    def test_codex_rebind_refuses_busy_unknown_and_permission_changes(self):
        for unknown in (False, True):
            with self.subTest(unknown=unknown):
                harness = Harness()
                try:
                    harness.rebind()
                    harness.jobs.status = "running"
                    harness.claude_operation("busy-claude")
                    if unknown:
                        rq.Controller(harness.queue, RaisingRunner(), owner="lost").pump()
                    else:
                        harness.pump()
                    with self.assertRaises(rq.QueueError) as raised:
                        harness.queue.rebind_executor(
                            "phone-main", "codex", "11111111-2222-4333-8444-555555555555",
                            profile=profile(harness.workdir, model="gpt-6.1-sol", permission_mode="default"), reason="fallback",
                        )
                    self.assertEqual(str(raised.exception), "resource_dispatch_unknown" if unknown else "resource_busy")
                    self.assertEqual(len(harness.queue.status(resource_id="phone-main")["executor_history"]), 2)
                finally:
                    harness.close()
        for mode in ("acceptEdits", "bypassPermissions", "--dangerously-skip-permissions"):
            with self.subTest(mode=mode), self.assertRaisesRegex(ValueError, "^invalid_permission_mode$"):
                self.h.queue.rebind_executor("phone-main", "codex", SESSION,
                                             profile=profile(self.h.workdir, permission_mode=mode), reason="fallback")

    def test_rebind_validates_target_session_and_profile(self):
        cases = [
            (dict(), "other", SESSION, "invalid_executor_provider"),
            (dict(), "codex", SESSION, "invalid_permission_mode"),
            (dict(), "claude", "not-a-uuid", "invalid_session_id"),
            (dict(profile=profile(self.h.workdir, effort="ultra")), "claude", SESSION, "invalid_effort"),
            (dict(profile=profile(self.h.workdir, permission_mode="bypassPermissions")), "claude", SESSION, "invalid_permission_mode"),
            (dict(profile=profile(self.h.workdir, permission_mode="--dangerously-skip-permissions")), "claude", SESSION,
             "invalid_permission_mode"),
            (dict(profile={"model": MODEL, "effort": EFFORT, "cwd": str(self.h.workdir)}), "claude", SESSION, "invalid_executor_profile"),
            (dict(reason=" "), "claude", SESSION, "invalid_rebind_reason"),
            (dict(), "claude", OLD_THREAD, "executor_session_reused"),
            (dict(expected_generation=7), "claude", SESSION, "executor_generation_conflict"),
        ]
        for overrides, provider, session, code in cases:
            with self.subTest(code=code):
                args = {"profile": profile(self.h.workdir), "reason": "r", **overrides}
                with self.assertRaises((ValueError, rq.QueueError)) as raised:
                    self.h.queue.rebind_executor("phone-main", provider, session, **args)
                self.assertEqual(str(raised.exception), code)
        self.assertEqual(len(self.h.queue.status(resource_id="phone-main")["executor_history"]), 1)

    def test_queued_task_prepared_for_old_executor_is_blocked_not_reused(self):
        self.h.codex_operation("queued-old")
        before = copy.deepcopy(self.h.queue.task("queued-old")["payload"])
        self.assertEqual(self.h.queue.task("queued-old")["state"], "ready")
        self.h.rebind()
        task = self.h.queue.task("queued-old")
        self.assertEqual((task["state"], task["blocker_code"]), ("blocked", "executor_rebind_required"))
        self.assertEqual(task["blocker"]["action"], "cancel_and_reprepare")
        self.assertEqual((task["blocker"]["task_executor"]["provider"], task["blocker"]["current_executor"]["provider"]), ("codex", "claude"))
        self.assertEqual((task["executor"]["provider"], task["dedicated_thread_id"], task["payload"]), ("codex", OLD_THREAD, before))
        self.assertIn("executor_binding_stale", [item["kind"] for item in self.h.queue.receipts("queued-old")])
        # Neither a repeated review nor a pump may revive it.
        self.h.queue.review("queued-old", task["input_hash"], task["procedure_hash"], "review:queued-old")
        self.h.pump()
        self.assertEqual(self.h.queue.task("queued-old")["blocker_code"], "executor_rebind_required")
        self.assertEqual(self.h.jobs.calls, [])
        # A launch for the other provider is never queued under a binding.
        with self.assertRaises(rq.QueueError) as raised:
            self.h.codex_operation("codex-after")
        self.assertEqual(str(raised.exception), "executor_launch_route_mismatch")
        self.assertEqual(self.h.queue.cancel("queued-old")["outcome"], "cancelled")
        self.h.claude_operation("fresh-1")
        self.h.pump()
        self.assertEqual(self.h.queue.task("fresh-1")["state"], "running")
        self.assertEqual(len(self.h.jobs.calls), 1)

    def test_claude_launch_cannot_be_queued_before_the_rebind(self):
        with self.assertRaises(rq.QueueError) as raised:
            self.h.claude_operation("too-early")
        self.assertEqual(str(raised.exception), "executor_launch_route_mismatch")

    def test_same_claude_session_runs_start_then_resume_and_binds_release_to_it(self):
        _, _, _ = self.finished_codex_task()
        self.h.rebind()
        prompt_file = self.h.root / "prompt.txt"
        prompt_file.write_text("do the QA step", encoding="utf-8")
        launched = []
        for operation_id in ("claude-1", "claude-2"):
            args = argparse.Namespace(operation_id=operation_id, resource_id="phone-main", prompt_file=str(prompt_file),
                                      claude_exe=CLAUDE_EXE, mode="auto", receipt_path=str(self.h.root / f"{operation_id}.receipt.json"))
            launch = cli._prepare_claude(self.h.queue, str(self.h.runtime), args)
            launched.append(launch)
            self.h.queue.enqueue(operation_id, "phone-main", "offline", input={"op": operation_id}, procedure={"name": "p"},
                                 launch=launch, review_ref=f"review:{operation_id}")
            task = self.h.queue.task(operation_id)
            self.h.queue.review(operation_id, task["input_hash"], task["procedure_hash"], f"review:{operation_id}")
        # Both are prepared against the same binding; the first starts the named session, the second resumes it.
        self.assertEqual(launched[0]["command"][11:13], ["--session-id", SESSION])
        self.assertEqual(launched[1]["command"][11:13], ["--resume", SESSION])
        for launch in launched:
            self.assertEqual(launch["command"][9:11], ["--permission-mode", PERMISSION])
        envelope = launched[0]["command"][-1]
        self.assertIn("current_operation_id: claude-1", envelope)
        self.assertIn(f"current_actor_job_id: {jobs.stable_job_id(self.h.op_key('claude-1'))}", envelope)
        self.assertIn("prior_terminal_job: " + self.h.queue.task("old-1")["job_id"], envelope)
        self.assertIn("do the QA step", envelope)
        self.assertEqual(launched[0]["route"], gateway.CLAUDE_ROUTE)
        self.assertEqual(launched[0]["env"], {})

        first = self.h.finish_by_pump("claude-1", gateway.PreparedLaunch(**{k: tuple(v) if k == "owned_processes" else v for k, v in launched[0].items()}))
        self.assertEqual((first["state"], first["outcome"]), ("terminal", "succeeded"))
        second = self.h.finish_by_pump("claude-2", gateway.PreparedLaunch(**{k: tuple(v) if k == "owned_processes" else v for k, v in launched[1].items()}))
        self.assertEqual((second["state"], second["outcome"]), ("terminal", "succeeded"))
        self.assertEqual([call["command"][11:13] for call in self.h.jobs.calls[-2:]], [["--session-id", SESSION], ["--resume", SESSION]])
        for call in self.h.jobs.calls[-2:]:
            self.assertNotIn("--fork-session", call["command"])
            self.assertNotIn("--continue", call["command"])
            self.assertNotIn("--dangerously-skip-permissions", call["command"])
            self.assertEqual(call["command"][9:11], ["--permission-mode", PERMISSION])
            self.assertEqual(call["env"], {})
        self.assertNotEqual(first["job_id"], second["job_id"])
        evidence = [r for r in self.h.queue.receipts("claude-2") if r["kind"] == "release"][-1]["data"]["evidence"]
        self.assertEqual((evidence["executor_provider"], evidence["executor_session_id"], evidence["executor_generation"]), ("claude", SESSION, 2))
        self.assertEqual(evidence["executor_result"]["model"], MODEL)
        self.assertEqual(evidence["executor_result"]["state"], "succeeded")
        # The real stream names no effort: the receipt keeps the request and the observation apart.
        self.assertEqual(evidence["executor_result"]["effort_evidence"], {
            "requested": EFFORT, "launch_argv": EFFORT, "stream_reported": None, "per_turn_effort_active": True,
            "basis": "launch_argv+per_turn_effort_active",
        })
        self.assertEqual(evidence["executor_result"]["permission_mode"], PERMISSION)
        # The claim recorded the binding it launched under.
        intent = [r for r in self.h.queue.receipts("claude-2") if r["kind"] == "intent"][0]["data"]["executor"]
        self.assertEqual(intent, {"provider": "claude", "session_id": SESSION, "generation": 2})

    def test_cli_release_and_pump_leave_the_same_terminal_projection(self):
        results = {}
        for path in ("pump", "cli"):
            harness = Harness()
            try:
                harness.rebind()
                prepared = harness.claude_operation(f"claude-{path}")
                harness.pump()
                task = harness.queue.task(f"claude-{path}")
                receipt = harness.receipt(task, prepared)
                if path == "pump":
                    harness.pump()
                else:
                    with harness.cli_jobs():
                        cli._record_release(harness.queue, str(harness.runtime), f"claude-{path}", receipt, "succeeded")
                final = harness.queue.task(f"claude-{path}")
                resource = harness.queue.status(resource_id="phone-main")
                evidence = [r for r in harness.queue.receipts(f"claude-{path}") if r["kind"] == "release"][-1]["data"]["evidence"]
                results[path] = (final["state"], final["outcome"], resource["state"], resource["active_operation"],
                                 evidence["executor_result"]["evidence_digest"] is not None, sorted(evidence["executor_result"]))
            finally:
                harness.close()
        self.assertEqual(results["pump"], results["cli"])
        self.assertEqual(results["pump"][:4], ("terminal", "succeeded", "idle", None))

    def test_session_and_profile_mismatch_in_the_stream_block_release(self):
        cases = {
            "session_identity_mismatch": dict(init_session="1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"),
            "session_identity_conflict/result": dict(result_session="1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"),
            "session_identity_conflict": dict(extra=({"type": "user", "session_id": "1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"},)),
            "actual_model_mismatch": dict(init_model="claude-haiku-5-5"),
            "actual_model_mismatch/message": dict(message_model="claude-opus-5-5"),
            "actual_model_mismatch/usage": dict(usage={"claude-haiku-5-5": {}}),
            "actual_effort_mismatch": dict(explicit_effort="low"),
            "actual_effort_unverified": dict(per_turn=False),
            "actual_permission_mode_mismatch": dict(permission="bypassPermissions"),
            "events_after_terminal": dict(),
            "init_record_invalid": dict(),
        }
        for label, overrides in cases.items():
            code = label.split("/")[0]
            with self.subTest(label=label):
                harness = Harness()
                try:
                    harness.rebind()
                    if code == "events_after_terminal":
                        harness.jobs.stdout = lambda: stream() + json.dumps({"type": "system", "subtype": "status"}) + "\n"
                    elif code == "init_record_invalid":
                        harness.jobs.stdout = lambda: "\n".join(line for line in stream().splitlines() if '"init"' not in line)
                    else:
                        harness.jobs.stdout = lambda overrides=overrides: stream(**overrides)
                    prepared = harness.claude_operation("claude-bad")
                    harness.pump()
                    task = harness.queue.task("claude-bad")
                    receipt = harness.receipt(task, prepared)
                    verified = harness.runner.verify_release(task, receipt)
                    self.assertFalse(verified["ok"])
                    self.assertEqual(verified["error"], code)
                    with harness.cli_jobs():
                        with self.assertRaises(rq.QueueError) as raised:
                            cli._record_release(harness.queue, str(harness.runtime), "claude-bad", receipt, "succeeded")
                    self.assertEqual(str(raised.exception), code)
                    harness.pump()
                    current = harness.queue.task("claude-bad")
                    self.assertEqual((current["state"], current["owner"]), ("running", task["owner"]))
                    self.assertNotEqual(harness.queue.status(resource_id="phone-main")["state"], "idle")
                finally:
                    harness.close()

    def test_a_failed_claude_turn_releases_as_failed_when_the_profile_is_proven(self):
        self.h.rebind()
        self.h.jobs.stdout = lambda: stream(is_error=True)
        prepared = self.h.claude_operation("claude-failed")
        task = self.h.finish_by_pump("claude-failed", prepared)
        self.assertEqual((task["state"], task["outcome"]), ("terminal", "failed"))

    def test_launch_with_another_model_cwd_or_provider_is_never_started(self):
        for label in ("model", "cwd", "permission"):
            with self.subTest(label=label):
                harness = Harness()
                try:
                    harness.rebind()
                    kwargs = {
                        "model": dict(model="claude-haiku-5-5"),
                        "cwd": dict(cwd=str(harness.root)),
                        "permission": dict(permission_mode="default"),
                    }[label]
                    harness.claude_operation("mismatch", **kwargs)
                    harness.pump()
                    task = harness.queue.task("mismatch")
                    self.assertEqual((task["state"], task["substate"]), ("running", "unknown"))
                    self.assertIn("executor_profile_mismatch", json.dumps(harness.queue.receipts("mismatch")))
                    self.assertEqual(harness.jobs.calls, [])
                    if label == "model":
                        flipped = rq.Controller._runner_task({**task, "executor": {**task["executor"], "provider": "codex"}})
                        with self.assertRaises(gateway.GatewayRunnerError) as raised:
                            harness.runner._launch_for_task(flipped)
                        self.assertEqual(raised.exception.code, "executor_provider_mismatch")
                finally:
                    harness.close()

    def test_task_tools_flow_from_cli_through_pump_and_release_and_stay_bound(self):
        self.h.rebind()
        tools_file = self.h.root / "tools.json"
        tools_file.write_text(json.dumps(TOOLS), encoding="utf-8")
        prompt_file = self.h.root / "prompt.txt"
        prompt_file.write_text("run the QA task", encoding="utf-8")

        def prepare(operation_id: str, tools_arg: str | None) -> dict:
            args = argparse.Namespace(operation_id=operation_id, resource_id="phone-main", prompt_file=str(prompt_file),
                                      claude_exe=CLAUDE_EXE, mode="auto", receipt_path=str(self.h.root / f"{operation_id}.receipt.json"),
                                      allowed_tools_file=tools_arg)
            launch = cli._prepare_claude(self.h.queue, str(self.h.runtime), args)
            self.h.queue.enqueue(operation_id, "phone-main", "offline", input={"op": operation_id}, procedure={"name": "p"},
                                 launch=launch, review_ref=f"review:{operation_id}")
            task = self.h.queue.task(operation_id)
            self.h.queue.review(operation_id, task["input_hash"], task["procedure_hash"], f"review:{operation_id}")
            return launch

        launch = prepare("tools-1", "@" + str(tools_file))
        self.assertEqual(launch["command"][11:14], ["--allowedTools", *TOOLS])
        self.assertEqual(launch["command"][14:16], ["--session-id", SESSION])
        self.assertEqual(launch["metadata"]["allowed_tools"], TOOLS)
        self.assertEqual(launch["metadata"]["allowed_tools_sha256"],
                         hashlib.sha256(json.dumps(TOOLS, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest())
        # The approvals belong to this task's launch hash, not to the resource binding.
        self.assertNotIn("allowed_tools", json.dumps(self.h.queue.status(resource_id="phone-main")["executor"]))

        # The recorded metadata and the argv must agree; neither can be altered on its own.
        runtime = {"runtime_dir": str(self.h.runtime)}
        stripped = {**launch, **runtime, "command": launch["command"][:11] + launch["command"][14:]}
        reduced = {**launch, **runtime, "metadata": {**launch["metadata"], "allowed_tools": TOOLS[:1]}}
        for label, tampered in (("argv without the tools", stripped), ("metadata with fewer tools", reduced)):
            with self.subTest(label=label):
                with self.assertRaises(gateway.GatewayRunnerError) as raised:
                    gateway._launch_from_mapping(tampered)
                self.assertEqual(raised.exception.code, "allowed_tools_metadata_mismatch")

        self.h.pump()
        task = self.h.queue.task("tools-1")
        self.assertEqual(task["state"], "running")
        self.assertEqual(self.h.jobs.calls[-1]["command"][11:14], ["--allowedTools", *TOOLS])
        receipt = self.h.receipt(task, as_prepared(launch))
        self.assertTrue(self.h.runner.verify_release(task, receipt)["ok"])
        # An argv the job actually ran with that dropped or reordered a tool is not the immutable launch.
        row = self.h.jobs.rows[task["job_id"]]
        recorded = row["command"]
        for label, command in (("dropped", recorded[:11] + recorded[14:]),
                               ("reordered", recorded[:12] + [recorded[13], recorded[12]] + recorded[14:])):
            with self.subTest(label=label):
                row["command"] = command
                verified = self.h.runner.verify_release(task, receipt)
                self.assertEqual((verified["ok"], verified["error"]), (False, "job_metadata_mismatch"))
        row["command"] = recorded
        self.h.pump()
        finished = self.h.queue.task("tools-1")
        self.assertEqual((finished["state"], finished["outcome"]), ("terminal", "succeeded"))
        evidence = [r for r in self.h.queue.receipts("tools-1") if r["kind"] == "release"][-1]["data"]["evidence"]
        self.assertEqual(evidence["executor_result"]["allowed_tools"], TOOLS)

        # A task without a list behaves exactly as before: no flag, empty metadata, resume of the same session.
        plain = prepare("plain-1", None)
        self.assertNotIn("--allowedTools", plain["command"])
        self.assertEqual(plain["command"][11:13], ["--resume", SESSION])
        self.assertEqual(plain["metadata"]["allowed_tools"], [])
        again = self.h.finish_by_pump("plain-1", as_prepared(plain))
        self.assertEqual((again["state"], again["outcome"]), ("terminal", "succeeded"))
        plain_evidence = [r for r in self.h.queue.receipts("plain-1") if r["kind"] == "release"][-1]["data"]["evidence"]
        self.assertEqual(plain_evidence["executor_result"]["allowed_tools"], [])

    def test_cli_rejects_wildcard_and_non_list_tool_files(self):
        self.h.rebind()
        prompt_file = self.h.root / "prompt.txt"
        prompt_file.write_text("x", encoding="utf-8")
        for label, content, code in (
            ("wildcard", ["Bash(*)"], "invalid_allowed_tool"),
            ("bare bash", ["Bash"], "invalid_allowed_tool"),
            ("object", {"allowed_tools": TOOLS}, "allowed_tools_must_be_list"),
        ):
            with self.subTest(label=label):
                path = self.h.root / f"{label}.json"
                path.write_text(json.dumps(content), encoding="utf-8")
                args = argparse.Namespace(operation_id=f"bad-{label}", resource_id="phone-main", prompt_file=str(prompt_file),
                                          claude_exe=CLAUDE_EXE, mode="auto", receipt_path=None, allowed_tools_file="@" + str(path))
                with self.assertRaises(ValueError) as raised:
                    cli._prepare_claude(self.h.queue, str(self.h.runtime), args)
                self.assertEqual(str(raised.exception), code)

    def test_job_argv_that_differs_from_the_immutable_launch_blocks_release(self):
        self.h.rebind()
        prepared = self.h.claude_operation("claude-argv")
        self.h.pump()
        task = self.h.queue.task("claude-argv")
        receipt = self.h.receipt(task, prepared)
        self.assertTrue(self.h.runner.verify_release(task, receipt)["ok"])
        row = self.h.jobs.rows[task["job_id"]]
        recorded = row["command"]
        for label, command in (
            ("lower effort", recorded[:8] + ["low"] + recorded[9:]),
            ("bypass", recorded[:10] + ["bypassPermissions"] + recorded[11:]),
            ("missing", None),
        ):
            with self.subTest(label=label):
                if command is None:
                    row.pop("command")
                else:
                    row["command"] = command
                verified = self.h.runner.verify_release(task, receipt)
                self.assertFalse(verified["ok"])
                self.assertEqual(verified["error"], "job_metadata_mismatch")
                row["command"] = recorded

    def test_old_codex_receipt_is_valid_only_for_the_old_provider(self):
        prepared, _, receipt = self.finished_codex_task()
        self.h.rebind()
        old_task = self.h.queue.task("old-1")
        # Terminal tasks drop their lease fields; restore the claim-time ones to re-verify the old evidence.
        at_claim = {**old_task, "owner": receipt["owner"], "generation": receipt["generation"]}
        self.assertFalse(old_task["executor"]["binding_current"])
        self.assertTrue(self.h.runner.verify_release(at_claim, receipt)["ok"])
        claude_prepared = self.h.claude_operation("claude-1")
        self.h.pump()
        task = self.h.queue.task("claude-1")
        good = self.h.receipt(task, claude_prepared)
        self.assertTrue(self.h.runner.verify_release(task, good)["ok"])
        wrong = {
            "thread": {**good, "thread_id": OLD_THREAD},
            "provider": {**good, "executor_provider": "codex"},
            "session": {**good, "executor_session_id": OLD_THREAD},
            "generation": {**good, "executor_generation": 1},
            "missing": {k: v for k, v in good.items() if k != "executor_provider"},
            "codex receipt": {**receipt},
        }
        for label, candidate in wrong.items():
            with self.subTest(label=label):
                self.assertFalse(self.h.runner.verify_release(task, candidate)["ok"])
        # The Codex launch of the old task is not accepted under the Claude binding either.
        self.assertEqual(old_task["executor"]["provider"], "codex")
        with self.assertRaises(gateway.GatewayRunnerError):
            self.h.runner._launch_for_task({**rq.Controller._runner_task(old_task), "executor": task["executor"]})

    def test_empty_queue_starts_no_model_and_one_operation_runs_once(self):
        self.h.rebind()
        self.assertEqual(self.h.pump()["actions"], [])
        self.assertEqual(self.h.jobs.calls, [])
        self.h.claude_operation("once-1")
        for _ in range(3):
            self.h.pump()
        self.assertEqual(len(self.h.jobs.calls), 1)
        self.assertEqual(self.h.queue.task("once-1")["attempts"], 1)


class LegacyLedgerTests(unittest.TestCase):
    def test_existing_ledger_is_migrated_in_place_and_atomically(self):
        harness = Harness()
        try:
            prepared = harness.codex_operation("old-1")
            harness.finish_by_pump("old-1", prepared)
            harness.codex_operation("queued-old")
            db = harness.queue.db_path
            before = sql(db, "SELECT * FROM receipts ORDER BY rowid")
            # Rewind the ledger to the pre-migration schema.
            sql(db, "DROP TRIGGER tasks_immutable_snapshot")
            sql(db, "DROP TABLE executor_bindings")
            for column in ("executor_provider", "executor_session_id", "executor_generation"):
                sql(db, f"ALTER TABLE tasks DROP COLUMN {column}")
            with mock.patch.object(rq.Queue, "_insert_binding_tx", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    rq.Queue(str(db.parent), now=lambda: NOW)
            self.assertNotIn("executor_generation", {row[1] for row in sql(db, "PRAGMA table_info(tasks)")})
            self.assertNotIn("executor_bindings", {row[0] for row in sql(db, "SELECT name FROM sqlite_master WHERE type='table'")})

            migrated = rq.Queue(str(db.parent), now=lambda: NOW)
            rq.Queue(str(db.parent), now=lambda: NOW)
            self.assertEqual(sql(db, "SELECT * FROM receipts ORDER BY rowid"), before)
            for operation_id in ("old-1", "queued-old"):
                task = migrated.task(operation_id)
                self.assertEqual((task["executor"]["provider"], task["executor"]["generation"], task["dedicated_thread_id"]),
                                 ("codex", 1, OLD_THREAD))
                self.assertTrue(task["executor"]["binding_current"])
            resource = migrated.status(resource_id="phone-main")
            self.assertEqual([(item["generation"], item["provider"], item["kind"]) for item in resource["executor_history"]],
                             [(1, "codex", "legacy_backfill")])
            self.assertEqual(migrated.task("old-1")["outcome"], "succeeded")
            with self.assertRaises(sqlite3.IntegrityError):
                sql(db, "UPDATE tasks SET executor_generation=9 WHERE operation_id='old-1'")
            # The migrated ledger rebinds like any other.
            moved = migrated.rebind_executor(
                "phone-main", "claude", SESSION, profile=profile(harness.workdir), reason="migration",
            )
            self.assertEqual(moved["binding"]["generation"], 2)
            self.assertEqual(migrated.task("queued-old")["blocker_code"], "executor_rebind_required")
        finally:
            harness.close()


def build(**overrides) -> list[str]:
    args = dict(claude_executable=CLAUDE_EXE, session_id=SESSION, prompt="go", model=MODEL, effort=EFFORT,
                permission_mode=PERMISSION, session_mode="start")
    args.update(overrides)
    return claude.build_claude_session_command(**args)


class ClaudeHelperTests(unittest.TestCase):
    def test_command_builder_names_a_standard_permission_mode_and_rejects_loosening(self):
        command = build(session_mode="resume", permission_mode="default")
        self.assertEqual(command, [CLAUDE_EXE, "-p", "--output-format", "stream-json", "--verbose", "--model", MODEL,
                                   "--effort", EFFORT, "--permission-mode", "default", "--resume", SESSION, "go"])
        self.assertEqual(claude.parse_claude_command(command)["session_mode"], "resume")
        self.assertEqual(build()[9:11], ["--permission-mode", "acceptEdits"])
        for mode in ("default", "acceptEdits"):
            self.assertEqual(claude.parse_claude_command(build(permission_mode=mode))["permission_mode"], mode)
        for tampered in (
            command + ["--fork-session"],
            command[:-1] + ["--dangerously-skip-permissions", "go"],
            command[:-1] + ["--allow-dangerously-skip-permissions", "go"],
            command[:10] + ["bypassPermissions"] + command[11:],
            command[:9] + ["--dangerously-skip-permissions", "x"] + command[11:],
            command[:11] + ["--continue", SESSION, "go"],
            [command[0], "--print", *command[2:]],
        ):
            with self.assertRaises(claude.ClaudeExecutorError):
                claude.parse_claude_command(tampered)
        for kwargs in (dict(claude_executable=r"C:\x\claude.ps1"), dict(claude_executable="claude.exe"),
                       dict(session_id=SESSION.upper()), dict(prompt="--help"), dict(model="a b"), dict(effort="ultra"),
                       dict(session_mode="fork"), dict(permission_mode="bypassPermissions"),
                       dict(permission_mode="--dangerously-skip-permissions"), dict(permission_mode="auto"),
                       dict(permission_mode="plan"), dict(permission_mode=None)):
            with self.assertRaises(claude.ClaudeExecutorError):
                build(**kwargs)

    def parse(self, data: str, **overrides) -> dict:
        args = dict(expected_session_id=SESSION, expected_model=MODEL, expected_effort=EFFORT,
                    expected_permission_mode=PERMISSION, observed_launch_command=build())
        args.update(overrides)
        return claude.parse_claude_stream_json(data.encode(), **args)

    def test_real_shaped_stream_without_an_effort_field_is_accepted_and_records_the_basis(self):
        parsed = self.parse(stream())
        self.assertEqual((parsed["state"], parsed["provider"], parsed["session_id"], parsed["model"], parsed["effort"],
                          parsed["permission_mode"]), ("succeeded", "claude", SESSION, MODEL, EFFORT, PERMISSION))
        self.assertEqual(parsed["effort_evidence"], {
            "requested": EFFORT, "launch_argv": EFFORT, "stream_reported": None, "per_turn_effort_active": True,
            "basis": "launch_argv+per_turn_effort_active",
        })
        self.assertNotIn("thread_id", parsed)
        # A stream that does state the effort is compared with it.
        stated = self.parse(stream(explicit_effort=EFFORT, per_turn=False))
        self.assertEqual((stated["state"], stated["effort_evidence"]["stream_reported"], stated["effort_evidence"]["basis"]),
                         ("succeeded", EFFORT, "stream_reported"))
        self.assertEqual(self.parse(stream(is_error=True))["state"], "failed")

    def test_stream_parser_rejects_a_profile_that_is_not_the_requested_one(self):
        cases = {
            "actual_effort_mismatch": self.parse(stream(explicit_effort="low")),
            "actual_effort_unverified": self.parse(stream(per_turn=False)),
            "actual_effort_unverified/none": self.parse(stream(per_turn=None)),
            "actual_model_mismatch": self.parse(stream(init_model="claude-haiku-5-5")),
            "actual_model_mismatch/failed-turn": self.parse(stream(init_model="claude-haiku-5-5", is_error=True)),
            "actual_permission_mode_mismatch": self.parse(stream(permission="bypassPermissions")),
            "session_identity_mismatch": self.parse(stream(init_session="1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e")),
            "session_identity_conflict": self.parse(stream(result_session="1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e")),
        }
        for label, parsed in cases.items():
            with self.subTest(label=label):
                self.assertEqual((parsed["state"], parsed["code"]), ("result_pending", label.split("/")[0]))

    def test_stream_parser_requires_the_immutable_launch_argv_as_explicit_evidence(self):
        good = build()
        cases = {
            "launch_evidence_missing": None,
            "launch_evidence_invalid": ["claude"],
            "launch_evidence_invalid/bypass": good[:10] + ["bypassPermissions"] + good[11:],
            "launch_evidence_mismatch/effort": build(effort="low"),
            "launch_evidence_mismatch/model": build(model="claude-haiku-5-5"),
            "launch_evidence_mismatch/session": build(session_id="1b2c3d4e-5f6a-4b7c-8d9e-0f1a2b3c4d5e"),
            "launch_evidence_mismatch/permission": build(permission_mode="default"),
        }
        for label, command in cases.items():
            with self.subTest(label=label):
                parsed = self.parse(stream(), observed_launch_command=command)
                self.assertEqual((parsed["state"], parsed["code"]), ("result_pending", label.split("/")[0]))
        # Resuming rather than starting is the same launch evidence.
        self.assertEqual(self.parse(stream(), observed_launch_command=build(session_mode="resume"))["state"], "succeeded")

    def test_allowed_tools_default_to_none_and_round_trip_as_exact_argv_elements(self):
        self.assertEqual(build(), build(allowed_tools=[]))
        self.assertNotIn("--allowedTools", build())
        self.assertEqual(claude.parse_claude_command(build())["allowed_tools"], [])
        command = build(allowed_tools=TOOLS, session_mode="resume")
        self.assertEqual(len(command), 17)
        self.assertEqual(command[9:11], ["--permission-mode", PERMISSION])
        self.assertEqual(command[11:14], ["--allowedTools", *TOOLS])
        # The variadic option ends at the session flag, so the prompt stays a plain positional.
        self.assertEqual(command[14:], ["--resume", SESSION, "go"])
        fields = claude.parse_claude_command(command)
        self.assertEqual((fields["allowed_tools"], fields["session_id"], fields["prompt"], fields["session_mode"]),
                         (TOOLS, SESSION, "go", "resume"))
        for tampered in (
            command[:11] + ["--allowedTools"] + command[14:],
            command[:12] + ["Bash(*)"] + command[12:],
            command[:11] + ["--allowedTools", TOOLS[0], "--dangerously-skip-permissions"] + command[14:],
            command[:11] + command[14:] + ["--allowedTools", TOOLS[0]],
        ):
            with self.assertRaises(claude.ClaudeExecutorError):
                claude.parse_claude_command(tampered)

    def test_allowed_tools_accept_only_explicit_bash_commands(self):
        for spec in ("Bash", "Bash()", "Bash(*)", "Bash(adb *)", "Bash(adb shell:*)", "Bash( adb)", "Bash(adb )", "Read",
                     "Edit(D:/x)", "mcp__x__y", "Bash(a;b)", "Bash(a && b)", "Bash(a | b)", "Bash(a > b)", "Bash(a,b)",
                     "Bash($(x))", "Bash(`x`)", "Bash(a\nb)", "--dangerously-skip-permissions", "bypassPermissions", 5, None):
            with self.subTest(spec=spec):
                for call in (lambda: claude.normalize_allowed_tools([spec]), lambda: build(allowed_tools=[spec])):
                    with self.assertRaises(claude.ClaudeExecutorError) as raised:
                        call()
                    self.assertEqual(raised.exception.code, "invalid_allowed_tool")
        too_many = [f"Bash(D:/qa/task.py step{i})" for i in range(claude.MAX_ALLOWED_TOOLS + 1)]
        for label, value in (("duplicate", [TOOLS[0], TOOLS[0]]), ("string", TOOLS[0]), ("object", {"a": 1}), ("too many", too_many)):
            with self.subTest(label=label):
                with self.assertRaises(claude.ClaudeExecutorError) as raised:
                    claude.normalize_allowed_tools(value)
                self.assertEqual(raised.exception.code, "invalid_allowed_tools")
        self.assertEqual(claude.normalize_allowed_tools(tuple(TOOLS)), TOOLS)
        self.assertEqual(claude.normalize_allowed_tools(None), [])

    def test_parser_requires_the_approved_tools_to_match_the_launch_argv(self):
        launched = build(allowed_tools=TOOLS)
        good = self.parse(stream(), observed_launch_command=launched, expected_allowed_tools=TOOLS)
        self.assertEqual((good["state"], good["allowed_tools"]), ("succeeded", TOOLS))
        self.assertEqual(self.parse(stream())["allowed_tools"], [])
        cases = {
            "argv has tools, none approved": dict(observed_launch_command=launched),
            "approved, argv has none": dict(observed_launch_command=build(), expected_allowed_tools=TOOLS),
            "reordered": dict(observed_launch_command=build(allowed_tools=TOOLS[::-1]), expected_allowed_tools=TOOLS),
            "extra tool": dict(observed_launch_command=build(allowed_tools=[*TOOLS, "Bash(D:/qa/task.py home)"]),
                               expected_allowed_tools=TOOLS),
            "fewer tools": dict(observed_launch_command=build(allowed_tools=TOOLS[:1]), expected_allowed_tools=TOOLS),
        }
        for label, overrides in cases.items():
            with self.subTest(label=label):
                parsed = self.parse(stream(), **overrides)
                self.assertEqual((parsed["state"], parsed["code"]), ("result_pending", "launch_evidence_mismatch"))
        wildcard = self.parse(stream(), observed_launch_command=launched, expected_allowed_tools=["Bash(*)"])
        self.assertEqual((wildcard["state"], wildcard["code"]), ("result_pending", "invalid_allowed_tool"))




if __name__ == "__main__":
    unittest.main()
