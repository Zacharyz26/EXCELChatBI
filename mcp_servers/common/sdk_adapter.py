"""Official MCP Python SDK adapter for ChatBI's canonical server contract.

This module is imported only by MCP service entrypoints or protocol tests.  The
core API can still be installed without the optional ``mcp`` dependency.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from typing import Any

import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.models import InitializationOptions

from mcp_servers.common.adapter import MCPServerAdapter
from mcp_servers.common.contracts import MCPRequestContext

SERVER_VERSION = "0.1.0"


def build_sdk_server(adapter: MCPServerAdapter) -> Server[Any, Any]:
    """Bind canonical tools/list and tools/call handlers to the official SDK."""
    server: Server[Any, Any] = Server(adapter.name, version=SERVER_VERSION)

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [_to_sdk_tool(descriptor.to_protocol_dict()) for descriptor in adapter.list_tools()]

    # ChatBI maps schema failures to stable error codes itself.
    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        current = request_ctx.get()
        raw_meta = _meta_to_dict(current.meta)
        try:
            context = MCPRequestContext.from_request_meta(raw_meta)
        except Exception as exc:
            from mcp_servers.common.contracts import MCPProtocolError

            error = exc if isinstance(exc, MCPProtocolError) else MCPProtocolError(
                "invalid_request_context", "MCP 请求上下文无效"
            )
            return _error_result(error.code, error.message, error.retryable)
        result = await asyncio.to_thread(adapter.call_tool, name, arguments, context)
        if result.is_error:
            return _error_result(
                result.error_code or "mcp_tool_error", result.text, result.retryable
            )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=result.text)],
            structuredContent=result.structured_content,
            isError=False,
            _meta={
                "com.chatbi/result-hash": result.result_hash,
                "com.chatbi/contract-version": "chatbi-mcp-tool-v1",
            },
        )

    return server


def run_stdio(adapter: MCPServerAdapter) -> None:
    asyncio.run(_run_stdio(adapter))


async def _run_stdio(adapter: MCPServerAdapter) -> None:
    server = build_sdk_server(adapter)
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name=adapter.name,
                server_version=SERVER_VERSION,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def run_adapter(adapter: MCPServerAdapter) -> None:
    """Run the selected transport; stage 1 exposes stdio, HTTP follows the probe."""
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    if transport != "stdio":
        raise RuntimeError(
            "当前已验收的 MCP 服务入口仅为 stdio；Streamable HTTP 必须先完成阶段 0 探针"
        )
    run_stdio(adapter)


def _to_sdk_tool(raw: dict[str, Any]) -> types.Tool:
    annotations = raw["annotations"]
    return types.Tool(
        name=raw["name"],
        description=raw["description"],
        inputSchema=raw["inputSchema"],
        outputSchema=raw["outputSchema"],
        annotations=types.ToolAnnotations(
            readOnlyHint=annotations["readOnlyHint"],
            destructiveHint=annotations["destructiveHint"],
            idempotentHint=annotations["idempotentHint"],
            openWorldHint=annotations["openWorldHint"],
        ),
        _meta=raw["_meta"],
    )


def _meta_to_dict(meta: Any) -> dict[str, Any]:
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        return dict(meta)
    model_dump = getattr(meta, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(by_alias=True)
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _error_result(code: str, message: str, retryable: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        isError=True,
        _meta={
            "com.chatbi/error-code": code,
            "com.chatbi/retryable": retryable,
        },
    )
