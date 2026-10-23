from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

import psutil

from home_mcp_gateway.codex_app_server_runtime import _Runtime, RuntimeFailure, run_app_server
from home_mcp_gateway import job_steering
from home_mcp_gateway.jobs import _save, identity


FIXTURE = Path(__file__).with_name("fake_codex_app_server.py")


class AppServerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / str(uuid.uuid4())
        self.directory.mkdir()

    def run_fake(self, mode="completed", request=False, on_save=None):
        spec = {"model": "offline-model", "model_provider": "openai", "effort": "high",
                "input_text": "initial private prompt", "cwd": str(self.directory),
                "sandbox": "read-only", "approval_policy": "never", "approvals_reviewer": "user"}
        self.capture = self.directory / "fixture.received.jsonl"
        self.data = {"app_server": spec, "cwd": str(self.directory), "transport": "codex_app_server",
                     "job_id": self.directory.name, "worker_pid": os.getpid(), "worker_create_time": psutil.Process().create_time(),
                     "command": [sys.executable, str(FIXTURE), mode, json.dumps(spec), str(self.capture)],
                     "stdout_path": str(self.directory / "stdout.log"), "stderr_path": str(self.directory / "stderr.log"),
                     "dispatcher_binding": {"offline": True, "gateway_job_id": self.directory.name}}
        self.request = None
        if request:
            self.request = job_steering.make_request(self.directory.name, "turn-1", "private steer input", "request-1")
            job_steering.publish_json(job_steering.request_path(self.directory, self.request["request_id"]), self.request)
        self.snapshots = []

        def save(data):
            self.snapshots.append(json.loads(json.dumps(data)))
            _save(self.directory / "metadata.json", data)
            if on_save:
                on_save(data)

        with patch("home_mcp_gateway.work_dispatcher_codex.steer_guard", return_value="open"):
            _Runtime(self.directory, self.data, save).run()
        self.frames = [json.loads(line) for line in self.capture.read_text().splitlines()] if self.capture.exists() else []
        self.journal = [json.loads(line) for line in (self.directory / "appserver.rpc.jsonl").read_text().splitlines()]
        return self.data

    def state(self):
        return job_steering.read_state(self.directory, self.request["request_id"])

    def test_pinned_handshake_and_normal_terminal(self):
        result = self.run_fake()
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["app_server_runtime"]["turn_outcome"], "completed")
        sent = {frame["method"]: frame for frame in self.frames if "method" in frame}
        self.assertEqual(set(sent), {"initialize", "initialized", "thread/start", "turn/start"})
        self.assertEqual(set(sent["initialize"]["params"]), {"clientInfo"})
        self.assertEqual(sent["initialized"], {"method": "initialized"})
        self.assertEqual(set(sent["thread/start"]["params"]), {"model", "modelProvider", "cwd", "sandbox", "approvalPolicy", "approvalsReviewer", "config"})
        self.assertEqual(sent["thread/start"]["params"]["config"], {"model_reasoning_effort": "high"})
        self.assertEqual(set(sent["turn/start"]["params"]), {"threadId", "input", "effort"})
        self.assertTrue(job_steering.mailbox_closed(self.directory))
        self.assertTrue(all(set(line) == {"connection_id", "timestamp", "direction", "frame"} for line in self.journal))
        self.assertEqual({line["direction"] for line in self.journal}, {"send", "receive"})

    def test_omitted_network_access_is_false(self):
        self.assertEqual(self.run_fake("network_omitted")["status"], "exited")

    def test_effective_policy_mismatch_prevents_turn_start(self):
        modes = ["mismatch:" + key for key in ["model", "modelProvider", "cwd", "approvalPolicy", "approvalsReviewer", "sandbox", "sandboxextra", "networkAccess", "reasoningEffort", "reasoningEffort_missing"]]
        modes += ["missing:" + key for key in ["model", "modelProvider", "cwd", "approvalPolicy", "approvalsReviewer", "sandbox", "reasoningEffort"]]
        for mode in modes:
            with self.subTest(mode=mode):
                self.directory = Path(self.temp.name) / str(uuid.uuid4())
                self.directory.mkdir()
                result = self.run_fake(mode)
                self.assertEqual(result.get("error_code"), "appserver_policy_mismatch")
                self.assertFalse(any(frame.get("method") == "turn/start" for frame in self.frames))

    def test_server_requests_are_schema_correct_and_noninteractive(self):
        started = time.monotonic()
        result = self.run_fake("requests")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["app_server_runtime"]["approval_request_count"], 5)
        self.assertEqual(result["app_server_runtime"]["policy_code"], "policy_unexpected_request")
        replies = {frame["id"]: frame for frame in self.frames if str(frame.get("id", "")).startswith("server-")}
        self.assertEqual(len(replies), 9)
        self.assertEqual(replies["server-0"]["result"], {"decision": "decline"})
        self.assertEqual(replies["server-2"]["result"], {"permissions": {}, "scope": "turn"})
        self.assertEqual(replies["server-3"]["result"]["decision"], {"denied": {"rejection": "Gateway read-only policy"}})
        self.assertEqual(replies["server-5"]["result"], {"action": "decline"})
        self.assertEqual(replies["server-8"]["error"]["code"], -32601)

    def test_auth_requests_fail_closed_without_persisting_secrets(self):
        for mode in ["auth", "attestation"]:
            with self.subTest(mode=mode):
                self.directory = Path(self.temp.name) / str(uuid.uuid4())
                self.directory.mkdir()
                result = self.run_fake(mode)
                self.assertEqual(result.get("error_code"), "appserver_auth_required")
                for file in [self.directory / "appserver.rpc.jsonl", self.directory / "stdout.log", self.directory / "stderr.log"]:
                    if file.exists():
                        self.assertNotIn(b"SECRET", file.read_bytes())
                auth = [line["frame"] for line in self.journal if line["frame"].get("method") in {"account/chatgptAuthTokens/refresh", "attestation/generate"}]
                self.assertTrue(auth[0]["redacted"])

    def test_benign_account_notifications_are_redacted_and_not_auth_failure(self):
        result = self.run_fake("benign_account")
        self.assertEqual(result["status"], "exited")
        self.assertIsNone(result["app_server_runtime"]["runtime_error_code"])
        account_frames = [line["frame"] for line in self.journal if line["frame"].get("method", "").startswith("account/")]
        self.assertEqual(account_frames, [
            {"method": "account/updated", "redacted": True},
            {"method": "account/rateLimits/updated", "redacted": True}])
        self.assertNotIn(b"SECRET", (self.directory / "appserver.rpc.jsonl").read_bytes())

    def test_steer_durable_before_write_and_exact_recording(self):
        result = self.run_fake("steer", request=True)
        self.assertEqual(result["status"], "exited")
        self.assertEqual(self.state()["state"], "accepted_by_runtime")
        self.assertTrue(self.state()["recorded"])
        frame = next(frame for frame in self.frames if frame.get("method") == "turn/steer")
        self.assertEqual(set(frame["params"]), {"threadId", "input", "expectedTurnId", "clientUserMessageId"})
        self.assertEqual(self.state()["rpc_id"], frame["id"])
        self.assertEqual(self.state()["client_user_message_id"], frame["params"]["clientUserMessageId"])
        self.assertTrue(next(frame["fixture_durable_sending"] for frame in self.frames if "fixture_durable_sending" in frame))
        ids = [frame["id"] for frame in self.frames if "method" in frame and "id" in frame]
        self.assertEqual(ids, sorted(set(ids)))
        self.assertNotIn("private steer", Path(result["stdout_path"]).read_text())

    def test_unrelated_native_item_does_not_imply_recording(self):
        self.run_fake("steer_unrecorded", request=True)
        self.assertEqual(self.state()["state"], "accepted_by_runtime")
        self.assertFalse(self.state().get("recorded", False))

    def test_pending_steer_at_terminal_remains_uncertain(self):
        result = self.run_fake("missing_steer", request=True)
        self.assertEqual(result["status"], "exited")
        self.assertEqual(self.state()["state"], "sending")
        self.assertEqual(sum(frame.get("method") == "turn/steer" for frame in self.frames), 1)

    def test_pipe_loss_after_steer_write_is_uncertain_and_never_retried(self):
        result = self.run_fake("steer_crash", request=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.state()["state"], "sending")
        self.assertEqual(sum(frame.get("method") == "turn/steer" for frame in self.frames), 1)
        receipt = job_steering.public_receipt(self.request, self.state(), closed=True, worker_alive=True,
            connection_id=result["app_server_runtime"]["connection_id"], connection_alive=False)
        self.assertEqual(receipt["state"], "delivery_uncertain")
        with patch("home_mcp_gateway.codex_app_server_runtime.subprocess.Popen") as launch:
            _Runtime(self.directory, self.data, lambda value: _save(self.directory / "metadata.json", value)).run()
        launch.assert_not_called()
        self.assertEqual(self.state()["state"], "sending")

    def test_wrong_turn_success_is_not_accepted_or_rejected(self):
        result = self.run_fake("steer_wrong", request=True)
        self.assertEqual(result.get("error_code"), "appserver_protocol_invalid")
        self.assertEqual(self.state()["state"], "sending")

    def test_explicit_rpc_error_is_durable_rejection(self):
        self.run_fake("steer_error", request=True)
        self.assertEqual(self.state()["state"], "rejected")

    def test_late_same_rpc_response_is_finalized_during_cleanup(self):
        self.run_fake("late_steer", request=True)
        self.assertEqual(self.state()["state"], "accepted_by_runtime")
        self.assertEqual(sum(frame.get("method") == "turn/steer" for frame in self.frames), 1)

    def test_late_protocol_error_after_terminal_cannot_succeed(self):
        result = self.run_fake("late_invalid")
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "appserver_protocol_invalid")

    def test_crash_before_terminal_cannot_succeed(self):
        result = self.run_fake("crash")
        self.assertEqual(result["status"], "failed")
        self.assertIsNone(result["app_server_runtime"]["turn_outcome"])

    def test_invalid_duplicate_and_oversized_frames_fail_closed(self):
        for mode, expected in [("invalid", "appserver_protocol_invalid"), ("huge", "appserver_evidence_limit"), ("duplicate_response", "appserver_protocol_invalid")]:
            with self.subTest(mode=mode):
                self.directory = Path(self.temp.name) / str(uuid.uuid4())
                self.directory.mkdir()
                self.assertEqual(self.run_fake(mode).get("error_code"), expected)

    def test_forced_shutdown_preserves_terminal_fact(self):
        result = self.run_fake("force")
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["app_server_runtime"]["turn_outcome"], "completed")
        self.assertEqual(result["app_server_runtime"]["shutdown_code"], "appserver_shutdown_forced")

    def test_graceful_root_exit_still_terminates_exact_descendant(self):
        result = self.run_fake("orphan")
        retained = result["app_server_runtime"]["descendant_stop_results"]
        child_pid = next(frame["fixture_child_pid"] for frame in self.frames if "fixture_child_pid" in frame)
        child = next(item for item in retained if item["pid"] == child_pid)
        self.assertNotEqual(identity(child_pid, child["process_create_time"]), "alive")

    def test_stop_published_after_sending_fsync_prevents_wire_write(self):
        original = _Runtime._write
        def stop_before_queue(runtime, frame, **kwargs):
            if frame.get("method") == "turn/steer":
                request = kwargs["steer_request"]
                state = job_steering.read_state(runtime.directory, request["request_id"])
                self.assertEqual(state["state"], "sending")
                (runtime.directory / "stop.json").write_text(json.dumps({"force": True, "deadline_monotonic": time.monotonic() + 4}))
            return original(runtime, frame, **kwargs)
        with patch.object(_Runtime, "_write", stop_before_queue):
            self.run_fake("stop", request=True)
        self.assertEqual(self.state()["state"], "rejected")
        self.assertEqual(self.state()["reason"], "stopping")
        self.assertFalse(any(frame.get("method") == "turn/steer" for frame in self.frames))
        self.assertFalse(any(line["frame"].get("method") == "turn/steer" for line in self.journal))

    def test_feature_disabled_never_spawns(self):
        with patch("home_mcp_gateway.work_dispatcher_codex.app_server_enabled", return_value=False), patch("subprocess.Popen") as launch:
            with self.assertRaisesRegex(RuntimeFailure, "appserver_disabled"):
                run_app_server(self.directory, {}, lambda _: None)
        launch.assert_not_called()

    def test_stop_interrupt_response_and_native_outcome_are_separate(self):
        requested = False
        def stop_when_active(data):
            nonlocal requested
            if data["app_server_runtime"]["current_turn_id"] and not requested:
                requested = True
                (self.directory / "stop.json").write_text(json.dumps({"force": True, "deadline_monotonic": time.monotonic() + 4}))
        result = self.run_fake("stop", on_save=stop_when_active)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["app_server_runtime"]["interrupt_response"], "acknowledged")
        self.assertEqual(result["app_server_runtime"]["turn_outcome"], "interrupted")

    def test_stop_does_not_mask_fatal_cleanup_protocol_error(self):
        requested = False
        def stop_when_active(data):
            nonlocal requested
            if data["app_server_runtime"]["current_turn_id"] and not requested:
                requested = True
                (self.directory / "stop.json").write_text(json.dumps({"force": True, "deadline_monotonic": time.monotonic() + 4}))
        result = self.run_fake("stop_late_invalid", on_save=stop_when_active)
        self.assertEqual(result["error_code"], "appserver_protocol_invalid")
        self.assertEqual(result["app_server_runtime"]["runtime_error_code"], "appserver_protocol_invalid")

    def test_stop_without_interrupt_reply_uses_existing_deadline(self):
        requested = False
        def stop_when_active(data):
            nonlocal requested
            if data["app_server_runtime"]["current_turn_id"] and not requested:
                requested = True
                (self.directory / "stop.json").write_text(json.dumps({"force": True, "deadline_monotonic": time.monotonic() + 2.5}))
        started = time.monotonic()
        result = self.run_fake("stop_no_reply", on_save=stop_when_active)
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(result["status"], "stopped")
        self.assertIsNone(result["app_server_runtime"]["turn_outcome"])
        self.assertNotIn("interrupt_response", result["app_server_runtime"])
        self.assertIsNone(result["app_server_runtime"]["runtime_error_code"])

    def test_journal_limit_is_failure_never_truncated_success(self):
        with patch("home_mcp_gateway.codex_app_server_runtime.MAX_JOURNAL_BYTES", 100):
            result = self.run_fake()
        self.assertEqual(result["error_code"], "appserver_evidence_limit")
        self.assertLessEqual((self.directory / "appserver.rpc.jsonl").stat().st_size, 100)

    def test_windows_helpers_are_hidden(self):
        if os.name != "nt":
            self.skipTest("Windows subprocess flags")
        original = subprocess.Popen
        observed = []
        def hidden(*args, **kwargs):
            observed.append(kwargs.get("creationflags", 0))
            return original(*args, **kwargs)
        with patch("home_mcp_gateway.codex_app_server_runtime.subprocess.Popen", side_effect=hidden):
            self.run_fake()
        self.assertTrue(all(flags & subprocess.CREATE_NO_WINDOW for flags in observed))


if __name__ == "__main__":
    unittest.main()
