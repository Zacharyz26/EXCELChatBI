"""Official MCP Python SDK adapter for ChatBI's canonical server contract.

This module is imported only by MCP service entrypoints or protocol tests.  The
core API can still be installed without the optional ``mcp`` dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
from collections.abc import AsyncIterator, Mapping
from typing import Any

import mcp.server.stdio
import mcp.types as types
import uvicorn
from dotenv import dotenv_values
from mcp.server.auth.middleware.bearer_auth import (
    BearerAuthBackend,
    RequireAuthMiddleware,
)
from mcp.server.auth.provider import AccessToken
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.lowlevel.server import request_ctx
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_servers.common.adapter import MCPServerAdapter
from mcp_servers.common.contracts import MCPRequestContext

SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
_DOTENV = dotenv_values(".env")


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


class _StaticTokenVerifier:
    """Minimal internal-service verifier; external OAuth belongs to v3.0."""

    def __init__(self, token: str, *, client_id: str) -> None:
        self._token = token
        self._client_id = client_id

    async def verify_token(self, token: str) -> AccessToken | None:
        if not hmac.compare_digest(token, self._token):
            return None
        return AccessToken(
            token=token,
            client_id=self._client_id,
            subject=self._client_id,
            scopes=["mcp:invoke"],
        )


class _PinnedProtocolMiddleware:
    """Reject initialize requests outside ChatBI's reviewed MCP protocol."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        protocol_version: str = MCP_PROTOCOL_VERSION,
        max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES,
    ) -> None:
        self._app = app
        self._protocol_version = protocol_version
        self._max_request_bytes = max_request_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._app(scope, receive, send)
            return
        messages: list[Message] = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > self._max_request_bytes:
                    await Response("Request body too large", status_code=413)(
                        scope, receive, send
                    )
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break
        try:
            import json

            payload = json.loads(body)
        except (UnicodeDecodeError, ValueError):
            payload = None
        if (
            isinstance(payload, dict)
            and payload.get("method") == "initialize"
            and (payload.get("params") or {}).get("protocolVersion")
            != self._protocol_version
        ):
            await JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": -32600,
                        "message": (
                            "Unsupported protocol version; "
                            f"expected {self._protocol_version}"
                        ),
                    },
                },
                status_code=400,
            )(scope, receive, send)
            return
        iterator = iter(messages)

        async def replay() -> Message:
            try:
                return next(iterator)
            except StopIteration:
                return {"type": "http.request", "body": b"", "more_body": False}

        await self._app(scope, replay, send)


class _PreflightTransportSecurity:
    """Validate Host/Origin before the SDK allocates a stateful session."""

    def __init__(self, app: ASGIApp, settings: TransportSecuritySettings) -> None:
        self._app = app
        self._validator = TransportSecurityMiddleware(settings)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        response = await self._validator.validate_request(
            Request(scope, receive),
            is_post=scope.get("method") == "POST",
        )
        if response is not None:
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)


def create_streamable_http_app(
    adapter: MCPServerAdapter,
    *,
    service_token: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
) -> ASGIApp:
    """Build a stateful, authenticated Streamable HTTP ASGI application."""
    if not service_token.strip():
        raise ValueError("Streamable HTTP 必须配置非空 MCP_SERVICE_TOKEN")
    if not allowed_hosts:
        raise ValueError("Streamable HTTP 必须配置至少一个 allowed host")
    server = build_sdk_server(adapter)
    security_settings = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    manager = StreamableHTTPSessionManager(
        server,
        stateless=False,
        json_response=True,
        security_settings=security_settings,
        session_idle_timeout=300,
    )
    endpoint: ASGIApp = _PinnedProtocolMiddleware(manager.handle_request)
    endpoint = _PreflightTransportSecurity(endpoint, security_settings)
    endpoint = RequireAuthMiddleware(endpoint, required_scopes=["mcp:invoke"])
    endpoint = AuthenticationMiddleware(
        endpoint,
        backend=BearerAuthBackend(
            _StaticTokenVerifier(service_token, client_id=adapter.name)
        ),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    return Starlette(routes=[Mount("/mcp", app=endpoint)], lifespan=lifespan)


def run_streamable_http(
    adapter: MCPServerAdapter,
    *,
    host: str,
    port: int,
    service_token: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
) -> None:
    """Run the reviewed stateful Streamable HTTP endpoint."""
    app = create_streamable_http_app(
        adapter,
        service_token=service_token,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )


def run_adapter(adapter: MCPServerAdapter, *, default_port: int = 8000) -> None:
    """Run stdio or authenticated stateful Streamable HTTP."""
    transport = _env("MCP_TRANSPORT", "stdio").strip().lower()
    if transport == "stdio":
        run_stdio(adapter)
        return
    if transport not in {"streamable-http", "streamable_http"}:
        raise RuntimeError(f"不支持的 MCP_TRANSPORT: {transport}")
    host = _env("MCP_HTTP_HOST", "127.0.0.1").strip()
    port = int(_env("MCP_HTTP_PORT", str(default_port)))
    token = _env("MCP_SERVICE_TOKEN", "")
    configured_hosts = _csv_env("MCP_ALLOWED_HOSTS")
    if configured_hosts:
        allowed_hosts = configured_hosts
    elif host in {"127.0.0.1", "localhost", "::1"}:
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    else:
        raise RuntimeError(
            "非 loopback MCP_HTTP_HOST 必须显式配置 MCP_ALLOWED_HOSTS"
        )
    run_streamable_http(
        adapter,
        host=host,
        port=port,
        service_token=token,
        allowed_hosts=allowed_hosts,
        allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS"),
    )


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in _env(name, "").split(",") if item.strip()]


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    dotenv_value = _DOTENV.get(name)
    return str(dotenv_value) if dotenv_value is not None else default


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
