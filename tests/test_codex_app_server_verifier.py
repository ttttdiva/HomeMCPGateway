"""Dedicated protocol verification uses synthetic journals only; no Codex runtime."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from home_mcp_gateway import codex_app_server as verifier
from home_mcp_gateway import work_supervisor as supervisor


THREAD = "11111111-1111-4111-8111-111111111111"
TURN = "22222222-2222-4222-8222-222222222222"
JOB = "33333333-3333-4333-8333-333333333333"
CONNECTION = "private-connection"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def spec(cwd=None):
    return {"model": "gpt-test", "model_provider": "openai", "effort": "high", "input_text": "private initial input",
            "cwd": cwd or str(Path.cwd()), "sandbox": "read-only", "approval_policy": "never", "approvals_reviewer": "user"}


def envelope(direction, frame):
    return {"connection_id": CONNECTION, "timestamp": "2026-10-23T00:00:00Z", "direction": direction, "frame": frame}


def notification(method, params):
    return envelope("receive", {"method": method, "params": params})


def agent(text, item="final", *, turn=TURN, thread=THREAD, delivery=None):
    return notification("item/completed", {"threadId": thread, "turnId": turn, "completedAtMs": 1,
        "item": {"id": item, "type": "agentMessage", "text": text, **({"delivery": delivery} if delivery else {})}})


def journal_records(pinned=None, outcome="completed"):
    pinned = pinned or spec()
    turn = {"id": TURN, "status": "inProgress", "items": []}
    return [
        envelope("send", {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "fixture", "version": "1"}}}),
        envelope("receive", {"id": 1, "result": {"userAgent": "fixture"}}),
        envelope("send", {"method": "initialized"}),
        envelope("send", {"id": 2, "method": "thread/start", "params": {"model": pinned["model"],
            "modelProvider": pinned["model_provider"], "cwd": pinned["cwd"], "sandbox": "read-only",
            "approvalPolicy": "never", "approvalsReviewer": "user", "config": {"model_reasoning_effort": pinned["effort"]}}}),
        notification("thread/started", {"thread": {"id": THREAD}}),
        envelope("receive", {"id": 2, "result": {"thread": {"id": THREAD}, "model": pinned["model"],
            "modelProvider": "openai", "reasoningEffort": pinned["effort"], "cwd": pinned["cwd"],
            "sandbox": {"type": "readOnly", "networkAccess": False}, "approvalPolicy": "never", "approvalsReviewer": "user"}}),
        envelope("send", {"id": 3, "method": "turn/start", "params": {"threadId": THREAD,
            "input": [{"type": "text", "text": pinned["input_text"]}], "effort": pinned["effort"]}}),
        notification("turn/started", {"threadId": THREAD, "turn": turn}),
        envelope("receive", {"id": 3, "result": {"turn": turn}}),
        agent("  exact final\ntext  "),
        notification("turn/completed", {"threadId": THREAD, "turn": {"id": TURN, "items": [], "status": outcome}}),
    ]


def encode(records):
    return b"".join(canonical(record) + b"\n" for record in records)


def with_steer(records, *, state="accepted_by_runtime", recorded=False, reply_after_terminal=False):
    records = copy.deepcopy(records)
    content = [{"type": "text", "text": "private steer text"}]
    send = envelope("send", {"id": 4, "method": "turn/steer", "params": {"threadId": THREAD,
        "expectedTurnId": TURN, "clientUserMessageId": "private-client", "input": content}})
    reply = envelope("receive", {"id": 4, "result": {"turnId": TURN}})
    additions = [send] + ([] if reply_after_terminal else [reply])
    if recorded:
        additions.append(notification("item/completed", {"threadId": THREAD, "turnId": TURN, "completedAtMs": 2,
            "item": {"id": "user-steer", "type": "userMessage", "clientId": "private-client", "content": content}}))
    records[-2:-2] = additions
    if reply_after_terminal:
        records.append(reply)
    row = {"request_id": "request-one", "job_id": JOB, "expected_turn_id": TURN,
        "request_digest": hashlib.sha256(canonical({"job_id": JOB, "expected_turn_id": TURN, "text": content[0]["text"]})).hexdigest(),
        "state": state, "client_user_message_id": "private-client", "sending_at": 1,
        "created_at": 0, "connection_id": CONNECTION, "rpc_id": 4}
    if recorded:
        row.update(recorded=True, recorded_turn_id=TURN)
    return records, [row]


class AppserverVerifier(unittest.TestCase):
    def parse(self, records=None, **kwargs):
        return verifier.parse_appserver_journal(encode(records or journal_records()), spec(), **kwargs)

    def test_target_last_completed_before_terminal_only(self):
        records = journal_records()
        records[-2:-2] = [agent("earlier", "earlier"), agent("other turn", turn="another-turn"),
            agent("other thread", thread="another-thread"), agent("proactive", delivery="async"),
            notification("item/agentMessage/delta", {"threadId": THREAD, "turnId": TURN, "delta": "delta"})]
        records.append(agent("after terminal", "late"))
        result = self.parse(records)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result_text"], "  exact final\ntext  ")
        self.assertEqual(result["final_item_id"], "final")

    def test_failed_and_interrupted_are_never_success(self):
        for outcome in ("failed", "interrupted"):
            with self.subTest(outcome=outcome):
                result = self.parse(journal_records(outcome=outcome))
                self.assertEqual(result["state"], "failed")
                self.assertEqual(result["native_outcome"], outcome)

    def test_wrong_identity_and_missing_native_start(self):
        self.assertEqual(self.parse(expected_thread_id="wrong")["code"], "thread_identity_mismatch")
        self.assertEqual(self.parse(expected_turn_id="wrong")["code"], "turn_identity_mismatch")
        for method in ("thread/started", "turn/started"):
            records = [r for r in journal_records() if r["frame"].get("method") != method]
            self.assertEqual(self.parse(records)["state"], "result_pending")

    def test_spec_mismatch_missing_effort_and_policy_override_fail_closed(self):
        for mutation in ("missing_effort", "wrong_model", "extra_config", "extra_sandbox", "turn_override", "experimental"):
            records = journal_records()
            if mutation == "missing_effort": del records[5]["frame"]["result"]["reasoningEffort"]
            if mutation == "wrong_model": records[5]["frame"]["result"]["model"] = "different"
            if mutation == "extra_config": records[3]["frame"]["params"]["config"]["tools"] = True
            if mutation == "extra_sandbox": records[5]["frame"]["result"]["sandbox"]["writableRoots"] = ["/"]
            if mutation == "turn_override": records[6]["frame"]["params"]["model"] = "different"
            if mutation == "experimental": records[0]["frame"]["params"]["capabilities"] = {"experimentalApi": True}
            with self.subTest(mutation=mutation): self.assertEqual(self.parse(records)["state"], "result_pending")
        for key, value in (("sandbox", "workspace-write"), ("model_provider", "other"), ("approval_policy", "on-request")):
            invalid = spec(); invalid[key] = value
            self.assertEqual(verifier.parse_appserver_journal(encode(journal_records()), invalid)["code"], "app_server_spec_invalid")

    def test_accepted_is_known_even_without_recorded_event(self):
        records, evidence = with_steer(journal_records())
        result = self.parse(records, steer_evidence=evidence)
        self.assertEqual(result["state"], "succeeded")
        self.assertFalse(result["input_set_uncertain"])
        self.assertEqual(result["recorded_steer_count"], 0)

    def test_late_success_is_correlated_to_sent_turn(self):
        records, evidence = with_steer(journal_records(), reply_after_terminal=True)
        self.assertFalse(self.parse(records, steer_evidence=evidence)["input_set_uncertain"])
        records[-1]["frame"]["result"]["turnId"] = "wrong"
        self.assertTrue(self.parse(records, steer_evidence=evidence)["input_set_uncertain"])

    def test_evidence_binds_spec_steer_state_recording_and_uncertainty(self):
        records, evidence = with_steer(journal_records())
        initial = self.parse(records, steer_evidence=evidence)
        recorded_records, recorded_evidence = with_steer(journal_records(), recorded=True)
        recorded = self.parse(recorded_records, steer_evidence=recorded_evidence)
        self.assertNotEqual(initial["evidence_digest"], recorded["evidence_digest"])
        self.assertEqual(recorded["recorded_steer_count"], 1)
        uncertain = self.parse(records, steer_evidence=[])
        self.assertTrue(uncertain["input_set_uncertain"])
        self.assertNotEqual(initial["evidence_digest"], uncertain["evidence_digest"])
        changed_state = copy.deepcopy(evidence); changed_state[0]["state"] = "sending"
        self.assertNotEqual(initial["evidence_digest"], self.parse(records, steer_evidence=changed_state)["evidence_digest"])
        different_spec = spec(); different_spec["input_text"] = "different private input"
        other = verifier.parse_appserver_journal(encode(journal_records(different_spec)), different_spec)
        self.assertNotEqual(initial["spec_digest"], other["spec_digest"])

    def test_pre_send_rejected_and_queued_are_known_nondelivery(self):
        evidence = [{"request_id": "unsent", "request_digest": "a" * 64, "state": "rejected", "reason": "stopping", "created_at": 0},
                    {"request_id": "queued", "request_digest": "b" * 64, "state": "queued", "created_at": 1}]
        result = self.parse(steer_evidence=evidence)
        self.assertFalse(result["input_set_uncertain"])
        self.assertEqual(result["steer_count"], 2)

    def test_writer_final_guard_rejection_with_sending_intent_is_known_unsent(self):
        row = {"request_id": "writer-refused", "request_digest": "a" * 64, "state": "rejected",
               "reason": "stopping", "created_at": 0, "sending_at": 1, "connection_id": CONNECTION,
               "rpc_id": 4, "client_user_message_id": "private-client"}
        before = copy.deepcopy(row)
        result = self.parse(steer_evidence=[row])
        self.assertEqual(result["state"], "succeeded")
        self.assertFalse(result["input_set_uncertain"])
        self.assertEqual(result["steer_count"], 1)
        self.assertEqual(self.parse(steer_evidence=[row])["evidence_digest"], result["evidence_digest"])
        self.assertEqual(row, before)
        for reason in ("job_finished", "guard_closed", "guard_mismatch", "guard_unavailable"):
            refused = self.parse(steer_evidence=[{**row, "reason": reason}])
            self.assertFalse(refused["input_set_uncertain"])
            self.assertNotEqual(refused["evidence_digest"], result["evidence_digest"])
        records, evidence = with_steer(journal_records())
        evidence[0].update(state="rejected", reason="stopping")
        self.assertTrue(self.parse(records, steer_evidence=evidence)["input_set_uncertain"])
        next(r for r in records if r["direction"] == "receive" and r["frame"].get("id") == 4)["frame"] = {
            "id": 4, "error": {"code": -32000, "message": "Rejected"}}
        self.assertFalse(self.parse(records, steer_evidence=evidence)["input_set_uncertain"])

    def test_digest_wrong_client_and_unresolved_send_are_uncertain(self):
        records, evidence = with_steer(journal_records())
        for field, value in (("request_digest", "f" * 64), ("client_user_message_id", "wrong"), ("connection_id", "wrong")):
            altered = copy.deepcopy(evidence); altered[0][field] = value
            self.assertTrue(self.parse(records, steer_evidence=altered)["input_set_uncertain"])
        records = [r for r in records if not (r["direction"] == "receive" and r["frame"].get("id") == 4)]
        evidence[0]["state"] = "sending"
        self.assertTrue(self.parse(records, steer_evidence=evidence)["input_set_uncertain"])

    def test_limits_partial_duplicate_keys_and_copied_exec_fail_closed(self):
        encoded = encode(journal_records())
        self.assertEqual(verifier.parse_appserver_journal(encoded[:-1], spec())["code"], "partial_rpc_journal")
        with mock.patch.object(verifier, "MAX_JOURNAL_BYTES", 8):
            self.assertEqual(verifier.parse_appserver_journal(encoded, spec())["code"], "result_too_large")
        bad = envelope("receive", {"method": "noise", "params": {"large": "x" * verifier.MAX_FRAME_BYTES}})
        self.assertEqual(self.parse(journal_records() + [bad])["code"], "rpc_frame_too_large")
        self.assertEqual(verifier.parse_appserver_journal(b'{"x":1,"x":2}\n', spec())["code"], "rpc_journal_invalid")
        copied = [{"type": "thread.started", "thread_id": THREAD}, {"type": "turn.completed"}]
        self.assertEqual(verifier.parse_appserver_journal(encode(copied), spec())["code"], "rpc_journal_invalid")

    def test_output_contains_no_steer_text_or_private_handles(self):
        records, evidence = with_steer(journal_records(), recorded=True)
        result = self.parse(records, steer_evidence=evidence)
        public = json.dumps(result)
        for secret in ("private steer text", "private-client", CONNECTION, "request-one", "private initial input"):
            self.assertNotIn(secret, public)

    def test_redacted_auth_frames_anywhere_fail_and_stray_response_rejected(self):
        records = journal_records() + [envelope("receive", {"id": "auth", "method": "account/login/start", "redacted": True}),
            envelope("send", {"id": "auth", "redacted": True})]
        result = self.parse(records)
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["code"], "appserver_auth_required")
        self.assertEqual(self.parse(journal_records() + [envelope("receive", {"id": 999, "result": {}})])["code"],
                         "rpc_response_identity_invalid")

    def test_exact_redacted_account_update_notifications_are_benign(self):
        records = journal_records()
        benign = ("account/updated", "account/rateLimits/updated")
        records[-2:-2] = [envelope("receive", {"method": method, "redacted": True}) for method in benign]
        result = self.parse(records)
        self.assertEqual(result["state"], "succeeded")
        self.assertEqual(result["result_text"], "  exact final\ntext  ")
        self.assertEqual(result["approval_request_count"], 0)
        for method in benign:
            requested = journal_records() + [envelope("receive", {"id": "account-request", "method": method, "redacted": True})]
            self.assertEqual(self.parse(requested)["code"], "appserver_auth_required")
        other = journal_records() + [envelope("receive", {"method": "account/login/completed", "redacted": True})]
        self.assertNotEqual(self.parse(other)["state"], "succeeded")

    def test_only_approval_families_count_and_request_ids_monotonic(self):
        records = journal_records()
        methods = ["item/commandExecution/requestApproval", "item/fileChange/requestApproval", "item/permissions/requestApproval",
                   "applyPatchApproval", "execCommandApproval", "mcpServer/elicitation/request", "item/tool/requestUserInput",
                   "item/tool/call", "unknown/method"]
        for number, method in enumerate(methods):
            records += [envelope("receive", {"id": f"server-{number}", "method": method, "params": {}}),
                        envelope("send", {"id": f"server-{number}", "error": {"code": -32601, "message": "Method not found"}})]
        result = self.parse(records)
        self.assertEqual(result["approval_request_count"], 5)
        self.assertEqual(result["policy_code"], "policy_unexpected_request")
        records = journal_records(); records[3]["frame"]["id"] = 0
        self.assertEqual(self.parse(records)["code"], "rpc_request_identity_invalid")


class AppserverSupervisor(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = self.root / "runtime"
        self.directory = self.runtime / "jobs" / JOB
        self.directory.mkdir(parents=True)
        self.node = self.root / ("node.exe" if __import__("os").name == "nt" else "node")
        self.node.write_text("offline fixture")
        self.entry = self.root / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        self.entry.parent.mkdir(parents=True); self.entry.write_text("offline fixture")
        self.pinned = spec(str(self.root))
        self.metadata = {"job_id": JOB, "transport": "codex_app_server", "command": [str(self.node), str(self.entry), "app-server"],
            "cwd": str(self.root), "started_at": 1, "runtime_dir": str(self.runtime), "status": "exited", "exit_code": 9,
            "app_server": self.pinned, "app_server_runtime": {"thread_id": THREAD, "turn_id": TURN,
                "journal_path": str(self.directory / "appserver.rpc.jsonl"), "shutdown_code": "appserver_shutdown_forced"}}
        self.metadata["dispatcher_binding"] = {"offline": True}
        self.guard_patch = mock.patch.object(supervisor.work_dispatcher_codex, "steer_guard", return_value="open")
        self.guard_patch.start(); self.addCleanup(self.guard_patch.stop)

    def write(self, records=None):
        (self.directory / "metadata.json").write_text(json.dumps(self.metadata), encoding="utf-8")
        (self.directory / "appserver.rpc.jsonl").write_bytes(encode(records or journal_records(self.pinned)))
        (self.directory / "stdout.log").write_text("human projection cannot prove completion", encoding="utf-8")

    def observe(self, previous=None):
        return supervisor._observe_appserver_job(self.metadata, JOB, self.directory, self.runtime, previous, 10, 64 << 20)[0]

    def test_completed_turn_succeeds_despite_forced_shutdown_nonzero(self):
        self.write()
        observed = self.observe()
        self.assertEqual((observed["result_state"], observed["execution_status"], observed["exit_code"]), ("succeeded", "succeeded", 9))
        self.assertEqual(observed["app_server_evidence"]["shutdown_code"], "appserver_shutdown_forced")
        self.assertEqual(observed["stdout_ref"], f"jobs/{JOB}/appserver.rpc.jsonl")

    def test_crash_before_terminal_cannot_succeed_and_worker_loss_not_running(self):
        self.metadata["status"] = "running"
        self.metadata["app_server_runtime"]["shutdown_code"] = "control_lost"
        self.write(journal_records(self.pinned)[:-1])
        observed = self.observe()
        self.assertEqual(observed["result_state"], "pending")
        self.assertEqual(observed["process_status"], "unknown")

    def test_completed_open_journal_waits_and_late_fatal_never_handoffs(self):
        self.metadata["status"] = "running"
        self.metadata["app_server_runtime"]["connection_state"] = "connected"
        self.write()
        open_result = self.observe()
        self.assertEqual(open_result["result_state"], "pending")
        self.assertEqual(open_result["turn_status"], "turn_completed")
        self.assertEqual(open_result["error_code"], "appserver_journal_open")
        conn = supervisor.connect(self.root / "open-ledger"); self.addCleanup(conn.close)
        fetched = supervisor.Fetched(10); fetched.gateway_observations = [open_result]
        supervisor._commit_gateway(conn, fetched, 10, {})
        self.assertEqual(conn.execute("SELECT count(*) FROM handoffs").fetchone()[0], 0)
        self.metadata["status"] = "failed"
        self.metadata["app_server_runtime"].update(connection_state="lost", runtime_error_code="appserver_protocol_invalid")
        final = self.observe()
        self.assertEqual(final["result_state"], "failed")
        fetched.gateway_observations = [final]
        supervisor._commit_gateway(conn, fetched, 11, {})
        self.assertEqual(conn.execute("SELECT count(*) FROM handoffs").fetchone()[0], 0)

    def test_scan_selects_pinned_transport_and_persists_safe_provenance(self):
        self.write()
        conn = supervisor.connect(self.root / "ledger")
        self.addCleanup(conn.close)
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime), \
             mock.patch.object(supervisor.work_dispatcher_codex, "parse_codex_jsonl", side_effect=AssertionError("stdout forbidden")):
            result = supervisor._scan_gateway_jobs("", {}, 10)
        fetched = supervisor.Fetched(10); fetched.gateway_observations = result["observations"]
        supervisor._commit_gateway(conn, fetched, 10, {})
        status = supervisor.build_status(conn, 10)
        job = status["gateway_codex"]["jobs"][0]
        self.assertEqual(job["app_server"]["native_outcome"], "completed")
        self.assertEqual(job["app_server"]["input_set_uncertain"], False)
        pending = supervisor.pending_view(conn, 10)["items"][0]
        self.assertEqual(pending["gateway_provenance"]["app_server"]["spec_digest"], job["app_server"]["spec_digest"])
        projected = json.dumps(status)
        self.assertNotIn("private initial input", projected)
        self.assertNotIn(CONNECTION, projected)
        self.metadata.pop("transport"); self.write()
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=self.runtime):
            observation = supervisor._scan_gateway_jobs("", {}, 10)["observations"][0]
            self.assertEqual(observation["error_code"], "app_server_transport_mismatch")

    def test_immutable_binding_guard_mismatch_prevents_evidence_read(self):
        self.write()
        with mock.patch.object(supervisor.work_dispatcher_codex, "steer_guard", return_value="mismatch"), \
             mock.patch.object(verifier, "parse_appserver_journal", side_effect=AssertionError("denied binding")):
            with self.assertRaisesRegex(supervisor.SupervisorError, "binding_mismatch"):
                self.observe()

    def test_real_bound_manifest_cannot_mutate_spec_or_downgrade_transport(self):
        # Reuse the narrow synthetic dispatcher fixture; no provider or process.
        import importlib
        fixture = importlib.import_module("test_codex_app_server_launch").AppServerLaunchTests("runTest")
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        binding, request, _, metadata, directory = fixture.reserve()
        fixture.mark_running(binding)
        metadata.update(status="exited", exit_code=9, started_at=1, runtime_dir=fixture.runtime_dir)
        metadata["app_server_runtime"].update(thread_id=THREAD, turn_id=TURN, shutdown_code="appserver_shutdown_forced")
        (directory / "appserver.rpc.jsonl").write_bytes(encode(journal_records(request["app_server"])))
        self.guard_patch.stop()
        observed, _ = supervisor._observe_appserver_job(metadata, binding["gateway_job_id"], directory,
            Path(fixture.runtime_dir), None, 10, 64 << 20)
        self.assertEqual(observed["result_state"], "succeeded")
        mutated = copy.deepcopy(metadata); mutated["app_server"]["model"] = "tampered"
        with self.assertRaisesRegex(supervisor.SupervisorError, "binding_mismatch"):
            supervisor._observe_appserver_job(mutated, binding["gateway_job_id"], directory,
                Path(fixture.runtime_dir), None, 10, 64 << 20)
        downgraded = copy.deepcopy(metadata); downgraded.pop("transport"); downgraded.pop("app_server")
        downgraded["command"] = [str(fixture.node), str(fixture.entry), "exec", "--json", "private"]
        (directory / "metadata.json").write_text(json.dumps(downgraded), encoding="utf-8")
        (directory / "stdout.log").write_text("not evidence", encoding="utf-8")
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=Path(fixture.runtime_dir)), \
             mock.patch.object(supervisor.work_dispatcher_codex, "parse_codex_jsonl", side_effect=AssertionError("cannot downgrade")):
            rejected = supervisor._scan_gateway_jobs("", {}, 10)["observations"][0]
        self.assertEqual(rejected["error_code"], "app_server_transport_mismatch")
        # Mutable operation JSON cannot override the transport in the original
        # digest-verified manifest, even when metadata also looks like exec.
        operation_request = {"command": downgraded["command"], "cwd": downgraded["cwd"],
                             "model": "gpt-offline", "effort": "high"}
        fixture.conn.execute("UPDATE ops SET request_json=? WHERE op_key=?",
                             (canonical(operation_request).decode("utf-8"), binding["op_key"]))
        with mock.patch("home_mcp_gateway.jobs.runtime_root", return_value=Path(fixture.runtime_dir)), \
             mock.patch.object(supervisor.work_dispatcher_codex, "parse_codex_jsonl", side_effect=AssertionError("manifest owns transport")), \
             mock.patch.object(verifier, "parse_appserver_journal", side_effect=AssertionError("refuse tampered binding")):
            refused = supervisor._scan_gateway_jobs("", {}, 10)["observations"][0]
        self.assertEqual(refused["error_code"], "app_server_transport_mismatch")

    def test_human_stdout_never_substitutes_missing_journal(self):
        self.write(); (self.directory / "appserver.rpc.jsonl").unlink()
        self.assertEqual(self.observe()["error_code"], "rpc_journal_missing")

    def test_safe_projection_discards_arbitrary_private_fields(self):
        result = supervisor._safe_appserver_evidence({"transport": "codex_app_server", "input_set_uncertain": True,
            "spec_digest": "a" * 64, "shutdown_code": "secret arbitrary text", "connection_id": CONNECTION,
            "text": "private steer text", "rpc_id": 42})
        self.assertEqual(result, {"transport": "codex_app_server", "input_set_uncertain": True, "spec_digest": "a" * 64})


if __name__ == "__main__":
    unittest.main()
