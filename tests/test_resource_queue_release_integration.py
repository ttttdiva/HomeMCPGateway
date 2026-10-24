"""Offline queue/GatewayRunner release regression coverage.

These tests use a temporary resource queue and a tiny keyed jobs seam.  The
seam records the request fingerprint passed to ``job_start`` as job metadata,
but never starts a process.  CLI release calls use the real ``GatewayRunner``
and only patch the module-level status/identity probes to the same fake rows.
"""
from __future__ import annotations

from contextlib import contextmanager
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from home_mcp_gateway import jobs
from home_mcp_gateway import resource_queue as rq
from home_mcp_gateway import resource_queue_cli as cli
from home_mcp_gateway import resource_queue_gateway as gateway


THREAD_ID = "99999999-9999-4999-8999-999999999999"
DEVICE_IDENTITY = "adb:offline-phone"
CODEX_EXE = r"C:\offline\codex.exe"


class RecordingJobs:
    """Keyed jobs seam that stores the exact launch metadata it receives."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.rows: dict[str, dict[str, object]] = {}
        self._next_pid = 3100

    def job_start(
        self,
        command: str | list[str],
        *,
        cwd: str,
        env: dict[str, str],
        runtime_dir: str | None,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> dict[str, object]:
        self.calls.append({
            "command": copy.deepcopy(command),
            "cwd": cwd,
            "env": dict(env),
            "runtime_dir": runtime_dir,
            "idempotency_key": idempotency_key,
            "request_fingerprint": request_fingerprint,
        })
        job_id = jobs.stable_job_id(idempotency_key)
        self._next_pid += 1
        pid = self._next_pid
        row: dict[str, object] = {
            "job_id": job_id,
            "idempotency_key": idempotency_key,
            "command": copy.deepcopy(command),
            "cwd": cwd,
            "env": dict(env),
            "explicit_env_digest": jobs._explicit_env_digest(env),
            # This is the exact B value received from GatewayRunner.start.
            "dispatcher_request_fingerprint": request_fingerprint,
            # jobs.py stores its internal keyed digest separately.
            "request_fingerprint": jobs._keyed_fingerprint(
                command, cwd, env, request_fingerprint, None,
            ),
            "status": "exited",
            "exit_code": 0,
            "pid": pid,
            "process_create_time": float(pid) / 100.0,
            "worker_pid": pid + 1,
            "worker_create_time": float(pid + 1) / 100.0,
            "thread_id": THREAD_ID,
        }
        self.rows[job_id] = copy.deepcopy(row)
        return copy.deepcopy(row)

    def job_status(self, job_id: str, runtime_dir: str | None = None) -> dict[str, object]:
        del runtime_dir
        try:
            return copy.deepcopy(self.rows[job_id])
        except KeyError as exc:
            raise FileNotFoundError(job_id) from exc

    def identity(self, pid: int, create_time: float) -> str:
        del pid, create_time
        return "missing"

    def _keyed_fingerprint(
        self,
        command: str | list[str],
        cwd: str,
        env: dict[str, str],
        request_fingerprint: str,
        dispatcher_binding: dict[str, object] | None,
    ) -> str:
        return jobs._keyed_fingerprint(command, cwd, env, request_fingerprint, dispatcher_binding)


class OfflineHarness:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.queue_dir = self.root / "queue"
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir()
        self.now = lambda: 1_900_000_000.0
        self.jobs = RecordingJobs()
        self.runner = gateway.GatewayRunner(str(self.runtime_dir), jobs_api=self.jobs, clock=self.now)
        self.queue = rq.Queue(str(self.queue_dir), now=self.now)
        self.queue.register_resource(
            "phone-main", "phone", THREAD_ID,
            initial_attestation={
                "resource_id": "phone-main",
                "kind": "phone",
                "state": "idle",
                "idle": True,
                "device_identity": DEVICE_IDENTITY,
            },
        )

    def close(self) -> None:
        self.temp.cleanup()

    def operation(self, operation_id: str) -> tuple[gateway.PreparedLaunch, dict[str, object]]:
        op_key = "rq1:" + hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
        prepared = self.runner.prepare_codex_resume(
            op_key,
            CODEX_EXE,
            THREAD_ID,
            f"offline resume prompt for {operation_id}",
            str(self.root),
            # Deliberately omit dispatcher_request_fingerprint.  The queue
            # owns the eventual B value after it hashes this launch payload.
            receipt_path=str(self.root / f"{operation_id}.receipt.json"),
            task_id=operation_id,
        )
        projection = self.queue.enqueue(
            operation_id,
            "phone-main",
            "offline-regression",
            input={"operation_id": operation_id},
            procedure={"name": "codex-resume"},
            launch=prepared.as_dict(),
            review_ref=f"review:{operation_id}",
        )
        self.queue.review(
            operation_id,
            projection["input_hash"],
            projection["procedure_hash"],
            f"review:{operation_id}",
        )
        return prepared, projection

    def receipt(self, task: dict[str, object], prepared: gateway.PreparedLaunch) -> dict[str, object]:
        receipt: dict[str, object] = {
            "operation_id": task["operation_id"],
            "resource_id": task["resource_id"],
            "job_id": task["job_id"],
            "op_key": task["operation_key"],
            "thread_id": task["dedicated_thread_id"],
            "owner": task["owner"],
            "generation": task["generation"],
            "payload_hash": task["payload_hash"],
            "device_identity": task["device_identity"],
            "receipt_state": "released",
            "owned_identities": [],
            "capture_identities": [],
            "stdout_jsonl": "\n".join((
                json.dumps({"type": "thread.started", "thread_id": THREAD_ID}),
                json.dumps({
                    "type": "item.completed",
                    "item": {"id": "message-final", "type": "agent_message", "text": "offline result"},
                }),
                json.dumps({"type": "turn.completed"}),
            )) + "\n",
            "device": {
                "kind": "phone",
                "state": "idle",
                "device_idle": True,
                "phone_input_stopped": True,
                "screen_state": "home",
                "screen_appropriate": True,
                "capture_stopped": True,
                "capture_flushed": True,
            },
        }
        Path(prepared.receipt_path or "").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    @contextmanager
    def cli_jobs(self):
        """Route real CLI GatewayRunner probes to this fake keyed ledger."""
        with mock.patch.object(gateway.jobs, "job_status", self.jobs.job_status), \
             mock.patch.object(gateway.jobs, "identity", self.jobs.identity):
            yield


class ResourceQueueReleaseIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.harness = OfflineHarness()

    def tearDown(self) -> None:
        self.harness.close()

    def interrupted_operation(self, name):
        prepared, _ = self.harness.operation(name)
        rq.Controller(self.harness.queue, self.harness.runner, owner="recovery-test").pump()
        self.harness.queue.cancel(name, "external actor stop")
        task = self.harness.queue.task(name)
        receipt = self.harness.receipt(task, prepared)
        receipt.pop("stdout_jsonl")
        raw = (json.dumps({"type": "thread.started", "thread_id": THREAD_ID}) + "\n").encode()
        path = self.harness.root / (name + ".stdout.jsonl")
        path.write_bytes(raw)
        row = self.harness.jobs.rows[task["job_id"]]
        row.update(status="stopped", exit_code=15, stdout_path=str(path),
                   stop_result={"affected_pids": [row["pid"]], "remaining_pids": [], "errors": []})
        receipt["interrupted_recovery"] = {"requested_by": "coordinator", "reason": "external stop",
                                           "stdout_sha256": hashlib.sha256(raw).hexdigest()}
        return task, receipt, row, path

    def test_interrupted_recovery_releases_cancelled_without_fabricated_terminal(self):
        task, receipt, row, path = self.interrupted_operation("interrupted-live-contract")
        before = path.read_bytes()
        with self.harness.cli_jobs():
            result = cli._record_release(self.harness.queue, str(self.harness.runtime_dir),
                                         task["operation_id"], receipt, "cancelled")
        self.assertEqual(result["state"], "terminal")
        self.assertEqual(result["outcome"], "cancelled")
        self.assertEqual(self.harness.queue.status(resource_id="phone-main")["state"], "idle")
        evidence = result["last_receipt"]["data"]["evidence"]
        self.assertEqual(evidence["job_outcome"], "failed")
        self.assertFalse(evidence["interrupted_recovery"]["original_success"])
        self.assertEqual(evidence["interrupted_recovery"]["parser_result"]["code"], "terminal_record_invalid")
        self.assertEqual(path.read_bytes(), before)

    def test_interrupted_recovery_fails_closed_on_missing_or_changed_proof(self):
        for case in ("no_cancel", "zero_exit", "not_stopped", "remaining", "stop_errors", "wrong_root",
                     "wrong_thread", "wrong_digest", "inline", "terminal", "alive_capture", "device", "binding"):
            with self.subTest(case=case):
                # A fresh harness prevents one intentionally retained lease affecting another case.
                self.harness.close()
                self.harness = OfflineHarness()
                task, receipt, row, path = self.interrupted_operation("reject-" + case)
                if case == "no_cancel": task["cancel_requested"] = False
                elif case == "zero_exit": row["exit_code"] = 0
                elif case == "not_stopped": row["status"] = "exited"
                elif case == "remaining": row["stop_result"]["remaining_pids"] = [9999]
                elif case == "stop_errors": row["stop_result"]["errors"] = ["denied"]
                elif case == "wrong_root": row["stop_result"]["affected_pids"] = [9999]
                elif case == "wrong_digest": receipt["interrupted_recovery"]["stdout_sha256"] = "0" * 64
                elif case == "inline": receipt["stdout_jsonl"] = "forged"
                elif case == "alive_capture":
                    receipt["capture_identities"] = [{"pid": 8888, "create_time": 88.0}]
                    self.harness.jobs.identity = lambda pid, created: "alive" if pid == 8888 else "missing"
                elif case == "device": receipt["device"]["phone_input_stopped"] = False
                elif case == "binding": receipt["generation"] += 1
                elif case in {"wrong_thread", "terminal"}:
                    raw = path.read_text()
                    raw = raw.replace(THREAD_ID, "88888888-8888-4888-8888-888888888888") if case == "wrong_thread" else raw + '{"type":"turn.completed"}\n'
                    path.write_text(raw)
                    receipt["interrupted_recovery"]["stdout_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                result = self.harness.runner.verify_release(task, receipt)
                self.assertFalse(result["ok"], result)
                self.assertNotEqual(self.harness.queue.status(resource_id="phone-main")["state"], "idle")
                self.assertEqual(self.harness.queue.task(task["operation_id"])["owner"], task["owner"])

    def test_queue_owned_fingerprint_is_used_for_raw_flattened_and_start_paths(self):
        prepared, projection = self.harness.operation("fingerprint-main")
        task = self.harness.queue.task("fingerprint-main")
        self.assertIsNotNone(task)
        assert task is not None
        a = prepared.dispatcher_request_fingerprint
        b = task["payload_hash"]
        self.assertNotEqual(a, b)
        self.assertEqual(task["launch"]["dispatcher_request_fingerprint"], a)

        before_payload = copy.deepcopy(task["payload"])
        raw_launch = self.harness.runner._launch_for_task(task)
        flattened = rq.Controller._runner_task(task)
        flattened_launch = self.harness.runner._launch_for_task(flattened)
        self.assertIsInstance(raw_launch, gateway.PreparedLaunch)
        self.assertEqual(raw_launch, flattened_launch)
        self.assertEqual(raw_launch.dispatcher_request_fingerprint, b)
        self.assertEqual(flattened_launch.dispatcher_request_fingerprint, b)
        self.assertEqual(task["payload"], before_payload)
        self.assertEqual(task["launch"], before_payload)
        self.assertEqual(projection["payload_hash"], b)

        rq.Controller(
            self.harness.queue,
            self.harness.runner,
            owner="offline-controller",
            lease_seconds=60,
        ).pump()
        self.assertEqual(len(self.harness.jobs.calls), 1)
        self.assertEqual(self.harness.jobs.calls[0]["request_fingerprint"], b)
        job_id = str(task["job_id"])
        self.assertEqual(self.harness.jobs.rows[job_id]["dispatcher_request_fingerprint"], b)
        self.assertEqual(self.harness.queue.task("fingerprint-main")["payload"], before_payload)

    def test_real_cli_release_frees_lease_and_next_ready_operation_dispatches(self):
        first, _ = self.harness.operation("release-first")
        second, _ = self.harness.operation("release-second")
        controller = rq.Controller(
            self.harness.queue,
            self.harness.runner,
            owner="offline-controller",
            lease_seconds=60,
        )
        # The first operation is the only dispatchable row while the resource
        # is idle.  The missing receipt leaves its lease fenced and the second
        # operation ready for the later pump.
        controller.pump()
        first_task = self.harness.queue.task("release-first")
        second_task = self.harness.queue.task("release-second")
        self.assertIsNotNone(first_task)
        self.assertIsNotNone(second_task)
        assert first_task is not None and second_task is not None
        self.assertEqual(first_task["state"], "running")
        self.assertEqual(second_task["state"], "ready")
        self.assertEqual(self.harness.jobs.calls[0]["request_fingerprint"], first_task["payload_hash"])

        receipt = self.harness.receipt(first_task, first)
        original_owner = first_task["owner"]
        original_generation = first_task["generation"]
        raw_verified = self.harness.runner.verify_release(first_task, receipt)
        flattened_verified = self.harness.runner.verify_release(
            rq.Controller._runner_task(first_task),
            receipt,
        )
        self.assertTrue(raw_verified.get("ok"), raw_verified)
        self.assertEqual(raw_verified, flattened_verified)
        with self.harness.cli_jobs():
            released = cli._record_release(
                self.harness.queue,
                str(self.harness.runtime_dir),
                "release-first",
                receipt,
                "succeeded",
            )
        self.assertEqual(released["state"], "terminal")
        self.assertEqual(released["outcome"], "succeeded")
        release_receipt = [
            item for item in self.harness.queue.receipts("release-first")
            if item["kind"] == "release"
        ][-1]
        self.assertEqual(release_receipt["data"]["evidence"], raw_verified["release_evidence"])
        self.assertFalse(self.harness.queue._lease_matches("release-first", original_owner, original_generation))
        resource = self.harness.queue.status(resource_id="phone-main")
        self.assertEqual(resource["state"], "idle")
        self.assertIsNone(resource["active_operation"])
        self.assertIsNone(self.harness.queue.task("release-first")["owner"])

        # A subsequent controller pump may now claim the next ready operation;
        # its absent receipt intentionally keeps that new lease running.
        controller.pump()
        second_after = self.harness.queue.task("release-second")
        self.assertEqual(second_after["state"], "running")
        self.assertEqual(self.harness.queue.status(resource_id="phone-main")["active_operation"], "release-second")
        self.assertEqual(len(self.harness.jobs.calls), 2)
        self.assertEqual(self.harness.jobs.calls[1]["request_fingerprint"], second_after["payload_hash"])

    def test_raw_task_direct_start_passes_queue_owned_fingerprint(self):
        prepared, _ = self.harness.operation("raw-direct-start")
        task = self.harness.queue.task("raw-direct-start")
        self.assertIsNotNone(task)
        assert task is not None
        response = self.harness.runner.start(task)
        self.assertEqual(response["status"], "unknown")
        self.assertEqual(response["error"], "release_receipt_pending")
        self.assertEqual(self.harness.jobs.calls[0]["request_fingerprint"], task["payload_hash"])
        self.assertEqual(
            self.harness.jobs.rows[task["job_id"]]["dispatcher_request_fingerprint"],
            task["payload_hash"],
        )
        self.assertNotEqual(prepared.dispatcher_request_fingerprint, task["payload_hash"])

    def test_successful_release_pump_and_cli_paths_leave_same_idle_terminal_projection(self):
        pump_harness = OfflineHarness()
        cli_harness = OfflineHarness()
        try:
            pump_prepared, _ = pump_harness.operation("success-pump")
            pump_controller = rq.Controller(
                pump_harness.queue,
                pump_harness.runner,
                owner="offline-controller",
                lease_seconds=60,
            )
            pump_controller.pump()
            pump_task = pump_harness.queue.task("success-pump")
            self.assertIsNotNone(pump_task)
            assert pump_task is not None
            pump_harness.receipt(pump_task, pump_prepared)
            pump_result = pump_controller.pump()
            self.assertEqual(pump_result["actions"][-1]["result"], "terminal")
            pump_finished = pump_harness.queue.task("success-pump")
            pump_resource = pump_harness.queue.status(resource_id="phone-main")

            cli_prepared, _ = cli_harness.operation("success-cli")
            cli_controller = rq.Controller(
                cli_harness.queue,
                cli_harness.runner,
                owner="offline-controller",
                lease_seconds=60,
            )
            cli_controller.pump()
            cli_task = cli_harness.queue.task("success-cli")
            self.assertIsNotNone(cli_task)
            assert cli_task is not None
            cli_receipt = cli_harness.receipt(cli_task, cli_prepared)
            with cli_harness.cli_jobs():
                cli_finished = cli._record_release(
                    cli_harness.queue,
                    str(cli_harness.runtime_dir),
                    "success-cli",
                    cli_receipt,
                    "succeeded",
                )
            cli_resource = cli_harness.queue.status(resource_id="phone-main")

            self.assertEqual(pump_finished["state"], "terminal")
            self.assertEqual(pump_finished["outcome"], "succeeded")
            self.assertEqual(cli_finished["state"], "terminal")
            self.assertEqual(cli_finished["outcome"], "succeeded")
            self.assertEqual(pump_resource["state"], "idle")
            self.assertEqual(cli_resource["state"], "idle")
            self.assertIsNone(pump_resource["active_operation"])
            self.assertIsNone(cli_resource["active_operation"])
        finally:
            pump_harness.close()
            cli_harness.close()

    def test_tampered_job_dispatcher_fingerprint_blocks_pump_and_cli(self):
        for path in ("pump", "cli"):
            with self.subTest(path=path):
                harness = OfflineHarness()
                try:
                    prepared, _, task = self._running_in(harness, f"tampered-{path}")
                    expected = task["payload_hash"]
                    self.assertEqual(harness.jobs.calls[0]["request_fingerprint"], expected)
                    harness.jobs.rows[task["job_id"]]["dispatcher_request_fingerprint"] = prepared.dispatcher_request_fingerprint
                    receipt = harness.receipt(task, prepared)
                    observed = harness.runner.lookup(task)
                    self.assertFalse(observed.get("ok", False))
                    self.assertEqual(observed.get("error"), "job_metadata_mismatch")
                    if path == "pump":
                        action = rq.Controller(
                            harness.queue,
                            harness.runner,
                            owner="offline-controller",
                            lease_seconds=60,
                        ).pump()
                        self.assertEqual(action["actions"][-1]["action"], "lookup")
                        self.assertEqual(action["actions"][-1]["result"], "unknown")
                    else:
                        with harness.cli_jobs():
                            with self.assertRaises(rq.QueueError) as raised:
                                cli._record_release(
                                    harness.queue,
                                    str(harness.runtime_dir),
                                    task["operation_id"],
                                    receipt,
                                    "succeeded",
                                )
                        self.assertEqual(str(raised.exception), "job_metadata_mismatch")
                    current = harness.queue.task(task["operation_id"])
                    self.assertEqual(current["state"], "running")
                    self.assertEqual(current["owner"], task["owner"])
                    self.assertEqual(current["generation"], task["generation"])
                    self.assertEqual(harness.queue.status(resource_id="phone-main")["active_operation"], task["operation_id"])
                finally:
                    harness.close()

    def test_malformed_release_bindings_block_cli_and_pump_without_releasing_lease(self):
        cases: dict[str, tuple[str, object]] = {
            "payload_hash": ("receipt_binding_payload_hash_mismatch", "0" * 64),
            "generation": ("receipt_binding_generation_mismatch", 999),
            "operation_id": ("receipt_binding_operation_id_mismatch", "other-operation"),
            "job_id": ("receipt_binding_job_id_mismatch", "00000000-0000-4000-8000-000000000000"),
            "thread_id": ("receipt_binding_thread_id_mismatch", "88888888-8888-4888-8888-888888888888"),
            "owner": ("receipt_binding_owner_mismatch", "other-owner"),
            "op_key": ("receipt_binding_op_key_mismatch", "rq1:" + "0" * 64),
            "device_identity": ("receipt_binding_device_identity_mismatch", "adb:other-phone"),
        }
        for field, (expected_error, replacement) in cases.items():
            with self.subTest(field=field):
                for path in ("pump", "cli"):
                    with self.subTest(path=path):
                        harness = OfflineHarness()
                        try:
                            prepared, _, task = self._running_in(harness, f"bad-{path}-{field}")
                            receipt = harness.receipt(task, prepared)
                            receipt[field] = replacement
                            Path(prepared.receipt_path or "").write_text(json.dumps(receipt), encoding="utf-8")
                            observed = harness.runner.lookup(task)
                            self.assertEqual(observed.get("status"), "unknown")
                            self.assertEqual(observed.get("error"), expected_error)

                            if path == "pump":
                                action = rq.Controller(
                                    harness.queue,
                                    harness.runner,
                                    owner="offline-controller",
                                    lease_seconds=60,
                                ).pump()
                                self.assertTrue(action["actions"])
                                self.assertEqual(action["actions"][-1]["action"], "lookup")
                                self.assertEqual(action["actions"][-1]["result"], "unknown")
                            else:
                                with harness.cli_jobs():
                                    with self.assertRaises(rq.QueueError) as raised:
                                        cli._record_release(
                                            harness.queue,
                                            str(harness.runtime_dir),
                                            task["operation_id"],
                                            receipt,
                                            "succeeded",
                                        )
                                self.assertEqual(str(raised.exception), expected_error)
                            current = harness.queue.task(task["operation_id"])
                            self.assertEqual(current["state"], "running")
                            self.assertEqual(current["owner"], task["owner"])
                            self.assertEqual(current["generation"], task["generation"])
                            resource = harness.queue.status(resource_id="phone-main")
                            self.assertEqual(resource["active_operation"], task["operation_id"])
                            self.assertNotEqual(resource["state"], "idle")
                        finally:
                            harness.close()

    @staticmethod
    def _running_in(
        harness: OfflineHarness,
        operation_id: str,
    ) -> tuple[gateway.PreparedLaunch, dict[str, object], dict[str, object]]:
        prepared, projection = harness.operation(operation_id)
        rq.Controller(
            harness.queue,
            harness.runner,
            owner="offline-controller",
            lease_seconds=60,
        ).pump()
        task = harness.queue.task(operation_id)
        if task is None:
            raise AssertionError("controller did not claim operation")
        if task["state"] != "running":
            raise AssertionError(f"operation did not retain lease: {task}")
        return prepared, projection, task


if __name__ == "__main__":
    unittest.main()
