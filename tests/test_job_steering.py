"""Offline steering durability, admission and public-boundary tests."""
import concurrent.futures
import hashlib
import inspect
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import uuid

from home_mcp_gateway import job_steering as mailbox, jobs
from home_mcp_gateway.work_dispatcher_codex import APP_SERVER_TRANSPORT


class MailboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.job_id = str(uuid.uuid4())
        self.directory = self.root / "jobs" / self.job_id
        self.directory.mkdir(parents=True)

    def tearDown(self):
        self.temp.cleanup()

    def request(self, key="key", text="Please clarify this step"):
        return mailbox.make_request(self.job_id, "turn-1", text, key)

    def publish(self, request):
        return mailbox.publish_json(mailbox.request_path(self.directory, request["request_id"]), request)

    def test_complete_exclusive_publish_and_ignore_partial_temps(self):
        request = self.request()
        self.assertTrue(self.publish(request))
        changed = dict(request, text="overwritten")
        self.assertFalse(self.publish(changed))
        self.assertEqual(mailbox.read_request(self.directory, request["request_id"]), request)
        (mailbox.request_path(self.directory, request["request_id"]).parent / ".incomplete.tmp").write_text('{"text":')
        self.assertEqual(mailbox.enumerate_requests(self.directory), [request])

    def test_utf8_size_key_and_turn_boundaries(self):
        self.request(text="x" * (32 * 1024))
        self.request(text="界" * ((32 * 1024) // 3))
        for text in ("", "x" * (32 * 1024 + 1), "\ud800"):
            with self.assertRaises((ValueError, UnicodeError)):
                self.request(text=text)
        for key in ("", "k" * 256, "a\x00b", "\ud800"):
            with self.assertRaises((ValueError, UnicodeError)):
                self.request(key=key)
        with self.assertRaises(ValueError):
            mailbox.make_request(self.job_id, "", "text", "key")

    def test_durable_send_before_rpc_and_no_resend(self):
        request = self.request()
        self.publish(request)
        with self.assertRaises(ValueError):
            mailbox.write_state(self.directory, request, "sending")
        sent = mailbox.write_state(self.directory, request, "sending", connection_id="connection", rpc_id=5)
        self.assertEqual(mailbox.read_state(self.directory, request["request_id"]), sent)
        self.assertEqual(sent["client_user_message_id"], request["client_user_message_id"])
        with self.assertRaises(ValueError):
            mailbox.write_state(self.directory, request, "sending", connection_id="new-connection", rpc_id=6)
        accepted = mailbox.write_state(self.directory, request, "accepted_by_runtime")
        self.assertEqual(accepted["rpc_id"], 5)
        self.assertEqual(mailbox.write_state(self.directory, request, "accepted_by_runtime"), accepted)
        with self.assertRaises(ValueError):
            mailbox.write_state(self.directory, request, "rejected", reason="runtime_rejected")
        mailbox.record_native_item(self.directory, request, turn_id="turn-1", client_id=request["client_user_message_id"])
        self.assertTrue(mailbox.steer_evidence(self.directory)[0]["recorded"])
        with self.assertRaises(ValueError):
            mailbox.record_native_item(self.directory, request, turn_id="turn-1", client_id="different")

    def test_sending_has_no_timer_uncertainty_and_terminal_is_durable(self):
        request = self.request()
        state = mailbox.write_state(self.directory, request, "sending", connection_id="c", rpc_id=1)
        state["updated_at"] = state["sending_at"] = 1.0
        args = dict(closed=False, worker_alive=True, connection_id="c", connection_alive=True)
        self.assertEqual(mailbox.public_receipt(request, state, **args)["state"], "sending")
        self.assertEqual(mailbox.public_receipt(request, state, **{**args, "connection_alive": False})["state"], "delivery_uncertain")
        accepted = mailbox.write_state(self.directory, request, "accepted_by_runtime")
        self.assertEqual(mailbox.public_receipt(request, accepted, **{**args, "worker_alive": False})["state"], "accepted_by_runtime")
        self.assertFalse({"connection_id", "rpc_id", "client_user_message_id", "text"} & set(mailbox.public_receipt(request, state, **args)))

    def test_close_fence_is_exclusive_and_queued_rejected_by_worker_only(self):
        request = self.request()
        self.publish(request)
        first = mailbox.close_mailbox(self.directory, "stopping")
        self.assertEqual(mailbox.close_mailbox(self.directory, "job_finished"), first)
        self.assertTrue((self.directory / "mailbox.closed").is_file())
        self.assertIsNone(mailbox.read_state(self.directory, request["request_id"]))
        receipt = mailbox.public_receipt(request, None, closed=True, worker_alive=True, connection_id="c", connection_alive=True)
        self.assertEqual(receipt["state"], "not_delivered")
        mailbox.write_state(self.directory, request, "rejected", reason="stopping")
        self.assertEqual(mailbox.read_state(self.directory, request["request_id"])["reason"], "stopping")
        self.assertEqual(mailbox.steer_evidence(self.directory)[0]["state"], "rejected")

    def test_crossprocess_admission_cap_eight(self):
        script = """from pathlib import Path
import sys
from home_mcp_gateway import job_steering as m
d=Path(sys.argv[1]); n=sys.argv[2]
with m.admission_lock(d,timeout=10):
 if m.pending_count(d)<8:
  r=m.make_request(d.name,'turn-1','text',n)
  m.publish_json(m.request_path(d,r['request_id']),r)
"""
        kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
        def submit(index):
            return subprocess.run([sys.executable, "-c", script, str(self.directory), str(index)], capture_output=True, text=True, **kwargs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(submit, range(16)))
        self.assertTrue(all(result.returncode == 0 for result in results), [r.stderr for r in results])
        self.assertEqual(mailbox.pending_count(self.directory), 8)


class PublicSteerTests(MailboxTests):
    def setUp(self):
        super().setUp()
        self.binding = {"db_path": str(self.root / "db.sqlite"), "manifest_id": "manifest", "manifest_digest": "a" * 64,
                        "root_source_id": "source", "root_epoch": 1, "generation": 1, "job_id": "dispatcher-job",
                        "op_key": "op", "request_digest": "b" * 64, "gateway_job_id": self.job_id,
                        "request_fingerprint": "c" * 64}
        self.metadata = {"job_id": self.job_id, "status": "running", "pid": 111, "process_create_time": 1.0,
                         "worker_pid": 222, "worker_create_time": 2.0, "started_at": time.time(),
                         "transport": APP_SERVER_TRANSPORT, "dispatcher_binding": self.binding,
                         "app_server": {"input_text": "private initial input"},
                         "app_server_runtime": {"connection_id": "private-connection", "connection_state": "connected",
                                                "thread_id": "private-thread", "current_turn_id": "turn-1", "journal_path": "private-journal"}}
        self.save()
        self.env_patch = patch.dict(os.environ, {"HOME_MCP_RUNTIME_DIR": str(self.root)})
        self.env_patch.start()
        self.identity_patch = patch.object(jobs, "identity", return_value="alive")
        self.identity_patch.start()
        self.guard_patch = patch("home_mcp_gateway.work_dispatcher_codex.steer_guard", return_value="open")
        self.guard = self.guard_patch.start()

    def tearDown(self):
        self.guard_patch.stop()
        self.identity_patch.stop()
        self.env_patch.stop()
        super().tearDown()

    def save(self):
        jobs._save(self.directory / "metadata.json", self.metadata)

    def test_public_signatures_and_tool_registration(self):
        self.assertEqual(list(inspect.signature(jobs.job_steer).parameters), ["job_id", "expected_turn_id", "text", "idempotency_key"])
        self.assertEqual(list(inspect.signature(jobs.job_steer_status).parameters), ["job_id", "idempotency_key"])
        self.assertIn(jobs.job_steer, jobs.TOOLS)
        self.assertIn(jobs.job_steer_status, jobs.TOOLS)

    def test_idempotency_receipt_conflict_and_no_private_data(self):
        first = jobs.job_steer(self.job_id, "turn-1", "private steer text", "same")
        second = jobs.job_steer(self.job_id, "turn-1", "private steer text", "same")
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "queued")
        self.assertEqual(jobs.job_steer(self.job_id, "turn-1", "different", "same")["error"], "idempotency_conflict")
        self.assertEqual(jobs.job_steer_status(self.job_id, "same"), first)
        self.assertNotIn("private", json.dumps(first))
        self.assertFalse((self.directory / "steering" / "states").exists())

    def test_new_submits_validate_active_turn_and_receipts_validate_guard(self):
        self.assertEqual(jobs.job_steer(self.job_id, "old-turn", "text", "key")["error"], "stale_turn")
        self.metadata["app_server_runtime"]["current_turn_id"] = None
        self.save()
        self.assertEqual(jobs.job_steer(self.job_id, "turn-1", "text", "key")["error"], "no_active_turn")
        self.guard.return_value = "mismatch"
        self.assertEqual(jobs.job_steer_status(self.job_id, "key")["error"], "steer_identity_mismatch")
        self.guard.assert_called_with(self.binding, self.metadata, require_running=False)

    def test_exec_unsupported_metadata_identity_and_pending_bound(self):
        self.metadata.pop("transport")
        self.save()
        self.assertEqual(jobs.job_steer(self.job_id, "turn-1", "text", "key")["error"], "steer_unsupported")
        self.metadata["transport"] = APP_SERVER_TRANSPORT
        self.metadata["job_id"] = str(uuid.uuid4())
        self.save()
        self.assertEqual(jobs.job_steer_status(self.job_id, "key")["error"], "steer_identity_mismatch")
        self.metadata["job_id"] = self.job_id
        self.save()
        for index in range(8):
            self.assertEqual(jobs.job_steer(self.job_id, "turn-1", "text", str(index))["state"], "queued")
        self.assertEqual(jobs.job_steer(self.job_id, "turn-1", "text", "ninth")["error"], "steer_pending_limit")

    def test_published_close_race_rechecks_fence_without_writing_state(self):
        original = mailbox.publish_json
        def close_after_publish(path, value):
            result = original(path, value)
            # Worker may publish its O_EXCL fence after the request is durable.
            (self.directory / "mailbox.closed").write_text('{}')
            return result
        with patch.object(mailbox, "publish_json", side_effect=close_after_publish):
            receipt = jobs.job_steer(self.job_id, "turn-1", "text", "racing")
        self.assertEqual(receipt["state"], "not_delivered")
        self.assertFalse((self.directory / "steering" / "states").exists())

    def test_worker_gone_derivation_and_durable_terminal_receipt(self):
        jobs.job_steer(self.job_id, "turn-1", "text", "key")
        request = mailbox.read_request(self.directory, mailbox.validate_key("key"))
        mailbox.write_state(self.directory, request, "sending", connection_id="private-connection", rpc_id=9)
        with patch.object(jobs, "identity", return_value="missing"):
            self.assertEqual(jobs.job_steer_status(self.job_id, "key")["state"], "delivery_uncertain")
            self.assertEqual(jobs.job_steer_status(self.job_id, "absent")["state"], "not_delivered")
        mailbox.write_state(self.directory, request, "accepted_by_runtime")
        self.metadata["status"] = "exited"
        self.metadata["app_server_runtime"]["connection_state"] = "lost"
        self.save()
        with patch.object(jobs, "identity", return_value="missing"):
            self.assertEqual(jobs.job_steer_status(self.job_id, "key")["state"], "accepted_by_runtime")

    def test_public_projection_and_lost_supervisor_stop_exact_identity(self):
        with patch.object(jobs, "identity", side_effect=lambda pid, _: "missing" if pid == 222 else "alive"):
            state = jobs.job_status(self.job_id)
            self.assertEqual((state["status"], state["error_code"]), ("unknown", "control_lost"))
            self.assertEqual(state["current_turn_id"], "turn-1")
            self.assertNotIn("input_text", state["app_server"])
            self.assertNotIn("app_server_runtime", state)
            with patch.object(jobs, "_terminate_tree", return_value={"identity": "alive"}) as kill, \
                    patch.object(jobs.time, "monotonic", return_value=100.0):
                stopped = jobs.job_stop(self.job_id)
            kill.assert_called_once_with(111, 1.0, True, deadline_monotonic=110.0)
            self.assertEqual(stopped["stop_result"], {"identity": "alive"})
        self.assertEqual(jobs._read(self.directory / "metadata.json"), self.metadata)


class LaunchCompatibilityTests(unittest.TestCase):
    def test_terminate_tree_wait_uses_shared_deadline_without_reset(self):
        parent = Mock(pid=111)
        parent.create_time.return_value = 1.0
        parent.children.return_value = []
        with patch.object(jobs, "identity", return_value="alive"), \
             patch.object(jobs.psutil, "Process", return_value=parent), \
             patch.object(jobs.psutil, "wait_procs", return_value=([], [])) as wait, \
             patch.object(jobs.time, "monotonic", side_effect=[100.0, 101.75, 103.0]) as clock:
            jobs._terminate_tree(111, 1.0)
            self.assertEqual(wait.call_args.kwargs["timeout"], 2)
            clock.assert_not_called()
            for remaining in (2, .25, 0.0):
                jobs._terminate_tree(111, 1.0, deadline_monotonic=102.0)
                self.assertEqual(wait.call_args.kwargs["timeout"], remaining)
            self.assertEqual(clock.call_count, 3)
            self.assertEqual(parent.kill.call_count, 4)

    def test_process_identity_rejects_nonfinite_or_pid_only_identity(self):
        self.assertEqual(jobs.identity(os.getpid(), float("nan")), "missing")
        self.assertEqual(jobs.identity(os.getpid(), None), "missing")

    def test_legacy_fingerprint_exactly_unchanged(self):
        args = (["binary", "exec"], "C:/workspace", "a" * 64, "b" * 64, None)
        payload = {"command": {"argv": args[0]}, "cwd": args[1], "env_sha256": args[2], "request_fingerprint": args[3], "dispatcher_binding": None}
        expected = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        self.assertEqual(jobs._keyed_fingerprint_from_env_digest(*args), expected)
        self.assertNotEqual(jobs._keyed_fingerprint_from_env_digest(*args, transport=APP_SERVER_TRANSPORT, app_server={}), expected)

    def test_transport_requires_keyed_dispatcher_binding(self):
        self.assertEqual(jobs.job_start(["unused"], transport=APP_SERVER_TRANSPORT, app_server={})["error"], "invalid_app_server_binding")
        self.assertEqual(jobs.job_start(["unused"], app_server={})["error"], "invalid_app_server_binding")

    def test_pinned_appserver_launch_idempotency_and_public_projection(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            node = root / "node.exe"
            node.touch()
            entry = root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
            entry.parent.mkdir(parents=True)
            entry.touch()
            command = [str(node.resolve()), str(entry.resolve()), "app-server"]
            key = "pinned-op"
            job_id = jobs.stable_job_id(key)
            binding = {"db_path": str(root / "db.sqlite"), "manifest_id": "manifest", "manifest_digest": "a" * 64,
                       "root_source_id": "source", "root_epoch": 1, "generation": 1, "job_id": "dispatcher-job",
                       "op_key": key, "request_digest": "b" * 64, "gateway_job_id": job_id,
                       "request_fingerprint": "b" * 64}
            spec = {"model": "test-model", "model_provider": "openai", "effort": "medium", "input_text": "private input",
                    "cwd": str(root.resolve()), "sandbox": "read-only", "approval_policy": "never", "approvals_reviewer": "user"}
            # This fake worker publishes a terminal record; no official CLI is run.
            def fake_launch(directory, env, metadata):
                metadata.update(status="exited", exit_code=0, finished_at=time.time())
                jobs._save(directory / "metadata.json", metadata)
            args = dict(cwd=str(root), runtime_dir=str(root), idempotency_key=key, request_fingerprint="b" * 64,
                        dispatcher_binding=binding, transport=APP_SERVER_TRANSPORT, app_server=spec)
            with patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "1"}), \
                 patch("home_mcp_gateway.windows_job_launch.launch", side_effect=fake_launch) as launch:
                first = jobs.job_start(command, **args)
                second = jobs.job_start(command, **args)
                self.assertEqual(first["job_id"], second["job_id"])
                self.assertEqual(launch.call_count, 1)
                changed = dict(spec, input_text="different private input")
                self.assertEqual(jobs.job_start(command, **{**args, "app_server": changed})["error"], "idempotency_conflict")
            self.assertNotIn("input_text", first["app_server"])
            raw = jobs._read(root / "jobs" / job_id / "metadata.json")
            self.assertEqual(raw["app_server"], spec)
            self.assertEqual(raw["transport"], APP_SERVER_TRANSPORT)


if __name__ == "__main__":
    unittest.main()
