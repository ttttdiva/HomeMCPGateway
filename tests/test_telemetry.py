import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from home_mcp_gateway import telemetry


class TelemetryTests(unittest.TestCase):
    def test_instrument_tool_logs_timing_without_sensitive_values(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"HOME_MCP_TOOL_LOG_DIR": td, "HOME_MCP_TOOL_LOG": "1"},
            clear=False,
        ):
            def sample(command: str, path: str, timeout_sec: float = 0) -> dict:
                time.sleep(0.005)
                return {"returncode": 0, "stdout": "secret-output", "duration_sec": 0.005}

            wrapped = telemetry.instrument_tool(sample)
            result = wrapped(command="TOP-SECRET-COMMAND", path=r"D:\safe\file.txt", timeout_sec=5)
            self.assertEqual(result["returncode"], 0)

            files = list(Path(td).glob("*/*.jsonl"))
            self.assertEqual(len(files), 1)
            raw = files[0].read_text(encoding="utf-8")
            event = json.loads(raw.splitlines()[0])
            self.assertEqual(event["tool"], "sample")
            self.assertEqual(event["outcome"], "ok")
            self.assertGreater(event["duration_ms"], 0)
            self.assertEqual(event["duration_ms"], event["tool_duration_ms"])
            # Direct instrumentation has no MCP middleware boundary.  Do not
            # invent a request-received or queue timestamp in that case.
            self.assertIsNone(event["request_received_at"])
            self.assertIsNone(event["queue_wait_ms"])
            self.assertIsNone(event["internal_duration_ms"])
            self.assertEqual(event["tool_started_at"], event["started_at"])
            self.assertEqual(event["args"]["path"], r"D:\safe\file.txt")
            self.assertEqual(event["args"]["command"], {"type": "str", "chars": 18})
            self.assertNotIn("TOP-SECRET-COMMAND", raw)
            self.assertNotIn("secret-output", raw)

    def test_middleware_records_mcp_ids_and_gateway_timing(self):
        class Connection:
            session_id = "transport-session-123"

        class Session:
            # Current MCP ServerSession keeps the transport connection here;
            # it does not expose session_id directly.
            _connection = Connection()

        class Context:
            request_id = 42
            method = "tools/call"
            session = Session()

        async def sample(value: str) -> dict:
            await asyncio.sleep(0.005)
            return {"ok": True, "value": value}

        async def exercise(ctx, wrapped):
            async def call_next(received_ctx):
                self.assertIs(received_ctx, ctx)
                return await wrapped(value="safe")

            return await telemetry.ToolTelemetryMiddleware()(ctx, call_next)

        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"HOME_MCP_TOOL_LOG_DIR": td, "HOME_MCP_TOOL_LOG": "1"},
            clear=False,
        ):
            wrapped = telemetry.instrument_tool(sample)
            result = asyncio.run(exercise(Context(), wrapped))
            self.assertEqual(result, {"ok": True, "value": "safe"})

            files = list(Path(td).glob("*/*.jsonl"))
            self.assertEqual(len(files), 1)
            event = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(event["mcp_request_id"], "42")
            self.assertEqual(event["mcp_method"], "tools/call")
            self.assertEqual(event["mcp_session_id"], "transport-session-123")
            received = telemetry.datetime.fromisoformat(event["request_received_at"])
            started = telemetry.datetime.fromisoformat(event["tool_started_at"])
            self.assertLessEqual(received, started)
            self.assertGreaterEqual(event["queue_wait_ms"], 0)
            self.assertGreater(event["tool_duration_ms"], 0)
            self.assertGreaterEqual(event["internal_duration_ms"], event["tool_duration_ms"])
            self.assertEqual(event["duration_ms"], event["tool_duration_ms"])

    def test_middleware_handles_high_level_context_and_broken_method_property(self):
        # The high-level MCP Context reaches the transport session through
        # ctx.request_context.session, rather than ctx.session directly.
        from mcp.server.mcpserver.context import Context as MCPContext

        class Connection:
            session_id = "nested-session-456"

        class Session:
            # Current SDK ServerSession uses the private connection slot.
            _connection = Connection()

        class RequestContext:
            request_id = 99
            session = Session()

        actual_context = MCPContext(request_context=RequestContext())

        class ContextWithBrokenMethod:
            request_context = actual_context.request_context
            request_id = actual_context.request_id

            @property
            def method(self):
                raise RuntimeError("context method unavailable")

        async def sample() -> dict:
            return {"ok": True}

        async def exercise(wrapped):
            async def call_next(_ctx):
                return await wrapped()

            return await telemetry.ToolTelemetryMiddleware()(ContextWithBrokenMethod(), call_next)

        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"HOME_MCP_TOOL_LOG_DIR": td, "HOME_MCP_TOOL_LOG": "1"},
            clear=False,
        ):
            self.assertEqual(asyncio.run(exercise(telemetry.instrument_tool(sample))), {"ok": True})
            files = list(Path(td).glob("*/*.jsonl"))
            self.assertEqual(len(files), 1)
            event = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(event["mcp_request_id"], "99")
            self.assertEqual(event["mcp_session_id"], "nested-session-456")
            self.assertEqual(event["mcp_method"], "")

    def test_summary_aggregates_multiple_process_files(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ,
            {"HOME_MCP_TOOL_LOG_DIR": td, "HOME_MCP_TOOL_LOG": "1"},
            clear=False,
        ):
            now = telemetry.datetime.now().astimezone()
            day = Path(td) / now.strftime("%Y-%m-%d")
            day.mkdir(parents=True)
            events = [
                {"tool": "a", "started_at": now.isoformat(), "duration_ms": 10, "outcome": "ok"},
                {"tool": "a", "started_at": now.isoformat(), "duration_ms": 30, "outcome": "error"},
                {"tool": "b", "started_at": now.isoformat(), "duration_ms": 5, "outcome": "ok"},
            ]
            (day / "gateway-1.jsonl").write_text(
                "\n".join(json.dumps(x) for x in events[:2]) + "\n", encoding="utf-8"
            )
            (day / "gateway-2.jsonl").write_text(
                json.dumps(events[2]) + "\n", encoding="utf-8"
            )
            summary = telemetry.summarize_logs(window_minutes=60, top_n=10)
            self.assertEqual(summary["events"], 3)
            rows = {row["tool"]: row for row in summary["tools"]}
            self.assertEqual(rows["a"]["calls"], 2)
            self.assertEqual(rows["a"]["errors"], 1)
            self.assertEqual(rows["a"]["avg_ms"], 20.0)


if __name__ == "__main__":
    unittest.main()
