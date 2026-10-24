"""GatewayRunner/CLI seams for the local resource queue.

The Gateway adapter is exercised against a fake ``jobs`` API and fake process
identity results.  No real ``job_start`` implementation, child process,
device, or model is invoked.  The CLI checks are limited to help/status in a
temporary ledger and hide the Windows subprocess window.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest import mock

from home_mcp_gateway import jobs
from home_mcp_gateway import resource_queue as rq
from home_mcp_gateway import resource_queue_cli as cli
from home_mcp_gateway import resource_queue_gateway as gateway


class FakeJobsAPI:
    def __init__(self):
        self.calls: list[dict] = []
        self.status_rows: dict[str, dict] = {}
        self.lose_start_once = False
        self.identity_states: dict[int, str] = {}
        self.next_status: dict[str, object] = {
            "status": "exited", "exit_code": 0, "pid": 3001, "process_create_time": 30.0,
            "worker_pid": 3002, "worker_create_time": 31.0, "thread_id": "thread-phone-main",
        }

    def job_start(self, command, *, cwd, env, runtime_dir, idempotency_key, request_fingerprint):
        self.calls.append({"command": command, "cwd": cwd, "env": env, "runtime_dir": runtime_dir,
                           "idempotency_key": idempotency_key, "request_fingerprint": request_fingerprint})
        job_id = jobs.stable_job_id(idempotency_key)
        row = dict(self.status_rows.get(job_id, self.next_status))
        row.update(job_id=job_id, idempotency_key=idempotency_key, command=command, cwd=cwd,
                   dispatcher_request_fingerprint=request_fingerprint,
                   explicit_env_digest=jobs._explicit_env_digest(env))
        self.status_rows[job_id] = row
        if self.lose_start_once:
            self.lose_start_once = False
            raise ConnectionError("fake start response lost")
        return dict(row)

    def job_status(self, job_id, runtime_dir=None):
        if job_id not in self.status_rows:
            raise gateway.GatewayRunnerError("job_not_found")
        return dict(self.status_rows[job_id])

    def identity(self, pid, create_time):
        return self.identity_states.get(pid, "missing")


class GatewayQueueIntegrationTests(unittest.TestCase):
    THREAD = "99999999-9999-4999-8999-999999999999"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue_dir = self.root / "queue"
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir()
        self.clock = lambda: 1_900_000_000.0
        self.jobs_api = FakeJobsAPI()
        self.runner = gateway.GatewayRunner(str(self.runtime_dir), jobs_api=self.jobs_api, clock=self.clock)
        self.queue = rq.Queue(str(self.queue_dir), now=self.clock)
        self.queue.register_resource(
            "phone-main", "phone", self.THREAD,
            initial_attestation={"resource_id": "phone-main", "kind": "phone", "state": "idle", "idle": True,
                                 "device_identity": "adb:phone-main"},
        )

    def tearDown(self):
        self.temp.cleanup()

    def adapter_task(self, operation_id: str = "adapter-op"):
        op_key = "rq1:" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        payload_hash = "e" * 64
        prepared = self.runner.prepare(
            op_key, ["fake-codex", "exec", "resume", self.THREAD, "prompt"], str(self.root),
            dispatcher_request_fingerprint="d" * 64, thread_id=self.THREAD, task_id=operation_id,
            receipt_path=str(self.root / f"{operation_id}.receipt.json"),
        )
        job_id = jobs.stable_job_id(op_key)
        self.jobs_api.status_rows[job_id] = {
            "status": "exited", "exit_code": 0, "pid": 3001, "process_create_time": 30.0,
            "worker_pid": 3002, "worker_create_time": 31.0, "thread_id": self.THREAD,
            "job_id": job_id, "idempotency_key": op_key, "command": prepared.command,
            # Controller dispatch pins job metadata to the queue payload hash.
            "cwd": prepared.cwd, "dispatcher_request_fingerprint": payload_hash,
            "explicit_env_digest": prepared.explicit_env_digest,
        }
        task = {
            "operation_id": operation_id, "operation_key": op_key, "op_key": op_key,
            "job_id": job_id, "resource_id": "phone-main", "resource_kind": "phone",
            "dedicated_thread_id": self.THREAD, "owner": "controller-test", "generation": 1,
            "payload_hash": payload_hash, "device_identity": "adb:phone-main",
            "launch": prepared.as_dict(), "payload": prepared.as_dict(),
        }
        receipt = {
            "job_id": job_id, "op_key": op_key, "operation_id": operation_id,
            "resource_id": "phone-main", "thread_id": self.THREAD,
            "owner": task["owner"], "generation": task["generation"],
            "payload_hash": task["payload_hash"], "device_identity": "adb:phone-main",
            "receipt_state": "released", "owned_identities": [], "capture_identities": [],
            "stdout_jsonl": "\n".join([
                json.dumps({"type": "thread.started", "thread_id": self.THREAD}),
                json.dumps({"type": "item.completed", "item": {"id": "message-final", "type": "agent_message", "text": "fake result"}}),
                json.dumps({"type": "turn.completed"}),
            ]) + "\n",
            "device": {"kind": "phone", "state": "idle", "device_idle": True,
                        "phone_input_stopped": True, "screen_state": "appropriate", "screen_appropriate": True,
                        "capture_stopped": True, "capture_flushed": True},
        }
        return task, prepared, receipt

    def test_resume_builder_is_exact_and_thread_bound(self):
        key = "rq1:" + "a" * 64
        command = gateway.build_codex_resume_command(
            r"C:\codex\codex.exe", "thread-phone-main", "resume prompt",
            model="gpt-test", effort="high", sandbox="workspace-write",
            approval_policy="never", config_overrides=("foo=bar",),
        )
        self.assertEqual(command[:3], [r"C:\codex\codex.exe", "exec", "resume"])
        self.assertIn("thread-phone-main", command)
        self.assertIn("resume prompt", command)
        self.assertIn("--json", command)
        prepared = self.runner.prepare_codex_resume(
            key, r"C:\codex\codex.exe", "thread-phone-main", "resume prompt", str(self.root),
            dispatcher_request_fingerprint="b" * 64,
        )
        self.assertEqual(prepared.op_key, key)
        self.assertEqual(prepared.thread_id, "thread-phone-main")
        self.assertEqual(prepared.dispatcher_request_fingerprint, "b" * 64)
        self.assertEqual(gateway.expected_job_id(key), jobs.stable_job_id(key))

    def test_release_rejects_alive_access_denied_owned_and_capture_identities(self):
        for label, pid, state, field in (
            ("root", 3001, "alive", None),
            ("worker", 3002, "access_denied", None),
            ("owned", 4001, "alive", "owned_identities"),
            ("capture", 4002, "alive", "capture_identities"),
        ):
            with self.subTest(label=label):
                task, prepared, receipt = self.adapter_task(f"identity-{label}")
                if field is not None:
                    receipt[field] = [{"label": label, "pid": pid, "create_time": float(pid) / 100.0}]
                self.jobs_api.identity_states[pid] = state
                with mock.patch.object(gateway.jobs, "identity", return_value="missing"):
                    result = self.runner.verify_release(task, receipt)
                self.assertFalse(result["ok"])
                self.assertIn(result["error"], {"release_identity_not_proven", "release_incomplete"})
                self.jobs_api.identity_states.clear()

    def test_lost_start_reconciles_same_key_and_same_gateway_job_once(self):
        operation_id = "lost-start"
        task, prepared, _ = self.adapter_task(operation_id)
        # The keyed reservation does not exist before the first start call;
        # job_start will persist it before its response is lost.
        self.jobs_api.status_rows.pop(task["job_id"])
        self.jobs_api.lose_start_once = True
        first = self.runner.start(prepared)
        self.assertFalse(first.get("ok", False))
        second = self.runner.start(prepared)
        self.assertTrue(second.get("ok", False), second)
        self.assertEqual(len(self.jobs_api.calls), 1)
        self.assertEqual(self.jobs_api.calls[0]["idempotency_key"], task["operation_key"])
        self.assertEqual(second["job_id"], task["job_id"])

    def test_incomplete_reservation_is_not_authoritative_absence_and_is_not_retried(self):
        operation_id = "incomplete-reservation"
        task, prepared, _ = self.adapter_task(operation_id)
        self.jobs_api.status_rows[task["job_id"]] = {
            "job_id": task["job_id"], "status": "unknown", "error": "reservation_incomplete",
        }
        result = self.runner.start(prepared)
        self.assertFalse(result.get("ok", False))
        self.assertEqual(self.jobs_api.calls, [])

    def test_wrong_thread_jsonl_is_rejected(self):
        task, prepared, receipt = self.adapter_task("wrong-thread")
        receipt["stdout_jsonl"] = receipt["stdout_jsonl"].replace(self.THREAD, "88888888-8888-4888-8888-888888888888")
        with mock.patch.object(gateway.jobs, "identity", return_value="missing"):
            result = self.runner.verify_release(task, receipt)
        self.assertFalse(result["ok"])
        self.assertIn(result["error"], {"thread_identity_mismatch", "thread_identity_invalid"})

    def test_turn_failed_with_zero_exit_preserves_failed_job_outcome_under_cancel(self):
        task, prepared, receipt = self.adapter_task("turn-failed")
        task["cancel_requested"] = True
        receipt["stdout_jsonl"] = "\n".join([
            json.dumps({"type": "thread.started", "thread_id": self.THREAD}),
            json.dumps({"type": "turn.failed", "error": {"message": "fake turn failure"}}),
        ]) + "\n"
        with mock.patch.object(gateway.jobs, "identity", return_value="missing"):
            result = self.runner.verify_release(task, receipt)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["release_evidence"]["outcome"], "failed")
        self.assertEqual(result["release_evidence"].get("job_outcome"), "failed")

    def test_nonzero_exit_or_actor_failed_receipt_overrides_successful_turn(self):
        for suffix, exit_code, receipt_outcome in (
            ("nonzero-exit", 7, None),
            ("actor-failed", 0, "failed"),
        ):
            with self.subTest(case=suffix):
                task, prepared, receipt = self.adapter_task(f"outcome-precedence-{suffix}")
                self.jobs_api.status_rows[task["job_id"]]["exit_code"] = exit_code
                if receipt_outcome is not None:
                    receipt["outcome"] = receipt_outcome
                with mock.patch.object(gateway.jobs, "identity", return_value="missing"):
                    result = self.runner.verify_release(task, receipt)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["release_evidence"]["outcome"], "failed")
                self.assertEqual(result["release_evidence"].get("job_outcome"), "failed")

    def test_payload_thread_binding_mismatch_rejects_before_job_start(self):
        task, prepared, _ = self.adapter_task("payload-thread-mismatch")
        payload = dict(task)
        payload["launch"] = dict(prepared.as_dict(), thread_id="88888888-8888-4888-8888-888888888888")
        with self.assertRaises(gateway.GatewayRunnerError):
            self.runner.start(payload)
        self.assertEqual(self.jobs_api.calls, [])

    def test_resume_argv_thread_is_not_inferred_from_prompt(self):
        command = gateway.build_codex_resume_command(
            r"C:\codex\codex.exe", self.THREAD, f"please resume {uuid.uuid4()}",
        )
        self.assertEqual(command[3], self.THREAD)
        self.assertNotEqual(command[3], command[4])

    def test_queue_controller_uses_keyed_fake_gateway_and_release_receipt(self):
        operation_id = "gateway-op"
        op_key = "rq1:" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        request_fp = "d" * 64
        prepared = self.runner.prepare(
            op_key, ["fake-codex", "exec", "resume", self.THREAD, "prompt"], str(self.root),
            dispatcher_request_fingerprint=request_fp, thread_id=self.THREAD, task_id=operation_id,
            receipt_path=str(self.root / "release-receipt.json"),
        )
        launch = prepared.as_dict()
        task_projection = self.queue.enqueue(
            operation_id, "phone-main", "gateway-test", input={"prompt": "prompt"},
            procedure={"name": "resume"}, launch=launch, review_ref="review:gateway",
        )
        self.queue.review(operation_id, task_projection["input_hash"], task_projection["procedure_hash"], "review:gateway")
        task = self.queue._claim_next("controller-gateway", 30)
        self.assertIsNotNone(task)
        self.assertEqual(task["job_id"], jobs.stable_job_id(task["operation_key"]))

        job_id = jobs.stable_job_id(op_key)
        receipt_path = Path(prepared.receipt_path)
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt_path.write_text(json.dumps({
            "job_id": job_id, "op_key": op_key, "operation_id": operation_id,
            "resource_id": "phone-main", "thread_id": self.THREAD,
            "owner": task["owner"], "generation": task["generation"],
            "payload_hash": task["payload_hash"], "receipt_state": "released",
            "device_identity": "adb:phone-main",
            "owned_identities": [], "capture_identities": [],
            "stdout_jsonl": "\n".join([
                json.dumps({"type": "thread.started", "thread_id": self.THREAD}),
                json.dumps({"type": "item.completed", "item": {"id": "message-final", "type": "agent_message", "text": "fake result"}}),
                json.dumps({"type": "turn.completed"}),
            ]) + "\n",
            "device": {"kind": "phone", "state": "idle", "device_idle": True,
                        "phone_input_stopped": True, "screen_state": "home", "screen_appropriate": True,
                        "capture_stopped": True, "capture_flushed": True},
        }), encoding="utf-8")

        with mock.patch.object(gateway.jobs, "identity", return_value="missing"):
            action = rq.Controller(self.queue, self.runner, owner="controller-gateway")._dispatch_claimed(task)
        self.assertEqual(action["result"], "terminal")
        finished = self.queue.task(operation_id)
        self.assertEqual(finished["state"], "terminal")
        self.assertEqual(finished["outcome"], "succeeded")
        self.assertEqual(len(self.jobs_api.calls), 1)
        self.assertEqual(self.jobs_api.calls[0]["idempotency_key"], op_key)
        self.assertEqual(self.queue.status()["resources"][0]["state"], "idle")

    def test_cli_help_and_status_are_one_shot_hidden_local_calls(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        hidden = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        help_result = subprocess.run(
            [sys.executable, "-m", "home_mcp_gateway.resource_queue_cli", "--help"],
            cwd=str(Path(__file__).resolve().parents[1]), env=env, capture_output=True, text=True,
            timeout=15, **hidden,
        )
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("One-shot local resource queue", help_result.stdout)
        status_result = subprocess.run(
            [sys.executable, "-m", "home_mcp_gateway.resource_queue_cli", "--queue-dir", str(self.queue_dir), "status"],
            cwd=str(Path(__file__).resolve().parents[1]), env=env, capture_output=True, text=True,
            timeout=15, **hidden,
        )
        self.assertEqual(status_result.returncode, 0, status_result.stderr)
        status = json.loads(status_result.stdout)
        self.assertEqual(status["tasks"], [])
        self.assertEqual({item["resource_id"] for item in status["resources"]}, {"phone-main"})

    def test_cli_release_must_pass_gateway_verifier_before_terminalizing(self):
        operation_id = "cli-release-op"
        task_projection = self.queue.enqueue(
            operation_id, "phone-main", "cli-test", input={"prompt": "x"},
            procedure={"name": "procedure"}, launch={"command": ["fake"]}, review_ref="review:cli",
        )
        self.queue.review(operation_id, task_projection["input_hash"], task_projection["procedure_hash"], "review:cli")
        task = self.queue._claim_next("controller-cli", 30)
        self.assertIsNotNone(task)
        evidence = {
            "operation_id": operation_id, "resource_id": "phone-main", "job_id": task["job_id"],
            "thread_id": task["dedicated_thread_id"], "owner": task["owner"],
            "generation": task["generation"], "payload_hash": task["payload_hash"], "outcome": "succeeded",
            # Deliberately self-declared. The adapter must reject this because
            # its exact process identity is still alive.
            "process": {
                "terminal": True, "identity": "verified", "identity_verified": True, "create_time": 44.0,
                "root_worker": {"verified": True, "create_time": 45.0}, "owned_children": [],
                "captures": {"stopped": True, "flushed": True},
            },
            "device": {"state": "idle", "device_idle": True, "device_identity": "adb:phone-main", "phone_input_stopped": True,
                        "screen_state": "appropriate", "screen_appropriate": True,
                        "capture_stopped": True, "capture_flushed": True},
        }
        holder: dict[str, Any] = {}

        class RefusingRunner:
            def __init__(self, *args: Any, **kwargs: Any):
                holder["runner"] = self
                self.calls = []

            def verify_release(self, launch: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
                self.calls.append((dict(launch), dict(receipt)))
                return {"ok": False, "status": "blocked", "error": "release_identity_not_proven"}

        with mock.patch.object(cli, "GatewayRunner", RefusingRunner):
            def invoke_release():
                try:
                    return cli._record_release(self.queue, str(self.runtime_dir), operation_id, evidence, "succeeded")
                except TypeError:
                    # Compatibility with the pre-cleanup private signature;
                    # either path must still invoke the adapter verifier.
                    return cli._record_release(self.queue, operation_id, evidence, "succeeded", False)

            with self.assertRaises(rq.QueueError):
                invoke_release()
        self.assertIn("runner", holder)
        self.assertEqual(len(holder["runner"].calls), 1)
        current = self.queue.task(operation_id)
        self.assertEqual(current["state"], "running")
        self.assertEqual(current["generation"], task["generation"])
        self.assertEqual(self.queue.status()["resources"][0]["active_operation"], operation_id)

    def test_cli_release_verifier_runs_under_controller_singleton(self):
        operation_id = "cli-lock-op"
        task_projection = self.queue.enqueue(
            operation_id, "phone-main", "cli-test", input={"prompt": "x"},
            procedure={"name": "procedure"}, launch={"command": ["fake"]}, review_ref="review:lock",
        )
        self.queue.review(operation_id, task_projection["input_hash"], task_projection["procedure_hash"], "review:lock")
        task = self.queue._claim_next("controller-cli", 30)
        self.assertIsNotNone(task)
        evidence = {
            "operation_id": operation_id, "resource_id": "phone-main", "job_id": task["job_id"],
            "thread_id": task["dedicated_thread_id"], "owner": task["owner"],
            "generation": task["generation"], "payload_hash": task["payload_hash"], "outcome": "succeeded",
            "process": {"terminal": True, "identity": "verified", "identity_verified": True, "create_time": 44.0,
                        "root_worker": {"verified": True, "create_time": 45.0}, "owned_children": [],
                        "captures": {"stopped": True, "flushed": True}},
            "device": {"state": "idle", "device_idle": True, "device_identity": "adb:phone-main",
                        "phone_input_stopped": True, "screen_state": "appropriate", "screen_appropriate": True,
                        "capture_stopped": True, "capture_flushed": True},
        }
        held = {"value": False}
        original_lock = self.queue.controller_lock

        from contextlib import contextmanager

        @contextmanager
        def observed_lock():
            held["value"] = True
            try:
                with original_lock():
                    yield
            finally:
                held["value"] = False

        holder: dict[str, Any] = {}

        class RecordingRunner:
            def __init__(self, *args: Any, **kwargs: Any):
                holder["runner"] = self
                self.lock_seen = False

            def verify_release(self, launch: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
                self.lock_seen = held["value"]
                return {"ok": False, "status": "blocked", "error": "release_identity_not_proven"}

        with mock.patch.object(self.queue, "controller_lock", observed_lock), \
             mock.patch.object(cli, "GatewayRunner", RecordingRunner):
            with self.assertRaises(rq.QueueError):
                cli._record_release(self.queue, str(self.runtime_dir), operation_id, evidence, "succeeded")
        self.assertTrue(holder["runner"].lock_seen)
        self.assertEqual(self.queue.task(operation_id)["state"], "running")

    def test_cli_cancel_receipt_without_raw_outcome_canonicalizes_cancelled(self):
        class CanonicalRunner:
            def __init__(self, *args: Any, **kwargs: Any):
                self.received: Mapping[str, Any] | None = None

            def verify_release(self, launch: Mapping[str, Any], receipt: Mapping[str, Any]) -> dict[str, Any]:
                self.received = receipt
                return {"ok": True, "status": "released", "release_evidence": {
                    **dict(receipt), "outcome": "succeeded", "job_outcome": "succeeded",
                }}

        holder: dict[str, Any] = {}

        class HolderRunner(CanonicalRunner):
            def __init__(self, *args: Any, **kwargs: Any):
                super().__init__(*args, **kwargs)
                holder["runner"] = self

        for suffix, effective_outcome in (("omitted", None), ("explicit", "cancelled")):
            with self.subTest(effective_outcome=effective_outcome):
                operation_id = f"cli-cancel-receipt-{suffix}"
                task_projection = self.queue.enqueue(
                    operation_id, "phone-main", "cli-test", input={"prompt": suffix},
                    procedure={"name": "procedure"}, launch={"command": ["fake"]}, review_ref=f"review:cancel:{suffix}",
                )
                self.queue.review(operation_id, task_projection["input_hash"], task_projection["procedure_hash"], f"review:cancel:{suffix}")
                task = self.queue._claim_next("controller-cli", 30)
                self.assertIsNotNone(task)
                self.queue.cancel(operation_id, "user-request")
                evidence = {
                    "operation_id": operation_id, "resource_id": "phone-main", "job_id": task["job_id"],
                    "thread_id": task["dedicated_thread_id"], "owner": task["owner"],
                    "generation": task["generation"], "payload_hash": task["payload_hash"],
                    # No raw outcome: the adapter supplies the actual job
                    # outcome while the caller supplies effective cancellation.
                    "process": {"terminal": True, "identity": "verified", "identity_verified": True, "create_time": 44.0,
                                "root_worker": {"verified": True, "create_time": 45.0}, "owned_children": [],
                                "captures": {"stopped": True, "flushed": True}},
                    "device": {"state": "idle", "device_idle": True, "device_identity": "adb:phone-main",
                                "phone_input_stopped": True, "screen_state": "appropriate", "screen_appropriate": True,
                                "capture_stopped": True, "capture_flushed": True},
                }
                with mock.patch.object(cli, "GatewayRunner", HolderRunner):
                    cli._record_release(self.queue, str(self.runtime_dir), operation_id, evidence, effective_outcome)
                self.assertNotIn("outcome", holder["runner"].received or {})
                release = [item for item in self.queue.receipts(operation_id) if item["kind"] == "release"][-1]
                self.assertEqual(release["data"]["evidence"]["job_outcome"], "succeeded")
                self.assertEqual(self.queue.task(operation_id)["outcome"], "cancelled")


if __name__ == "__main__":
    unittest.main()
