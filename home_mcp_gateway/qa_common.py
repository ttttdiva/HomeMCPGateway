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
        server.tool()(instrument_tool(wrap(fn)))
