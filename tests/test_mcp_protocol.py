"""v2.4 MCP contract, official SDK adapter and Client Gateway tests."""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
import sys
import textwrap
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import duckdb
import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.agent_tools import AgentToolRegistry  # noqa: E402
from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamable_http_client  # noqa: E402
from mcp.shared.memory import create_connected_server_and_client_session  # noqa: E402
from mcp_servers.chart.server import build_server as build_chart_server  # noqa: E402
from mcp_servers.common.adapter import MCPServerAdapter, MCPToolBinding  # noqa: E402
from mcp_servers.common.catalog import tool_metadata  # noqa: E402
from mcp_servers.common.client_gateway import (  # noqa: E402
    InProcessMCPTransport,
    ManagedMCPClientGateway,
    MCPClientConfig,
    MCPClientGateway,
    MCPGatewayExecutionError,
    MCPResourceNotificationBuffer,
    MCPShadowComparator,
    OfficialSDKClientTransport,
    OfficialSDKSessionTransport,
)
from mcp_servers.common.contracts import (  # noqa: E402
    CHATBI_CONTEXT_KEY,
    MCPCallResult,
    MCPProtocolError,
    MCPRequestContext,
    MCPResourceContents,
    MCPResourceDescriptor,
    MCPResourceNotification,
    MCPResourcePage,
    MCPToolDescriptor,
    stable_hash,
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
from packages.common.config import Settings  # noqa: E402
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
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
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


class _ResourceProvider:
    uri = "chatbi://domain-definitions/definition-1"

    def list_resources(self, context: MCPRequestContext) -> tuple[MCPResourceDescriptor, ...]:
        if context.project_id != "project-1" or context.subject_id != "user-1":
            raise PermissionError("not visible")
        return (
            MCPResourceDescriptor(
                uri=self.uri,
                name="metric.example.v1",
                title="Example metric",
                description="Versioned metric definition",
                metadata={"com.chatbi/definition-version": 1},
            ),
        )

    def read_resource(self, uri: str, context: MCPRequestContext) -> MCPResourceContents:
        if context.project_id != "project-1" or context.subject_id != "user-1":
            raise PermissionError("not visible")
        if uri != self.uri:
            raise FileNotFoundError(uri)
        return MCPResourceContents(
            uri=uri,
            text='{"definition_id":"definition-1","version":1}',
            metadata={"com.chatbi/definition-version": 1},
        )


class _MutableResourceProvider:
    def __init__(self) -> None:
        self.revision = 1
        self.resource_count = 3

    def publish(self) -> None:
        self.revision += 1
        self.resource_count += 1

    def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]:
        if context.project_id != "project-1" or context.subject_id != "user-1":
            raise PermissionError("not visible")
        return tuple(
            MCPResourceDescriptor(
                uri=f"chatbi://mutable/resource-{index}",
                name=f"mutable-resource-{index}",
                title=f"Mutable resource {index}",
                description="Mutable protocol fixture",
                metadata={
                    "com.chatbi/revision": self.revision,
                    "com.chatbi/index": index,
                },
            )
            for index in range(1, self.resource_count + 1)
        )

    def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents:
        if context.project_id != "project-1" or context.subject_id != "user-1":
            raise PermissionError("not visible")
        valid_uris = {
            f"chatbi://mutable/resource-{index}" for index in range(1, self.resource_count + 1)
        }
        if uri not in valid_uris:
            raise FileNotFoundError(uri)
        return MCPResourceContents(
            uri=uri,
            text=json.dumps(
                {"uri": uri, "revision": self.revision},
                sort_keys=True,
                separators=(",", ":"),
            ),
            metadata={"com.chatbi/revision": self.revision},
        )


def _mutable_resource_adapter(
    provider: _MutableResourceProvider,
) -> MCPServerAdapter:
    return MCPServerAdapter(
        "mutable-knowledge-resources",
        [],
        resource_provider=provider,
    )


def _resource_adapter() -> MCPServerAdapter:
    return MCPServerAdapter(
        "knowledge-resources",
        [],
        resource_provider=_ResourceProvider(),
    )


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
    signed = context.to_request_meta(signing_key="context-secret")
    assert (
        MCPRequestContext.from_request_meta(
            signed,
            signing_key="context-secret",
            require_signature=True,
        )
        == context
    )
    signed[CHATBI_CONTEXT_KEY]["project_id"] = "tampered"
    with pytest.raises(MCPProtocolError, match="签名无效"):
        MCPRequestContext.from_request_meta(
            signed,
            signing_key="context-secret",
            require_signature=True,
        )

    expired = _context(deadline_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
    with pytest.raises(MCPProtocolError, match="截止时间"):
        expired.validate()
    with pytest.raises(MCPProtocolError, match="不完整"):
        _context(memory_snapshot_id="../host-path").validate()
    with pytest.raises(MCPProtocolError, match="不完整"):
        _context(evidence_ledger_version=-1).validate()


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
async def test_high_risk_tool_requires_exact_approval_at_gateway_and_server() -> None:
    executed: list[int] = []

    def execute_double(args: dict[str, Any]) -> dict[str, int]:
        value = int(args["value"])
        executed.append(value)
        return {"doubled": value * 2}

    descriptor = MCPToolDescriptor(
        name="double",
        description="High-risk double",
        input_schema=INPUT_SCHEMA,
        output_schema=OUTPUT_SCHEMA,
        metadata=tool_metadata("test.double", risk_level="high"),
    )
    adapter = MCPServerAdapter(
        "high-risk-tools",
        [
            MCPToolBinding(
                descriptor,
                execute_double,
            )
        ],
    )
    arguments = {"value": 4}
    approved_context = _context(
        approval_id="a" * 32,
        approval_version=3,
        approval_contract_hash=descriptor.contract_hash,
        approval_parameter_hash=stable_hash(arguments),
    )

    denied_by_server = adapter.call_tool("double", arguments, _context())
    assert denied_by_server.error_code == "approval_required"
    assert executed == []
    drifted_by_server = adapter.call_tool(
        "double",
        {"value": 5},
        approved_context,
    )
    assert drifted_by_server.error_code == "approval_required"
    assert executed == []
    assert adapter.call_tool(
        "double",
        arguments,
        approved_context,
    ).structured_content == {"doubled": 8}
    assert executed == [4]

    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(),
        expected=adapter.list_tools(),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: InProcessMCPTransport(adapter),
    )
    with pytest.raises(MCPGatewayExecutionError) as missing:
        await gateway.execute(
            "double",
            arguments,
            _context(),
            timeout_seconds=1,
        )
    assert missing.value.code == "approval_required"
    assert executed == [4]
    result = await gateway.execute(
        "double",
        arguments,
        approved_context,
        timeout_seconds=1,
    )
    assert result.result == {"doubled": 8}
    assert executed == [4, 4]
    await gateway.aclose()


@pytest.mark.asyncio
async def test_in_process_transport_keeps_sync_tool_attached_to_event_loop() -> None:
    descriptor = MCPToolDescriptor(
        name="thread_probe",
        description="prove compatibility calls stay attached to the caller",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"worker_thread": {"type": "boolean"}},
            "required": ["worker_thread"],
            "additionalProperties": False,
        },
        metadata=tool_metadata("test.thread_probe"),
    )

    def handler(_arguments: dict[str, Any]) -> dict[str, bool]:
        assert asyncio.get_running_loop() is not None
        return {"worker_thread": threading.get_ident() != threading.main_thread().ident}

    adapter = MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])
    result = await InProcessMCPTransport(adapter).call_tool("thread_probe", {}, _context())

    assert result.structured_content == {"worker_thread": False}


@pytest.mark.asyncio
async def test_in_process_mutating_tool_never_detaches_a_slow_write(tmp_path: Path) -> None:
    descriptor = MCPToolDescriptor(
        name="slow_write",
        description="write only while the caller remains attached",
        input_schema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
        output_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
        metadata=tool_metadata(
            "test.slow_write", read_only=False, idempotent=False, risk_level="medium"
        ),
    )

    def handler(arguments: dict[str, Any]) -> dict[str, str]:
        time.sleep(0.03)
        target = tmp_path / f"{arguments['name']}.txt"
        target.write_text("complete", encoding="utf-8")
        return {"path": str(target)}

    adapter = MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])
    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(),
        expected=(descriptor,),
        allowed_tools=frozenset({"slow_write"}),
        transport_factory=lambda: InProcessMCPTransport(adapter),
    )

    first = await gateway.execute(
        "slow_write", {"name": "first"}, _context(), timeout_seconds=0.001
    )
    assert first.result == {"path": str(tmp_path / "first.txt")}
    assert sorted(path.name for path in tmp_path.iterdir()) == ["first.txt"]

    second = await gateway.execute("slow_write", {"name": "second"}, _context(), timeout_seconds=1)
    assert second.result == {"path": str(tmp_path / "second.txt")}
    await asyncio.sleep(0.05)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["first.txt", "second.txt"]
    await gateway.aclose()


@pytest.mark.asyncio
async def test_in_process_opt_in_offloads_sync_tool_without_blocking_event_loop() -> None:
    descriptor = MCPToolDescriptor(
        name="thread_probe_offloaded",
        description="prove opted-in compatibility calls do not block the event loop",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={
            "type": "object",
            "properties": {"completed": {"type": "boolean"}},
            "required": ["completed"],
            "additionalProperties": False,
        },
        metadata=tool_metadata("test.thread_probe"),
    )

    def handler(_arguments: dict[str, Any]) -> dict[str, bool]:
        time.sleep(0.05)
        return {"completed": True}

    adapter = MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])
    transport = InProcessMCPTransport(adapter, abandon_result=lambda _name, _result: None)
    heartbeat = 0

    async def tick() -> None:
        nonlocal heartbeat
        while heartbeat < 3:
            await asyncio.sleep(0.005)
            heartbeat += 1

    result, _ = await asyncio.gather(
        transport.call_tool("thread_probe_offloaded", {}, _context()),
        tick(),
    )

    assert result.structured_content == {"completed": True}
    assert heartbeat == 3


@pytest.mark.asyncio
async def test_in_process_cancel_is_bounded_and_cleans_late_side_effect(tmp_path: Path) -> None:
    descriptor = MCPToolDescriptor(
        name="slow_file",
        description="produce a file after the caller timeout",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True},
        metadata=tool_metadata("test.slow_file", read_only=False, idempotent=False),
    )
    target = tmp_path / "late.txt"

    def handler(_arguments: dict[str, Any]) -> dict[str, str]:
        time.sleep(0.08)
        target.write_text("late", encoding="utf-8")
        return {"path": str(target)}

    def abandon(_name: str, result: MCPCallResult) -> None:
        if result.structured_content is not None:
            Path(str(result.structured_content["path"])).unlink(missing_ok=True)

    adapter = MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])
    transport = InProcessMCPTransport(adapter, abandon_result=abandon)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await transport.call_tool("slow_file", {}, _context())
    assert time.monotonic() - started < 0.05
    await asyncio.sleep(0.12)
    assert not target.exists()


@pytest.mark.asyncio
async def test_in_process_abandoned_cleanup_retries_transient_failure(
    tmp_path: Path,
) -> None:
    descriptor = MCPToolDescriptor(
        name="retry_cleanup",
        description="produce a late file whose cleanup fails transiently",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True},
        metadata=tool_metadata("test.retry_cleanup", read_only=False, idempotent=False),
    )
    target = tmp_path / "retry.txt"
    attempts = 0

    def handler(_arguments: dict[str, Any]) -> dict[str, str]:
        time.sleep(0.04)
        target.write_text("late", encoding="utf-8")
        return {"path": str(target)}

    def abandon(_name: str, result: MCPCallResult) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise OSError("transient cleanup failure")
        assert result.structured_content is not None
        Path(str(result.structured_content["path"])).unlink(missing_ok=True)

    context = _context()
    adapter = MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])
    transport = InProcessMCPTransport(adapter, abandon_result=abandon)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.005):
            await transport.call_tool("retry_cleanup", {}, context)
    await asyncio.sleep(0.12)
    await transport.aclose()

    assert attempts == 3
    assert not target.exists()
    assert transport.abandoned_cleanup_state(context.invocation_id) == {
        "tool": "retry_cleanup",
        "status": "completed",
        "attempts": 3,
        "error_type": None,
    }


@pytest.mark.asyncio
async def test_in_process_abandoned_cleanup_records_bounded_permanent_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    descriptor = MCPToolDescriptor(
        name="failed_cleanup",
        description="produce a late file whose cleanup always fails",
        input_schema={"type": "object", "additionalProperties": False},
        output_schema={"type": "object", "additionalProperties": True},
        metadata=tool_metadata("test.failed_cleanup", read_only=False, idempotent=False),
    )
    target = tmp_path / "failed.txt"
    attempts = 0

    def handler(_arguments: dict[str, Any]) -> dict[str, str]:
        time.sleep(0.04)
        target.write_text("late", encoding="utf-8")
        return {"path": str(target)}

    def abandon(_name: str, _result: MCPCallResult) -> None:
        nonlocal attempts
        attempts += 1
        raise PermissionError("permanent cleanup failure")

    context = _context()
    adapter = MCPServerAdapter("test-tools", [MCPToolBinding(descriptor, handler)])
    transport = InProcessMCPTransport(adapter, abandon_result=abandon)
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.005):
            await transport.call_tool("failed_cleanup", {}, context)
    await asyncio.sleep(0.12)
    await transport.aclose()

    assert attempts == 3
    assert target.exists()
    assert transport.abandoned_cleanup_state(context.invocation_id) == {
        "tool": "failed_cleanup",
        "status": "failed",
        "attempts": 3,
        "error_type": "PermissionError",
    }
    assert "mcp.in_process_abandoned_cleanup_failed" in capsys.readouterr().out


_LATE_CHART_DESCRIPTOR = MCPToolDescriptor(
    name="chart_screenshot",
    description="produce a late managed chart screenshot",
    input_schema={"type": "object", "additionalProperties": False},
    output_schema={"type": "object", "additionalProperties": True},
    metadata=tool_metadata(
        "test.chart_screenshot",
        read_only=False,
        idempotent=False,
    ),
)


async def _run_late_chart_cleanup(
    cleanup_owner: AgentToolRegistry,
    target: Path,
    context: MCPRequestContext,
) -> dict[str, str | int | None]:
    def handler(_arguments: dict[str, Any]) -> dict[str, str]:
        time.sleep(0.04)
        target.write_bytes(b"png")
        return {"image_path": str(target)}

    adapter = MCPServerAdapter(
        "chart",
        [MCPToolBinding(_LATE_CHART_DESCRIPTOR, handler)],
    )
    transport = InProcessMCPTransport(
        adapter,
        abandon_result=cleanup_owner._cleanup_abandoned_mcp_result,
    )
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.005):
            await transport.call_tool("chart_screenshot", {}, context)
    await asyncio.sleep(0.12)
    await transport.aclose()
    state = transport.abandoned_cleanup_state(context.invocation_id)
    assert state is not None
    return state


@pytest.fixture
def late_chart_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[AgentToolRegistry, Path]:
    report_dir = tmp_path / "reports"
    chart_dir = report_dir / "charts"
    chart_dir.mkdir(parents=True)
    target = chart_dir / f"chart_{'a' * 32}.png"
    monkeypatch.setattr(
        "apps.orchestrator.agent_tools.get_settings",
        lambda: Settings(report_dir=str(report_dir)),
    )
    return AgentToolRegistry.__new__(AgentToolRegistry), target


@pytest.mark.asyncio
async def test_real_chart_cleanup_callback_retries_transient_unlink_error(
    late_chart_cleanup: tuple[AgentToolRegistry, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_owner, target = late_chart_cleanup
    original_unlink = Path.unlink
    attempts = 0

    def flaky_unlink(path: Path, *args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        if path == target:
            attempts += 1
            if attempts < 3:
                raise OSError("transient chart cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    state = await _run_late_chart_cleanup(
        cleanup_owner,
        target,
        _context(invocation_id="chart-transient"),
    )

    assert attempts == 3
    assert not target.exists()
    assert state == {
        "tool": "chart_screenshot",
        "status": "completed",
        "attempts": 3,
        "error_type": None,
    }


@pytest.mark.asyncio
async def test_real_chart_cleanup_callback_records_permanent_false_as_failure(
    late_chart_cleanup: tuple[AgentToolRegistry, Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cleanup_owner, target = late_chart_cleanup
    attempts = 0

    def refuse_delete(_file_ref: object, _report_dir: object) -> bool:
        nonlocal attempts
        attempts += 1
        return False

    monkeypatch.setattr(
        "apps.orchestrator.agent_tools.delete_chart_file",
        refuse_delete,
    )
    state = await _run_late_chart_cleanup(
        cleanup_owner,
        target,
        _context(invocation_id="chart-permanent"),
    )

    assert attempts == 3
    assert target.exists()
    assert state == {
        "tool": "chart_screenshot",
        "status": "failed",
        "attempts": 3,
        "error_type": "RuntimeError",
    }


@pytest.mark.asyncio
async def test_real_chart_cleanup_callback_deletes_normally_once(
    late_chart_cleanup: tuple[AgentToolRegistry, Path],
) -> None:
    cleanup_owner, target = late_chart_cleanup
    state = await _run_late_chart_cleanup(
        cleanup_owner,
        target,
        _context(invocation_id="chart-success"),
    )

    assert not target.exists()
    assert state == {
        "tool": "chart_screenshot",
        "status": "completed",
        "attempts": 1,
        "error_type": None,
    }


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

    gateway = MCPClientGateway(DriftTransport(), expected, allowed_tools=frozenset({"double"}))
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
async def test_official_sdk_resource_list_read_require_signed_host_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_run_sync(function: Any, *args: Any, **_kwargs: Any) -> Any:
        return function(*args)

    # The production adapter retains its worker boundary. This protocol-focused
    # test isolates request metadata/serialization from the runner's thread pool.
    monkeypatch.setattr(anyio.to_thread, "run_sync", direct_run_sync)
    signing_key = "resource-context-secret"
    server = build_sdk_server(
        _resource_adapter(),
        context_signing_key=signing_key,
        require_signed_context=True,
    )
    async with create_connected_server_and_client_session(server) as session:
        unsigned = OfficialSDKSessionTransport(session)
        with pytest.raises(MCPProtocolError) as denied:
            await unsigned.list_resources(_context())
        assert denied.value.code == "invalid_context_signature"

        transport = OfficialSDKSessionTransport(
            session,
            context_signing_key=signing_key,
        )
        resources = await transport.list_resources(_context())
        assert [item.uri for item in resources] == [_ResourceProvider.uri]
        assert resources[0].metadata == {"com.chatbi/definition-version": 1}

        gateway = MCPClientGateway(
            transport,
            (),
            allowed_tools=frozenset(),
        )
        assert (await gateway.refresh()).healthy is True
        gateway_resources = await gateway.list_resources(_context())
        assert gateway_resources == resources

        contents = await transport.read_resource(_ResourceProvider.uri, _context())
        assert contents.uri == _ResourceProvider.uri
        assert contents.metadata == {"com.chatbi/definition-version": 1}
        assert '"version":1' in contents.text

        with pytest.raises(MCPProtocolError) as missing:
            await transport.read_resource(
                "chatbi://domain-definitions/missing",
                _context(),
            )
        assert missing.value.code == "resource_not_found"


@pytest.mark.asyncio
async def test_resource_pages_resume_safely_and_reject_catalog_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_run_sync(function: Any, *args: Any, **_kwargs: Any) -> Any:
        return function(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", direct_run_sync)
    provider = _MutableResourceProvider()
    signing_key = "resource-pagination-secret"
    server = build_sdk_server(
        _mutable_resource_adapter(provider),
        context_signing_key=signing_key,
        require_signed_context=True,
        resource_page_size=1,
    )
    async with create_connected_server_and_client_session(server) as session:
        transport = OfficialSDKSessionTransport(
            session,
            context_signing_key=signing_key,
        )
        first = await transport.list_resource_page(_context())
        assert [item.name for item in first.resources] == ["mutable-resource-1"]
        assert first.next_cursor is not None
        assert len(first.catalog_version) == 64

        resumed_context = _context(
            run_id="run-after-reconnect",
            invocation_id="invocation-after-reconnect",
            idempotency_key="idempotency-after-reconnect",
        )
        second = await transport.list_resource_page(
            resumed_context,
            cursor=first.next_cursor,
        )
        assert [item.name for item in second.resources] == ["mutable-resource-2"]
        assert second.catalog_version == first.catalog_version

        assert first.next_cursor is not None
        replacement = "0" if first.next_cursor[-1] != "0" else "1"
        tampered_cursor = f"{first.next_cursor[:-1]}{replacement}"
        with pytest.raises(MCPProtocolError) as tampered:
            await transport.list_resource_page(
                _context(),
                cursor=tampered_cursor,
            )
        assert tampered.value.code == "invalid_resource_cursor"

        with pytest.raises(MCPProtocolError) as crossed_scope:
            await transport.list_resource_page(
                _context(conversation_id="conversation-2"),
                cursor=first.next_cursor,
            )
        assert crossed_scope.value.code == "invalid_resource_cursor"

        provider.publish()
        with pytest.raises(MCPProtocolError) as drifted:
            await transport.list_resource_page(
                _context(),
                cursor=first.next_cursor,
            )
        assert drifted.value.code == "resource_catalog_changed"
        assert drifted.value.retryable is True

        all_resources = await transport.list_resources(_context())
        assert [item.name for item in all_resources] == [
            "mutable-resource-1",
            "mutable-resource-2",
            "mutable-resource-3",
            "mutable-resource-4",
        ]


@pytest.mark.asyncio
async def test_resource_subscription_emits_versioned_notifications_and_unsubscribes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def direct_run_sync(function: Any, *args: Any, **_kwargs: Any) -> Any:
        return function(*args)

    monkeypatch.setattr(anyio.to_thread, "run_sync", direct_run_sync)
    provider = _MutableResourceProvider()
    signing_key = "resource-subscription-secret"
    server = build_sdk_server(
        _mutable_resource_adapter(provider),
        context_signing_key=signing_key,
        require_signed_context=True,
        resource_poll_interval_seconds=0.01,
    )
    notifications = MCPResourceNotificationBuffer()
    async with create_connected_server_and_client_session(
        server,
        message_handler=notifications,
    ) as session:
        capabilities = session.get_server_capabilities()
        assert capabilities is not None and capabilities.resources is not None
        assert capabilities.tools is not None
        assert capabilities.tools.listChanged is True
        assert capabilities.resources.subscribe is True
        assert capabilities.resources.listChanged is True

        unsigned = OfficialSDKSessionTransport(session)
        with pytest.raises(MCPProtocolError) as denied:
            await unsigned.subscribe_resource(
                "chatbi://mutable/resource-1",
                _context(),
            )
        assert denied.value.code == "invalid_context_signature"

        transport = OfficialSDKSessionTransport(
            session,
            context_signing_key=signing_key,
            resource_notifications=notifications,
        )
        with pytest.raises(MCPProtocolError) as crossed_subject:
            await transport.subscribe_resource(
                "chatbi://mutable/resource-1",
                _context(subject_id="user-2"),
            )
        assert crossed_subject.value.code == "resource_not_found"
        await transport.subscribe_resource(
            "chatbi://mutable/resource-1",
            _context(),
        )
        provider.publish()
        list_changed = await transport.next_resource_notification(
            timeout_seconds=1,
        )
        updated = await transport.next_resource_notification(
            timeout_seconds=1,
        )
        assert list_changed.kind == "list_changed"
        assert list_changed.uri is None
        assert updated.kind == "updated"
        assert updated.uri == "chatbi://mutable/resource-1"
        assert list_changed.catalog_version == updated.catalog_version
        assert list_changed.previous_catalog_version == (updated.previous_catalog_version)
        assert list_changed.catalog_version != list_changed.previous_catalog_version

        await transport.unsubscribe_resource(
            "chatbi://mutable/resource-1",
            _context(),
        )
        provider.publish()
        with pytest.raises(TimeoutError):
            await transport.next_resource_notification(timeout_seconds=0.05)


@pytest.mark.asyncio
async def test_sdk_buffer_receives_tool_list_changed_notification() -> None:
    from mcp import types

    notifications = MCPResourceNotificationBuffer()
    await notifications(types.ServerNotification(root=types.ToolListChangedNotification()))

    await notifications.next_tool_list_changed(timeout_seconds=0.1)
    with pytest.raises(TimeoutError):
        await notifications.next_tool_list_changed(timeout_seconds=0.01)


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
                unsigned = await session.call_tool(
                    "double",
                    {"value": 7},
                    meta=_context().to_request_meta(),
                )
                assert unsigned.isError is True
                assert unsigned.meta is not None
                assert unsigned.meta["com.chatbi/error-code"] == "invalid_context_signature"
                called = await session.call_tool(
                    "double",
                    {"value": 7},
                    meta=_context().to_request_meta(signing_key=token),
                )
                assert called.isError is False
                assert called.structuredContent == {"doubled": 14}

                managed = ManagedMCPClientGateway(
                    config=MCPClientConfig(transport="streamable_http"),
                    expected=_adapter().list_tools(),
                    allowed_tools=frozenset({"double"}),
                    transport_factory=lambda: OfficialSDKSessionTransport(
                        session,
                        context_signing_key=token,
                    ),
                )
                gateway_result = await managed.execute(
                    "double",
                    {"value": 8},
                    _context(invocation_id="http-managed-invocation"),
                    timeout_seconds=1,
                )
                assert gateway_result.result == {"doubled": 16}
                assert gateway_result.transport == "streamable_http"
                await managed.aclose()


@pytest.mark.asyncio
async def test_official_sdk_client_transport_stdio_round_trip(
    tmp_path: Path,
) -> None:
    dataset_ref = "a" * 32
    dataset_path = tmp_path / f"{dataset_ref}.parquet"
    connection = duckdb.connect()
    try:
        connection.execute(
            """
            COPY (
                SELECT * FROM (
                    VALUES ('east', 10), ('east', 15), ('west', 8)
                ) AS probe(region, amount)
            ) TO ? (FORMAT PARQUET)
            """,
            [str(dataset_path)],
        )
    finally:
        connection.close()
    descriptor = next(
        item
        for item in build_data_server().as_mcp_adapter().list_tools()
        if item.name == "aggregate_preview"
    )
    config = MCPClientConfig(
        transport="stdio",
        stdio_command=(
            sys.executable,
            "-m",
            "scripts.mcp_transport_probe_server",
        ),
        stdio_cwd=str(ROOT),
        stdio_env={
            "DATASET_DIR": str(tmp_path),
            "MCP_TRANSPORT": "stdio",
            "MCP_PROBE_DELAY_SECONDS": "0",
            "MCP_CONTEXT_SIGNING_KEY": "stdio-context-secret",
        },
        context_signing_key="stdio-context-secret",
    )
    gateway = ManagedMCPClientGateway(
        config=config,
        expected=(descriptor,),
        allowed_tools=frozenset({"aggregate_preview"}),
        transport_factory=lambda: OfficialSDKClientTransport(config),
    )
    result = await gateway.execute(
        "aggregate_preview",
        {
            "dataset_ref": dataset_ref,
            "group_col": "region",
            "value_col": "amount",
            "agg": "sum",
            "sort": "group",
        },
        _context(invocation_id="stdio-managed-invocation"),
        timeout_seconds=5,
    )
    await gateway.aclose()

    assert result.transport == "stdio"
    assert result.result["rows"] == [
        {
            "group": "east",
            "value": 25.0,
            "count": 2,
            "row_count": 2,
            "valid_value_count": 2,
        },
        {
            "group": "west",
            "value": 8.0,
            "count": 1,
            "row_count": 1,
            "valid_value_count": 1,
        },
    ]


@pytest.mark.asyncio
async def test_official_sdk_transport_closes_contexts_in_owner_task() -> None:
    class _EmptySession:
        async def list_tools(self) -> Any:
            return type("ToolList", (), {"tools": ()})()

    class _TaskOwnedTransport(OfficialSDKClientTransport):
        def __init__(self) -> None:
            super().__init__(MCPClientConfig(transport="stdio"))
            self.entered_task: asyncio.Task[Any] | None = None
            self.exited_task: asyncio.Task[Any] | None = None

        @asynccontextmanager
        async def _open_connection(
            self,
        ) -> AsyncIterator[OfficialSDKSessionTransport]:
            self.entered_task = asyncio.current_task()
            try:
                yield OfficialSDKSessionTransport(_EmptySession())
            finally:
                self.exited_task = asyncio.current_task()

    transport = _TaskOwnedTransport()
    request_task = asyncio.create_task(transport.list_tools())

    discovered = await request_task
    await asyncio.wait_for(transport.aclose(), timeout=5)

    assert discovered == ()
    assert transport.entered_task is transport.exited_task
    assert transport.entered_task is not request_task


@pytest.mark.asyncio
async def test_official_sdk_transport_cancels_connect_in_owner_task() -> None:
    class _SlowConnectTransport(OfficialSDKClientTransport):
        def __init__(self) -> None:
            super().__init__(
                MCPClientConfig(
                    transport="streamable_http",
                    connect_timeout_seconds=0.01,
                )
            )
            self.entered_task: asyncio.Task[Any] | None = None
            self.exited_task: asyncio.Task[Any] | None = None

        @asynccontextmanager
        async def _open_connection(
            self,
        ) -> AsyncIterator[OfficialSDKSessionTransport]:
            self.entered_task = asyncio.current_task()
            try:
                await asyncio.Event().wait()
                raise AssertionError("unreachable")
                yield  # pragma: no cover
            finally:
                self.exited_task = asyncio.current_task()

    transport = _SlowConnectTransport()

    with pytest.raises(TimeoutError):
        await transport.list_tools()

    assert transport.entered_task is transport.exited_task


@pytest.mark.asyncio
async def test_streamable_http_client_cancellation_keeps_session_usable() -> None:
    started = threading.Event()
    release = threading.Event()
    descriptor = _adapter().list_tools()[0]

    def slow_handler(args: dict[str, Any]) -> dict[str, int]:
        started.set()
        release.wait(timeout=2)
        return {"doubled": args["value"] * 2}

    adapter = MCPServerAdapter("slow-tools", [MCPToolBinding(descriptor, slow_handler)])
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
                        "_meta": _context().to_request_meta(signing_key=token),
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


def _directly_raises_not_implemented(handler: Any) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(raised, ast.Name) and raised.id == "NotImplementedError":
            return True
        if isinstance(raised, ast.Attribute) and raised.attr == "NotImplementedError":
            return True
    return False


def test_every_project_server_exports_complete_governed_tools() -> None:
    servers = (
        build_excel_server(),
        build_stats_server(),
        build_chart_server(),
        build_data_server(),
        build_report_server(),
    )
    tools = [tool for server in servers for tool in server.tools]
    descriptors = [
        descriptor for server in servers for descriptor in server.as_mcp_adapter().list_tools()
    ]
    assert len(tools) == 19
    assert len(descriptors) == len(tools)
    assert all(tool.metadata is not None for tool in tools)
    assert all(descriptor.metadata.capabilities for descriptor in descriptors)
    assert all(descriptor.output_schema.get("type") == "object" for descriptor in descriptors)
    assert all(descriptor.output_schema.get("required") for descriptor in descriptors)
    assert not any(_directly_raises_not_implemented(tool.handler) for tool in tools)
    assert all(descriptor.to_protocol_dict()["_meta"] for descriptor in descriptors)


class _ScriptedTransport:
    def __init__(
        self,
        descriptors: tuple[MCPToolDescriptor, ...],
        *,
        list_error: Exception | None = None,
        call_error: Exception | None = None,
        wait_forever: bool = False,
    ) -> None:
        self.descriptors = descriptors
        self.list_error = list_error
        self.call_error = call_error
        self.wait_forever = wait_forever
        self.calls: list[tuple[str, dict[str, Any], MCPRequestContext]] = []
        self.call_started = asyncio.Event()
        self.closed = False

    async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        if self.list_error is not None:
            raise self.list_error
        return self.descriptors

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: MCPRequestContext,
    ) -> MCPCallResult:
        self.calls.append((name, arguments, context))
        self.call_started.set()
        if self.wait_forever:
            await asyncio.Event().wait()
        if self.call_error is not None:
            raise self.call_error
        return MCPCallResult.success(name, {"doubled": arguments["value"] * 2})

    async def aclose(self) -> None:
        self.closed = True


class _MutableToolCatalogTransport(_ScriptedTransport):
    def __init__(self, descriptors: tuple[MCPToolDescriptor, ...]) -> None:
        super().__init__(descriptors)
        self.notifications: asyncio.Queue[None] = asyncio.Queue()

    async def next_tool_list_changed(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        async with asyncio.timeout(timeout_seconds):
            await self.notifications.get()

    async def publish(self) -> None:
        await self.notifications.put(None)


class _ScriptedResourceTransport(_ScriptedTransport):
    def __init__(
        self,
        descriptors: tuple[MCPToolDescriptor, ...],
        *,
        list_resources_error: Exception | None = None,
    ) -> None:
        super().__init__(descriptors)
        self.list_resources_error = list_resources_error
        self.subscriptions: list[tuple[str, MCPRequestContext]] = []

    async def list_resource_page(
        self,
        context: MCPRequestContext,
        *,
        cursor: str | None = None,
    ) -> MCPResourcePage:
        if cursor is not None:
            raise MCPProtocolError("invalid_resource_cursor", "cursor rejected")
        resources = await self.list_resources(context)
        return MCPResourcePage(
            resources=resources,
            catalog_version=stable_hash([item.uri for item in resources]),
        )

    async def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]:
        del context
        if self.list_resources_error is not None:
            raise self.list_resources_error
        return (
            MCPResourceDescriptor(
                uri="chatbi://mutable/resource-1",
                name="mutable-resource-1",
                title="Mutable resource 1",
                description="Managed reconnect fixture",
            ),
        )

    async def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents:
        del context
        return MCPResourceContents(uri=uri, text='{"revision":1}')

    async def subscribe_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> None:
        self.subscriptions.append((uri, context))

    async def unsubscribe_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> None:
        self.subscriptions = [item for item in self.subscriptions if item != (uri, context)]

    async def next_resource_notification(
        self,
        *,
        timeout_seconds: float = 5.0,
    ) -> MCPResourceNotification:
        del timeout_seconds
        raise TimeoutError


@pytest.mark.asyncio
async def test_managed_gateway_validates_tool_catalog_updates_fail_closed() -> None:
    descriptor = _adapter().list_tools()[0]
    transport = _MutableToolCatalogTransport((descriptor,))
    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(transport="streamable_http"),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: transport,
    )

    initial = await gateway.validate_catalog()
    assert initial.report.healthy is True
    assert initial.descriptors == (descriptor,)
    assert len(initial.content_hash) == 64

    await transport.publish()
    unchanged = await gateway.next_tool_catalog_update(timeout_seconds=0.1)
    assert unchanged.content_hash == initial.content_hash
    assert gateway.health.state == "healthy"

    transport.descriptors = (replace(descriptor, description="drifted"),)
    await transport.publish()
    with pytest.raises(MCPProtocolError) as drifted:
        await gateway.next_tool_catalog_update(timeout_seconds=0.1)
    assert drifted.value.code == "mcp_catalog_drift"
    assert gateway.health.state == "unhealthy"
    assert gateway.health.last_error_code == "mcp_catalog_drift"
    assert transport.closed is True


@pytest.mark.asyncio
async def test_managed_gateway_reconnects_only_read_only_idempotent_calls() -> None:
    descriptor = _adapter().list_tools()[0]
    disconnected = _ScriptedTransport(
        (descriptor,),
        call_error=ConnectionError("connection reset"),
    )
    recovered = _ScriptedTransport((descriptor,))
    transports = iter((disconnected, recovered))
    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(transport="stdio", max_reconnects=1),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: next(transports),
    )

    result = await gateway.execute("double", {"value": 4}, _context(), timeout_seconds=1)

    assert result.result == {"doubled": 8}
    assert result.transport == "stdio"
    assert result.health.state == "healthy"
    assert result.health.generation == 2
    assert disconnected.closed is True
    assert disconnected.calls[0][2].idempotency_key == (recovered.calls[0][2].idempotency_key)
    await gateway.aclose()


@pytest.mark.asyncio
async def test_managed_gateway_restores_resource_subscriptions_after_reconnect() -> None:
    descriptor = _adapter().list_tools()[0]
    disconnected = _ScriptedResourceTransport(
        (descriptor,),
        list_resources_error=MCPProtocolError(
            "mcp_resource_error",
            "resource session reset",
        ),
    )
    recovered = _ScriptedResourceTransport((descriptor,))
    transports = iter((disconnected, recovered))
    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(transport="streamable_http", max_reconnects=1),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: next(transports),
    )
    context = _context()

    await gateway.subscribe_resource("chatbi://mutable/resource-1", context)
    resources = await gateway.list_resources(context)

    assert [item.uri for item in resources] == ["chatbi://mutable/resource-1"]
    assert disconnected.closed is True
    assert [item[0] for item in disconnected.subscriptions] == ["chatbi://mutable/resource-1"]
    assert recovered.subscriptions == [("chatbi://mutable/resource-1", context)]
    assert gateway.health.generation == 2
    await gateway.aclose()


@pytest.mark.asyncio
async def test_managed_gateway_does_not_retry_ambiguous_mutation() -> None:
    original = _adapter().list_tools()[0]
    descriptor = replace(
        original,
        metadata=replace(
            original.metadata,
            read_only=False,
            idempotent=False,
            destructive=True,
        ),
    )
    disconnected = _ScriptedTransport(
        (descriptor,),
        call_error=ConnectionError("connection reset"),
    )
    factory_calls = 0

    def factory() -> _ScriptedTransport:
        nonlocal factory_calls
        factory_calls += 1
        return disconnected

    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(transport="streamable_http", max_reconnects=3),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=factory,
    )

    with pytest.raises(MCPGatewayExecutionError) as captured:
        await gateway.execute("double", {"value": 4}, _context(), timeout_seconds=1)

    assert captured.value.code == "mcp_transport_disconnected"
    assert captured.value.result_unknown is True
    assert factory_calls == 1


@pytest.mark.asyncio
async def test_managed_gateway_timeout_and_cancellation_invalidate_session() -> None:
    descriptor = _adapter().list_tools()[0]
    timed_transport = _ScriptedTransport((descriptor,), wait_forever=True)
    timed_gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(transport="streamable_http"),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: timed_transport,
    )
    with pytest.raises(MCPGatewayExecutionError) as captured:
        await timed_gateway.execute("double", {"value": 2}, _context(), timeout_seconds=0.01)
    assert captured.value.code == "mcp_call_timeout"
    assert captured.value.result_unknown is True
    assert timed_transport.closed is True
    assert timed_gateway.health.state == "unhealthy"

    cancelled_transport = _ScriptedTransport((descriptor,), wait_forever=True)
    cancelled_gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(transport="stdio"),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: cancelled_transport,
    )
    task = asyncio.create_task(
        cancelled_gateway.execute("double", {"value": 2}, _context(), timeout_seconds=10)
    )
    await cancelled_transport.call_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled_transport.closed is True
    assert cancelled_gateway.health.last_error_code == "mcp_call_cancelled"


@pytest.mark.asyncio
async def test_managed_gateway_fallback_is_explicit_read_only_and_fail_closed_on_drift() -> None:
    adapter = _adapter()
    descriptor = adapter.list_tools()[0]
    unavailable = _ScriptedTransport(
        (descriptor,),
        list_error=ConnectionError("service unavailable"),
    )
    gateway = ManagedMCPClientGateway(
        config=MCPClientConfig(
            transport="streamable_http",
            allow_in_process_fallback=True,
        ),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: unavailable,
        compatibility_transport_factory=lambda: InProcessMCPTransport(adapter),
    )

    result = await gateway.execute("double", {"value": 5}, _context(), timeout_seconds=1)

    assert result.result == {"doubled": 10}
    assert result.transport == "in_process"
    assert result.degraded is True
    assert result.health.state == "degraded"

    drifted = _ScriptedTransport((replace(descriptor, description="drift"),))
    fail_closed = ManagedMCPClientGateway(
        config=MCPClientConfig(
            transport="streamable_http",
            allow_in_process_fallback=True,
        ),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: drifted,
        compatibility_transport_factory=lambda: InProcessMCPTransport(adapter),
    )
    with pytest.raises(MCPGatewayExecutionError) as captured:
        await fail_closed.execute("double", {"value": 5}, _context(), timeout_seconds=1)
    assert captured.value.code == "mcp_catalog_drift"

    class _AuthError(Exception):
        response = type("_Response", (), {"status_code": 401})()

    unauthorized = _ScriptedTransport(
        (descriptor,),
        list_error=_AuthError("401 Unauthorized"),
    )
    auth_fail_closed = ManagedMCPClientGateway(
        config=MCPClientConfig(
            transport="streamable_http",
            allow_in_process_fallback=True,
        ),
        expected=(descriptor,),
        allowed_tools=frozenset({"double"}),
        transport_factory=lambda: unauthorized,
        compatibility_transport_factory=lambda: InProcessMCPTransport(adapter),
    )
    with pytest.raises(MCPGatewayExecutionError) as auth_error:
        await auth_fail_closed.execute("double", {"value": 5}, _context(), timeout_seconds=1)
    assert auth_error.value.code == "mcp_authentication_failed"


def test_deployed_settings_require_authenticated_streamable_http_without_fallback(
    tmp_path: Path,
) -> None:
    auth_registry = tmp_path / "api-auth.json"
    auth_registry.write_text(
        '{"production-auth-token-000000000001":{"user_id":"u","tenant_id":"t"}}',
        encoding="utf-8",
    )
    service_token = tmp_path / "service.token"
    service_token.write_text("internal-service-secret", encoding="utf-8")
    signing_key = tmp_path / "context.key"
    signing_key.write_text("context-signing-secret", encoding="utf-8")
    base = {
        "_env_file": None,
        "app_env": "production",
        "auth_mode": "bearer",
        "auth_tokens_file": str(auth_registry),
    }

    with pytest.raises(ValueError, match="Streamable HTTP"):
        Settings(**base)

    settings = Settings(
        **base,
        agent_mcp_transport="streamable_http",
        agent_mcp_http_url="http://agent-tools:8000/mcp/",
        agent_mcp_service_token_file=str(service_token),
        agent_mcp_context_signing_key_file=str(signing_key),
    )
    assert settings.agent_mcp_transport == "streamable_http"

    with pytest.raises(ValueError, match="AUTH_TOKENS_FILE"):
        Settings(
            **{key: value for key, value in base.items() if key != "auth_tokens_file"},
            auth_tokens_json=auth_registry.read_text(encoding="utf-8"),
            agent_mcp_transport="streamable_http",
            agent_mcp_http_url="http://agent-tools:8000/mcp/",
            agent_mcp_service_token_file=str(service_token),
            agent_mcp_context_signing_key_file=str(signing_key),
        )

    with pytest.raises(ValueError, match="禁止降级"):
        Settings(
            **base,
            agent_mcp_transport="streamable_http",
            agent_mcp_http_url="http://agent-tools:8000/mcp/",
            agent_mcp_service_token_file=str(service_token),
            agent_mcp_context_signing_key_file=str(signing_key),
            agent_mcp_allow_in_process_fallback=True,
        )
