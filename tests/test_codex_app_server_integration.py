"""Offline contract checks crossing the opt-in and legacy boundaries."""
from __future__ import annotations

import hashlib
import inspect
import os
import unittest
from unittest.mock import patch

from home_mcp_gateway import jobs, work_dispatcher_codex as codex, job_steering
from home_mcp_gateway.codex_app_server import parse_appserver_journal


class LegacyContractTests(unittest.TestCase):
    def test_legacy_argv_fingerprint_and_parser_source_are_unchanged(self):
        argv = codex.make_argv(
            "C:/node.exe", "C:/official/node_modules/@openai/codex/bin/codex.js",
            "synthetic-model", "high", "Synthetic legacy input \u65e5\u672c\u8a9e")
        self.assertEqual(argv[2:], ["exec", "-m", "synthetic-model", "-c",
                                   'model_reasoning_effort="high"', "--json",
                                   "Synthetic legacy input \u65e5\u672c\u8a9e"])
        self.assertEqual(jobs._keyed_fingerprint(argv, "C:/gateway", {}, "a" * 64, None),
                         "cb78ebb9788f41c4367ae6b6ba515a70fc6a978aadf9da0ccc5f2256f13627b9")
        for function, expected in (
            (codex.make_argv, "ee91597432a891fb5cded55132c7f47cc6d5ba114b65c0fbc867372fbcb7b324"),
            (codex.parse_codex_jsonl, "429b9cc319546713e16522dcfa9e2c1063132dfcbcf5bb7a7912bc21202ab4a7"),
        ):
            self.assertEqual(hashlib.sha256(inspect.getsource(function).encode()).hexdigest(), expected)

    def test_feature_requires_explicit_one_and_defaults_off(self):
        for value in (None, "", "0", "false", "true", "yes"):
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["HOME_MCP_CODEX_APP_SERVER_ENABLED"] = value
                self.assertFalse(codex.app_server_enabled())
        with patch.dict(os.environ, {"HOME_MCP_CODEX_APP_SERVER_ENABLED": "1"}, clear=True):
            self.assertTrue(codex.app_server_enabled())

    def test_steer_public_arguments_are_narrow(self):
        self.assertEqual(list(inspect.signature(jobs.job_steer).parameters),
                         ["job_id", "expected_turn_id", "text", "idempotency_key"])
        self.assertEqual(list(inspect.signature(jobs.job_steer_status).parameters),
                         ["job_id", "idempotency_key"])
        self.assertIn(jobs.job_steer, jobs.TOOLS)
        self.assertIn(jobs.job_steer_status, jobs.TOOLS)


class NativeEvidenceIntegrationTests(unittest.TestCase):
    def verify_fake(self, mode, *, request=False):
        # Reuse only the stdio harness; both runtime and verifier remain real.
        from tests import test_codex_app_server_runtime as harness
        fixture = harness.AppServerRuntimeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        metadata = fixture.run_fake(mode, request=request)
        evidence = job_steering.steer_evidence(fixture.directory)
        parsed = parse_appserver_journal(
            (fixture.directory / "appserver.rpc.jsonl").read_bytes(), metadata["app_server"],
            expected_thread_id=metadata["app_server_runtime"]["thread_id"],
            expected_turn_id=metadata["app_server_runtime"]["turn_id"], steer_evidence=evidence)
        return metadata, parsed, fixture

    def test_real_adapter_journal_passes_real_verifier(self):
        metadata, parsed, _ = self.verify_fake("completed")
        self.assertEqual(metadata["status"], "exited")
        self.assertEqual(parsed["state"], "succeeded", parsed)
        self.assertFalse(parsed["input_set_uncertain"])

    def test_benign_account_notifications_preserve_native_success(self):
        metadata, parsed, _ = self.verify_fake("benign_account")
        self.assertEqual(metadata["status"], "exited")
        self.assertEqual(parsed["state"], "succeeded", parsed)

    def test_acceptance_without_recording_has_a_known_input_set(self):
        _, parsed, fixture = self.verify_fake("steer_unrecorded", request=True)
        self.assertEqual(fixture.state()["state"], "accepted_by_runtime")
        self.assertFalse(fixture.state().get("recorded", False))
        self.assertEqual(parsed["state"], "succeeded", parsed)
        self.assertFalse(parsed["input_set_uncertain"])

    def test_exact_native_recording_is_covered_by_evidence(self):
        _, parsed, fixture = self.verify_fake("steer", request=True)
        self.assertTrue(fixture.state()["recorded"])
        self.assertEqual(parsed["state"], "succeeded", parsed)
        self.assertFalse(parsed["input_set_uncertain"])
        _, unrecorded, _ = self.verify_fake("steer_unrecorded", request=True)
        self.assertNotEqual(parsed["steer_evidence_digest"], unrecorded["steer_evidence_digest"])

    def test_connection_closed_with_unanswered_steer_exposes_uncertainty(self):
        _, parsed, fixture = self.verify_fake("missing_steer", request=True)
        self.assertEqual(fixture.state()["state"], "sending")
        self.assertEqual(parsed["state"], "succeeded", parsed)
        self.assertTrue(parsed["input_set_uncertain"])

    def test_forced_shutdown_after_native_completion_remains_success(self):
        metadata, parsed, _ = self.verify_fake("force")
        self.assertEqual(metadata["app_server_runtime"]["shutdown_code"], "appserver_shutdown_forced")
        self.assertEqual(parsed["state"], "succeeded", parsed)

    def test_preterminal_crash_never_verifies_success(self):
        metadata, parsed, _ = self.verify_fake("crash")
        self.assertEqual(metadata["status"], "failed")
        self.assertNotEqual(parsed["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
