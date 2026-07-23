"""v2.4 MCP contract, official SDK adapter and Client Gateway tests."""

from __future__ import annotations

import asyncio
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
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
from mcp_servers.common.sdk_adapter import (  # noqa: E402
    MCP_PROTOCOL_VERSION,
    build_sdk_server,
    create_streamable_http_app,
)
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


@asynccontextmanager
async def _http_client(
    adapter: MCPServerAdapter,
    *,
    token: str,
) -> AsyncIterator[tuple[httpx.AsyncClient, str]]:
    app = create_streamable_http_app(
        adapter,
        service_token=token,
        allowed_hosts=["127.0.0.1:*"],
        allowed_origins=["https://trusted.example"],
    )
    lifespan_context = app.router.lifespan_context  # type: ignore[attr-defined]
    async with lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:8000",
            timeout=3,
        ) as client:
            yield client, "http://127.0.0.1:8000/mcp/"


def _initialize_payload(protocol: str = MCP_PROTOCOL_VERSION) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": protocol,
            "capabilities": {},
            "clientInfo": {"name": "chatbi-test", "version": "1"},
        },
    }


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
        assert listed.tools[0].meta is not None
        assert listed.tools[0].meta["com.chatbi/capabilities"] == ["test.double"]

        no_context = await session.call_tool("double", {"value": 3})
        assert no_context.isError is True
        assert no_context.meta is not None
        assert no_context.meta["com.chatbi/error-code"] == "invalid_request_context"

        invalid = await session.call_tool(
            "double", {"value": "3"}, meta=_context().to_request_meta()
        )
        assert invalid.isError is True
        assert invalid.meta is not None
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


@pytest.mark.asyncio
async def test_streamable_http_is_stateful_authenticated_and_fail_closed() -> None:
    token = "stage0-test-token"
    auth = {"Authorization": f"Bearer {token}"}
    async with _http_client(_adapter(), token=token) as (client, url):
        no_auth = await client.post(url, json=_initialize_payload())
        assert no_auth.status_code == 401

        bad_origin = await client.post(
            url,
            json=_initialize_payload(),
            headers={**auth, "Origin": "https://evil.example"},
        )
        assert bad_origin.status_code == 403

        bad_protocol = await client.post(
            url,
            json=_initialize_payload("2099-01-01"),
            headers=auth,
        )
        assert bad_protocol.status_code == 400
        assert MCP_PROTOCOL_VERSION in bad_protocol.text

        stale_session = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={
                **auth,
                "mcp-session-id": "no-such-session",
                "mcp-protocol-version": MCP_PROTOCOL_VERSION,
            },
        )
        assert stale_session.status_code == 404

        client.headers.update(auth)
        async with streamable_http_client(url, http_client=client) as streams:
            read_stream, write_stream, get_session_id = streams
            async with ClientSession(read_stream, write_stream) as session:
                initialized = await session.initialize()
                assert initialized.protocolVersion == MCP_PROTOCOL_VERSION
                assert get_session_id()
                listed = await session.list_tools()
                assert [tool.name for tool in listed.tools] == ["double"]
                called = await session.call_tool(
                    "double",
                    {"value": 7},
                    meta=_context().to_request_meta(),
                )
                assert called.isError is False
                assert called.structuredContent == {"doubled": 14}


@pytest.mark.asyncio
async def test_streamable_http_client_cancellation_keeps_session_usable() -> None:
    started = threading.Event()
    release = threading.Event()
    descriptor = _adapter().list_tools()[0]

    def slow_handler(args: dict[str, Any]) -> dict[str, int]:
        started.set()
        release.wait(timeout=2)
        return {"doubled": args["value"] * 2}

    adapter = MCPServerAdapter(
        "slow-tools", [MCPToolBinding(descriptor, slow_handler)]
    )
    token = "stage0-cancel-token"
    async with _http_client(adapter, token=token) as (client, url):
        auth = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/event-stream",
        }
        initialized = await client.post(url, json=_initialize_payload(), headers=auth)
        assert initialized.status_code == 200
        session_id = initialized.headers["mcp-session-id"]
        session_headers = {
            **auth,
            "mcp-session-id": session_id,
            "mcp-protocol-version": MCP_PROTOCOL_VERSION,
        }
        ready = await client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
            headers=session_headers,
        )
        assert ready.status_code == 202
        call = asyncio.create_task(
            client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "double",
                        "arguments": {"value": 2},
                        "_meta": _context().to_request_meta(),
                    },
                },
                headers=session_headers,
            )
        )
        assert await asyncio.to_thread(started.wait, 1)
        cancelled = await client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": 2, "reason": "probe cancellation"},
            },
            headers=session_headers,
        )
        assert cancelled.status_code == 202
        release.set()
        result = await asyncio.wait_for(call, timeout=2)
        assert result.status_code == 200
        assert result.json()["error"]["message"] == "Request cancelled"
        listed = await client.post(
            url,
            json={"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
            headers=session_headers,
        )
        assert listed.status_code == 200
        assert [tool["name"] for tool in listed.json()["result"]["tools"]] == ["double"]
        terminated = await client.delete(url, headers=session_headers)
        assert terminated.status_code == 200


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
