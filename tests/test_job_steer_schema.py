"""Offline MCP discovery and invocation validation for public steering tools."""
import unittest
from unittest.mock import Mock

from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.tools.base import Tool
from pydantic import ValidationError

from home_mcp_gateway import jobs
from home_mcp_gateway.qa_common import register_tools


class JobSteerSchemaTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.server = MCPServer("offline-job-steer-schema")
        register_tools(self.server, jobs.TOOLS)
        self.arguments = {
            "job_steer": {
                "job_id": "88888888-8888-4888-8888-888888888888",
                "expected_turn_id": "turn-1",
                "text": "Please clarify this step",
                "idempotency_key": "steer-key",
            },
            "job_steer_status": {
                "job_id": "88888888-8888-4888-8888-888888888888",
                "idempotency_key": "steer-key",
            },
        }
        self.invocations = {}
        for name in self.arguments:
            tool = self.server._tool_manager.get_tool(name)
            # Keep the actual jobs.TOOLS registration and its validation path,
            # but never execute jobs, a provider or any server lifecycle.
            tool.fn = self.invocations[name] = Mock(return_value={"ok": True})

    async def test_actual_discovery_requires_only_fixed_string_arguments(self):
        discovered = {tool.name: tool for tool in await self.server.list_tools()}
        self.assertEqual(set(discovered), {fn.__name__ for fn in jobs.TOOLS})
        for name, arguments in self.arguments.items():
            with self.subTest(tool=name):
                schema = discovered[name].input_schema
                self.assertEqual(list(schema["properties"]), list(arguments))
                self.assertEqual(schema["required"], list(arguments))
                self.assertIs(schema["additionalProperties"], False)
                self.assertTrue(all(field["type"] == "string" for field in schema["properties"].values()))

    async def test_exact_arguments_reach_the_registered_callable(self):
        for name, arguments in self.arguments.items():
            with self.subTest(tool=name):
                result = await self.server.call_tool(name, arguments)
                self.assertFalse(result.is_error)
                self.assertEqual(result.structured_content, {"ok": True})
                self.invocations[name].assert_called_once_with(**arguments)

    async def test_runtime_launch_and_user_input_injections_fail_before_invocation(self):
        injections = {
            "runtime_dir": "alternate-runtime",
            "command": ["unwanted-program"],
            "path": "alternate-path",
            "model": "alternate-model",
            "cwd": "alternate-cwd",
            "input": [{"type": "text", "text": "override"}],
            "UserInput": {"type": "text", "text": "override"},
        }
        for name, arguments in self.arguments.items():
            for field, value in injections.items():
                with self.subTest(tool=name, field=field):
                    with self.assertRaises(ToolError) as caught:
                        await self.server.call_tool(name, {**arguments, field: value})
                    self.assertIsInstance(caught.exception.__cause__, ValidationError)
                    self.assertEqual(
                        [(error["loc"], error["type"]) for error in caught.exception.__cause__.errors()],
                        [((field,), "extra_forbidden")],
                    )
                    self.invocations[name].assert_not_called()

    async def test_every_declared_argument_is_required(self):
        for name, arguments in self.arguments.items():
            for field in arguments:
                with self.subTest(tool=name, field=field):
                    incomplete = {key: value for key, value in arguments.items() if key != field}
                    with self.assertRaises(ToolError) as caught:
                        await self.server.call_tool(name, incomplete)
                    self.assertIsInstance(caught.exception.__cause__, ValidationError)
                    self.assertEqual(
                        [(error["loc"], error["type"]) for error in caught.exception.__cause__.errors()],
                        [((field,), "missing")],
                    )
                    self.invocations[name].assert_not_called()

    async def test_existing_job_tools_keep_the_sdk_schema_and_extra_argument_behavior(self):
        discovered = {tool.name: tool for tool in await self.server.list_tools()}
        for fn in jobs.TOOLS:
            if fn.__name__ not in self.arguments:
                with self.subTest(tool=fn.__name__):
                    self.assertEqual(discovered[fn.__name__].input_schema, Tool.from_function(fn).parameters)
        tool = self.server._tool_manager.get_tool("job_status")
        invoke = Mock(return_value={"ok": True})
        tool.fn = invoke
        result = await self.server.call_tool("job_status", {
            "job_id": "88888888-8888-4888-8888-888888888888", "legacy_extra": True,
        })
        self.assertFalse(result.is_error)
        invoke.assert_called_once_with(job_id="88888888-8888-4888-8888-888888888888", runtime_dir=None)


if __name__ == "__main__":
    unittest.main()
