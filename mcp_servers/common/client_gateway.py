"""Governed MCP Client Gateway, managed transports and contract comparison."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, TypeVar

from packages.common.logging import get_logger
from packages.session.models import ArtifactDraft

from mcp_servers.common.adapter import MCPServerAdapter
from mcp_servers.common.contracts import (
    CHATBI_META_PREFIX,
    MCP_RESOURCE_CONTRACT_VERSION,
    MCPCallResult,
    MCPProtocolError,
    MCPRequestContext,
    MCPResourceContents,
    MCPResourceDescriptor,
    MCPResourceNotification,
    MCPResourcePage,
    MCPToolDescriptor,
    ToolCapabilityMetadata,
    normalize_structured_result,
    stable_hash,
    validate_json,
    validate_tool_approval,
)

_log = get_logger("mcp.client_gateway")

MCPTransportName = Literal["in_process", "stdio", "streamable_http"]
_ResourceResult = TypeVar("_ResourceResult")


class MCPTransport(Protocol):
    """Minimal transport surface used by the gateway after SDK initialization."""

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult: ...

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage: ...

    async def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]: ...

    async def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents: ...

    async def subscribe_resource(self, uri: str, context: MCPRequestContext) -> None: ...

    async def unsubscribe_resource(self, uri: str, context: MCPRequestContext) -> None: ...

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification: ...

    async def next_tool_list_changed(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> None: ...

    async def aclose(self) -> None: ...


class InProcessMCPTransport:
    """Compatibility/test transport that exercises the same contract exactly once."""

    def __init__(self, adapter: MCPServerAdapter) -> None:
        self._adapter = adapter

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return self._adapter.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult:
        # Compatibility/test mode is deliberately synchronous. Production tools
        # execute in independent MCP service processes, where the SDK adapter owns
        # its worker boundary and cancellation semantics.
        return self._adapter.call_tool(name, arguments, context)

    async def list_resources(self, context: MCPRequestContext) -> tuple[MCPResourceDescriptor, ...]:
        return self._adapter.list_resources(context)

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage:
        if cursor is not None:
            raise MCPProtocolError(
                "invalid_resource_cursor",
                "进程内兼容传输不接受分页游标",
            )
        resources = self._adapter.list_resources(context)
        return MCPResourcePage(
            resources=resources,
            catalog_version=self._adapter.resource_catalog_version(context),
        )

    async def read_resource(self, uri: str, context: MCPRequestContext) -> MCPResourceContents:
        return self._adapter.read_resource(uri, context)

    async def subscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        self._adapter.resource_subscription_snapshot(uri, context)

    async def unsubscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        self._adapter.read_resource(uri, context)

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification:
        del timeout_seconds
        raise MCPProtocolError(
            "resource_notifications_unavailable",
            "进程内兼容传输不提供异步 Resource 通知",
        )

    async def next_tool_list_changed(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        del timeout_seconds
        raise MCPProtocolError(
            "tool_notifications_unavailable",
            "进程内兼容传输不提供异步 Tool 目录通知",
        )

    async def aclose(self) -> None:
        return None


class MCPResourceNotificationBuffer:
    """Collect standard Resource and Tool catalog notifications from one SDK session."""

    def __init__(self) -> None:
        self._queue: asyncio.Queue[MCPResourceNotification] = asyncio.Queue()
        self._tool_list_changed: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, message: Any) -> None:
        from mcp import types

        if not isinstance(message, types.ServerNotification):
            return
        notification = message.root
        if isinstance(notification, types.ResourceListChangedNotification):
            metadata = _resource_notification_metadata(notification.params)
            await self._queue.put(
                _resource_notification_from_meta(
                    kind="list_changed",
                    uri=None,
                    metadata=metadata,
                )
            )
        elif isinstance(notification, types.ResourceUpdatedNotification):
            metadata = _resource_notification_metadata(notification.params)
            await self._queue.put(
                _resource_notification_from_meta(
                    kind="updated",
                    uri=str(notification.params.uri),
                    metadata=metadata,
                )
            )
        elif isinstance(notification, types.ToolListChangedNotification):
            await self._tool_list_changed.put(None)

    async def next(self, *, timeout_seconds: float = 5.0) -> MCPResourceNotification:
        if timeout_seconds <= 0:
            raise ValueError("Resource notification timeout 必须大于 0")
        async with asyncio.timeout(timeout_seconds):
            return await self._queue.get()

    async def next_tool_list_changed(self, *, timeout_seconds: float = 5.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Tool 目录通知 timeout 必须大于 0")
        async with asyncio.timeout(timeout_seconds):
            await self._tool_list_changed.get()


class OfficialSDKSessionTransport:
    """Adapter over an initialized official ``mcp.ClientSession`` instance."""

    def __init__(
        self,
        session: Any,
        *,
        context_signing_key: str | None = None,
        resource_notifications: MCPResourceNotificationBuffer | None = None,
    ) -> None:
        self._session = session
        self._context_signing_key = context_signing_key
        self._resource_notifications = resource_notifications

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        response = await self._session.list_tools()
        descriptors: list[MCPToolDescriptor] = []
        for tool in response.tools:
            dumped = tool.model_dump(by_alias=True)
            annotations = dumped.get("annotations") or {}
            meta = dumped.get("_meta") or {}
            descriptors.append(
                MCPToolDescriptor(
                    name=tool.name,
                    description=tool.description or "",
                    input_schema=tool.inputSchema,
                    output_schema=tool.outputSchema or {"type": "object"},
                    metadata=ToolCapabilityMetadata.from_meta(
                        meta,
                        read_only=bool(annotations.get("readOnlyHint", False)),
                        destructive=bool(annotations.get("destructiveHint", True)),
                        idempotent=bool(annotations.get("idempotentHint", False)),
                        open_world=bool(annotations.get("openWorldHint", True)),
                    ),
                )
            )
        return tuple(descriptors)

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult:
        result = await self._session.call_tool(
            name,
            arguments=arguments,
            meta=context.to_request_meta(signing_key=self._context_signing_key),
        )
        meta = result.meta or {}
        error_code = meta.get("com.chatbi/error-code")
        retryable = meta.get("com.chatbi/retryable") is True
        text = "\n".join(
            item.text for item in result.content if getattr(item, "type", None) == "text"
        )
        if result.isError:
            return MCPCallResult(
                tool_name=name,
                structured_content=None,
                text=text or "MCP 工具调用失败",
                is_error=True,
                error_code=error_code if isinstance(error_code, str) else "mcp_tool_error",
                retryable=retryable,
            )
        structured = normalize_structured_result(result.structuredContent)
        return MCPCallResult.success(name, structured)

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage:
        from mcp import types
        from mcp.shared.exceptions import McpError

        request_meta = types.RequestParams.Meta(
            **context.to_request_meta(signing_key=self._context_signing_key)
        )
        params = types.PaginatedRequestParams(cursor=cursor, _meta=request_meta)
        try:
            response = await self._session.list_resources(params=params)
        except McpError as exc:
            raise _resource_protocol_error(exc) from exc
        descriptors: list[MCPResourceDescriptor] = []
        for resource in response.resources:
            meta = dict(resource.meta or {})
            _validate_resource_contract(meta)
            meta.pop(f"{CHATBI_META_PREFIX}contract-version", None)
            descriptors.append(
                MCPResourceDescriptor(
                    uri=str(resource.uri),
                    name=resource.name,
                    title=resource.title or resource.name,
                    description=resource.description or "",
                    mime_type=resource.mimeType or "application/octet-stream",
                    size=resource.size,
                    metadata=meta,
                )
            )
        response_meta = dict(response.meta or {})
        _validate_resource_contract(response_meta)
        catalog_version = response_meta.get(f"{CHATBI_META_PREFIX}catalog-version")
        if not isinstance(catalog_version, str) or len(catalog_version) != 64:
            raise MCPProtocolError(
                "invalid_resource_output",
                "MCP Resource 列表缺少目录版本",
            )
        return MCPResourcePage(
            resources=tuple(descriptors),
            catalog_version=catalog_version,
            next_cursor=(str(response.nextCursor) if response.nextCursor is not None else None),
        )

    async def list_resources(self, context: MCPRequestContext) -> tuple[MCPResourceDescriptor, ...]:
        resources: list[MCPResourceDescriptor] = []
        seen_uris: set[str] = set()
        cursor: str | None = None
        catalog_version: str | None = None
        for _ in range(10_000):
            page = await self.list_resource_page(context, cursor=cursor)
            if catalog_version is None:
                catalog_version = page.catalog_version
            elif page.catalog_version != catalog_version:
                raise MCPProtocolError(
                    "resource_catalog_changed",
                    "MCP Resource 分页期间目录发生变化",
                    retryable=True,
                )
            for descriptor in page.resources:
                if descriptor.uri in seen_uris:
                    raise MCPProtocolError(
                        "invalid_resource_output",
                        "MCP Resource 分页返回重复 URI",
                    )
                seen_uris.add(descriptor.uri)
                resources.append(descriptor)
            cursor = page.next_cursor
            if cursor is None:
                return tuple(resources)
        raise MCPProtocolError(
            "invalid_resource_output",
            "MCP Resource 分页超过安全上限",
        )

    async def read_resource(self, uri: str, context: MCPRequestContext) -> MCPResourceContents:
        # MCP SDK 1.28 的 read_resource 高层方法尚不能携带 _meta；直接构造
        # 标准请求，避免把共享服务令牌错误地当成最终用户身份。
        from mcp import types
        from mcp.shared.exceptions import McpError
        from pydantic import AnyUrl

        request_meta = types.RequestParams.Meta(
            **context.to_request_meta(signing_key=self._context_signing_key)
        )
        params = types.ReadResourceRequestParams(
            uri=AnyUrl(uri),
            _meta=request_meta,
        )
        try:
            response = await self._session.send_request(
                types.ClientRequest(types.ReadResourceRequest(params=params)),
                types.ReadResourceResult,
            )
        except McpError as exc:
            raise _resource_protocol_error(exc) from exc
        if len(response.contents) != 1:
            raise MCPProtocolError("invalid_resource_output", "MCP Resource 必须返回一个文本内容块")
        content = response.contents[0]
        text = getattr(content, "text", None)
        if not isinstance(text, str) or str(content.uri) != uri:
            raise MCPProtocolError("invalid_resource_output", "MCP Resource 返回内容无效")
        meta = dict(content.meta or {})
        _validate_resource_contract(meta)
        meta.pop(f"{CHATBI_META_PREFIX}contract-version", None)
        return MCPResourceContents(
            uri=uri,
            text=text,
            mime_type=content.mimeType or "application/octet-stream",
            metadata=meta,
        )

    async def subscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        from mcp import types
        from mcp.shared.exceptions import McpError
        from pydantic import AnyUrl

        request_meta = types.RequestParams.Meta(
            **context.to_request_meta(signing_key=self._context_signing_key)
        )
        request = types.SubscribeRequest(
            params=types.SubscribeRequestParams(uri=AnyUrl(uri), _meta=request_meta)
        )
        try:
            await self._session.send_request(
                types.ClientRequest(request),
                types.EmptyResult,
            )
        except McpError as exc:
            raise _resource_protocol_error(exc) from exc

    async def unsubscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        from mcp import types
        from mcp.shared.exceptions import McpError
        from pydantic import AnyUrl

        request_meta = types.RequestParams.Meta(
            **context.to_request_meta(signing_key=self._context_signing_key)
        )
        request = types.UnsubscribeRequest(
            params=types.UnsubscribeRequestParams(uri=AnyUrl(uri), _meta=request_meta)
        )
        try:
            await self._session.send_request(
                types.ClientRequest(request),
                types.EmptyResult,
            )
        except McpError as exc:
            raise _resource_protocol_error(exc) from exc

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification:
        if self._resource_notifications is None:
            raise MCPProtocolError(
                "resource_notifications_unavailable",
                "当前 SDK Session 未配置 Resource 通知处理器",
            )
        return await self._resource_notifications.next(timeout_seconds=timeout_seconds)

    async def next_tool_list_changed(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        if self._resource_notifications is None:
            raise MCPProtocolError(
                "tool_notifications_unavailable",
                "当前 SDK Session 未配置 Tool 目录通知处理器",
            )
        await self._resource_notifications.next_tool_list_changed(
            timeout_seconds=timeout_seconds
        )

    async def aclose(self) -> None:
        return None


@dataclass(slots=True)
class _OfficialSDKConnectionOwner:
    """State shared with the single task that owns SDK async contexts."""

    stop: asyncio.Event
    ready: asyncio.Future[OfficialSDKSessionTransport]
    task: asyncio.Task[None] | None = None
    failure: BaseException | None = None


@dataclass(frozen=True, slots=True)
class MCPClientConfig:
    """Static, Host-owned MCP transport configuration.

    ``in_process`` is intentionally a compatibility/test mode. Staging and
    production validation lives in :mod:`packages.common.config` and rejects it.
    """

    transport: MCPTransportName = "in_process"
    stdio_command: tuple[str, ...] = ()
    stdio_cwd: str | None = None
    stdio_env: Mapping[str, str] = field(default_factory=dict)
    http_url: str = ""
    service_token: str = ""
    context_signing_key: str = ""
    service_urls: Mapping[str, str] = field(default_factory=dict)
    service_tokens: Mapping[str, str] = field(default_factory=dict)
    connect_timeout_seconds: float = 10.0
    max_reconnects: int = 1
    allow_in_process_fallback: bool = False


class OfficialSDKClientTransport:
    """Lazy official-SDK client for stdio or stateful Streamable HTTP.

    A dedicated task owns every async context for one initialized
    ``ClientSession``. AnyIO cancel scopes must be exited by the task that
    entered them; request, reconnect and shutdown callers may otherwise be
    different tasks. Closing an unhealthy connection signals and joins its
    owner, so the next managed gateway performs a clean initialize +
    tools/list cycle.
    """

    def __init__(self, config: MCPClientConfig) -> None:
        if config.transport not in {"stdio", "streamable_http"}:
            raise ValueError("OfficialSDKClientTransport 仅支持 stdio/streamable_http")
        self._config = config
        self._owner: _OfficialSDKConnectionOwner | None = None
        self._owner_lock = asyncio.Lock()

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        transport = await self._connected_transport()
        return await transport.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult:
        transport = await self._connected_transport()
        return await transport.call_tool(name, arguments, context)

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage:
        transport = await self._connected_transport()
        return await transport.list_resource_page(context, cursor=cursor)

    async def list_resources(self, context: MCPRequestContext) -> tuple[MCPResourceDescriptor, ...]:
        transport = await self._connected_transport()
        return await transport.list_resources(context)

    async def read_resource(self, uri: str, context: MCPRequestContext) -> MCPResourceContents:
        transport = await self._connected_transport()
        return await transport.read_resource(uri, context)

    async def subscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        transport = await self._connected_transport()
        await transport.subscribe_resource(uri, context)

    async def unsubscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        transport = await self._connected_transport()
        await transport.unsubscribe_resource(uri, context)

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification:
        transport = await self._connected_transport()
        return await transport.next_resource_notification(
            timeout_seconds=timeout_seconds
        )

    async def next_tool_list_changed(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        transport = await self._connected_transport()
        await transport.next_tool_list_changed(timeout_seconds=timeout_seconds)

    async def aclose(self) -> None:
        async with self._owner_lock:
            owner, self._owner = self._owner, None
        if owner is not None:
            await self._stop_owner(owner)

    async def _connected_transport(self) -> OfficialSDKSessionTransport:
        async with self._owner_lock:
            owner = self._owner
            if owner is None:
                owner = self._new_owner()
                self._owner = owner
        if owner.task is not None and owner.task.done():
            await self._discard_owner(owner)
            raise self._owner_disconnected(owner) from owner.failure
        try:
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                transport = await asyncio.shield(owner.ready)
        except BaseException:
            await self._discard_owner(owner)
            raise
        if owner.task is not None and owner.task.done():
            await self._discard_owner(owner)
            raise self._owner_disconnected(owner) from owner.failure
        return transport

    def _new_owner(self) -> _OfficialSDKConnectionOwner:
        loop = asyncio.get_running_loop()
        owner = _OfficialSDKConnectionOwner(
            stop=asyncio.Event(),
            ready=loop.create_future(),
        )
        owner.task = asyncio.create_task(
            self._run_connection_owner(owner),
            name=f"mcp-{self._config.transport}-connection-owner",
        )
        return owner

    async def _run_connection_owner(
        self,
        owner: _OfficialSDKConnectionOwner,
    ) -> None:
        try:
            async with self._open_connection() as transport:
                if not owner.ready.done():
                    owner.ready.set_result(transport)
                await owner.stop.wait()
        except BaseException as exc:
            owner.failure = exc
            if not owner.ready.done():
                if owner.stop.is_set():
                    owner.ready.cancel()
                elif isinstance(exc, Exception):
                    owner.ready.set_exception(exc)
                else:
                    owner.ready.set_exception(self._owner_disconnected(owner))

    @asynccontextmanager
    async def _open_connection(
        self,
    ) -> AsyncIterator[OfficialSDKSessionTransport]:
        # Optional MCP runtime imports stay off the compatibility-only API path.
        import httpx
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.streamable_http import streamable_http_client

        from mcp_servers.common.sdk_adapter import MCP_PROTOCOL_VERSION

        resource_notifications = MCPResourceNotificationBuffer()
        async with AsyncExitStack() as stack:
            if self._config.transport == "stdio":
                if not self._config.stdio_command:
                    raise MCPProtocolError(
                        "mcp_client_misconfigured",
                        "MCP stdio 未配置服务命令",
                    )
                params = StdioServerParameters(
                    command=self._config.stdio_command[0],
                    args=list(self._config.stdio_command[1:]),
                    env=dict(self._config.stdio_env) or None,
                    cwd=self._config.stdio_cwd,
                )
                read_stream, write_stream = await stack.enter_async_context(stdio_client(params))
            else:
                if not self._config.http_url or not self._config.service_token:
                    raise MCPProtocolError(
                        "mcp_client_misconfigured",
                        "MCP Streamable HTTP 缺少 URL 或服务令牌",
                    )
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers={
                            "Authorization": f"Bearer {self._config.service_token}",
                        },
                        timeout=None,
                        trust_env=False,
                    )
                )
                streams = await stack.enter_async_context(
                    streamable_http_client(
                        self._config.http_url,
                        http_client=client,
                    )
                )
                read_stream, write_stream, _get_session_id = streams
            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    message_handler=resource_notifications,
                )
            )
            initialized = await session.initialize()
            if str(initialized.protocolVersion) != MCP_PROTOCOL_VERSION:
                raise MCPProtocolError(
                    "incompatible_protocol",
                    "MCP Server 协议版本不在固定 allowlist",
                )
            yield OfficialSDKSessionTransport(
                session,
                context_signing_key=(
                    self._config.context_signing_key
                    or self._config.service_token
                    or None
                ),
                resource_notifications=resource_notifications,
            )

    async def _discard_owner(self, owner: _OfficialSDKConnectionOwner) -> None:
        async with self._owner_lock:
            if self._owner is owner:
                self._owner = None
        await self._stop_owner(owner)

    @staticmethod
    async def _stop_owner(owner: _OfficialSDKConnectionOwner) -> None:
        owner.stop.set()
        task = owner.task
        if task is None or task is asyncio.current_task():
            return
        if not owner.ready.done():
            # A stop event cannot interrupt SDK initialize(). Cancellation is
            # delivered inside the owner task, which still unwinds every
            # AnyIO context from the task that entered it.
            task.cancel()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.done():
                raise

    @staticmethod
    def _owner_disconnected(owner: _OfficialSDKConnectionOwner) -> ConnectionError:
        detail = (
            f": {type(owner.failure).__name__}"
            if owner.failure is not None
            else ""
        )
        return ConnectionError(f"MCP SDK connection owner stopped{detail}")


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    healthy: bool
    expected_count: int
    discovered_count: int
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    mismatched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class MCPToolCatalogUpdate:
    """One strictly validated Tool catalog generation ready for publication."""

    descriptors: tuple[MCPToolDescriptor, ...]
    content_hash: str
    report: DiscoveryReport


def _tool_catalog_update(
    descriptors: tuple[MCPToolDescriptor, ...],
    report: DiscoveryReport,
) -> MCPToolCatalogUpdate:
    ordered = tuple(sorted(descriptors, key=lambda item: item.name))
    return MCPToolCatalogUpdate(
        descriptors=ordered,
        content_hash=stable_hash([item.to_protocol_dict() for item in ordered]),
        report=report,
    )


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    tool_name: str
    schema_match: bool
    output_valid: bool
    artifact_match: bool
    equivalent: bool
    result_hash: str | None
    code: str

    def evidence_fields(self) -> dict[str, Any]:
        return {
            "mcp_contract_validation": self.code,
            "mcp_contract_equivalent": self.equivalent,
        }


class MCPClientGateway:
    """Fail-closed discovery/call boundary for an allowlisted MCP server."""

    def __init__(
        self,
        transport: MCPTransport,
        expected: Iterable[MCPToolDescriptor],
        *,
        allowed_tools: frozenset[str],
    ) -> None:
        self._transport = transport
        self._expected = {tool.name: tool for tool in expected}
        self._allowed_tools = allowed_tools
        self._discovered: dict[str, MCPToolDescriptor] = {}
        self._healthy = False
        self._last_report: DiscoveryReport | None = None

    @property
    def last_discovery_report(self) -> DiscoveryReport | None:
        return self._last_report

    @property
    def discovered_tools(self) -> tuple[MCPToolDescriptor, ...]:
        if not self._healthy:
            return ()
        return tuple(self._discovered[name] for name in sorted(self._discovered))

    async def refresh(self) -> DiscoveryReport:
        discovered_tools = await self._transport.list_tools()
        discovered = {tool.name: tool for tool in discovered_tools}
        duplicate_names = {
            tool.name
            for tool in discovered_tools
            if sum(item.name == tool.name for item in discovered_tools) > 1
        }
        expected_names = set(self._expected) & set(self._allowed_tools)
        discovered_names = set(discovered)
        missing = tuple(sorted(expected_names - discovered_names))
        unexpected = tuple(sorted(discovered_names - expected_names))
        mismatched = tuple(
            sorted(
                duplicate_names
                | {
                    name
                    for name in expected_names & discovered_names
                    if self._expected[name].contract_hash
                    != discovered[name].contract_hash
                }
            )
        )
        self._healthy = not missing and not unexpected and not mismatched
        self._discovered = discovered if self._healthy else {}
        report = DiscoveryReport(
            healthy=self._healthy,
            expected_count=len(expected_names),
            discovered_count=len(discovered_names),
            missing=missing,
            unexpected=unexpected,
            mismatched=mismatched,
        )
        self._last_report = report
        _log.info(
            "mcp.discovery",
            healthy=report.healthy,
            expected_count=report.expected_count,
            discovered_count=report.discovered_count,
            missing=list(report.missing),
            unexpected=list(report.unexpected),
            mismatched=list(report.mismatched),
        )
        return report

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult:
        if not self._healthy or name not in self._discovered:
            raise MCPProtocolError("mcp_server_unhealthy", "MCP Server 尚未通过工具发现校验")
        if name not in self._allowed_tools:
            raise MCPProtocolError("tool_not_allowlisted", f"MCP 工具未获准: {name}")
        expected = self._expected[name]
        validate_json(
            arguments,
            expected.input_schema,
            code="invalid_arguments",
            label="MCP 工具输入",
        )
        _log.info("mcp.call_started", tool=name, invocation_id=context.invocation_id)
        result = await self._transport.call_tool(name, arguments, context)
        _log.info(
            "mcp.call_finished",
            tool=name,
            invocation_id=context.invocation_id,
            is_error=result.is_error,
            error_code=result.error_code,
        )
        if result.is_error:
            return result
        structured = normalize_structured_result(result.structured_content)
        validate_json(
            structured,
            expected.output_schema,
            code="invalid_tool_output",
            label="MCP 工具输出",
        )
        return MCPCallResult.success(name, structured)

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage:
        self._require_healthy_resources()
        return await self._transport.list_resource_page(context, cursor=cursor)

    async def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]:
        self._require_healthy_resources()
        return await self._transport.list_resources(context)

    async def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents:
        self._require_healthy_resources()
        return await self._transport.read_resource(uri, context)

    async def subscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        self._require_healthy_resources()
        await self._transport.subscribe_resource(uri, context)

    async def unsubscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        self._require_healthy_resources()
        await self._transport.unsubscribe_resource(uri, context)

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification:
        self._require_healthy_resources()
        return await self._transport.next_resource_notification(
            timeout_seconds=timeout_seconds
        )

    async def next_tool_list_changed(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._require_healthy_resources()
        await self._transport.next_tool_list_changed(timeout_seconds=timeout_seconds)

    def _require_healthy_resources(self) -> None:
        if not self._healthy:
            raise MCPProtocolError(
                "mcp_server_unhealthy",
                "MCP Server 尚未通过工具发现校验",
            )

    async def aclose(self) -> None:
        await self._transport.aclose()
        self._healthy = False
        self._discovered = {}


@dataclass(frozen=True, slots=True)
class GatewayHealth:
    """Observable connection state; it is not a replacement for discovery."""

    state: Literal["cold", "healthy", "degraded", "unhealthy", "closed"]
    transport: MCPTransportName
    generation: int = 0
    consecutive_failures: int = 0
    last_error_code: str | None = None


@dataclass(frozen=True, slots=True)
class MCPExecutionResult:
    """One successful transport-neutral execution and its routing evidence."""

    result: dict[str, Any]
    transport: MCPTransportName
    degraded: bool
    health: GatewayHealth
    service_name: str | None = None


class MCPGatewayExecutionError(MCPProtocolError):
    """Stable execution error with ambiguity and transport state attached."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        result_unknown: bool = False,
        transport: MCPTransportName,
    ) -> None:
        super().__init__(code, message, retryable=retryable)
        self.result_unknown = result_unknown
        self.transport = transport


TransportFactory = Callable[[], Awaitable[MCPTransport] | MCPTransport]


class ManagedMCPClientGateway:
    """Own connection lifecycle, health, bounded reconnect and safe degradation.

    Calls that may have reached the server are retried only when the canonical
    contract declares them read-only and idempotent. A compatibility fallback is
    eligible only for connection failures before ``tools/call`` and never for
    discovery/schema/auth drift.
    """

    def __init__(
        self,
        *,
        config: MCPClientConfig,
        expected: Iterable[MCPToolDescriptor],
        allowed_tools: frozenset[str],
        transport_factory: TransportFactory,
        compatibility_transport_factory: TransportFactory | None = None,
    ) -> None:
        self._config = config
        self._expected = {item.name: item for item in expected}
        self._allowed_tools = allowed_tools
        self._transport_factory = transport_factory
        self._compatibility_transport_factory = compatibility_transport_factory
        self._gateway: MCPClientGateway | None = None
        self._fallback_gateway: MCPClientGateway | None = None
        self._lock = asyncio.Lock()
        self._resource_subscriptions: dict[
            tuple[str, str, str, str],
            tuple[str, MCPRequestContext],
        ] = {}
        self._health = GatewayHealth("cold", config.transport)

    @property
    def health(self) -> GatewayHealth:
        return self._health

    async def validate_catalog(self) -> MCPToolCatalogUpdate:
        """Rediscover and publish only an exact Host-approved Tool catalog."""
        gateway: MCPClientGateway | None = None
        try:
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                gateway = await self._ready_gateway()
                report = await gateway.refresh()
        except TimeoutError as exc:
            await self._invalidate("mcp_connect_timeout")
            raise MCPProtocolError(
                "mcp_connect_timeout",
                "MCP 工具目录校验超时",
                retryable=True,
            ) from exc
        except MCPProtocolError as exc:
            await self._invalidate(exc.code)
            raise
        except Exception as exc:
            error_code = _connection_error_code(exc)
            await self._invalidate(error_code)
            raise MCPProtocolError(
                error_code,
                "MCP 工具目录校验失败",
                retryable=error_code != "mcp_authentication_failed",
            ) from exc
        if not report.healthy:
            await self._invalidate("mcp_catalog_drift")
            raise MCPProtocolError(
                "mcp_catalog_drift",
                "MCP 工具发现结果与静态契约不一致",
            )
        self._mark_healthy()
        return _tool_catalog_update(gateway.discovered_tools, report)

    async def next_tool_catalog_update(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPToolCatalogUpdate:
        """Wait for ``tools/list_changed`` and strictly validate its replacement."""
        try:
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                gateway = await self._ready_gateway()
        except TimeoutError as exc:
            await self._invalidate("mcp_connect_timeout")
            raise MCPProtocolError(
                "mcp_connect_timeout",
                "MCP Tool 目录通知连接超时",
                retryable=True,
            ) from exc
        except MCPProtocolError:
            raise
        except Exception as exc:
            error_code = _connection_error_code(exc)
            await self._invalidate(error_code)
            raise MCPProtocolError(
                error_code,
                "MCP Tool 目录通知连接中断",
                retryable=error_code != "mcp_authentication_failed",
            ) from exc
        try:
            await gateway.next_tool_list_changed(timeout_seconds=timeout_seconds)
        except TimeoutError:
            # A quiet, healthy Tool notification stream is an ordinary timeout.
            # Callers decide whether and when to poll again.
            raise
        except MCPProtocolError as exc:
            if exc.code != "tool_notifications_unavailable":
                await self._invalidate(exc.code)
            raise
        except Exception as exc:
            error_code = _connection_error_code(exc)
            await self._invalidate(error_code)
            raise MCPProtocolError(
                error_code,
                "MCP Tool 目录通知连接中断",
                retryable=error_code != "mcp_authentication_failed",
            ) from exc
        try:
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                report = await gateway.refresh()
        except TimeoutError as exc:
            await self._invalidate("mcp_connect_timeout")
            raise MCPProtocolError(
                "mcp_connect_timeout",
                "MCP Tool 目录变更校验超时",
                retryable=True,
            ) from exc
        except MCPProtocolError as exc:
            await self._invalidate(exc.code)
            raise
        except Exception as exc:
            error_code = _connection_error_code(exc)
            await self._invalidate(error_code)
            raise MCPProtocolError(
                error_code,
                "MCP Tool 目录通知连接中断",
                retryable=error_code != "mcp_authentication_failed",
            ) from exc
        if not report.healthy:
            await self._invalidate("mcp_catalog_drift")
            raise MCPProtocolError(
                "mcp_catalog_drift",
                "MCP Tool 目录变更未通过 Host 契约校验",
            )
        self._mark_healthy()
        return _tool_catalog_update(gateway.discovered_tools, report)

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage:
        return await self._run_resource_operation(
            lambda gateway: gateway.list_resource_page(context, cursor=cursor)
        )

    async def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]:
        return await self._run_resource_operation(
            lambda gateway: gateway.list_resources(context)
        )

    async def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents:
        return await self._run_resource_operation(
            lambda gateway: gateway.read_resource(uri, context)
        )

    async def subscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        await self._run_resource_operation(
            lambda gateway: gateway.subscribe_resource(uri, context)
        )
        key = self._resource_subscription_key(uri, context)
        async with self._lock:
            self._resource_subscriptions[key] = (uri, context)

    async def unsubscribe_resource(self, uri: str, context: MCPRequestContext) -> None:
        key = self._resource_subscription_key(uri, context)
        async with self._lock:
            self._resource_subscriptions.pop(key, None)
        await self._run_resource_operation(
            lambda gateway: gateway.unsubscribe_resource(uri, context)
        )

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification:
        attempts = 1 + self._config.max_reconnects
        for attempt in range(attempts):
            gateway: MCPClientGateway | None = None
            try:
                async with asyncio.timeout(self._config.connect_timeout_seconds):
                    gateway = await self._ready_gateway()
                return await gateway.next_resource_notification(
                    timeout_seconds=timeout_seconds
                )
            except TimeoutError:
                if gateway is None:
                    await self._invalidate("mcp_transport_disconnected")
                    if attempt + 1 < attempts:
                        continue
                    raise MCPProtocolError(
                        "mcp_transport_disconnected",
                        "MCP Resource 通知重连超时",
                        retryable=True,
                    ) from None
                # A quiet subscription and a dead SDK reader both surface as an
                # empty notification queue. Probe discovery before deciding: a
                # healthy session preserves ordinary timeout semantics, while a
                # dead one is rebuilt and all desired subscriptions are restored.
                try:
                    async with asyncio.timeout(
                        self._config.connect_timeout_seconds
                    ):
                        await gateway.refresh()
                except Exception as exc:
                    await self._invalidate(_connection_error_code(exc))
                    if attempt + 1 < attempts:
                        continue
                    raise MCPProtocolError(
                        "mcp_transport_disconnected",
                        "MCP Resource 通知连接中断",
                        retryable=True,
                    ) from exc
                raise
            except MCPProtocolError:
                raise
            except Exception as exc:
                await self._invalidate(_connection_error_code(exc))
                if attempt + 1 < attempts:
                    continue
                raise MCPProtocolError(
                    _connection_error_code(exc),
                    "MCP Resource 通知连接中断",
                    retryable=True,
                ) from exc
        raise AssertionError("MCP Resource notification reconnect loop exhausted")

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        context: MCPRequestContext,
        *,
        timeout_seconds: float,
    ) -> MCPExecutionResult:
        descriptor = self._expected.get(name)
        if descriptor is None or name not in self._allowed_tools:
            raise MCPGatewayExecutionError(
                "tool_not_allowlisted",
                f"MCP 工具未获准: {name}",
                transport=self._config.transport,
            )
        context.validate()
        try:
            validate_tool_approval(descriptor, arguments, context)
        except MCPProtocolError as exc:
            raise MCPGatewayExecutionError(
                exc.code,
                exc.message,
                retryable=exc.retryable,
                transport=self._config.transport,
            ) from exc
        attempts = 1 + (
            self._config.max_reconnects
            if descriptor.metadata.read_only and descriptor.metadata.idempotent
            else 0
        )
        for attempt in range(attempts):
            call_started = False
            try:
                async with asyncio.timeout(timeout_seconds):
                    gateway = await self._ready_gateway()
                    call_started = True
                    call_result = await gateway.call_tool(name, arguments, context)
                if call_result.is_error:
                    raise MCPGatewayExecutionError(
                        call_result.error_code or "mcp_tool_error",
                        call_result.text or "MCP 工具调用失败",
                        retryable=call_result.retryable,
                        transport=self._config.transport,
                    )
                assert call_result.structured_content is not None
                self._mark_healthy()
                return MCPExecutionResult(
                    result=call_result.structured_content,
                    transport=self._config.transport,
                    degraded=False,
                    health=self._health,
                )
            except asyncio.CancelledError:
                await self._invalidate("mcp_call_cancelled")
                raise
            except TimeoutError as exc:
                error_code = "mcp_call_timeout" if call_started else "mcp_connect_timeout"
                await self._invalidate(error_code)
                raise MCPGatewayExecutionError(
                    error_code,
                    (
                        f"MCP 工具执行超过 {timeout_seconds:g} 秒，结果状态未知。"
                        if call_started
                        else f"MCP 服务连接超过 {timeout_seconds:g} 秒。"
                    ),
                    retryable=not call_started,
                    result_unknown=call_started,
                    transport=self._config.transport,
                ) from exc
            except MCPGatewayExecutionError:
                raise
            except MCPProtocolError as exc:
                # Contract/policy failures are deterministic and must fail closed.
                await self._invalidate(exc.code)
                raise MCPGatewayExecutionError(
                    exc.code,
                    exc.message,
                    retryable=exc.retryable,
                    result_unknown=call_started,
                    transport=self._config.transport,
                ) from exc
            except Exception as exc:
                error_code = _connection_error_code(exc)
                await self._invalidate(error_code)
                if error_code == "mcp_authentication_failed":
                    raise MCPGatewayExecutionError(
                        error_code,
                        "MCP 服务认证失败",
                        transport=self._config.transport,
                    ) from exc
                if call_started and attempt + 1 < attempts:
                    _log.warning(
                        "mcp.call_reconnect",
                        tool=name,
                        attempt=attempt + 1,
                        max_attempts=attempts,
                        error_type=type(exc).__name__,
                    )
                    continue
                if (
                    not call_started
                    and self._config.allow_in_process_fallback
                    and descriptor.metadata.read_only
                    and descriptor.metadata.idempotent
                ):
                    return await self._execute_fallback(
                        name,
                        arguments,
                        context,
                        timeout_seconds=timeout_seconds,
                    )
                raise MCPGatewayExecutionError(
                    error_code,
                    "MCP 传输连接中断" + ("，调用结果状态未知。" if call_started else "。"),
                    retryable=not call_started,
                    result_unknown=call_started,
                    transport=self._config.transport,
                ) from exc
        raise AssertionError("MCP reconnect loop exhausted without result")

    async def aclose(self) -> None:
        async with self._lock:
            gateway, self._gateway = self._gateway, None
            fallback, self._fallback_gateway = self._fallback_gateway, None
            self._resource_subscriptions.clear()
            self._health = GatewayHealth(
                "closed",
                self._config.transport,
                generation=self._health.generation,
                consecutive_failures=self._health.consecutive_failures,
                last_error_code=self._health.last_error_code,
            )
        if gateway is not None:
            await self._close_gateway(gateway)
        if fallback is not None:
            await self._close_gateway(fallback)

    async def _ready_gateway(self) -> MCPClientGateway:
        if self._gateway is not None:
            return self._gateway
        async with self._lock:
            if self._gateway is not None:
                return self._gateway
            created = self._transport_factory()
            transport = await created if inspect.isawaitable(created) else created
            gateway = MCPClientGateway(
                transport,
                self._expected.values(),
                allowed_tools=self._allowed_tools,
            )
            try:
                report = await gateway.refresh()
                if report.healthy:
                    for uri, context in self._resource_subscriptions.values():
                        await gateway.subscribe_resource(uri, context)
            except BaseException:
                await self._close_gateway(gateway)
                raise
            if not report.healthy:
                await gateway.aclose()
                self._mark_unhealthy("mcp_catalog_drift")
                raise MCPProtocolError(
                    "mcp_catalog_drift",
                    "MCP 工具发现结果与静态契约不一致",
                )
            self._gateway = gateway
            self._health = GatewayHealth(
                "healthy",
                self._config.transport,
                generation=self._health.generation + 1,
            )
            return gateway

    async def _run_resource_operation(
        self,
        operation: Callable[[MCPClientGateway], Awaitable[_ResourceResult]],
    ) -> _ResourceResult:
        """Retry transport failures for read-only/idempotent Resource operations."""
        attempts = 1 + self._config.max_reconnects
        for attempt in range(attempts):
            try:
                async with asyncio.timeout(self._config.connect_timeout_seconds):
                    return await operation(await self._ready_gateway())
            except MCPProtocolError as exc:
                # ChatBI server decisions always carry a namespaced stable code.
                # A bare SDK Resource error has no reviewed server decision and
                # commonly represents a stateful session that disappeared.
                if exc.code != "mcp_resource_error":
                    # Signed-context, cursor and authorization failures are
                    # deterministic and must never reconnect.
                    raise
                await self._invalidate("mcp_transport_disconnected")
                if attempt + 1 < attempts:
                    continue
                raise MCPProtocolError(
                    "mcp_transport_disconnected",
                    "MCP Resource 传输连接中断",
                    retryable=True,
                ) from exc
            except Exception as exc:
                error_code = _connection_error_code(exc)
                await self._invalidate(error_code)
                if attempt + 1 < attempts:
                    continue
                raise MCPProtocolError(
                    error_code,
                    "MCP Resource 传输连接中断",
                    retryable=error_code != "mcp_authentication_failed",
                ) from exc
        raise AssertionError("MCP Resource reconnect loop exhausted")

    @staticmethod
    def _resource_subscription_key(
        uri: str,
        context: MCPRequestContext,
    ) -> tuple[str, str, str, str]:
        return (
            context.subject_id,
            context.project_id,
            context.conversation_id,
            uri,
        )

    async def _execute_fallback(
        self,
        name: str,
        arguments: dict[str, Any],
        context: MCPRequestContext,
        *,
        timeout_seconds: float,
    ) -> MCPExecutionResult:
        if self._compatibility_transport_factory is None:
            raise MCPGatewayExecutionError(
                "mcp_transport_disconnected",
                "MCP 传输不可用，且未配置兼容降级。",
                retryable=True,
                transport=self._config.transport,
            )
        if self._fallback_gateway is None:
            created = self._compatibility_transport_factory()
            transport = await created if inspect.isawaitable(created) else created
            fallback = MCPClientGateway(
                transport,
                self._expected.values(),
                allowed_tools=self._allowed_tools,
            )
            try:
                report = await fallback.refresh()
            except BaseException:
                await self._close_gateway(fallback)
                raise
            if not report.healthy:
                await fallback.aclose()
                raise MCPGatewayExecutionError(
                    "mcp_fallback_catalog_drift",
                    "兼容执行器契约不一致",
                    transport=self._config.transport,
                )
            self._fallback_gateway = fallback
        async with asyncio.timeout(timeout_seconds):
            result = await self._fallback_gateway.call_tool(name, arguments, context)
        if result.is_error or result.structured_content is None:
            raise MCPGatewayExecutionError(
                result.error_code or "mcp_tool_error",
                result.text or "兼容执行器调用失败",
                retryable=result.retryable,
                transport=self._config.transport,
            )
        self._health = GatewayHealth(
            "degraded",
            self._config.transport,
            generation=self._health.generation,
            consecutive_failures=self._health.consecutive_failures,
            last_error_code=self._health.last_error_code,
        )
        _log.warning("mcp.compatibility_fallback", tool=name)
        return MCPExecutionResult(
            result=result.structured_content,
            transport="in_process",
            degraded=True,
            health=self._health,
        )

    async def _invalidate(self, error_code: str) -> None:
        async with self._lock:
            gateway, self._gateway = self._gateway, None
            self._mark_unhealthy(error_code)
        if gateway is not None:
            await self._close_gateway(gateway)

    def _mark_healthy(self) -> None:
        self._health = GatewayHealth(
            "healthy",
            self._config.transport,
            generation=self._health.generation,
        )

    def _mark_unhealthy(self, error_code: str) -> None:
        self._health = GatewayHealth(
            "unhealthy",
            self._config.transport,
            generation=self._health.generation,
            consecutive_failures=self._health.consecutive_failures + 1,
            last_error_code=error_code,
        )

    async def _close_gateway(self, gateway: MCPClientGateway) -> None:
        try:
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                await gateway.aclose()
        except TimeoutError:
            _log.warning("mcp.transport_close_timeout")
        except Exception as exc:
            _log.warning(
                "mcp.transport_close_failed",
                error_type=type(exc).__name__,
            )


def client_config_from_json(
    *,
    transport: MCPTransportName,
    stdio_command_json: str = "",
    stdio_cwd: str = "",
    http_url: str = "",
    service_token: str = "",
    context_signing_key: str = "",
    service_urls_json: str = "",
    service_tokens_json: str = "",
    connect_timeout_seconds: float = 10.0,
    max_reconnects: int = 1,
    allow_in_process_fallback: bool = False,
) -> MCPClientConfig:
    """Parse settings without allowing shell interpretation of stdio commands."""
    command: tuple[str, ...] = ()
    if stdio_command_json.strip():
        try:
            raw = json.loads(stdio_command_json)
        except json.JSONDecodeError as exc:
            raise ValueError("MCP stdio command 必须是 JSON 字符串数组") from exc
        if (
            not isinstance(raw, list)
            or not raw
            or any(not isinstance(item, str) or not item.strip() for item in raw)
        ):
            raise ValueError("MCP stdio command 必须是非空 JSON 字符串数组")
        command = tuple(raw)
    service_urls = _string_mapping_from_json(service_urls_json, label="MCP 服务 URL")
    service_tokens = _string_mapping_from_json(service_tokens_json, label="MCP 服务令牌")
    return MCPClientConfig(
        transport=transport,
        stdio_command=command,
        stdio_cwd=stdio_cwd.strip() or None,
        http_url=http_url.strip(),
        service_token=service_token,
        context_signing_key=context_signing_key,
        service_urls=service_urls,
        service_tokens=service_tokens,
        connect_timeout_seconds=connect_timeout_seconds,
        max_reconnects=max_reconnects,
        allow_in_process_fallback=allow_in_process_fallback,
    )


def _string_mapping_from_json(raw: str, *, label: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}必须是 JSON 对象") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not key.strip() or not isinstance(item, str) or not item.strip()
        for key, item in value.items()
    ):
        raise ValueError(f"{label}必须是非空字符串到非空字符串的 JSON 对象")
    return {key.strip(): item.strip() for key, item in value.items()}


def _connection_error_code(exc: Exception) -> str:
    """Classify authentication without coupling the core gateway to httpx."""
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code in {401, 403}:
        return "mcp_authentication_failed"
    text = str(exc).lower()
    if any(marker in text for marker in ("401 unauthorized", "403 forbidden")):
        return "mcp_authentication_failed"
    return "mcp_transport_disconnected"


def _validate_resource_contract(meta: Mapping[str, Any]) -> None:
    if meta.get(f"{CHATBI_META_PREFIX}contract-version") != MCP_RESOURCE_CONTRACT_VERSION:
        raise MCPProtocolError(
            "incompatible_resource_contract",
            "MCP Resource 契约版本不受支持",
        )


def _resource_notification_metadata(params: Any) -> dict[str, Any]:
    meta = getattr(params, "meta", None)
    if meta is None:
        return {}
    if isinstance(meta, Mapping):
        return dict(meta)
    model_dump = getattr(meta, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump(by_alias=True)
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _resource_notification_from_meta(
    *,
    kind: Literal["list_changed", "updated"],
    uri: str | None,
    metadata: Mapping[str, Any],
) -> MCPResourceNotification:
    _validate_resource_contract(metadata)
    catalog_version = metadata.get(f"{CHATBI_META_PREFIX}catalog-version")
    previous_catalog_version = metadata.get(
        f"{CHATBI_META_PREFIX}previous-catalog-version"
    )
    if (
        not isinstance(catalog_version, str)
        or len(catalog_version) != 64
        or not isinstance(previous_catalog_version, str)
        or len(previous_catalog_version) != 64
    ):
        raise MCPProtocolError(
            "invalid_resource_notification",
            "MCP Resource 通知缺少目录版本",
        )
    return MCPResourceNotification(
        kind=kind,
        uri=uri,
        catalog_version=catalog_version,
        previous_catalog_version=previous_catalog_version,
    )


def _resource_protocol_error(exc: Any) -> MCPProtocolError:
    error = getattr(exc, "error", None)
    data = getattr(error, "data", None)
    code = data.get("com.chatbi/error-code") if isinstance(data, dict) else None
    retryable = data.get("com.chatbi/retryable") is True if isinstance(data, dict) else False
    message = getattr(error, "message", None)
    return MCPProtocolError(
        code if isinstance(code, str) else "mcp_resource_error",
        message if isinstance(message, str) else "MCP Resource 请求失败",
        retryable=retryable,
    )


class MCPShadowComparator:
    """Validate a live outcome and Host-owned Artifact against the MCP contract.

    It never invokes a tool, so enabling the shadow cannot duplicate mutations or
    create a second Artifact. It remains useful after the canonical switch because
    Artifact creation is a Host postcondition outside the remote tools/call result.
    """

    def __init__(self, expected: Iterable[MCPToolDescriptor]) -> None:
        self._expected = {descriptor.name: descriptor for descriptor in expected}

    def compare_catalog(self, discovered: Iterable[MCPToolDescriptor]) -> DiscoveryReport:
        actual = {descriptor.name: descriptor for descriptor in discovered}
        expected_names = set(self._expected)
        actual_names = set(actual)
        missing = tuple(sorted(expected_names - actual_names))
        unexpected = tuple(sorted(actual_names - expected_names))
        mismatched = tuple(
            sorted(
                name
                for name in expected_names & actual_names
                if self._expected[name].contract_hash != actual[name].contract_hash
            )
        )
        return DiscoveryReport(
            healthy=not missing and not unexpected and not mismatched,
            expected_count=len(expected_names),
            discovered_count=len(actual_names),
            missing=missing,
            unexpected=unexpected,
            mismatched=mismatched,
        )

    def compare_success(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        artifact: ArtifactDraft | None,
    ) -> ShadowComparison:
        descriptor = self._expected.get(tool_name)
        if descriptor is None:
            return self._record(
                ShadowComparison(tool_name, False, False, False, False, None, "unknown_tool")
            )
        try:
            validate_json(
                arguments,
                descriptor.input_schema,
                code="invalid_arguments",
                label="影子调用入参",
            )
            normalized = normalize_structured_result(result)
            validate_json(
                normalized,
                descriptor.output_schema,
                code="invalid_tool_output",
                label="影子调用输出",
            )
            output_valid = True
            result_hash = stable_hash(normalized)
        except MCPProtocolError as exc:
            return self._record(
                ShadowComparison(
                    tool_name,
                    True,
                    False,
                    False,
                    False,
                    None,
                    exc.code,
                )
            )
        expected_artifacts = descriptor.metadata.artifact_types
        artifact_match = (
            artifact is None
            if not expected_artifacts
            else artifact is not None and artifact.type in expected_artifacts
        )
        equivalent = output_valid and artifact_match
        return self._record(
            ShadowComparison(
                tool_name,
                True,
                output_valid,
                artifact_match,
                equivalent,
                result_hash,
                "equivalent" if equivalent else "artifact_postcondition_mismatch",
            )
        )

    def compare_error(self, tool_name: str, error_code: str) -> ShadowComparison:
        known = tool_name in self._expected
        return self._record(
            ShadowComparison(
                tool_name,
                known,
                False,
                True,
                known,
                None,
                f"error:{error_code}" if known else "unknown_tool",
            )
        )

    @staticmethod
    def _record(comparison: ShadowComparison) -> ShadowComparison:
        level = _log.info if comparison.equivalent else _log.warning
        level(
            "mcp.shadow_comparison",
            tool=comparison.tool_name,
            code=comparison.code,
            schema_match=comparison.schema_match,
            output_valid=comparison.output_valid,
            artifact_match=comparison.artifact_match,
            equivalent=comparison.equivalent,
            result_hash=comparison.result_hash,
        )
        return comparison
