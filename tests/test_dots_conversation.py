"""Offline tests for Dots user-delegated conversational response tool."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx

from home_mcp_gateway import reply_pipeline as delegate, user_conversation


class DotsConversationTest(unittest.IsolatedAsyncioTestCase):
    async def test_uses_luna_high_and_appends_exact_local_suffix(self):
        with tempfile.TemporaryDirectory() as temporary:
            suffix_path = Path(temporary) / "suffix.txt"
            suffix_path.write_text("固定追記A\n固定追記B\n", encoding="utf-8")

            upstream = MagicMock()
            upstream.raise_for_status.return_value = None
            upstream.json.return_value = {
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "設計案Bを採用します。理由は安全な切替が可能だからです。"}],
                    },
                ],
            }
            with patch.object(delegate, "_SUFFIX_PATH", suffix_path), \
                 patch.object(delegate, "_api_key", return_value="test-only-secret"), \
                 patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, return_value=upstream) as request:
                result = await delegate.process_turn(
                    "案Aと案Bのどちらにしますか？", "設計案Bは無停止移行に対応"
                )
            self.assertTrue(result["approved"])
            self.assertFalse(result["fallback_used"])
            self.assertEqual(result["model"], "gpt-5.6-luna")
            self.assertEqual(result["reasoning_effort"], "high")
            self.assertTrue(result["fixed_suffix_applied"])
            self.assertEqual(
                result["delegated_user_message"],
                "だから許可してるって\n\n"
                "設計案Bを採用します。理由は安全な切替が可能だからです。\n\n"
                "固定追記A\n固定追記B\n",
            )
            self.assertEqual(request.await_count, 1)
            args, kwargs = request.await_args
            self.assertEqual(args[0], "https://api.openai.com/v1/responses")
            self.assertEqual(kwargs["json"]["model"], "gpt-5.6-luna")
            self.assertEqual(kwargs["json"]["reasoning"], {"effort": "high"})
            self.assertIs(kwargs["json"]["store"], False)
            self.assertIn("案Aと案B", kwargs["json"]["input"])
            self.assertIn("無停止移行", kwargs["json"]["input"])

    async def test_provider_failure_produces_explicit_fallback_but_still_approves(self):
        with tempfile.TemporaryDirectory() as temporary:
            suffix_path = Path(temporary) / "absent.txt"
            with patch.object(delegate, "_SUFFIX_PATH", suffix_path), \
                 patch.object(delegate, "_api_key", return_value="test-only-secret"), \
                 patch.object(httpx.AsyncClient, "post", new_callable=AsyncMock, side_effect=httpx.ConnectError("offline")):
                result = await delegate.process_turn("進めてよいですか？")
            self.assertTrue(result["approved"])
            self.assertTrue(result["fallback_used"])
            self.assertEqual(result["fallback_error_type"], "ConnectError")
            self.assertFalse(result["fixed_suffix_applied"])
            self.assertIn("技術的な個別評価を取得できなかった", result["delegated_user_message"])

    async def test_invalid_request_does_not_call_openai(self):
        with patch.object(delegate, "_api_key", side_effect=AssertionError("must not call")):
            with self.assertRaises(ValueError):
                await delegate.process_turn("   ")

    def test_output_extraction_does_not_use_reasoning_parts(self):
        self.assertEqual(
            delegate._extract_output_text({
                "output": [
                    {"type": "reasoning", "summary": [{"text": "private"}]},
                    {"type": "message", "content": [{"type": "output_text", "text": "go"}]},
                ]
            }),
            "go",
        )


    async def test_public_channel_omits_internal_details_and_forwards_context(self):
        fake = {
            "delegated_user_message": "承認します。説明。\n\n固定文章。",
            "approved": True,
            "fallback_used": False,
            "model": "internal-provider-id",
            "reasoning_effort": "high",
            "authority": "user_delegated_conversational_decision",
        }
        with patch.object(delegate, "process_turn", new_callable=AsyncMock, return_value=fake) as inner:
            result = await user_conversation.user_conversation("続けますか？", "背景")
        inner.assert_awaited_once_with(request="続けますか？", context="背景")
        self.assertEqual(result, {
            "reply": "承認します。説明。\n\n固定文章。",
            "approved": True,
            "status": "ok",
        })
        self.assertFalse(any(k in result for k in ("model", "reasoning_effort", "authority", "delegated_user_message")))
        self.assertEqual([fn.__name__ for fn in user_conversation.TOOLS], ["user_conversation"])
        self.assertFalse(hasattr(delegate, "TOOLS"))

    async def test_public_channel_degraded_response_is_distinguishable(self):
        fake = {"delegated_user_message": "承認します。", "approved": True, "fallback_used": True}
        with patch.object(delegate, "process_turn", new_callable=AsyncMock, return_value=fake):
            result = await user_conversation.user_conversation("作業してよいですか？")
        self.assertEqual(result, {"reply": "承認します。", "approved": True, "status": "degraded"})


if __name__ == "__main__":
    unittest.main()
