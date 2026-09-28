import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest import mock

import httpx

from home_mcp_gateway import browser_agent, browser_qa, decision_receipt, jev as jev_module
from home_mcp_gateway.browser_agent import (
    BrowserAgentError,
    _jev_safe_url,
    execute_browser_plan,
    validate_steps,
)
from home_mcp_gateway.jev import (
    JevChoice,
    JevDecisionClient,
    JevError,
    validate_host_choice,
)
from home_mcp_gateway.decision_receipt import (
    create_decision_receipt,
    decision_digest,
    finalize_decision_receipt,
    recent_decision_receipts,
    record_decision_receipt,
    summarize_decision_receipts,
)
from qa_test_support import local_server


class FakeJev:
    model = "jev-test"
    input_tokens = 0
    output_tokens = 0

    def __init__(self):
        self.attempts = 0
        self.seen = []

    async def choose(
        self,
        *,
        state,
        instruction,
        candidates,
        decision_kind="bounded_candidate",
        candidate_metadata=None,
    ):
        self.attempts += 1
        self.seen.append(
            {
                "state": state,
                "instruction": instruction,
                "candidates": candidates,
                "decision_kind": decision_kind,
                "candidate_metadata": dict(candidate_metadata or {}),
            }
        )
        target = instruction.rsplit(":", 1)[-1].strip().casefold()
        for key, item in candidates.items():
            haystack = " ".join(
                str(item.get(name, ""))
                for name in ("label", "text", "name", "placeholder")
            ).casefold()
            if target in haystack:
                return SimpleNamespace(
                    choice=key,
                    confidence=0.99,
                    probabilities={key: 0.99, "none": 0.01},
                    model=self.model,
                )
        return SimpleNamespace(
            choice="none",
            confidence=0.99,
            probabilities={"none": 0.99},
            model=self.model,
        )


class JevCredentialTests(unittest.TestCase):
    def test_gateway_owns_jev_credential_resolution(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / ".env").write_text("JEV_API_KEY=gateway-test-key\n", encoding="utf-8")
            external = root / "external.env"
            external.write_text("JEV_API_KEY=external-test-key\n", encoding="utf-8")
            with mock.patch.object(jev_module, "_gateway_root", return_value=root):
                with mock.patch.dict(
                    os.environ,
                    {"JEV_API_KEY": "", "HOME_MCP_JEV_ENV_FILE": str(external)},
                    clear=False,
                ):
                    self.assertEqual(jev_module.load_jev_api_key(), "gateway-test-key")

                (root / ".env").write_text("", encoding="utf-8")
                with mock.patch.dict(
                    os.environ,
                    {"JEV_API_KEY": "", "HOME_MCP_JEV_ENV_FILE": str(external)},
                    clear=False,
                ):
                    with self.assertRaises(JevError) as caught:
                        jev_module.load_jev_api_key()
                    self.assertEqual(caught.exception.code, "jev_credential_missing")


class JevClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_choice_contract_and_closed_candidate_set(self):
        captured = {}

        async def handle(request):
            payload = json.loads(request.content)
            captured["payload"] = payload
            captured["authorization"] = request.headers.get("authorization")
            options = payload["questions"]["target"]["criteria"]
            probabilities = {key: 0.0 for key in options}
            probabilities["e2"] = 1.0
            return httpx.Response(
                200,
                json={
                    "model": "jev-1.13.0",
                    "answers": {
                        "target": {
                            "type": "choice",
                            "choice": "e2",
                            "confidence": 0.98,
                            "probabilities": probabilities,
                        }
                    },
                    "usage": {"input_tokens": 31, "output_tokens": 7},
                },
            )

        client = JevDecisionClient(
            api_key="unit-test-key",
            transport=httpx.MockTransport(handle),
        )
        async with client:
            result = await client.choose(
                state={"url": "https://example.test/page", "title": "Fixture"},
                instruction="Operation: click. Target: Apply",
                candidates={
                    "e1": {"label": "Cancel"},
                    "e2": {"label": "Apply"},
                },
            )
        self.assertEqual(result.choice, "e2")
        self.assertEqual(result.confidence, 0.98)
        self.assertEqual(result.receipt.selected_candidate, "e2")
        self.assertEqual(result.receipt.probabilities["e2"], 1.0)
        self.assertEqual(result.receipt.outcome, "pending")
        self.assertEqual(result.receipt.host_action, "")
        self.assertEqual(client.input_tokens, 31)
        self.assertEqual(client.output_tokens, 7)
        self.assertEqual(captured["authorization"], "Bearer unit-test-key")
        self.assertNotIn("unit-test-key", json.dumps(captured["payload"]))
        self.assertEqual(
            set(captured["payload"]["questions"]["target"]["criteria"]),
            {"e1", "e2", "none"},
        )


class HostChoiceValidationTests(unittest.TestCase):
    def test_host_revalidation_rejects_stale_and_low_confidence_choice(self):
        choice = JevChoice(
            choice="e1",
            confidence=0.91,
            probabilities={"e1": 0.91, "none": 0.09},
            model="jev-1.13.0",
            decision_kind="tool_pack",
        )
        valid = validate_host_choice(
            choice, {"e1": {"label": "Allowed"}}, minimum_confidence=0.8
        )
        self.assertTrue(valid.valid)
        self.assertEqual(valid.status, "validated")

        stale = validate_host_choice(
            choice, {"e2": {"label": "Changed"}}, minimum_confidence=0.8
        )
        self.assertFalse(stale.valid)
        self.assertEqual(stale.status, "stale")

        low = validate_host_choice(
            JevChoice(
                choice="e1",
                confidence=0.2,
                probabilities={"e1": 0.2, "none": 0.8},
                model="jev-1.13.0",
            ),
            {"e1": {}},
            minimum_confidence=0.8,
        )
        self.assertFalse(low.valid)
        self.assertEqual(low.status, "low_confidence")


class BrowserPlanValidationTests(unittest.TestCase):
    def test_plan_validation_and_safe_url_projection(self):
        steps = validate_steps(
            [
                {"action": "type", "instruction": "Name", "value": "Aoi"},
                {"action": "click", "instruction": "Apply"},
            ]
        )
        self.assertEqual([step.action for step in steps], ["type", "click"])
        with self.assertRaises(BrowserAgentError):
            validate_steps([{"action": "type", "instruction": "Name"}])
        self.assertEqual(
            _jev_safe_url("https://example.test/path?q=secret#fragment"),
            "https://example.test/path",
        )
        self.assertIn(browser_qa.browser_run_plan, browser_qa.TOOLS)


class DecisionReceiptTests(unittest.TestCase):
    def test_credential_shaped_ids_and_statuses_are_not_retained(self):
        environment = {
            "TEST_API_KEY": "receipt-key-synthetic",
            "TEST_AUTH_TOKEN": "receipt-token-synthetic",
            "TEST_CLIENT_SECRET": "receipt-secret-synthetic",
            "TEST_PASSWORD": "receipt-password-synthetic",
        }
        explicit = "receipt-explicit-synthetic"
        secrets = [*environment.values(), explicit]
        with mock.patch.dict(os.environ, environment):
            receipt = create_decision_receipt(
                decision_kind=environment["TEST_API_KEY"], state={},
                candidates={secret: {} for secret in secrets},
                selected_candidate=explicit,
                probabilities={secret: 0.2 for secret in secrets},
                engine=explicit, fallback_reason=explicit, validation_result=explicit,
                sensitive_values=(explicit,),
            )
            receipt = finalize_decision_receipt(
                receipt, host_candidate=explicit, host_action=explicit,
                outcome=explicit, validation_result=explicit,
                sensitive_values=(explicit,),
            )
        encoded = json.dumps(receipt.to_dict())
        for secret in secrets:
            self.assertNotIn(secret, encoded)
        self.assertEqual(receipt.host_candidate, receipt.selected_candidate)
        self.assertEqual(len(receipt.probabilities), 5)
        self.assertEqual(receipt.decision_kind, "")
        self.assertEqual(receipt.host_action, "")
        self.assertEqual(receipt.outcome, "")

    def test_receipt_omits_raw_content_and_keeps_only_closed_set_distribution(self):
        receipt = create_decision_receipt(
            decision_kind="browser_dom", state={"secret": "private-state"},
            candidates={"e1": {"label": "private-label"}, "personal name": {}},
            selected_candidate="e1", probabilities={"e1": 0.8, "none": 0.2, "private-key": 0.1},
            confidence=0.9, threshold=0.5,
            fallback_reason="error contains private text",
        )
        encoded = json.dumps(receipt.to_dict())
        for secret in ("private-state", "private-label", "personal name", "private-key", "private text"):
            self.assertNotIn(secret, encoded)
        self.assertEqual(receipt.probabilities, {"e1": 0.8, "none": 0.2})
        self.assertEqual(receipt.selected_candidate, "e1")
        self.assertEqual(receipt.outcome, "pending")
        self.assertEqual(decision_digest({"b": 2, "a": 1}), decision_digest({"a": 1, "b": 2}))
        self.assertNotEqual(decision_digest({"a": 1}), decision_digest({"a": 2}))
        updated = finalize_decision_receipt(
            receipt, host_action="click", host_candidate="personal name", outcome="failed",
            validation_result="postcondition_failed",
        )
        self.assertEqual(updated.selected_candidate, "e1")
        self.assertEqual(updated.host_candidate, receipt.candidate_ids[1])
        self.assertEqual(updated.decision_id, receipt.decision_id)
        self.assertNotIn("personal name", json.dumps(updated.to_dict()))

    def test_latest_receipts_and_summary_are_bounded_and_count_decisions_once(self):
        with mock.patch.object(decision_receipt, "_RECENT_RECEIPTS", decision_receipt.OrderedDict()), \
                mock.patch.object(decision_receipt, "MAX_RECENT_RECEIPTS", 2):
            first = create_decision_receipt(decision_kind="browser_dom", state={}, candidates={"e1": {}})
            record_decision_receipt(first)
            final = finalize_decision_receipt(first, outcome="succeeded", host_action="click", host_candidate="e1")
            self.assertEqual(len(recent_decision_receipts()), 1)
            self.assertEqual(summarize_decision_receipts([first, final])["outcomes"], {"succeeded": 1})
            for _ in range(2):
                record_decision_receipt(create_decision_receipt(decision_kind="browser_dom", state={}, candidates={"e1": {}}))
            self.assertEqual(summarize_decision_receipts()["count"], 2)
            self.assertNotIn(first.decision_id, {row["decision_id"] for row in recent_decision_receipts()})
            self.assertEqual(recent_decision_receipts(0), [])


class BrowserReceiptLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.session = {"page": object()}
        self.choice = JevChoice("e1", 0.9, {"e1": 0.9, "none": 0.1}, "jev-test")
        self.client = SimpleNamespace(choose=mock.AsyncMock(return_value=self.choice))
        self.observe = mock.AsyncMock(return_value=({"action": "click"}, {"e1": {"label": "Apply"}}, {"e1": object()}))
        self.act = mock.AsyncMock(return_value=self.session["page"])
        self.verify = mock.AsyncMock(return_value={})
        for name, replacement in (
            ("_observe_candidates", self.observe), ("_act_on_target", self.act),
            ("_verify", self.verify), ("_page_snapshot", mock.AsyncMock(return_value={})),
        ):
            patcher = mock.patch.object(browser_agent, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def run_plan(self):
        return await execute_browser_plan(
            self.session, [{"action": "click", "instruction": "Apply", "expected_text": "done"}],
            decision_client=self.client,
        )

    @staticmethod
    def receipts(result):
        return [event["receipt"] for event in result["trace"] if event["event"] == "decision"]

    async def test_action_success_is_finalized_only_after_postcondition(self):
        self.verify.side_effect = BrowserAgentError("browser_expectation_failed")
        result = await self.run_plan()
        receipt, = self.receipts(result)
        self.act.assert_awaited_once()
        self.assertEqual(receipt["selected_candidate"], "e1")
        self.assertEqual(receipt["host_candidate"], "e1")
        self.assertEqual(receipt["host_action"], "click")
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["validation_result"], "browser_expectation_failed")
        self.assertEqual(result["status"], "failed")

    async def test_rejected_choices_never_act(self):
        for selected, confidence, validation, outcome in (
            ("e1", 0.1, "low_confidence", "rejected"),
            ("outside", 0.9, "stale", "rejected"),
            ("none", 0.9, "abstained", "abstained"),
        ):
            with self.subTest(selected=selected):
                self.client.choose.return_value = JevChoice(selected, confidence, {selected: confidence}, "jev-test")
                receipt, = self.receipts(await self.run_plan())
                self.assertEqual(receipt["validation_result"], validation)
                self.assertEqual(receipt["outcome"], outcome)
                self.assertEqual(receipt["host_action"], "")
        self.act.assert_not_awaited()

    async def test_stale_attempt_and_successful_retry_have_distinct_receipts(self):
        self.act.side_effect = [BrowserAgentError("browser_stale_element"), self.session["page"]]
        result = await self.run_plan()
        first, second = self.receipts(result)
        self.assertEqual([first["outcome"], second["outcome"]], ["stale", "succeeded"])
        self.assertNotEqual(first["decision_id"], second["decision_id"])
        self.assertEqual(second["validation_result"], "postcondition_verified")
        self.assertEqual(self.client.choose.await_count, 2)

    async def test_missing_current_target_does_not_execute(self):
        self.observe.return_value = ({}, {"e1": {}}, {})
        receipt, = self.receipts(await self.run_plan())
        self.assertEqual(receipt["outcome"], "stale")
        self.assertEqual(receipt["validation_result"], "target_missing")
        self.assertEqual(receipt["host_action"], "")
        self.act.assert_not_awaited()

    async def test_unexpected_action_error_is_unknown_without_raw_error(self):
        self.act.side_effect = RuntimeError("private exception text")
        receipt, = self.receipts(await self.run_plan())
        self.assertEqual(receipt["outcome"], "unknown")
        self.assertNotIn("private exception text", json.dumps(receipt))

    async def test_provider_failure_is_recorded_without_host_action(self):
        self.client.choose.side_effect = JevError("jev_timeout")
        receipt, = self.receipts(await self.run_plan())
        self.assertEqual(receipt["outcome"], "failed")
        self.assertEqual(receipt["fallback_reason"], "jev_timeout")
        self.assertEqual(receipt["host_action"], "")

    async def test_credential_in_candidate_id_is_safe_on_provider_rejection(self):
        secret = "test-explicit-credential-in-id"
        self.observe.return_value = ({}, {secret: {"label": "Apply"}}, {secret: object()})
        transport = mock.Mock(side_effect=AssertionError("Provider must not be called"))
        self.client = JevDecisionClient(api_key=secret, transport=httpx.MockTransport(transport))
        result = await self.run_plan()
        self.assertEqual(result["reason"], "jev_credential_in_payload")
        self.assertNotIn(secret, json.dumps(result["trace"]))
        receipt, = self.receipts(result)
        self.assertTrue(receipt["candidate_ids"][0].startswith("id_"))
        self.assertEqual(receipt["outcome"], "failed")
        transport.assert_not_called()
        self.act.assert_not_awaited()

    async def test_known_credentials_are_redacted_from_successful_decision_trace(self):
        secret = "test-custom-client-credential-in-id"
        self.client.api_key = secret
        self.observe.return_value = ({}, {secret: {"label": secret}}, {secret: object()})
        self.client.choose.return_value = JevChoice(secret, 0.9, {secret: 0.9, "none": 0.1}, "jev-test")
        result = await self.run_plan()
        receipt, = self.receipts(result)
        self.assertEqual(receipt["outcome"], "succeeded")
        self.assertEqual(receipt["selected_candidate"], receipt["host_candidate"])
        self.assertNotIn(secret, json.dumps(result["trace"]))

    async def test_cancelled_action_is_recorded_and_cancellation_propagates(self):
        self.act.side_effect = asyncio.CancelledError()
        before = {row["decision_id"] for row in recent_decision_receipts(2048)}
        with self.assertRaises(asyncio.CancelledError):
            await self.run_plan()
        receipt, = [row for row in recent_decision_receipts(2048) if row["decision_id"] not in before]
        self.assertEqual(receipt["outcome"], "cancelled")
        self.assertEqual(receipt["host_action"], "click")

    async def test_jev_receipt_id_survives_host_validation_and_execution(self):
        receipt = create_decision_receipt(
            decision_kind="browser_dom", state={}, candidates={"e1": {}},
            selected_candidate="e1", confidence=0.9,
        )
        self.client.choose.return_value = JevChoice(
            "e1", 0.9, {"e1": 0.9, "none": 0.1}, "jev-test", receipt=receipt,
        )
        final, = self.receipts(await self.run_plan())
        self.assertEqual(final["decision_id"], receipt.decision_id)
        self.assertEqual(final["outcome"], "succeeded")
        self.assertEqual(final["threshold"], 0.5)


class BrowserAgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_browser_executes_one_complete_semantic_plan(self):
        try:
            started = await browser_qa.browser_start()
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install the browser: python -m playwright install chromium")
            raise
        sid = started["session_id"]
        fake = FakeJev()
        try:
            with local_server() as (url, _):
                async with browser_qa._session(sid) as session:
                    result = await execute_browser_plan(
                        session,
                        [
                            {
                                "action": "type",
                                "instruction": "Name",
                                "value": "Gateway JEV",
                            },
                            {"action": "click", "instruction": "Apply", "expected_text": "Gateway JEV"},
                            {"action": "read"},
                        ],
                        start_url=url + "/?token=must-not-reach-jev#fragment",
                        completion_text="Gateway JEV",
                        decision_client=fake,
                    )
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["completion_verified"])
            self.assertTrue(result["observation"]["text"].endswith("Gateway JEV"))
            self.assertEqual(fake.attempts, 2)
            receipts = [event["receipt"] for event in result["trace"] if event["event"] == "decision"]
            self.assertEqual([row["outcome"] for row in receipts], ["succeeded", "succeeded"])
            self.assertEqual([row["host_action"] for row in receipts], ["type", "click"])
            self.assertEqual(receipts[-1]["validation_result"], "postcondition_verified")
            self.assertNotIn("Gateway JEV", json.dumps(receipts))
            sent = json.dumps(fake.seen, ensure_ascii=False)
            self.assertNotIn("Gateway JEV", sent)
            self.assertNotIn("must-not-reach-jev", sent)
        finally:
            await browser_qa.browser_close(sid)


    async def test_select_rechecks_actionability_after_decision(self):
        try:
            started = await browser_qa.browser_start()
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install the browser: python -m playwright install chromium")
            raise
        sid = started["session_id"]
        try:
            async with browser_qa._session(sid) as session:
                await session["page"].set_content(
                    '<label>Category <select id="category"><option value="all">All</option>'
                    '<option value="books">Books</option></select></label>'
                )
                fake = FakeJev()
                choose = fake.choose

                async def disable_after_choice(**kwargs):
                    choice = await choose(**kwargs)
                    await session["page"].locator("#category").evaluate("e => e.disabled = true")
                    return choice

                fake.choose = disable_after_choice
                result = await execute_browser_plan(
                    session, [{"action": "select", "instruction": "Category", "value": "Books"}],
                    decision_client=fake,
                )
                self.assertEqual(await session["page"].locator("#category").input_value(), "all")
            self.assertEqual(result["reason"], "browser_element_not_actionable")
            receipt, = BrowserReceiptLifecycleTests.receipts(result)
            self.assertEqual(receipt["outcome"], "failed")
            self.assertEqual(receipt["validation_result"], "browser_element_not_actionable")
        finally:
            await browser_qa.browser_close(sid)


    async def test_select_check_and_click_use_native_controls(self):
        try:
            started = await browser_qa.browser_start()
        except RuntimeError as exc:
            if "Executable doesn't exist" in str(exc):
                self.skipTest("Install the browser: python -m playwright install chromium")
            raise
        sid = started["session_id"]
        fake = FakeJev()
        try:
            async with browser_qa._session(sid) as session:
                await session["page"].set_content(
                    """<!doctype html><title>Controls</title>
                    <label>Category <select id="category">
                      <option value="all">All</option>
                      <option value="books">Books</option>
                    </select></label>
                    <label><input id="stock" type="checkbox">In stock</label>
                    <button onclick="document.querySelector('#result').textContent =
                      document.querySelector('#category').value + '|' +
                      document.querySelector('#stock').checked">Apply</button>
                    <p id="result">waiting</p>"""
                )
                result = await execute_browser_plan(
                    session,
                    [
                        {
                            "action": "select",
                            "instruction": "Category",
                            "value": "Books",
                        },
                        {"action": "check", "instruction": "In stock"},
                        {"action": "click", "instruction": "Apply"},
                    ],
                    completion_text="books|true",
                    decision_client=fake,
                )
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["completion_verified"])
            self.assertEqual(fake.attempts, 3)
        finally:
            await browser_qa.browser_close(sid)



if __name__ == "__main__":
    unittest.main()
