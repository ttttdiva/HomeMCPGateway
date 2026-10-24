"""Fake-runtime acceptance tests for :mod:`home_mcp_gateway.resource_queue`.

The tests exercise only the local ledger and the injected runner boundary.
They never invoke a real Gateway job, model, phone, GPU, or device API.
"""
from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
import json
from pathlib import Path
from typing import Any, Mapping
from unittest import mock

from home_mcp_gateway import resource_queue as rq


BASE = 1_900_000_000.0


class FakeClock:
    def __init__(self, value: float = BASE):
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeProcessTable:
    """Identity table used by tests to model PID reuse and liveness."""

    def __init__(self):
        self.rows: dict[str, tuple[int, float, bool]] = {}

    def add(self, owner: str, pid: int, create_time: float, alive: bool = True) -> None:
        self.rows[owner] = (pid, create_time, alive)

    def stop(self, owner: str) -> None:
        pid, create_time, _ = self.rows[owner]
        self.rows[owner] = (pid, create_time, False)

    def reuse_pid(self, owner: str, create_time: float) -> None:
        pid, _, alive = self.rows[owner]
        self.rows[owner] = (pid, create_time, alive)

    def identity(self, pid: int, create_time: float) -> str:
        for candidate_pid, candidate_time, alive in self.rows.values():
            if candidate_pid == pid:
                if candidate_time != create_time:
                    return "pid_reused"
                return "alive" if alive else "missing"
        return "missing"


class FakeDevices:
    def __init__(self):
        self.rows: dict[str, dict[str, Any]] = {}

    def add(self, resource_id: str, identity: str) -> None:
        self.rows[resource_id] = {"identity": identity, "active_key": None}

    def activate(self, resource_id: str, key: str) -> None:
        self.rows[resource_id]["active_key"] = key

    def clear(self, resource_id: str) -> None:
        self.rows[resource_id]["active_key"] = None

    def identity(self, resource_id: str) -> str:
        return self.rows[resource_id]["identity"]


class FakeRunner:
    """Idempotent runner; response loss never duplicates its effect."""

    def __init__(self, clock: FakeClock, devices: FakeDevices):
        self.clock = clock
        self.devices = devices
        self.effects: dict[str, dict[str, Any]] = {}
        self.starts: list[dict[str, Any]] = []
        self.lookups: list[str] = []
        self.cancel_requests: list[str] = []
        self.lose_once: set[str] = set()
        self.unknown_lookup: set[str] = set()
        self.absent_lookup: set[str] = set()
        self.fail_without_release: set[str] = set()
        self.release_ready: set[str] = set()
        self.release_override: dict[str, dict[str, Any]] = {}
        self.verify_false: set[str] = set()
        self.cancel_unknown_once: set[str] = set()
        self.verify_calls: list[str] = []
        self.block_start = False
        self.start_entered = threading.Event()
        self.start_continue = threading.Event()
        self.cancelled: set[str] = set()
        self._job_counter = 0

    @staticmethod
    def _key(task: Mapping[str, Any]) -> str:
        return str(task["operation_key"])

    def _evidence(self, task: Mapping[str, Any], outcome: str, *, device_idle: bool = True) -> dict[str, Any]:
        return {
            "operation_id": task["operation_id"],
            "resource_id": task["resource_id"],
            "job_id": task["job_id"],
            "thread_id": task.get("dedicated_thread_id"),
            "owner": task["owner"],
            "generation": task["generation"],
            "payload_hash": task["payload_hash"],
            "device_identity": self.devices.identity(task["resource_id"]),
            "outcome": outcome,
            "process": {
                "terminal": True,
                "identity": "verified",
                "identity_verified": True,
                "create_time": 123.0,
                "root_worker": {"verified": True, "create_time": 124.0},
                "owned_children": [],
                "captures": {"stopped": True, "flushed": True},
            },
            "device": {
                "state": "idle" if device_idle else "busy",
                "idle": device_idle,
                "device_identity": self.devices.identity(task["resource_id"]),
                "input": {"released": device_idle},
                "screen": {"released": device_idle},
                "screen_state": "appropriate" if device_idle else "busy",
                "screen_appropriate": device_idle,
                "capture": {"stopped": device_idle, "flushed": device_idle},
            },
        }

    def _new_effect(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._job_counter += 1
        key = self._key(task)
        self.devices.activate(task["resource_id"], key)
        if key in self.fail_without_release:
            return {
                "status": "failed",
                "runner_outcome": "failed",
                "ok": False,
                "operation_id": task["operation_id"],
                "resource_id": task["resource_id"],
                "job_id": task["job_id"],
                "idempotency_key": key,
                "payload_hash": task["payload_hash"],
                "job_effect_id": f"fake-job-{self._job_counter}",
            }
        if key in self.cancelled:
            outcome = "cancelled"
            return {
                "status": outcome,
                "runner_outcome": "cancelled",
                "ok": True,
                "operation_id": task["operation_id"],
                "resource_id": task["resource_id"],
                "job_id": task["job_id"],
                "idempotency_key": key,
                "payload_hash": task["payload_hash"],
                "job_effect_id": f"fake-job-{self._job_counter}",
                "release_evidence": self._evidence(task, outcome),
            }
        return {
            "status": "succeeded",
            "runner_outcome": "succeeded",
            "ok": True,
            "operation_id": task["operation_id"],
            "resource_id": task["resource_id"],
            "job_id": task["job_id"],
            "idempotency_key": key,
            "payload_hash": task["payload_hash"],
            "job_effect_id": f"fake-job-{self._job_counter}",
            "release_evidence": self._evidence(task, "succeeded"),
        }

    def start(self, task: Mapping[str, Any]) -> dict[str, Any]:
        key = self._key(task)
        self.starts.append(dict(task))
        if key not in self.effects:
            self.effects[key] = self._new_effect(task)
        if self.block_start:
            self.start_entered.set()
            if not self.start_continue.wait(10):
                raise TimeoutError("fake start remained blocked")
        if key in self.lose_once:
            self.lose_once.remove(key)
            raise ConnectionError("fake response lost after effect")
        return dict(self.effects[key])

    def lookup(self, task: Mapping[str, Any]) -> dict[str, Any]:
        key = self._key(task)
        self.lookups.append(key)
        if key in self.unknown_lookup:
            return {"status": "unknown", "idempotency_key": key, "payload_hash": task["payload_hash"]}
        if key not in self.effects:
            status = "not_found" if key in self.absent_lookup else "unknown"
            return {
                "status": status, "idempotency_key": key, "operation_key": key,
                "payload_hash": task["payload_hash"],
                "can_retry_same_key": status == "not_found",
            }
        result = dict(self.effects[key])
        if key in self.release_override:
            result["release_evidence"] = self.release_override[key]
        elif key in self.release_ready and result.get("status") == "failed":
            result["release_evidence"] = self._evidence(task, "failed")
        if key in self.cancelled:
            # Keep the actual runner terminal outcome alongside the queue's
            # effective cancellation outcome; cancellation is a cleanup
            # decision and must not erase what the runner observed.
            actual = result.get("runner_outcome", result.get("status"))
            result.update(status="cancelled", ok=True, job_outcome=actual,
                          release_evidence={**self._evidence(task, "cancelled"), "job_outcome": actual})
        return result

    def request_cancel(self, task: Mapping[str, Any]) -> dict[str, Any]:
        key = self._key(task)
        self.cancel_requests.append(key)
        if key in self.cancel_unknown_once:
            self.cancel_unknown_once.remove(key)
            raise ConnectionError("fake cancel response lost")
        self.cancelled.add(key)
        self.devices.clear(task["resource_id"])
        return {"status": "accepted", "operation_id": task["operation_id"], "idempotency_key": key}

    def verify_release(self, task: Mapping[str, Any], receipt: Mapping[str, Any]) -> Mapping[str, Any]:
        self.verify_calls.append(self._key(task))
        if self._key(task) in self.verify_false:
            return {"ok": False}
        return {"ok": True, "status": "released", **dict(receipt)}


class QueueCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "queue.sqlite3"
        self.clock = FakeClock()
        self.processes = FakeProcessTable()
        self.processes.add("controller-a", 1001, 10.0)
        self.processes.add("controller-b", 1002, 20.0)
        self.devices = FakeDevices()
        self.devices.add("phone-main", "adb:phone-main")
        self.devices.add("gpu-main", "gpu:main")
        self.runner = FakeRunner(self.clock, self.devices)
        self.queues: list[rq.Queue] = []
        self.controllers: list[rq.Controller] = []

    def tearDown(self) -> None:
        for queue in self.queues:
            close = getattr(queue, "close", None)
            if close:
                close()
        self.temp.cleanup()

    def new_queue(self, path: Path | None = None) -> rq.Queue:
        queue = rq.Queue(str(path or self.path), now=self.clock)
        queue.register_resource(
            "phone-main", "phone", "thread-phone-main",
            initial_attestation={"resource_id": "phone-main", "kind": "phone", "state": "idle", "idle": True,
                                 "device_identity": "adb:phone-main"},
        )
        queue.register_resource(
            "gpu-main", "gpu", None,
            initial_attestation={"resource_id": "gpu-main", "kind": "gpu", "state": "idle", "idle": True,
                                 "device_identity": "gpu:main"},
        )
        self.queues.append(queue)
        return queue

    def controller(self, queue: rq.Queue, owner: str = "controller-a") -> rq.Controller:
        controller = rq.Controller(queue, self.runner, lease_seconds=5, owner=owner)
        self.controllers.append(controller)
        return controller

    def enqueue(self, queue: rq.Queue, operation_id: str, resource_id: str = "phone-main", *, priority: int = 0,
                input_value: Any = None, procedure: Any = None, launch: Any = None,
                dependencies: list[str] | None = None, review_ref: str | None = None,
                reviewed: bool = True) -> dict[str, Any]:
        review_ref = review_ref or f"review:{operation_id}"
        value = queue.enqueue(
            operation_id, resource_id, "fake-project",
            input={"prompt": operation_id} if input_value is None else input_value,
            procedure={"name": "procedure-v1"} if procedure is None else procedure,
            launch={"command": ["fake-runner", operation_id]} if launch is None else launch,
            dependencies=dependencies, priority=priority, review_ref=review_ref,
        )
        if reviewed:
            queue.review(operation_id, value["input_hash"], value["procedure_hash"], review_ref)
            value = queue.task(operation_id) or value
        return value

    def task(self, queue: rq.Queue, operation_id: str) -> dict[str, Any]:
        value = queue.task(operation_id)
        self.assertIsNotNone(value)
        return value

    def resource(self, queue: rq.Queue, resource_id: str) -> dict[str, Any]:
        result = queue.status()
        self.assertIsNotNone(result)
        for item in result["resources"]:
            if item["resource_id"] == resource_id:
                return item
        self.fail(f"resource missing: {resource_id}")


class ResourceQueueAcceptanceTests(QueueCase):
    def test_two_controllers_one_resource_exactly_one_claim(self):
        first = self.new_queue()
        second = self.new_queue(self.path)
        self.enqueue(first, "op-a")
        self.enqueue(first, "op-b")
        claim_a = first._claim_next("controller-a", 5, owner_pid=1001, owner_create_time=10.0)
        claim_b = second._claim_next("controller-b", 5, owner_pid=1002, owner_create_time=20.0)
        self.assertIsNotNone(claim_a)
        self.assertIsNone(claim_b)
        resource = self.resource(first, "phone-main")
        self.assertEqual(resource["active_operation"], "op-a")
        self.assertEqual(resource["active_generation"], 1)
        self.assertEqual(self.task(first, "op-a")["owner"], "controller-a")
        self.assertEqual(self.task(first, "op-a")["owner_pid"], 1001)
        self.assertEqual(self.task(first, "op-a")["owner_create_time"], 10.0)

    def test_duplicate_operation_same_effect_changed_payload_rejected(self):
        queue = self.new_queue()
        first = self.enqueue(queue, "op-dup", input_value={"x": 1})
        second = self.enqueue(queue, "op-dup", input_value={"x": 1})
        self.assertEqual(first["operation_key"], second["operation_key"])
        self.assertEqual(first["job_id"], second["job_id"])
        with self.assertRaises(rq.OperationConflict):
            self.enqueue(queue, "op-dup", input_value={"x": 2})
        self.controller(queue).pump()
        self.assertEqual(len(self.runner.starts), 1)
        self.assertEqual(len(self.runner.effects), 1)

    def test_success_lost_response_reconciles_without_second_effect(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-lost")
        key = self.task(queue, "op-lost")["operation_key"]
        self.runner.lose_once.add(key)
        self.controller(queue).pump()
        self.assertEqual(self.task(queue, "op-lost")["state"], "running")
        self.assertEqual(len(self.runner.effects), 1)
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-lost")["state"], "terminal")
        self.assertEqual(self.task(queue, "op-lost")["outcome"], "succeeded")
        self.assertEqual(len(self.runner.effects), 1)
        self.assertEqual(self.runner.lookups.count(key), 1)

    def test_expired_live_old_owner_cannot_reclaim(self):
        first = self.new_queue()
        second = self.new_queue(self.path)
        self.enqueue(first, "op-expire")
        first._claim_next("controller-a", 5, owner_pid=1001, owner_create_time=10.0)
        self.clock.advance(10_000)
        self.enqueue(first, "op-next")
        self.assertIsNone(second._claim_next("controller-b", 5))
        self.assertEqual(self.resource(second, "phone-main")["active_operation"], "op-expire")
        self.assertEqual(self.resource(second, "phone-main")["owner"], "controller-a")
        self.assertEqual(self.resource(second, "phone-main")["state"], "busy")
        self.assertEqual(self.processes.identity(1001, 10.0), "alive")
        self.processes.stop("controller-a")
        self.processes.reuse_pid("controller-a", 999.0)
        self.assertEqual(self.processes.identity(1001, 10.0), "pid_reused")
        self.assertIsNone(second._claim_next("controller-b", 5))

    def test_heartbeat_requires_owner_generation_and_persists(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-heartbeat")
        task = queue._claim_next("controller-a", 5, owner_pid=1001, owner_create_time=10.0)
        self.assertIsNotNone(task)
        before = self.task(queue, "op-heartbeat")
        self.assertIsNone(queue.heartbeat("op-heartbeat", "controller-b", task["generation"]))
        self.assertEqual(self.task(queue, "op-heartbeat")["lease_until"], before["lease_until"])
        self.clock.advance(2)
        renewed = queue.heartbeat("op-heartbeat", "controller-a", task["generation"])
        self.assertIsNotNone(renewed)
        self.assertGreater(renewed["lease_until"], before["lease_until"])
        self.assertEqual(renewed["last_heartbeat_at"], self.clock())

    def test_blocked_head_is_skipped_and_ready_later_job_runs(self):
        queue = self.new_queue()
        self.enqueue(queue, "blocked", review_ref="review:block", reviewed=False)
        self.enqueue(queue, "ready")
        claimed = queue._claim_next("controller-a", 5)
        self.assertIsNotNone(claimed)
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "ready")
        self.assertEqual(self.task(queue, "blocked")["blocker_code"], "review_required")

    def test_priority_then_fifo_ordering(self):
        queue = self.new_queue()
        self.enqueue(queue, "low", priority=1)
        self.enqueue(queue, "high-a", priority=9)
        self.enqueue(queue, "high-b", priority=9)
        self.controller(queue).pump()
        self.assertEqual([item["operation_id"] for item in self.runner.starts], ["high-a", "high-b", "low"])

    def test_queued_priority_update_affects_next_claim_and_running_is_not_preempted(self):
        queue = self.new_queue()
        self.enqueue(queue, "first", priority=0)
        self.enqueue(queue, "second", priority=0)
        queue.set_priority("second", 10)
        claimed = queue._claim_next("controller-a", 5)
        self.assertEqual(claimed["operation_id"], "second")
        with self.assertRaises(rq.QueueError):
            queue.set_priority("second", 100)
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "second")

    def test_readiness_review_and_dependencies_gate_claim(self):
        queue = self.new_queue()
        predecessor = self.enqueue(queue, "predecessor", review_ref="review:pre", reviewed=False)
        self.enqueue(queue, "dependent", dependencies=["predecessor"])
        with self.assertRaises(rq.OperationConflict):
            queue.review("predecessor", "0" * 64, predecessor["procedure_hash"], "review:pre")
        queue.review("predecessor", predecessor["input_hash"], predecessor["procedure_hash"], "review:pre")
        claimed = queue._claim_next("controller-a", 5)
        self.assertEqual(claimed["operation_id"], "predecessor")
        self.assertEqual(self.task(queue, "dependent")["blocker_code"], "dependency_pending")
        self.assertIsNone(queue._claim_next("controller-b", 5))

    def test_cancel_holds_lease_until_owned_worker_release(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-cancel")
        key = self.task(queue, "op-cancel")["operation_key"]
        task = self.task(queue, "op-cancel")
        self.runner.effects[key] = {
            "status": "running", "operation_id": "op-cancel", "resource_id": "phone-main",
            "job_id": task["job_id"], "idempotency_key": key, "payload_hash": task["payload_hash"],
        }
        self.controller(queue).pump()
        queue.cancel("op-cancel", "user-request")
        self.assertTrue(self.task(queue, "op-cancel")["cancel_requested"])
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "op-cancel")
        self.controller(queue).reconcile()
        self.assertIn(key, self.runner.cancel_requests)
        # Request acceptance is not terminal completion; the owned worker
        # must be observed after its cooperative cleanup has finished.
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-cancel")["state"], "terminal")
        self.assertEqual(self.task(queue, "op-cancel")["outcome"], "cancelled")
        self.assertEqual(self.resource(queue, "phone-main")["state"], "idle")

    def test_cancel_during_blocking_start_cannot_finalize_stale_success(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-cancel-race")
        self.runner.block_start = True
        controller = self.controller(queue)
        errors: list[BaseException] = []

        def pump() -> None:
            try:
                controller.pump()
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)

        thread = threading.Thread(target=pump, daemon=True)
        thread.start()
        self.assertTrue(self.runner.start_entered.wait(5))
        queue.cancel("op-cancel-race", "user-request")
        self.assertTrue(self.task(queue, "op-cancel-race")["cancel_requested"])
        self.runner.start_continue.set()
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        # The stale start snapshot cannot terminalize a cancellation request
        # as succeeded; the lease remains until cooperative cleanup is seen.
        current = self.task(queue, "op-cancel-race")
        self.assertNotEqual(current["outcome"], "succeeded")
        self.assertTrue(current["cancel_requested"] or current["state"] == "terminal")
        controller.reconcile()
        controller.reconcile()
        self.assertEqual(self.task(queue, "op-cancel-race")["outcome"], "cancelled")
        release_receipts = [item for item in queue.receipts("op-cancel-race") if item["kind"] == "release"]
        self.assertTrue(release_receipts)
        self.assertIn("succeeded", json.dumps(release_receipts[-1]["data"], sort_keys=True))

    def test_device_attestation_cannot_change_while_queued_or_active(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-attestation")
        with self.assertRaises((rq.OperationConflict, ValueError, rq.QueueError)):
            queue.register_resource(
                "phone-main", "phone", "thread-phone-main",
                initial_attestation={"resource_id": "phone-main", "kind": "phone", "state": "idle", "idle": True,
                                     "device_identity": "adb:replacement"},
            )
        self.assertEqual(self.resource(queue, "phone-main")["attestation"]["device_identity"], "adb:phone-main")
        queue._claim_next("controller-a", 5)
        with self.assertRaises((rq.OperationConflict, ValueError, rq.QueueError)):
            queue.register_resource(
                "phone-main", "phone", "thread-phone-main",
                initial_attestation={"resource_id": "phone-main", "kind": "phone", "state": "idle", "idle": True,
                                     "device_identity": "adb:replacement"},
            )

    def test_same_dedicated_thread_cannot_be_registered_to_phone_and_gpu(self):
        queue = self.new_queue()
        with self.assertRaises((rq.OperationConflict, ValueError, rq.QueueError)):
            queue.register_resource(
                "gpu-shared-thread", "gpu", "thread-phone-main",
                initial_attestation={"resource_id": "gpu-shared-thread", "kind": "gpu", "state": "idle", "idle": True,
                                     "device_identity": "gpu:shared"},
            )

    def test_lookup_and_verifier_recheck_owner_generation_before_io(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-lookup-fence")
        task = queue._claim_next("controller-a", 5)
        self.assertIsNotNone(task)
        self.runner.effects[task["operation_key"]] = {
            "status": "running", "operation_id": task["operation_id"], "resource_id": task["resource_id"],
            "job_id": task["job_id"], "idempotency_key": task["operation_key"], "payload_hash": task["payload_hash"],
        }
        conn = queue._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET owner='controller-b',generation=generation+1 WHERE operation_id=?", ("op-lookup-fence",))
            conn.execute("UPDATE leases SET owner='controller-b',generation=generation+1 WHERE operation_id=?", ("op-lookup-fence",))
            conn.commit()
        finally:
            conn.close()
        controller = self.controller(queue, "controller-a")
        # Simulate a controller holding an older active snapshot across a
        # restart/lease replacement. The live ledger now names B, but this
        # controller must fence its stale A snapshot before runner I/O.
        with mock.patch.object(queue, "_active_tasks", return_value=[task]):
            controller.reconcile()
        self.assertEqual(self.runner.lookups, [])
        # A stale terminal snapshot must not reach the release verifier.
        stale = dict(task)
        response = dict(self.runner.effects[task["operation_key"]])
        response.update(status="succeeded", release_evidence=self.runner._evidence(task, "succeeded"))
        controller._observe(stale, response, "lookup")
        self.assertEqual(self.runner.verify_calls, [])

    def test_failed_operation_needs_valid_release_before_next(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-fail")
        self.enqueue(queue, "op-next")
        key = self.task(queue, "op-fail")["operation_key"]
        self.runner.fail_without_release.add(key)
        self.controller(queue).pump()
        self.assertEqual(self.task(queue, "op-fail")["state"], "running")
        task_generation = self.task(queue, "op-fail")["generation"]
        self.runner.release_ready.add(key)
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-fail")["outcome"], "failed")
        self.assertEqual(self.resource(queue, "phone-main")["state"], "idle")
        released = self.task(queue, "op-fail")
        self.assertEqual(released["claim_generation"], task_generation)
        self.assertEqual(self.resource(queue, "phone-main")["last_receipt"]["kind"], "release")
        self.controller(queue).pump()
        self.assertEqual(self.task(queue, "op-next")["outcome"], "succeeded")

    def test_release_receipt_is_gate_for_terminal_and_next_claim(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-gate")
        key = self.task(queue, "op-gate")["operation_key"]
        self.runner.fail_without_release.add(key)
        self.controller(queue).pump()
        self.assertEqual(self.task(queue, "op-gate")["state"], "running")
        self.assertIn(self.resource(queue, "phone-main")["state"], {"busy", "blocked"})
        self.assertFalse(any(item["kind"] == "release" for item in queue.receipts("op-gate")))
        bad = self.runner._evidence(self.task(queue, "op-gate"), "failed", device_idle=False)
        self.runner.release_override[key] = bad
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-gate")["state"], "running")
        # A failed proof may quarantine the resource as blocked; either way
        # the active operation must remain fenced and unreleased.
        self.assertIn(self.resource(queue, "phone-main")["state"], {"busy", "blocked"})
        self.runner.release_override[key] = self.runner._evidence(self.task(queue, "op-gate"), "failed")
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-gate")["state"], "terminal")
        self.assertEqual(self.resource(queue, "phone-main")["state"], "idle")

    def test_restart_recovers_intent_without_second_effect(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-restart")
        key = self.task(queue, "op-restart")["operation_key"]
        self.runner.lose_once.add(key)
        self.controller(queue).pump()
        self.assertEqual(len(self.runner.effects), 1)
        queue2 = self.new_queue(self.path)
        self.controller(queue2).reconcile()
        self.assertEqual(self.task(queue2, "op-restart")["outcome"], "succeeded")
        self.assertEqual(len(self.runner.effects), 1)
        self.assertEqual(self.task(queue2, "op-restart")["operation_key"], key)

    def test_empty_queue_never_starts_model(self):
        queue = self.new_queue()
        result = self.controller(queue).pump()
        self.assertEqual(self.runner.starts, [])
        self.assertEqual(result["actions"], [])

    def test_dedicated_thread_resumes_sequentially_with_distinct_jobs(self):
        queue = self.new_queue()
        self.enqueue(queue, "first")
        self.controller(queue).pump()
        self.assertEqual(self.task(queue, "first")["state"], "terminal")
        self.devices.clear("phone-main")
        self.enqueue(queue, "second")
        self.controller(queue).pump()
        self.assertEqual(len(self.runner.starts), 2)
        self.assertEqual(self.runner.starts[0]["dedicated_thread_id"], "thread-phone-main")
        self.assertEqual(self.runner.starts[1]["dedicated_thread_id"], "thread-phone-main")
        self.assertNotEqual(self.runner.starts[0]["job_id"], self.runner.starts[1]["job_id"])

    def test_phone_and_gpu_claim_in_parallel(self):
        queue = self.new_queue()
        self.enqueue(queue, "phone-op", "phone-main")
        self.enqueue(queue, "gpu-op", "gpu-main")
        first = queue._claim_next("controller-a", 5)
        second = queue._claim_next("controller-a", 5)
        self.assertEqual(first["resource_id"], "phone-main")
        self.assertEqual(second["resource_id"], "gpu-main")
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "phone-op")
        self.assertEqual(self.resource(queue, "gpu-main")["active_operation"], "gpu-op")

    def test_negative_receipts_generation_owner_thread_job_payload_device(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-negative")
        key = self.task(queue, "op-negative")["operation_key"]
        task = self.task(queue, "op-negative")
        self.runner.effects[key] = {
            "status": "succeeded", "operation_id": "op-negative", "resource_id": "phone-main",
            "job_id": "wrong-job", "idempotency_key": key, "payload_hash": "0" * 64,
            "release_evidence": {"operation_id": "wrong-op", "resource_id": "phone-main", "job_id": "wrong-job",
                                  "thread_id": "wrong-thread", "owner": "controller-b", "generation": 99,
                                  "payload_hash": "0" * 64},
        }
        self.controller(queue).pump()
        self.assertEqual(self.task(queue, "op-negative")["state"], "running")
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "op-negative")
        self.assertEqual(task["payload_hash"], self.task(queue, "op-negative")["payload_hash"])

    def test_attempt_receipts_append_only_and_release_rollback_keeps_lease(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-receipts")
        self.controller(queue).pump()
        conn = queue._connect()
        try:
            before = conn.execute("SELECT COUNT(*) FROM receipts WHERE operation_id=?", ("op-receipts",)).fetchone()[0]
            with self.assertRaises(sqlite3.Error):
                conn.execute("UPDATE receipts SET kind='tampered' WHERE operation_id=?", ("op-receipts",))
            after = conn.execute("SELECT COUNT(*) FROM receipts WHERE operation_id=?", ("op-receipts",)).fetchone()[0]
            self.assertEqual(before, after)
        finally:
            conn.close()

        original = queue._insert_receipt_tx

        def fail_after_release(conn: sqlite3.Connection, operation_id: str, kind: str, stamp: float, **kwargs: Any) -> Any:
            value = original(conn, operation_id, kind, stamp, **kwargs)
            if kind == "release":
                raise RuntimeError("receipt-terminal fault")
            return value

        queue._insert_receipt_tx = fail_after_release  # type: ignore[method-assign]
        self.enqueue(queue, "op-rollback")
        key = self.task(queue, "op-rollback")["operation_key"]
        self.runner.fail_without_release.add(key)
        self.runner.release_ready.add(key)
        self.controller(queue).pump()
        with self.assertRaises(RuntimeError):
            self.controller(queue).reconcile()
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "op-rollback")

    def test_unknown_dispatch_cancellation_retains_lease(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-unknown-cancel")
        key = self.task(queue, "op-unknown-cancel")["operation_key"]
        self.runner.lose_once.add(key)
        self.runner.unknown_lookup.add(key)
        self.controller(queue).pump()
        queue.cancel("op-unknown-cancel", "user-request")
        self.controller(queue).reconcile()
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "op-unknown-cancel")
        self.assertNotEqual(self.resource(queue, "phone-main")["state"], "idle")

    def test_release_verifier_false_keeps_lease_even_with_complete_raw_proof(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-verify-false")
        key = self.task(queue, "op-verify-false")["operation_key"]
        self.runner.fail_without_release.add(key)
        self.runner.release_ready.add(key)
        self.runner.verify_false.add(key)
        self.controller(queue).pump()
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-verify-false")["state"], "running")
        self.assertEqual(self.resource(queue, "phone-main")["active_operation"], "op-verify-false")

    def test_intent_before_start_then_absent_lookup_retries_same_key(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-crash-before-start")
        key = self.task(queue, "op-crash-before-start")["operation_key"]
        # Commit the intent/lease, then simulate a process crash before the
        # runner call. The first lookup is indeterminate; a later authoritative
        # not-found permits the same key to be retried, never a new key.
        self.assertIsNotNone(queue._claim_next("controller-a", 5))
        self.runner.unknown_lookup.add(key)
        self.controller(queue).reconcile()
        self.assertEqual(len(self.runner.starts), 0)
        self.assertEqual(self.task(queue, "op-crash-before-start")["state"], "running")
        self.runner.unknown_lookup.remove(key)
        self.runner.absent_lookup.add(key)
        self.controller(queue).pump()
        self.assertEqual(len(self.runner.starts), 1)
        self.runner.absent_lookup.remove(key)
        self.controller(queue).pump()
        self.assertEqual(len(self.runner.effects), 1)
        self.assertTrue(all(item["operation_key"] == key for item in self.runner.starts))

    def test_cancel_request_unknown_is_retried_on_later_reconcile(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-cancel-retry")
        key = self.task(queue, "op-cancel-retry")["operation_key"]
        task = self.task(queue, "op-cancel-retry")
        self.runner.effects[key] = {
            "status": "running", "operation_id": "op-cancel-retry", "resource_id": "phone-main",
            "job_id": task["job_id"], "idempotency_key": key, "payload_hash": task["payload_hash"],
        }
        self.controller(queue).pump()
        queue.cancel("op-cancel-retry", "user-request")
        self.runner.cancel_unknown_once.add(key)
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-cancel-retry")["state"], "running")
        self.controller(queue).reconcile()
        self.assertGreaterEqual(self.runner.cancel_requests.count(key), 2)
        self.controller(queue).reconcile()
        self.assertEqual(self.task(queue, "op-cancel-retry")["outcome"], "cancelled")

    def test_stale_controller_snapshot_cannot_start_or_cancel_after_fence_change(self):
        queue = self.new_queue()
        self.enqueue(queue, "op-stale")
        task = queue._claim_next("controller-a", 5)
        self.assertIsNotNone(task)
        # Simulate a newer generation replacing the old owner before the old
        # controller reaches its runner call.
        conn = queue._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("UPDATE tasks SET owner='controller-b',generation=generation+1 WHERE operation_id=?", ("op-stale",))
            conn.execute("UPDATE leases SET owner='controller-b',generation=generation+1 WHERE operation_id=?", ("op-stale",))
            conn.commit()
        finally:
            conn.close()
        controller = self.controller(queue, "controller-a")
        result = controller._dispatch_claimed(task)
        self.assertIn(result["result"], {"stale", "unknown"})
        self.assertEqual(self.runner.starts, [])


if __name__ == "__main__":
    unittest.main()
