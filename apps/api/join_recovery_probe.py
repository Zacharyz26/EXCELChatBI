"""Stage 6E Compose gate for governed-Join transport equivalence and recovery."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.common.client_gateway import (
    ManagedMCPClientGateway,
    MCPClientConfig,
    MCPGatewayExecutionError,
    OfficialSDKClientTransport,
)
from mcp_servers.common.contracts import (
    MCPProtocolError,
    MCPRequestContext,
    MCPToolDescriptor,
    stable_hash,
)
from packages.common.config import Settings, get_settings
from packages.common.dataset_store import delete_dataset, save_dataframe, save_metadata
from packages.governance.permissions import Principal
from packages.session.store import SessionStore

_PRINCIPAL = Principal(user_id="local-user", tenant_id="local")
_EXPECTED_TOOL_NAMES = (
    "aggregate_preview",
    "get_data_profile",
    "join_datasets",
    "join_preflight",
    "transform_dataset",
)


@dataclass(frozen=True, slots=True)
class JoinProbeFixture:
    project_id: str
    conversation_id: str
    left_ref: str
    right_ref: str
    protected_ref: str
    foreign_ref: str


def _context(
    *,
    project_id: str,
    conversation_id: str,
    run_id: str,
) -> MCPRequestContext:
    return MCPRequestContext(
        subject_id=_PRINCIPAL.user_id,
        project_id=project_id,
        conversation_id=conversation_id,
        run_id=run_id,
        plan_version=1,
        step_id="compose-governed-join",
        invocation_id=f"compose-join-invocation:{run_id}",
        idempotency_key=f"compose-join-idempotency:{run_id}",
        permission_snapshot_id="compose-join-permissions",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
        trace_id=f"compose-join-trace:{run_id}",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )


def _seed(settings: Settings) -> JoinProbeFixture:
    left_ref = save_dataframe(
        pd.DataFrame(
            {
                "left_key": [1, 1, 1, 2],
                "left_signal": [10, 20, 30, 40],
            }
        )
    )
    right_frame = pd.DataFrame(
        {
            "right_key": [1, 1, 1, 1, 3],
            "right_signal": [50, 60, 70, 80, 90],
        }
    )
    right_ref = save_dataframe(right_frame)
    protected_ref = save_dataframe(right_frame)
    save_metadata(
        protected_ref,
        {"policy": {"columns": {"right_key": "mask"}}},
    )
    foreign_ref = save_dataframe(right_frame)

    sessions = SessionStore(settings.chat_db_path)
    project = sessions.create_project(
        f"Stage 6E Join probe {uuid.uuid4().hex[:8]}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = sessions.create_conversation(project.id, "Join reconnect")
    for dataset_ref, filename in (
        (left_ref, "stage-6e-anonymous-left.parquet"),
        (right_ref, "stage-6e-anonymous-right.parquet"),
        (protected_ref, "stage-6e-protected-key.parquet"),
    ):
        sessions.register_dataset(
            ref=dataset_ref,
            project_id=project.id,
            filename=filename,
            profile={},
        )
    foreign_project = sessions.create_project(
        f"Stage 6E foreign probe {uuid.uuid4().hex[:8]}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    sessions.register_dataset(
        ref=foreign_ref,
        project_id=foreign_project.id,
        filename="stage-6e-foreign-right.parquet",
        profile={},
    )
    return JoinProbeFixture(
        project_id=project.id,
        conversation_id=conversation.id,
        left_ref=left_ref,
        right_ref=right_ref,
        protected_ref=protected_ref,
        foreign_ref=foreign_ref,
    )


def _arguments(fixture: JoinProbeFixture, *, right_ref: str | None = None) -> dict[str, Any]:
    return {
        "left_dataset_ref": fixture.left_ref,
        "right_dataset_ref": right_ref or fixture.right_ref,
        "left_key": "left_key",
        "right_key": "right_key",
        "join_type": "inner",
    }


def _approved_context(
    context: MCPRequestContext,
    descriptor: MCPToolDescriptor,
    arguments: dict[str, Any],
) -> MCPRequestContext:
    return replace(
        context,
        approval_id="a" * 32,
        approval_version=1,
        approval_contract_hash=descriptor.contract_hash,
        approval_parameter_hash=stable_hash(arguments),
    )


def _gateway(
    config: MCPClientConfig,
    expected: tuple[MCPToolDescriptor, ...],
) -> ManagedMCPClientGateway:
    return ManagedMCPClientGateway(
        config=config,
        expected=expected,
        allowed_tools=frozenset(_EXPECTED_TOOL_NAMES),
        transport_factory=lambda: OfficialSDKClientTransport(config),
    )


def _catalog_hash(descriptors: tuple[MCPToolDescriptor, ...]) -> str:
    ordered = sorted(descriptors, key=lambda item: item.name)
    return stable_hash([item.to_protocol_dict() for item in ordered])


def _assert_catalog(
    descriptors: tuple[MCPToolDescriptor, ...],
) -> tuple[MCPToolDescriptor, MCPToolDescriptor]:
    names = tuple(sorted(item.name for item in descriptors))
    if names != _EXPECTED_TOOL_NAMES:
        raise RuntimeError(f"data-tools 目录不完整: {names}")
    preflight = next(item for item in descriptors if item.name == "join_preflight")
    execute = next(item for item in descriptors if item.name == "join_datasets")
    if (
        preflight.metadata.tool_version != "1.0.0"
        or preflight.metadata.capabilities != ("dataset.join.preflight",)
        or not preflight.metadata.read_only
        or not preflight.metadata.idempotent
        or preflight.metadata.risk_level != "low"
    ):
        raise RuntimeError("join_preflight 受治理元数据漂移")
    if (
        execute.metadata.tool_version != "1.0.0"
        or execute.metadata.capabilities != ("dataset.join.execute",)
        or execute.metadata.read_only
        or execute.metadata.idempotent
        or execute.metadata.risk_level != "high"
    ):
        raise RuntimeError("join_datasets 高风险元数据漂移")
    required = [
        "left_dataset_ref",
        "right_dataset_ref",
        "left_key",
        "right_key",
        "join_type",
    ]
    if (
        preflight.input_schema.get("required") != required
        or execute.input_schema.get("required") != required
        or preflight.input_schema.get("additionalProperties") is not False
        or execute.input_schema.get("additionalProperties") is not False
    ):
        raise RuntimeError("Join 封闭输入契约漂移")
    return preflight, execute


def _assert_preflight(result: dict[str, Any]) -> None:
    risks = result.get("risks")
    risk_codes = [
        item.get("code") for item in risks if isinstance(item, dict)
    ] if isinstance(risks, list) else []
    if (
        result.get("schema") != "chatbi-join-preflight-v1"
        or result.get("status") != "requires_confirmation"
        or result.get("relationship") != "many_to_many"
        or result.get("matching_key_count") != 1
        or result.get("matched_left_rows") != 3
        or result.get("matched_right_rows") != 4
        or result.get("estimated_output_rows") != 12
        or result.get("expansion_ratio") != 2.4
        or risk_codes != ["many_to_many", "row_expansion"]
        or result.get("requires_confirmation") is not True
        or result.get("executable") is not True
        or result.get("mutates_data") is not False
        or result.get("raw_rows_returned") is not False
    ):
        raise RuntimeError("Join 固定多对多/膨胀预检结果发生漂移")


def _assert_execution(
    result: dict[str, Any],
    *,
    fixture: JoinProbeFixture,
) -> None:
    risks = result.get("risks")
    risk_codes = [
        item.get("code") for item in risks if isinstance(item, dict)
    ] if isinstance(risks, list) else []
    if (
        result.get("schema") != "chatbi-join-result-v1"
        or result.get("parent_refs") != [fixture.left_ref, fixture.right_ref]
        or result.get("parent_ref") != fixture.left_ref
        or result.get("rows") != 12
        or result.get("relationship") != "many_to_many"
        or result.get("preflight_status") != "requires_confirmation"
        or risk_codes != ["many_to_many", "row_expansion"]
        or result.get("mutates_data") is not True
        or result.get("raw_rows_returned") is not False
    ):
        raise RuntimeError("Join 授权执行或双父结果契约发生漂移")


async def _wait_for_recovery(
    gateway: ManagedMCPClientGateway,
    context: MCPRequestContext,
    *,
    fixture: JoinProbeFixture,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            execution = await gateway.execute(
                "join_preflight",
                _arguments(fixture),
                context,
                timeout_seconds=20,
            )
        except (MCPGatewayExecutionError, MCPProtocolError) as exc:
            if exc.code in {
                "mcp_authentication_failed",
                "mcp_catalog_drift",
                "invalid_tool_output",
            }:
                raise
            await asyncio.sleep(0.25)
            continue
        if execution.health.generation >= 2:
            _assert_preflight(execution.result)
            return execution.result
        await asyncio.sleep(0.25)
    raise TimeoutError("Compose data-tools 重启后未恢复受治理 Join 预检")


async def run_probe() -> dict[str, Any]:
    settings = get_settings()
    fixture = _seed(settings)
    arguments = _arguments(fixture)
    initial_context = _context(
        project_id=fixture.project_id,
        conversation_id=fixture.conversation_id,
        run_id="compose-join-run-1",
    )
    recovered_context = replace(
        _context(
            project_id=fixture.project_id,
            conversation_id=fixture.conversation_id,
            run_id="compose-join-run-2",
        ),
        data_version_hash=stable_hash(
            {"left": fixture.left_ref, "right": fixture.right_ref}
        ),
    )

    canonical_runtime = AgentServiceRuntime("data-tools", settings)
    expected = canonical_runtime.adapter.list_tools()
    _preflight_descriptor, execute_descriptor = _assert_catalog(expected)
    canonical_catalog_hash = _catalog_hash(expected)
    direct = canonical_runtime.adapter.call_tool(
        "join_preflight",
        arguments,
        initial_context,
    )
    if direct.is_error or direct.structured_content is None:
        raise RuntimeError(f"data-tools 直接 Join 预检失败: {direct.error_code}")
    _assert_preflight(direct.structured_content)
    direct_hash = stable_hash(direct.structured_content)

    cross_project = canonical_runtime.adapter.call_tool(
        "join_preflight",
        _arguments(fixture, right_ref=fixture.foreign_ref),
        initial_context,
    )
    if not cross_project.is_error or cross_project.error_code != "project_scope_violation":
        raise RuntimeError("Join 跨项目数据集未失败关闭")
    protected = canonical_runtime.adapter.call_tool(
        "join_preflight",
        _arguments(fixture, right_ref=fixture.protected_ref),
        initial_context,
    )
    if not protected.is_error or protected.error_code != "tool_business_error":
        raise RuntimeError("Join 受保护关联键未失败关闭")
    missing_approval = canonical_runtime.adapter.call_tool(
        "join_datasets",
        arguments,
        initial_context,
    )
    if not missing_approval.is_error or missing_approval.error_code != "approval_required":
        raise RuntimeError("Join 高风险执行缺少授权时未失败关闭")

    raw_tokens = json.loads(settings.agent_mcp_service_tokens_json)
    service_token = raw_tokens.get("data-tools") if isinstance(raw_tokens, dict) else None
    if not service_token or not settings.agent_mcp_context_signing_key:
        raise RuntimeError("Compose Join 探针缺少 data-tools 内部凭据")
    http_config = MCPClientConfig(
        transport="streamable_http",
        http_url=os.getenv("MCP_JOIN_PROBE_HTTP_URL", "http://data-tools:8000/mcp/"),
        service_token=service_token,
        context_signing_key=settings.agent_mcp_context_signing_key,
        connect_timeout_seconds=10,
        max_reconnects=3,
    )
    stdio_config = MCPClientConfig(
        transport="stdio",
        stdio_command=(sys.executable, "-m", "mcp_servers.agent_service.server"),
        stdio_cwd=str(Path.cwd()),
        stdio_env={
            **os.environ,
            "PROCESS_ROLE": "mcp_server",
            "MCP_AGENT_SERVICE": "data-tools",
            "MCP_TRANSPORT": "stdio",
            "MCP_CONTEXT_SIGNING_KEY": settings.agent_mcp_context_signing_key,
        },
        context_signing_key=settings.agent_mcp_context_signing_key,
        connect_timeout_seconds=20,
        max_reconnects=1,
    )
    http_gateway = _gateway(http_config, expected)
    stdio_gateway = _gateway(stdio_config, expected)
    output_ref: str | None = None
    try:
        http_catalog = await http_gateway.validate_catalog()
        stdio_catalog = await stdio_gateway.validate_catalog()
        if {
            canonical_catalog_hash,
            http_catalog.content_hash,
            stdio_catalog.content_hash,
        } != {canonical_catalog_hash}:
            raise RuntimeError("Join 在直接调用、stdio 与 HTTP 间目录不等价")
        http_result = await http_gateway.execute(
            "join_preflight",
            arguments,
            initial_context,
            timeout_seconds=20,
        )
        stdio_result = await stdio_gateway.execute(
            "join_preflight",
            arguments,
            initial_context,
            timeout_seconds=20,
        )
        _assert_preflight(http_result.result)
        _assert_preflight(stdio_result.result)
        if {
            direct_hash,
            stable_hash(http_result.result),
            stable_hash(stdio_result.result),
        } != {direct_hash}:
            raise RuntimeError("join_preflight 在直接调用、stdio 与 HTTP 间结果不等价")

        approved = await http_gateway.execute(
            "join_datasets",
            arguments,
            _approved_context(initial_context, execute_descriptor, arguments),
            timeout_seconds=20,
        )
        _assert_execution(approved.result, fixture=fixture)
        raw_output_ref = approved.result.get("dataset_ref")
        if not isinstance(raw_output_ref, str):
            raise RuntimeError("Join 授权执行未返回 Dataset reference")
        output_ref = raw_output_ref

        print(
            json.dumps(
                {
                    "status": "ready",
                    "catalog_hash": canonical_catalog_hash,
                    "result_hash": direct_hash,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            flush=True,
        )
        recovered = await _wait_for_recovery(
            http_gateway,
            recovered_context,
            fixture=fixture,
        )
        recovered_catalog = await http_gateway.validate_catalog()
        if recovered_catalog.content_hash != canonical_catalog_hash:
            raise RuntimeError("data-tools 重连后的 Join 目录版本发生漂移")
        recovered_hash = stable_hash(recovered)
        if recovered_hash != direct_hash:
            raise RuntimeError("data-tools 重连后的 Join 预检结果发生漂移")
        return {
            "schema": "chatbi-compose-join-recovery-probe-v1",
            "status": "passed",
            "tools": ["join_preflight", "join_datasets"],
            "tool_version": execute_descriptor.metadata.tool_version,
            "catalog_hash": canonical_catalog_hash,
            "preflight_result_hash": direct_hash,
            "direct_stdio_http_equivalent": True,
            "recovered_result_equivalent": True,
            "connection_generation": http_gateway.health.generation,
            "cross_project_rejected": True,
            "sensitive_key_rejected": True,
            "missing_approval_rejected": True,
            "approved_execution_passed": True,
            "many_to_many_detected": True,
            "row_expansion_detected": True,
            "two_parent_result_verified": True,
            "raw_data_in_report": False,
            "key_values_in_report": False,
            "dataset_refs_in_report": False,
        }
    finally:
        if output_ref is not None:
            delete_dataset(output_ref)
        await stdio_gateway.aclose()
        await http_gateway.aclose()


def main() -> int:
    report = asyncio.run(run_probe())
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
