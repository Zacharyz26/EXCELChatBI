"""Governed MCP Client Gateway and migration-time shadow comparison."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

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
)

_log = get_logger("mcp.client_gateway")


class MCPTransport(Protocol):
    """Minimal transport surface used by the gateway after SDK initialization."""

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]: ...

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult: ...


class InProcessMCPTransport:
    """Migration transport that exercises the same contract exactly once."""

    def __init__(self, adapter: MCPServerAdapter) -> None:
        self._adapter = adapter

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return self._adapter.list_tools()

    async def call_tool(
        self, name: str, arguments: dict[str, Any], context: MCPRequestContext
    ) -> MCPCallResult:
        return await asyncio.to_thread(self._adapter.call_tool, name, arguments, context)


class OfficialSDKSessionTransport:
    """Adapter over an initialized official ``mcp.ClientSession`` instance."""

    def __init__(self, session: Any) -> None:
        self._session = session

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
            meta=context.to_request_meta(),
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
            "mcp_shadow": self.code,
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
        result = await self._transport.call_tool(name, arguments, context)
        if result.is_error:
            return result
        expected = self._expected[name]
        structured = normalize_structured_result(result.structured_content)
        validate_json(
            structured,
            expected.output_schema,
            code="invalid_tool_output",
            label="MCP 工具输出",
        )
        return MCPCallResult.success(name, structured)


class MCPShadowComparator:
    """Compare the live in-process outcome with the canonical MCP contract.

    It never invokes a tool, so enabling the shadow cannot duplicate mutations or
    create a second Artifact.  The real MCP call path is exercised separately by
    ``MCPClientGateway`` contract tests until stage 2 switches the executor.
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
