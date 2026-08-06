"""Official MCP Python SDK adapter for ChatBI's canonical server contract.

This module is imported only by MCP service entrypoints or protocol tests.  The
core API can still be installed without the optional ``mcp`` dependency.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import anyio
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
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.lowlevel.server import request_ctx
from mcp.server.models import InitializationOptions
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import (
    TransportSecurityMiddleware,
    TransportSecuritySettings,
)
from mcp.shared.exceptions import McpError
from pydantic import AnyUrl
from starlette.applications import Starlette
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from mcp_servers.common.adapter import MCPServerAdapter
from mcp_servers.common.contracts import (
    CHATBI_META_PREFIX,
    MCP_RESOURCE_CONTRACT_VERSION,
    MCPProtocolError,
    MCPRequestContext,
    MCPResourcePage,
    MCPResourceSubscriptionSnapshot,
    stable_hash,
)

SERVER_VERSION = "0.1.0"
MCP_PROTOCOL_VERSION = "2025-11-25"
DEFAULT_MAX_REQUEST_BYTES = 1024 * 1024
DEFAULT_RESOURCE_PAGE_SIZE = 50
DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS = 1.0
_DOTENV = dotenv_values(".env")
_LOG = logging.getLogger("chatbi.mcp.resource")


@dataclass(slots=True)
class _ActiveResourceSubscription:
    context: MCPRequestContext
    snapshot: MCPResourceSubscriptionSnapshot
    task: asyncio.Task[None] | None = None


def build_sdk_server(
    adapter: MCPServerAdapter,
    *,
    context_signing_key: str | None = None,
    require_signed_context: bool = False,
    resource_page_size: int = DEFAULT_RESOURCE_PAGE_SIZE,
    resource_poll_interval_seconds: float = DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS,
) -> Server[Any, Any]:
    """Bind canonical tools/list and tools/call handlers to the official SDK."""
    if require_signed_context and not context_signing_key:
        raise ValueError("要求 MCP 上下文签名时必须配置 signing key")
    if not 1 <= resource_page_size <= 500:
        raise ValueError("MCP Resource page size 必须在 1 到 500 之间")
    if resource_poll_interval_seconds <= 0:
        raise ValueError("MCP Resource 订阅轮询间隔必须大于 0")
    server: Server[Any, Any] = Server(adapter.name, version=SERVER_VERSION)
    cursor_signing_key = context_signing_key or f"{adapter.name}:development-resource-cursor"
    subscriptions: dict[
        tuple[int, str, str, str, str],
        _ActiveResourceSubscription,
    ] = {}

    @server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
    async def list_tools() -> list[types.Tool]:
        return [_to_sdk_tool(descriptor.to_protocol_dict()) for descriptor in adapter.list_tools()]

    # ChatBI maps schema failures to stable error codes itself.
    @server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        try:
            context = _current_request_context(
                context_signing_key=context_signing_key,
                require_signed_context=require_signed_context,
            )
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, MCPProtocolError)
                else MCPProtocolError("invalid_request_context", "MCP 请求上下文无效")
            )
            return _error_result(error.code, error.message, error.retryable)
        result = await anyio.to_thread.run_sync(
            adapter.call_tool,
            name,
            arguments,
            context,
            abandon_on_cancel=True,
        )
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

    if adapter.has_resources:

        async def watch_subscription(
            key: tuple[int, str, str, str, str],
            session: Any,
            active: _ActiveResourceSubscription,
        ) -> None:
            try:
                while True:
                    await asyncio.sleep(resource_poll_interval_seconds)
                    current = await anyio.to_thread.run_sync(
                        adapter.resource_subscription_snapshot,
                        active.snapshot.uri,
                        active.context,
                        abandon_on_cancel=True,
                    )
                    previous = active.snapshot
                    if current.catalog_version != previous.catalog_version:
                        await _send_resource_list_changed(
                            session,
                            catalog_version=current.catalog_version,
                            previous_catalog_version=previous.catalog_version,
                        )
                    if current.content_hash != previous.content_hash:
                        await _send_resource_updated(
                            session,
                            uri=current.uri,
                            catalog_version=current.catalog_version,
                            previous_catalog_version=previous.catalog_version,
                        )
                    active.snapshot = current
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log_resource_subscription_stop(adapter.name, exc)
            finally:
                if subscriptions.get(key) is active:
                    subscriptions.pop(key, None)

        @server.list_resources()  # type: ignore[no-untyped-call,untyped-decorator]
        async def list_resources(
            request: types.ListResourcesRequest,
        ) -> types.ListResourcesResult:
            try:
                context = _current_request_context(
                    context_signing_key=context_signing_key,
                    require_signed_context=require_signed_context,
                )
                resources = await anyio.to_thread.run_sync(
                    adapter.list_resources,
                    context,
                    abandon_on_cancel=True,
                )
                page = _resource_page(
                    resources,
                    cursor=(request.params.cursor if request.params is not None else None),
                    context=context,
                    signing_key=cursor_signing_key,
                    page_size=resource_page_size,
                )
            except MCPProtocolError as exc:
                raise _resource_error(exc) from exc
            return types.ListResourcesResult(
                resources=[
                    types.Resource(**descriptor.to_protocol_dict())
                    for descriptor in page.resources
                ],
                nextCursor=page.next_cursor,
                _meta={
                    f"{CHATBI_META_PREFIX}contract-version": MCP_RESOURCE_CONTRACT_VERSION,
                    f"{CHATBI_META_PREFIX}catalog-version": page.catalog_version,
                },
            )

        @server.read_resource()  # type: ignore[no-untyped-call,untyped-decorator]
        async def read_resource(uri: Any) -> list[ReadResourceContents]:
            raw_uri = str(uri)
            try:
                context = _current_request_context(
                    context_signing_key=context_signing_key,
                    require_signed_context=require_signed_context,
                )
                contents = await anyio.to_thread.run_sync(
                    adapter.read_resource,
                    raw_uri,
                    context,
                    abandon_on_cancel=True,
                )
            except MCPProtocolError as exc:
                raise _resource_error(exc) from exc
            return [
                ReadResourceContents(
                    content=contents.text,
                    mime_type=contents.mime_type,
                    meta={
                        **(contents.metadata or {}),
                        f"{CHATBI_META_PREFIX}contract-version": MCP_RESOURCE_CONTRACT_VERSION,
                    },
                )
            ]

        @server.subscribe_resource()  # type: ignore[no-untyped-call,untyped-decorator]
        async def subscribe_resource(uri: Any) -> None:
            raw_uri = str(uri)
            try:
                context = _current_request_context(
                    context_signing_key=context_signing_key,
                    require_signed_context=require_signed_context,
                )
                snapshot = await anyio.to_thread.run_sync(
                    adapter.resource_subscription_snapshot,
                    raw_uri,
                    context,
                    abandon_on_cancel=True,
                )
            except MCPProtocolError as exc:
                raise _resource_error(exc) from exc
            session = request_ctx.get().session
            key = (
                id(session),
                context.subject_id,
                context.project_id,
                context.conversation_id,
                raw_uri,
            )
            existing = subscriptions.get(key)
            if existing is not None:
                existing.snapshot = snapshot
                return
            active = _ActiveResourceSubscription(
                context=context,
                snapshot=snapshot,
            )
            subscriptions[key] = active
            active.task = asyncio.create_task(
                watch_subscription(key, session, active),
                name=f"mcp-resource-subscription:{adapter.name}",
            )

        @server.unsubscribe_resource()  # type: ignore[no-untyped-call,untyped-decorator]
        async def unsubscribe_resource(uri: Any) -> None:
            raw_uri = str(uri)
            try:
                context = _current_request_context(
                    context_signing_key=context_signing_key,
                    require_signed_context=require_signed_context,
                )
                await anyio.to_thread.run_sync(
                    adapter.read_resource,
                    raw_uri,
                    context,
                    abandon_on_cancel=True,
                )
            except MCPProtocolError as exc:
                raise _resource_error(exc) from exc
            session = request_ctx.get().session
            key = (
                id(session),
                context.subject_id,
                context.project_id,
                context.conversation_id,
                raw_uri,
            )
            active = subscriptions.pop(key, None)
            if active is not None and active.task is not None:
                active.task.cancel()

    original_get_capabilities = server.get_capabilities

    def governed_capabilities(
        notification_options: NotificationOptions,
        experimental_capabilities: dict[str, dict[str, Any]],
    ) -> types.ServerCapabilities:
        notification_options.tools_changed = True
        if adapter.has_resources:
            notification_options.resources_changed = True
        capabilities = original_get_capabilities(
            notification_options,
            experimental_capabilities,
        )
        if capabilities.resources is not None:
            capabilities.resources.subscribe = True
        return capabilities

    server.get_capabilities = governed_capabilities  # type: ignore[method-assign]

    return server


def run_stdio(
    adapter: MCPServerAdapter,
    *,
    context_signing_key: str | None = None,
    resource_page_size: int = DEFAULT_RESOURCE_PAGE_SIZE,
    resource_poll_interval_seconds: float = DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS,
) -> None:
    asyncio.run(
        _run_stdio(
            adapter,
            context_signing_key=context_signing_key,
            resource_page_size=resource_page_size,
            resource_poll_interval_seconds=resource_poll_interval_seconds,
        )
    )


async def _run_stdio(
    adapter: MCPServerAdapter,
    *,
    context_signing_key: str | None = None,
    resource_page_size: int = DEFAULT_RESOURCE_PAGE_SIZE,
    resource_poll_interval_seconds: float = DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS,
) -> None:
    server = build_sdk_server(
        adapter,
        context_signing_key=context_signing_key,
        require_signed_context=context_signing_key is not None,
        resource_page_size=resource_page_size,
        resource_poll_interval_seconds=resource_poll_interval_seconds,
    )
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
                    await Response("Request body too large", status_code=413)(scope, receive, send)
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
            and (payload.get("params") or {}).get("protocolVersion") != self._protocol_version
        ):
            await JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": payload.get("id"),
                    "error": {
                        "code": -32600,
                        "message": (
                            "Unsupported protocol version; " f"expected {self._protocol_version}"
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
    context_signing_key: str | None = None,
    readiness_check: Callable[[], tuple[bool, dict[str, Any]]] | None = None,
    resource_page_size: int = DEFAULT_RESOURCE_PAGE_SIZE,
    resource_poll_interval_seconds: float = DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS,
) -> ASGIApp:
    """Build a stateful, authenticated Streamable HTTP ASGI application."""
    if not service_token.strip():
        raise ValueError("Streamable HTTP 必须配置非空 MCP_SERVICE_TOKEN")
    if not allowed_hosts:
        raise ValueError("Streamable HTTP 必须配置至少一个 allowed host")
    server = build_sdk_server(
        adapter,
        context_signing_key=context_signing_key or service_token,
        require_signed_context=True,
        resource_page_size=resource_page_size,
        resource_poll_interval_seconds=resource_poll_interval_seconds,
    )
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
        backend=BearerAuthBackend(_StaticTokenVerifier(service_token, client_id=adapter.name)),
    )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        async with manager.run():
            yield

    async def health(_: Request) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "service": adapter.name,
            }
        )

    async def ready(_: Request) -> JSONResponse:
        is_ready, details = (
            readiness_check()
            if readiness_check is not None
            else (True, {"tool_count": len(adapter.names)})
        )
        return JSONResponse(
            {
                "status": "ready" if is_ready else "not_ready",
                "service": adapter.name,
                **details,
            },
            status_code=200 if is_ready else 503,
        )

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/health/ready", ready, methods=["GET"]),
            Mount("/mcp", app=endpoint),
        ],
        lifespan=lifespan,
    )


def run_streamable_http(
    adapter: MCPServerAdapter,
    *,
    host: str,
    port: int,
    service_token: str,
    allowed_hosts: list[str],
    allowed_origins: list[str],
    context_signing_key: str | None = None,
    readiness_check: Callable[[], tuple[bool, dict[str, Any]]] | None = None,
    resource_page_size: int = DEFAULT_RESOURCE_PAGE_SIZE,
    resource_poll_interval_seconds: float = DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS,
) -> None:
    """Run the reviewed stateful Streamable HTTP endpoint."""
    app = create_streamable_http_app(
        adapter,
        service_token=service_token,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        context_signing_key=context_signing_key,
        readiness_check=readiness_check,
        resource_page_size=resource_page_size,
        resource_poll_interval_seconds=resource_poll_interval_seconds,
    )
    uvicorn.run(
        app,
        host=host,
        port=port,
        access_log=False,
        server_header=False,
        proxy_headers=False,
    )


def run_adapter(
    adapter: MCPServerAdapter,
    *,
    default_port: int = 8000,
    readiness_check: Callable[[], tuple[bool, dict[str, Any]]] | None = None,
) -> None:
    """Run stdio or authenticated stateful Streamable HTTP."""
    transport = _env("MCP_TRANSPORT", "stdio").strip().lower()
    context_signing_key = (
        _env_or_file(
            "MCP_CONTEXT_SIGNING_KEY",
            "MCP_CONTEXT_SIGNING_KEY_FILE",
        )
        or None
    )
    resource_page_size = int(
        _env("MCP_RESOURCE_PAGE_SIZE", str(DEFAULT_RESOURCE_PAGE_SIZE))
    )
    resource_poll_interval_seconds = float(
        _env(
            "MCP_RESOURCE_POLL_INTERVAL_SECONDS",
            str(DEFAULT_RESOURCE_POLL_INTERVAL_SECONDS),
        )
    )
    if transport == "stdio":
        run_stdio(
            adapter,
            context_signing_key=context_signing_key,
            resource_page_size=resource_page_size,
            resource_poll_interval_seconds=resource_poll_interval_seconds,
        )
        return
    if transport not in {"streamable-http", "streamable_http"}:
        raise RuntimeError(f"不支持的 MCP_TRANSPORT: {transport}")
    host = _env("MCP_HTTP_HOST", "127.0.0.1").strip()
    port = int(_env("MCP_HTTP_PORT", str(default_port)))
    token = _env_or_file("MCP_SERVICE_TOKEN", "MCP_SERVICE_TOKEN_FILE")
    configured_hosts = _csv_env("MCP_ALLOWED_HOSTS")
    if configured_hosts:
        allowed_hosts = configured_hosts
    elif host in {"127.0.0.1", "localhost", "::1"}:
        allowed_hosts = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    else:
        raise RuntimeError("非 loopback MCP_HTTP_HOST 必须显式配置 MCP_ALLOWED_HOSTS")
    run_streamable_http(
        adapter,
        host=host,
        port=port,
        service_token=token,
        allowed_hosts=allowed_hosts,
        allowed_origins=_csv_env("MCP_ALLOWED_ORIGINS"),
        context_signing_key=context_signing_key,
        readiness_check=readiness_check,
        resource_page_size=resource_page_size,
        resource_poll_interval_seconds=resource_poll_interval_seconds,
    )


def _csv_env(name: str) -> list[str]:
    return [item.strip() for item in _env(name, "").split(",") if item.strip()]


def _env(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    dotenv_value = _DOTENV.get(name)
    return str(dotenv_value) if dotenv_value is not None else default


def _env_or_file(value_name: str, file_name: str) -> str:
    value = _env(value_name, "").strip()
    file_path = _env(file_name, "").strip()
    if value and file_path:
        raise RuntimeError(f"{value_name} 与 {file_name} 不能同时配置")
    if not file_path:
        return value
    try:
        secret = Path(file_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise RuntimeError(f"{file_name} 不可读") from exc
    if not secret:
        raise RuntimeError(f"{file_name} 不能为空")
    return secret


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


def _resource_page(
    resources: tuple[Any, ...],
    *,
    cursor: str | None,
    context: MCPRequestContext,
    signing_key: str,
    page_size: int,
) -> MCPResourcePage:
    catalog_version = _resource_catalog_version(resources)
    offset = 0
    if cursor is not None:
        offset = _decode_resource_cursor(
            str(cursor),
            context=context,
            signing_key=signing_key,
            catalog_version=catalog_version,
        )
    if offset > len(resources):
        raise MCPProtocolError(
            "invalid_resource_cursor",
            "MCP Resource 分页游标超出目录范围",
        )
    end = min(offset + page_size, len(resources))
    next_cursor = (
        _encode_resource_cursor(
            offset=end,
            context=context,
            signing_key=signing_key,
            catalog_version=catalog_version,
        )
        if end < len(resources)
        else None
    )
    return MCPResourcePage(
        resources=resources[offset:end],
        catalog_version=catalog_version,
        next_cursor=next_cursor,
    )


def _resource_catalog_version(resources: tuple[Any, ...]) -> str:
    catalog_versions = {
        value
        for descriptor in resources
        if (descriptor.metadata or {}).get("com.chatbi/resource-kind")
        == "domain-definition-catalog"
        if isinstance(
            (value := (descriptor.metadata or {}).get("com.chatbi/catalog-version")),
            str,
        )
        and len(value) == 64
    }
    if len(catalog_versions) == 1:
        return next(iter(catalog_versions))
    return stable_hash(
        [descriptor.to_protocol_dict() for descriptor in resources]
    )


def _encode_resource_cursor(
    *,
    offset: int,
    context: MCPRequestContext,
    signing_key: str,
    catalog_version: str,
) -> str:
    payload = json.dumps(
        {
            "contract_version": MCP_RESOURCE_CONTRACT_VERSION,
            "catalog_version": catalog_version,
            "subject_id": context.subject_id,
            "project_id": context.project_id,
            "conversation_id": context.conversation_id,
            "offset": offset,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(
        signing_key.encode("utf-8"),
        encoded.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    return f"{encoded}.{signature}"


def _decode_resource_cursor(
    cursor: str,
    *,
    context: MCPRequestContext,
    signing_key: str,
    catalog_version: str,
) -> int:
    try:
        encoded, signature = cursor.split(".", 1)
        expected = hmac.new(
            signing_key.encode("utf-8"),
            encoded.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding))
        offset = payload["offset"]
        if (
            payload.get("contract_version") != MCP_RESOURCE_CONTRACT_VERSION
            or payload.get("subject_id") != context.subject_id
            or payload.get("project_id") != context.project_id
            or payload.get("conversation_id") != context.conversation_id
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset < 0
        ):
            raise ValueError("scope")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MCPProtocolError(
            "invalid_resource_cursor",
            "MCP Resource 分页游标无效或不属于当前主体",
        ) from exc
    if payload.get("catalog_version") != catalog_version:
        raise MCPProtocolError(
            "resource_catalog_changed",
            "MCP Resource 目录已变化，必须从第一页重新发现",
            retryable=True,
        )
    return cast(int, offset)


def _resource_notification_meta(
    *,
    catalog_version: str,
    previous_catalog_version: str,
) -> types.NotificationParams.Meta:
    return types.NotificationParams.Meta(
        **{
            f"{CHATBI_META_PREFIX}contract-version": MCP_RESOURCE_CONTRACT_VERSION,
            f"{CHATBI_META_PREFIX}catalog-version": catalog_version,
            f"{CHATBI_META_PREFIX}previous-catalog-version": previous_catalog_version,
        }
    )


async def _send_resource_list_changed(
    session: Any,
    *,
    catalog_version: str,
    previous_catalog_version: str,
) -> None:
    await session.send_notification(
        types.ServerNotification(
            types.ResourceListChangedNotification(
                params=types.NotificationParams(
                    _meta=_resource_notification_meta(
                        catalog_version=catalog_version,
                        previous_catalog_version=previous_catalog_version,
                    )
                )
            )
        )
    )


async def _send_resource_updated(
    session: Any,
    *,
    uri: str,
    catalog_version: str,
    previous_catalog_version: str,
) -> None:
    await session.send_notification(
        types.ServerNotification(
            types.ResourceUpdatedNotification(
                params=types.ResourceUpdatedNotificationParams(
                    uri=AnyUrl(uri),
                    _meta=_resource_notification_meta(
                        catalog_version=catalog_version,
                        previous_catalog_version=previous_catalog_version,
                    ),
                )
            )
        )
    )


def _log_resource_subscription_stop(service_name: str, error: Exception) -> None:
    _LOG.info(
        "resource subscription stopped service=%s reason=%s",
        service_name,
        type(error).__name__,
    )


def _current_request_context(
    *,
    context_signing_key: str | None,
    require_signed_context: bool,
) -> MCPRequestContext:
    current = request_ctx.get()
    return MCPRequestContext.from_request_meta(
        _meta_to_dict(current.meta),
        signing_key=context_signing_key,
        require_signature=require_signed_context,
    )


def _resource_error(error: MCPProtocolError) -> McpError:
    return McpError(
        types.ErrorData(
            code=-32001,
            message=error.message,
            data={
                "com.chatbi/error-code": error.code,
                "com.chatbi/retryable": error.retryable,
            },
        )
    )


def _error_result(code: str, message: str, retryable: bool) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)],
        isError=True,
        _meta={
            "com.chatbi/error-code": code,
            "com.chatbi/retryable": retryable,
        },
    )
