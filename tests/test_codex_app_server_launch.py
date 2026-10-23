"""Offline launch, identity, and dispatcher proof tests; no Codex process is run."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from home_mcp_gateway import jobs, work_dispatcher as wd, work_dispatcher_codex as codex


class AppServerLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.node = self.root / "install" / "node.exe"
        self.entry = self.root / "install" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js"
        self.entry.parent.mkdir(parents=True)
        self.node.write_bytes(b"offline fixture, never executed")
        self.entry.write_text("// offline fixture, never executed", encoding="utf-8")
        self.now = float(int(time.time()))
        self.conn = wd.connect(self.root / "dispatcher")
        self.addCleanup(self.conn.close)
        self.runtime_dir = str(self.root / "runtime")
        self.statuses = {}

        class FakeAdapter:
            runtime_dir = self.runtime_dir

            def status(_self, gateway_job_id):
                return copy.deepcopy(self.statuses.get(gateway_job_id, {}))

        self.dispatcher = wd.Dispatcher(self.conn, object(), now=lambda: self.now, codex_backend=FakeAdapter())

    def item(self, **over):
        result = {"job_id": "one", "route": "gateway-codex", "authorization_ref": "root:offline-approved",
                  "model": "gpt-offline", "effort": "high", "message": "Inspect the offline fixture.\nReport findings.",
                  "node_executable": str(self.node.resolve()), "codex_entrypoint": str(self.entry.resolve())}
        result.update(over)
        return result

    def manifest(self, item=None):
        return json.dumps({"version": 2, "manifest_id": "offline-appserver", "root_source_id": "root-offline",
                           "root_epoch": 7, "created_at": wd.iso(self.now - 10), "expires_at": wd.iso(self.now + 7200),
                           "max_concurrency": 1, "uncertain_retry_budget": 1,
                           "jobs": [item or self.item(transport=codex.APP_SERVER_TRANSPORT)]},
                          ensure_ascii=False).encode("utf-8")

    def reserve(self, *, enabled=True):
        raw = self.manifest()
        wd.arm(self.conn, raw, hashlib.sha256(raw).hexdigest(), self.now + 3600, self.now)
        manifest = self.conn.execute("SELECT * FROM manifests").fetchone()
        with mock.patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "1" if enabled else "0"}):
            key = self.dispatcher._reserve_create(manifest)
        if key is None:
            return None
        execution = self.conn.execute("SELECT * FROM codex_execution").fetchone()
        binding = self.dispatcher._codex_binding(execution)
        request = json.loads(self.conn.execute("SELECT create_json FROM jobs").fetchone()[0])
        launch = {"command": request["command"], "cwd": request["cwd"], "idempotency_key": key,
                  "dispatcher_request_fingerprint": execution["request_fingerprint"],
                  "explicit_env_digest": jobs._explicit_env_digest({}), "transport": request["transport"],
                  "app_server": request["app_server"]}
        launch["request_fingerprint"] = jobs._keyed_fingerprint(
            request["command"], request["cwd"], {}, execution["request_fingerprint"], binding,
            transport=request["transport"], app_server=request["app_server"])
        directory = jobs._directory(binding["gateway_job_id"], self.runtime_dir)
        directory.mkdir(parents=True)
        metadata = {**launch, "job_id": binding["gateway_job_id"], "dispatcher_binding": binding,
                    "status": "running", "exit_code": None, "process_identity": "alive", "worker_identity": "alive",
                    "stdout_path": str(directory / "stdout.log"), "stderr_path": str(directory / "stderr.log"),
                    "app_server_runtime": {"journal_path": str(directory / "appserver.rpc.jsonl"),
                                           "thread_id": "private-thread", "turn_id": "private-turn"}}
        return binding, request, launch, metadata, directory

    def mark_running(self, binding):
        self.conn.execute("UPDATE jobs SET state='running', maybe_executing=1")
        self.conn.execute("UPDATE codex_execution SET state='running'")
        self.conn.execute("UPDATE ops SET state='receipted', attempts=1, receipt_json=?",
                          (wd.canonical({"gateway_job_id": binding["gateway_job_id"], "status": "running"}),))

    def test_transport_is_opt_in_and_legacy_request_and_fingerprint_are_unchanged(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(codex.app_server_enabled())
            compiled = wd.parse_manifest(self.manifest(self.item()))["jobs"][0]
        request = compiled["create_request"]
        expected = {"command": [str(self.node.resolve()), str(self.entry.resolve()), "exec", "-m", "gpt-offline", "-c",
                                'model_reasoning_effort="high"', "--json", self.item()["message"]],
                    "cwd": str(Path(wd.__file__).resolve().parents[1]), "model": "gpt-offline", "effort": "high"}
        self.assertEqual(wd.canonical(request), wd.canonical(expected))
        payload = {"command": {"argv": expected["command"]}, "cwd": expected["cwd"],
                   "env_sha256": jobs._explicit_env_digest({}), "request_fingerprint": "f" * 64, "dispatcher_binding": None}
        expected_digest = hashlib.sha256(wd.canonical(payload).encode()).hexdigest()
        self.assertEqual(jobs._keyed_fingerprint(expected["command"], expected["cwd"], {}, "f" * 64, None), expected_digest)

    def test_manifest_pins_complete_spec_and_rejects_arbitrary_configuration(self):
        compiled = wd.parse_manifest(self.manifest())["jobs"][0]
        request = compiled["create_request"]
        self.assertEqual(request["command"], [str(self.node.resolve()), str(self.entry.resolve()), "app-server"])
        self.assertEqual(set(request["app_server"]), codex.APP_SERVER_SPEC_KEYS)
        self.assertEqual(request["app_server"], {"model": "gpt-offline", "model_provider": "openai", "effort": "high",
                         "input_text": self.item()["message"], "cwd": request["cwd"], "sandbox": "read-only",
                         "approval_policy": "never", "approvals_reviewer": "user"})
        self.assertEqual(wd.parse_manifest(self.manifest(self.item(transport=codex.APP_SERVER_TRANSPORT,
                                                                  app_server=request["app_server"]))) ["jobs"][0]["create_request"], request)
        for over in ({"transport": "exec"}, {"transport": "app-server"}, {"transport": ""},
                     {"app_server": request["app_server"]},
                     {"transport": codex.APP_SERVER_TRANSPORT, "app_server": {}},
                     {"transport": codex.APP_SERVER_TRANSPORT, "app_server": {**request["app_server"], "config": {}}},
                     {"transport": codex.APP_SERVER_TRANSPORT, "app_server": {**request["app_server"], "model_provider": "other"}},
                     {"transport": codex.APP_SERVER_TRANSPORT, "app_server": {**request["app_server"], "input_text": "different"}}):
            with self.subTest(over=over), self.assertRaises(wd.DispatchError):
                wd.parse_manifest(self.manifest(self.item(**over)))

    def test_command_validation_rejects_extra_flags_and_nonofficial_entrypoint(self):
        command = [str(self.node.resolve()), str(self.entry.resolve()), "app-server"]
        self.assertEqual(codex.validate_app_server_command(command), command)
        for candidate in (command + ["--listen", "stdio://"], command[:-1] + ["exec"], " ".join(command),
                          [str(self.node.resolve()), str(self.node.resolve()), "app-server"]):
            with self.subTest(candidate=candidate), self.assertRaises((ValueError, OSError)):
                codex.validate_app_server_command(candidate)

    def test_disabled_dispatcher_reserves_nothing(self):
        self.assertIsNone(self.reserve(enabled=False))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM ops").fetchone()[0], 0)
        self.assertEqual(self.conn.execute("SELECT state FROM jobs").fetchone()[0], "queued")

    def test_prefire_and_worker_launch_recheck_feature_and_pinned_spec(self):
        binding, request, launch, _, _ = self.reserve()
        with mock.patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "1"}):
            self.assertEqual(codex.worker_launch_guard(binding, launch), "open")
            modified = copy.deepcopy(launch)
            modified["app_server"]["input_text"] = "tampered"
            self.assertEqual(codex.worker_launch_guard(binding, modified), "mismatch")
            self.assertEqual(codex.worker_launch_guard(binding, {**launch, "rpc": "thread/resume"}), "mismatch")
        with mock.patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "0"}):
            self.assertEqual(codex.worker_launch_guard(binding, launch), "closed")
            op = self.conn.execute("SELECT * FROM ops").fetchone()
            self.assertFalse(self.dispatcher._prefire(op))
        self.assertEqual(self.conn.execute("SELECT state FROM codex_execution").fetchone()[0], "not_launched")
        self.assertEqual(self.conn.execute("SELECT error_code FROM ops").fetchone()[0], "app_server_disabled")

    def test_adapter_passes_transport_only_for_opted_in_request(self):
        request = wd.parse_manifest(self.manifest())["jobs"][0]["create_request"]
        with mock.patch.object(jobs, "job_start", return_value={"ok": True}) as start:
            adapter = codex.GatewayJobAdapter(self.runtime_dir)
            adapter.start(request, "key", "f" * 64, {"binding": True})
            self.assertEqual(start.call_args.kwargs["transport"], codex.APP_SERVER_TRANSPORT)
            self.assertEqual(start.call_args.kwargs["app_server"], request["app_server"])
            legacy = wd.parse_manifest(self.manifest(self.item()))["jobs"][0]["create_request"]
            adapter.start(legacy, "key", "f" * 64, {"binding": True})
            self.assertNotIn("transport", start.call_args.kwargs)
            self.assertNotIn("app_server", start.call_args.kwargs)

    def test_adapter_provenance_read_failure_becomes_fatal_private_status(self):
        binding, _, _, metadata, _ = self.reserve()
        public = {key: value for key, value in metadata.items() if key not in {"app_server", "app_server_runtime"}}
        with mock.patch.object(jobs, "job_status", return_value=public), \
                mock.patch.object(jobs, "_read", return_value=metadata), \
                mock.patch("home_mcp_gateway.job_steering.steer_evidence", side_effect=ValueError("malformed fixture")):
            result = codex.GatewayJobAdapter(self.runtime_dir).status(binding["gateway_job_id"])
        self.assertEqual(result["error_code"], "appserver_steer_evidence_unavailable")
        self.assertEqual(result["app_server"], metadata["app_server"])

    def test_steer_ignores_arm_lease_and_checks_running_without_mutation(self):
        binding, _, _, metadata, _ = self.reserve()
        self.mark_running(binding)
        self.conn.execute("UPDATE manifests SET arm_expires_at=?", (self.now - 1,))
        before = list(self.conn.iterdump())
        with mock.patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "1"}):
            self.assertEqual(codex.steer_guard(binding, metadata), "open")
            self.assertEqual(codex.steer_guard(binding, {**metadata, "job_id": "different"}), "mismatch")
            modified = copy.deepcopy(metadata)
            modified["app_server"]["model"] = "different"
            self.assertEqual(codex.steer_guard(binding, modified), "mismatch")
            self.assertEqual(codex.steer_guard(binding, {**metadata, "status": "exited"}), "closed")
        self.assertEqual(list(self.conn.iterdump()), before)

    def test_terminal_receipt_survives_disarm_expiry_and_disabled_flag(self):
        binding, _, _, metadata, _ = self.reserve()
        self.mark_running(binding)
        wd.disarm(self.conn, binding["manifest_id"], self.now)
        metadata["status"] = "exited"
        self.conn.execute("UPDATE jobs SET state='succeeded'")
        self.conn.execute("UPDATE codex_execution SET state='succeeded'")
        with mock.patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "0"}), \
                mock.patch.object(codex.time, "time", return_value=self.now + 99999):
            self.assertEqual(codex.steer_guard(binding, metadata), "closed")
            self.assertEqual(codex.steer_guard(binding, metadata, require_running=False), "open")
        self.conn.execute("UPDATE roots SET max_epoch=max_epoch+1")
        self.assertEqual(codex.steer_guard(binding, metadata, require_running=False), "closed")

    def test_steer_rejects_changed_manifest_generation_and_pinned_operation(self):
        binding, _, _, metadata, _ = self.reserve()
        self.mark_running(binding)
        with mock.patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "1"}):
            self.conn.execute("UPDATE manifests SET generation=generation+1")
            self.assertEqual(codex.steer_guard(binding, metadata), "closed")
            self.conn.execute("UPDATE manifests SET generation=generation-1")
            self.conn.execute("UPDATE ops SET request_json='{}'")
            self.assertEqual(codex.steer_guard(binding, metadata), "mismatch")

    def test_reconcile_uses_native_journal_and_terminal_proof_despite_child_exit(self):
        binding, request, _, metadata, directory = self.reserve()
        self.mark_running(binding)
        (directory / "stdout.log").write_bytes(b"not an exec journal")
        journal = b"offline native journal fixture\n"
        (directory / "appserver.rpc.jsonl").write_bytes(journal)
        metadata.update(status="exited", exit_code=37, process_identity="missing", worker_identity="missing")
        metadata["app_server_runtime"]["shutdown_code"] = "appserver_shutdown_forced"
        metadata["steer_evidence"] = {"input_set_uncertain": True}
        self.statuses[binding["gateway_job_id"]] = metadata
        parsed = {"state": "succeeded", "thread_id": "private-thread", "turn_id": "private-turn", "result_text": "offline final",
                  "result_digest": hashlib.sha256(b"offline final").hexdigest(), "result_chars": 13,
                  "log_digest": hashlib.sha256(journal).hexdigest(), "log_bytes": len(journal), "input_set_uncertain": True,
                  "native_outcome": "completed", "spec_digest": "a" * 64, "native_terminal_digest": "b" * 64,
                  "steer_evidence_digest": "c" * 64, "evidence_digest": "d" * 64}
        with mock.patch("home_mcp_gateway.codex_app_server.parse_appserver_journal", return_value=parsed) as parse:
            self.dispatcher._reconcile_codex()
        self.assertEqual(parse.call_args.args, (journal, request["app_server"]))
        self.assertEqual(parse.call_args.kwargs["expected_thread_id"], "private-thread")
        self.assertEqual(self.conn.execute("SELECT state FROM jobs").fetchone()[0], "succeeded")
        execution = self.conn.execute("SELECT * FROM codex_execution").fetchone()
        self.assertTrue(execution["result_ref"].endswith("appserver.rpc.jsonl"))
        evidence = json.loads(execution["app_server_evidence_json"])
        self.assertTrue(evidence["input_set_uncertain"])
        self.assertEqual(evidence["shutdown_code"], "appserver_shutdown_forced")
        public = wd.build_status(self.conn, self.now)["manifests"][0]["jobs"][0]["execution"]
        self.assertEqual(public["thread_id"], "private-thread")
        self.assertEqual(public["app_server_evidence"]["turn_id"], "private-turn")
        self.assertNotIn("input_text", json.dumps(public))
        self.assertNotIn("connection_id", json.dumps(public))
        self.assertNotIn(self.item()["message"], json.dumps(public))
        self.assertTrue(public["app_server_evidence"]["input_set_uncertain"])

    def test_native_failed_or_pending_proof_cannot_succeed(self):
        binding, _, _, metadata, directory = self.reserve()
        self.mark_running(binding)
        (directory / "appserver.rpc.jsonl").write_bytes(b"offline")
        metadata.update(status="exited", exit_code=0)
        self.statuses[binding["gateway_job_id"]] = metadata
        with mock.patch("home_mcp_gateway.codex_app_server.parse_appserver_journal",
                        return_value={"state": "failed", "code": "turn_interrupted", "native_outcome": "interrupted"}):
            self.dispatcher._reconcile_codex()
        self.assertEqual(self.conn.execute("SELECT state FROM jobs").fetchone()[0], "failed")
        self.conn.execute("UPDATE jobs SET state='running'")
        self.conn.execute("UPDATE codex_execution SET state='running'")
        with mock.patch("home_mcp_gateway.codex_app_server.parse_appserver_journal",
                        return_value={"state": "result_pending", "code": "terminal_missing", "input_set_uncertain": True}):
            self.dispatcher._reconcile_codex()
        self.assertEqual(self.conn.execute("SELECT state FROM jobs").fetchone()[0], "result_pending")

    def test_mutable_job_row_cannot_downgrade_pinned_transport_to_exec_verifier(self):
        binding, request, _, metadata, directory = self.reserve()
        self.mark_running(binding)
        tampered = {key: value for key, value in request.items() if key not in {"transport", "app_server"}}
        self.conn.execute("UPDATE jobs SET create_json=?", (wd.canonical(tampered),))
        metadata = {key: value for key, value in metadata.items() if key not in {"transport", "app_server", "app_server_runtime"}}
        metadata.update(status="exited", exit_code=0,
                        request_fingerprint=jobs._keyed_fingerprint(request["command"], request["cwd"], {},
                                                                    binding["request_fingerprint"], binding))
        (directory / "stdout.log").write_bytes(b"malicious exec result")
        self.statuses[binding["gateway_job_id"]] = metadata
        with mock.patch.object(codex, "parse_codex_jsonl") as exec_parse, \
                mock.patch("home_mcp_gateway.codex_app_server.parse_appserver_journal") as rpc_parse:
            self.dispatcher._reconcile_codex()
        exec_parse.assert_not_called()
        rpc_parse.assert_not_called()
        self.assertEqual(self.conn.execute("SELECT state FROM codex_execution").fetchone()[0], "unknown")

    def test_fatal_runtime_metadata_cannot_use_completed_journal_as_success(self):
        binding, _, _, metadata, directory = self.reserve()
        self.mark_running(binding)
        (directory / "appserver.rpc.jsonl").write_bytes(b"offline completed journal")
        metadata.update(status="exited", exit_code=0)
        parsed = {"state": "succeeded", "thread_id": "thread", "turn_id": "turn", "result_text": "final",
                  "result_digest": "a" * 64, "result_chars": 5, "log_digest": "b" * 64, "log_bytes": 25,
                  "native_outcome": "completed", "input_set_uncertain": False}
        for field, code in (("runtime_error_code", "appserver_evidence_limit"),
                            ("runtime_error_code", "appserver_protocol_invalid"),
                            ("error_code", "appserver_auth_required")):
            with self.subTest(field=field, code=code):
                self.conn.execute("UPDATE jobs SET state='running'")
                self.conn.execute("UPDATE codex_execution SET state='running'")
                attempt = copy.deepcopy(metadata)
                if field == "runtime_error_code":
                    attempt["app_server_runtime"][field] = code
                else:
                    attempt[field] = code
                self.statuses[binding["gateway_job_id"]] = attempt
                with mock.patch("home_mcp_gateway.codex_app_server.parse_appserver_journal", return_value=copy.deepcopy(parsed)):
                    self.dispatcher._reconcile_codex()
                execution = self.conn.execute("SELECT * FROM codex_execution").fetchone()
                self.assertEqual(execution["state"], "failed")
                self.assertEqual(execution["safe_code"], code)
                self.assertTrue(json.loads(execution["app_server_evidence_json"])["input_set_uncertain"])


if __name__ == "__main__":
    unittest.main()
