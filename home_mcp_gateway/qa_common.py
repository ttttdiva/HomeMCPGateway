"""Shared QA response and registration helpers; no access policy is imposed."""
from __future__ import annotations

import base64
import functools
import inspect
import io
import json
from typing import Annotated, Any, get_args, get_type_hints

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from PIL import Image
from pydantic import ConfigDict, create_model

from .telemetry import instrument_tool


def png_result(data: bytes, **metadata: Any) -> CallToolResult:
    with Image.open(io.BytesIO(data)) as picture:
        if picture.format != "PNG":
            raise ValueError("Capture did not return PNG data")
        picture.load()
        metadata.update(width=picture.width, height=picture.height, mime_type="image/png")
    return CallToolResult(
        content=[TextContent(type="text", text=json.dumps(metadata, ensure_ascii=False)),
                 ImageContent(type="image", data=base64.b64encode(data).decode("ascii"), mimeType="image/png")],
        structuredContent=metadata,
    )


def image_result(picture: Image.Image, **metadata: Any) -> CallToolResult:
    output = io.BytesIO()
    picture.save(output, format="PNG")
    return png_result(output.getvalue(), **metadata)


def register_tools(server: Any, functions: list) -> None:
    """Preserve typed discovery schemas and expose actionable workspace diagnostics."""
    for fn in functions:
        def wrap(target):
            hints = get_type_hints(target)
            mixed_result = set(get_args(hints.get("return"))) == {dict[str, Any], CallToolResult}

            def adapt(result):
                # The SDK rejects Union[dict, CallToolResult]. Normalize only
                # this mixed response at the MCP boundary; native callers keep
                # their historical dict response and the wire keeps its schema.
                if mixed_result and isinstance(result, dict):
                    return CallToolResult(
                        content=[TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
                        structuredContent=result,
                    )
                return result

            if inspect.iscoroutinefunction(target):
                @functools.wraps(target)
                async def invoke(**kwargs):
                    try:
                        return adapt(await target(**kwargs))
                    except Exception as exc:
                        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
            else:
                @functools.wraps(target)
                def invoke(**kwargs):
                    try:
                        return adapt(target(**kwargs))
                    except Exception as exc:
                        raise ToolError(f"{type(exc).__name__}: {exc}") from exc
            signature = inspect.signature(target, eval_str=True)
            if mixed_result:
                hints["return"] = Annotated[CallToolResult, dict[str, Any]]
                signature = signature.replace(return_annotation=hints["return"])
            invoke.__signature__ = signature
            invoke.__annotations__ = hints
            return invoke
        wrapped = instrument_tool(wrap(fn))
        if fn.__module__ == "home_mcp_gateway.jobs" and fn.__name__ in {"job_steer", "job_steer_status"}:
            # These public steering boundaries must reject launch/runtime
            # overrides. The SDK's default argument model silently ignores
            # extras, so constrain only these registrations, including their
            # advertised schemas. Do not change the SDK's global tool factory.
            tool = server._tool_manager.add_tool(wrapped)
            arguments = tool.fn_metadata.arg_model
            strict_arguments = create_model(
                arguments.__name__, __base__=arguments,
                __config__=ConfigDict(extra="forbid"),
            )
            tool.fn_metadata.arg_model = strict_arguments
            tool.parameters = strict_arguments.model_json_schema(by_alias=True)
        else:
            server.tool()(wrapped)
