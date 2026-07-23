"""v2.4 MCP contract, official SDK adapter and Client Gateway tests."""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from mcp_servers.chart.server import build_server as build_chart_server  # noqa: E402
from mcp_servers.common.adapter import MCPServerAdapter, MCPToolBinding  # noqa: E402
from mcp_servers.common.catalog import tool_metadata  # noqa: E402
from mcp_servers.common.client_gateway import (  # noqa: E402
    MCPClientGateway,
    MCPShadowComparator,
    OfficialSDKSessionTransport,
)
from mcp_servers.common.contracts import (  # noqa: E402
    CHATBI_CONTEXT_KEY,
    MCPProtocolError,
    MCPRequestContext,
    MCPToolDescriptor,
)
from mcp_servers.common.sdk_adapter import build_sdk_server  # noqa: E402
from mcp_servers.dataset_ops.server import build_server as build_data_server  # noqa: E402
from mcp_servers.excel_parser.server import build_server as build_excel_server  # noqa: E402
from mcp_servers.report.server import build_server as build_report_server  # noqa: E402
from mcp_servers.stats.server import build_server as build_stats_server  # noqa: E402
from packages.session.models import ArtifactDraft  # noqa: E402

INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"value": {"type": "integer"}},
    "required": ["value"],
    "additionalProperties": False,
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"doubled": {"type": "integer"}},
    "required": ["doubled"],
    "additionalProperties": False,
}


def _context(**changes: Any) -> MCPRequestContext:
    context = MCPRequestContext(
        subject_id="user-1",
        project_id="project-1",
        conversation_id="conversation-1",
        run_id="run-1",
        plan_version=0,
        step_id="step-1",
        invocation_id="invocation-1",
        idempotency_key="idempotency-1",
        permission_snapshot_id="permissions-1",
        trace_id="trace-1",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )
    return replace(context, **changes)


def _adapter(*, bad_output: bool = False) -> MCPServerAdapter:
    descriptor = MCPToolDescriptor(
        name="double",
        description="Double an integer",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        metadata=tool_metadata("test.double", "table"),
    )
    handler = (
        (lambda _args: {"wrong": True})
        if bad_output
        else (lambda args: {"doubled": args["value"] * 2})
    )
    return MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])


def test_request_context_is_host_metadata_and_rejects_expired_deadline() -> None:
    context = _context()
    meta = context.to_request_meta()
    assert list(meta) == [CHATBI_CONTEXT_KEY]
    assert MCPRequestContext.from_request_meta(meta) == context

    expired = _context(deadline_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    with pytest.raises(MCPProtocolError, match="截止时间"):
        expired.validate()


def test_server_adapter_maps_schema_output_and_unknown_tool_errors() -> None:
    adapter = _adapter()
    success = adapter.call_tool("double", {"value": 4}, _context())
    assert success.is_error is False
    assert success.structured_content == {"doubled": 8}
    assert success.result_hash

    invalid = adapter.call_tool("double", {"value": "4"}, _context())
    assert invalid.is_error is True and invalid.error_code == "invalid_arguments"
    unknown = adapter.call_tool("missing", {}, _context())
    assert unknown.is_error is True and unknown.error_code == "tool_not_found"
    bad_output = _adapter(bad_output=True).call_tool("double", {"value": 4}, _context())
    assert bad_output.is_error is True and bad_output.error_code == "invalid_tool_output"


@pytest.mark.asyncio
async def test_client_gateway_fails_closed_on_discovery_drift() -> None:
    adapter = _adapter()
    expected = list(adapter.list_tools())

    class DriftTransport:
        async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
            drifted = replace(expected[0], description="changed")
            return (drifted,)

        async def call_tool(
            self, name: str, arguments: dict[str, Any], context: MCPRequestContext
        ) -> Any:
            raise AssertionError("unhealthy discovery must block calls")

    gateway = MCPClientGateway(
        DriftTransport(), expected, allowed_tools=frozenset({"double"})
    )
    report = await gateway.refresh()
    assert report.healthy is False and report.mismatched == ("double",)
    with pytest.raises(MCPProtocolError, match="尚未通过"):
        await gateway.call_tool("double", {"value": 2}, _context())


def test_shadow_comparison_checks_artifact_postcondition_without_second_call() -> None:
    adapter = _adapter()
    shadow = MCPShadowComparator(adapter.list_tools())
    missing = shadow.compare_success(
        tool_name="double", arguments={"value": 2}, result={"doubled": 4}, artifact=None
    )
    assert missing.equivalent is False
    assert missing.code == "artifact_postcondition_mismatch"

    artifact = ArtifactDraft(
        type="table",
        payload={"doubled": 4},
        file_ref=None,
        source_tool="double",
        params={"value": 2},
        dataset_ref=None,
    )
    matched = shadow.compare_success(
        tool_name="double",
        arguments={"value": 2},
        result={"doubled": 4},
        artifact=artifact,
    )
    assert matched.equivalent is True and matched.result_hash


@pytest.mark.asyncio
async def test_official_sdk_tools_list_call_and_gateway_round_trip() -> None:
    adapter = _adapter()
    server = build_sdk_server(adapter)
    async with create_connected_server_and_client_session(server) as session:
        listed = await session.list_tools()
        assert len(listed.tools) == 1
        assert listed.tools[0].inputSchema == INPUT_SCHEMA
        assert listed.tools[0].outputSchema == OUTPUT_SCHEMA
        assert listed.tools[0].meta["com.chatbi/capabilities"] == ["test.double"]

        no_context = await session.call_tool("double", {"value": 3})
        assert no_context.isError is True
        assert no_context.meta["com.chatbi/error-code"] == "invalid_request_context"

        invalid = await session.call_tool(
            "double", {"value": "3"}, meta=_context().to_request_meta()
        )
        assert invalid.isError is True
        assert invalid.meta["com.chatbi/error-code"] == "invalid_arguments"

        transport = OfficialSDKSessionTransport(session)
        gateway = MCPClientGateway(
            transport,
            adapter.list_tools(),
            allowed_tools=frozenset({"double"}),
        )
        discovery = await gateway.refresh()
        assert discovery.healthy is True
        result = await gateway.call_tool("double", {"value": 3}, _context())
        assert result.structured_content == {"doubled": 6}


def test_every_project_server_exports_governed_mcp_metadata() -> None:
    servers = (
        build_excel_server(),
        build_stats_server(),
        build_chart_server(),
        build_data_server(),
        build_report_server(),
    )
    descriptors = [
        descriptor
        for server in servers
        for descriptor in server.as_mcp_adapter().list_tools()
    ]
    assert len(descriptors) == 15
    assert all(descriptor.metadata.capabilities for descriptor in descriptors)
    assert all(descriptor.output_schema.get("type") == "object" for descriptor in descriptors)
    assert all(
        descriptor.output_schema.get("required")
        for descriptor in descriptors
        if descriptor.name != "multi_layout"
    )
    assert all(descriptor.to_protocol_dict()["_meta"] for descriptor in descriptors)
