"""Governed MCP Client Gateway, managed transports and contract comparison."""

from __future__ import annotations

import asyncio
import inspect
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from packages.common.logging import get_logger
from packages.session.models import ArtifactDraft

from mcp_servers.common.adapter import MCPServerAdapter
from mcp_servers.common.contracts import (
    MCPCallResult,
    MCPProtocolError,
    MCPRequestContext,
    MCPToolDescriptor,
    ToolCapabilityMetadata,
    normalize_structured_result,
    stable_hash,
    validate_json,
    validate_tool_approval,
)

_log = get_logger("mcp.client_gateway")

MCPTransportName = Literal["in_process", "stdio", "streamable_http"]


class MCPTransport(Protocol):
    """Minimal transport surface used by the gateway after SDK initialization."""

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult: ...

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

    async def aclose(self) -> None:
        return None


class OfficialSDKSessionTransport:
    """Adapter over an initialized official ``mcp.ClientSession`` instance."""

    def __init__(
        self,
        session: Any,
        *,
        context_signing_key: str | None = None,
    ) -> None:
        self._session = session
        self._context_signing_key = context_signing_key

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

    async def aclose(self) -> None:
        return None


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

    A connection owns one initialized ``ClientSession``. Closing an unhealthy
    connection terminates the underlying subprocess/HTTP session, so the next
    gateway call performs a clean initialize + tools/list cycle.
    """

    def __init__(self, config: MCPClientConfig) -> None:
        if config.transport not in {"stdio", "streamable_http"}:
            raise ValueError("OfficialSDKClientTransport 仅支持 stdio/streamable_http")
        self._config = config
        self._stack: AsyncExitStack | None = None
        self._session_transport: OfficialSDKSessionTransport | None = None

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        transport = await self._connected_transport()
        return await transport.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult:
        transport = await self._connected_transport()
        return await transport.call_tool(name, arguments, context)

    async def aclose(self) -> None:
        stack, self._stack = self._stack, None
        self._session_transport = None
        if stack is not None:
            await stack.aclose()

    async def _connected_transport(self) -> OfficialSDKSessionTransport:
        if self._session_transport is not None:
            return self._session_transport
        try:
            async with asyncio.timeout(self._config.connect_timeout_seconds):
                await self._connect()
        except BaseException:
            await self.aclose()
            raise
        assert self._session_transport is not None
        return self._session_transport

    async def _connect(self) -> None:
        # Optional MCP runtime imports stay off the compatibility-only API path.
        import httpx
        from mcp import ClientSession
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp.client.streamable_http import streamable_http_client

        from mcp_servers.common.sdk_adapter import MCP_PROTOCOL_VERSION

        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
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
                read_stream, write_stream = await stack.enter_async_context(
                    stdio_client(params)
                )
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
                ClientSession(read_stream, write_stream)
            )
            initialized = await session.initialize()
            if str(initialized.protocolVersion) != MCP_PROTOCOL_VERSION:
                raise MCPProtocolError(
                    "incompatible_protocol",
                    "MCP Server 协议版本不在固定 allowlist",
                )
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session_transport = OfficialSDKSessionTransport(
            session,
            context_signing_key=(
                self._config.context_signing_key
                or self._config.service_token
                or None
            ),
        )


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    healthy: bool
    expected_count: int
    discovered_count: int
    missing: tuple[str, ...]
    unexpected: tuple[str, ...]
    mismatched: tuple[str, ...]


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

    async def refresh(self) -> DiscoveryReport:
        discovered_tools = await self._transport.list_tools()
        discovered = {tool.name: tool for tool in discovered_tools}
        expected_names = set(self._expected) & set(self._allowed_tools)
        discovered_names = set(discovered)
        missing = tuple(sorted(expected_names - discovered_names))
        unexpected = tuple(sorted(discovered_names - expected_names))
        mismatched = tuple(
            sorted(
                name
                for name in expected_names & discovered_names
                if self._expected[name].contract_hash != discovered[name].contract_hash
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
        self._health = GatewayHealth("cold", config.transport)

    @property
    def health(self) -> GatewayHealth:
        return self._health

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
                error_code = (
                    "mcp_call_timeout" if call_started else "mcp_connect_timeout"
                )
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
                    "MCP 传输连接中断"
                    + ("，调用结果状态未知。" if call_started else "。"),
                    retryable=not call_started,
                    result_unknown=call_started,
                    transport=self._config.transport,
                ) from exc
        raise AssertionError("MCP reconnect loop exhausted without result")

    async def aclose(self) -> None:
        async with self._lock:
            gateway, self._gateway = self._gateway, None
            fallback, self._fallback_gateway = self._fallback_gateway, None
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
    service_urls = _string_mapping_from_json(
        service_urls_json, label="MCP 服务 URL"
    )
    service_tokens = _string_mapping_from_json(
        service_tokens_json, label="MCP 服务令牌"
    )
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
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(item, str)
        or not item.strip()
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
