"""Stage 6B Compose gate for data-role contract equivalence and recovery."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid
from dataclasses import replace
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
from packages.common.dataset_store import save_dataframe
from packages.governance.permissions import Principal
from packages.session.store import SessionStore

_PRINCIPAL = Principal(user_id="local-user", tenant_id="local")
_EXPECTED_TOOL_NAMES = (
    "aggregate_preview",
    "get_data_profile",
    "transform_dataset",
)


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
        step_id="compose-data-role-profile",
        invocation_id=f"compose-data-role-invocation:{run_id}",
        idempotency_key=f"compose-data-role-idempotency:{run_id}",
        permission_snapshot_id="compose-data-role-permissions",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
        trace_id=f"compose-data-role-trace:{run_id}",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )


def _seed(settings: Settings) -> tuple[str, str, str]:
    frame = pd.DataFrame(
        {
            "业务日期": pd.to_datetime(
                [
                    "2026-01-01",
                    "2026-01-01",
                    "2026-01-02",
                    "2026-01-02",
                    "2026-01-03",
                    "2026-01-03",
                ]
            ),
            "地区": ["华东", "华东", "华南", "华南", "华北", "华北"],
            "销售额": [100, 100, 120, 120, 130, 130],
            "订单编号": ["A-001", "A-001", "A-002", "A-002", "A-003", "A-003"],
            "批次": ["v1"] * 6,
            "备注": [None, None, None, None, "复核", "复核"],
        }
    )
    dataset_ref = save_dataframe(frame)
    sessions = SessionStore(settings.chat_db_path)
    project = sessions.create_project(
        f"Stage 6B data-role probe {uuid.uuid4().hex[:8]}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = sessions.create_conversation(project.id, "Data-role reconnect")
    sessions.register_dataset(
        ref=dataset_ref,
        project_id=project.id,
        filename="stage-6b-data-role-probe.parquet",
        profile={},
    )
    return project.id, conversation.id, dataset_ref


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


def _assert_catalog(descriptors: tuple[MCPToolDescriptor, ...]) -> MCPToolDescriptor:
    names = tuple(sorted(item.name for item in descriptors))
    if names != _EXPECTED_TOOL_NAMES:
        raise RuntimeError(f"data-tools 目录不完整: {names}")
    profile = next(item for item in descriptors if item.name == "get_data_profile")
    metadata = profile.metadata
    if (
        metadata.tool_version != "1.1.0"
        or metadata.capabilities != ("data.profile", "data.roles", "data.quality")
        or not metadata.read_only
        or not metadata.idempotent
        or metadata.destructive
    ):
        raise RuntimeError("get_data_profile 1.1.0 治理元数据漂移")
    properties = profile.output_schema.get("properties")
    if not isinstance(properties, dict) or set(properties) != {"profile", "roles", "quality"}:
        raise RuntimeError("get_data_profile 输出契约缺少角色或质量字段")
    return profile


def _assert_result(result: dict[str, Any], *, dataset_ref: str) -> None:
    profile = result.get("profile")
    roles = result.get("roles")
    quality = result.get("quality")
    if (
        not isinstance(profile, dict)
        or not isinstance(roles, dict)
        or not isinstance(quality, dict)
    ):
        raise RuntimeError("get_data_profile 未返回完整结构化结果")
    if (
        profile.get("dataset_ref") != dataset_ref
        or profile.get("row_count") != 6
        or profile.get("column_count") != 6
    ):
        raise RuntimeError("get_data_profile 数据画像与固定数据集不一致")
    summary = roles.get("summary")
    if (
        roles.get("schema") != "chatbi-data-roles-v1"
        or not isinstance(summary, dict)
        or any(summary.get(role, 0) < 1 for role in ("time", "metric", "dimension", "identifier"))
    ):
        raise RuntimeError("get_data_profile 未覆盖固定角色分类")
    recommendations = quality.get("recommendations")
    if (
        quality.get("schema") != "chatbi-data-quality-v1"
        or quality.get("mutates_data") is not False
        or quality.get("duplicate_rows") != 3
        or "批次" not in quality.get("constant_columns", [])
        or not isinstance(recommendations, list)
        or not recommendations
        or any(item.get("automatic") is not False for item in recommendations)
    ):
        raise RuntimeError("get_data_profile 质量建议未保持只读固定契约")


async def _wait_for_recovery(
    gateway: ManagedMCPClientGateway,
    context: MCPRequestContext,
    *,
    dataset_ref: str,
    timeout_seconds: float = 120,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            execution = await gateway.execute(
                "get_data_profile",
                {"dataset_ref": dataset_ref},
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
            _assert_result(execution.result, dataset_ref=dataset_ref)
            return execution.result
        await asyncio.sleep(0.25)
    raise TimeoutError("Compose data-tools 重启后未恢复受治理工具调用")


async def run_probe() -> dict[str, Any]:
    settings = get_settings()
    project_id, conversation_id, dataset_ref = _seed(settings)
    initial_context = _context(
        project_id=project_id,
        conversation_id=conversation_id,
        run_id="compose-data-role-run-1",
    )
    recovered_context = replace(
        _context(
            project_id=project_id,
            conversation_id=conversation_id,
            run_id="compose-data-role-run-2",
        ),
        data_version_hash=stable_hash({"dataset_ref": dataset_ref}),
    )

    canonical_runtime = AgentServiceRuntime("data-tools", settings)
    expected = canonical_runtime.adapter.list_tools()
    profile_descriptor = _assert_catalog(expected)
    canonical_catalog_hash = _catalog_hash(expected)
    direct = canonical_runtime.adapter.call_tool(
        "get_data_profile",
        {"dataset_ref": dataset_ref},
        initial_context,
    )
    if direct.is_error or direct.structured_content is None:
        raise RuntimeError(f"data-tools 直接调用失败: {direct.error_code}")
    _assert_result(direct.structured_content, dataset_ref=dataset_ref)
    direct_hash = stable_hash(direct.structured_content)

    raw_tokens = json.loads(settings.agent_mcp_service_tokens_json)
    service_token = raw_tokens.get("data-tools") if isinstance(raw_tokens, dict) else None
    if not service_token or not settings.agent_mcp_context_signing_key:
        raise RuntimeError("Compose data-role 探针缺少 data-tools 内部凭据")
    http_config = MCPClientConfig(
        transport="streamable_http",
        http_url=os.getenv("MCP_DATA_ROLE_PROBE_HTTP_URL", "http://data-tools:8000/mcp/"),
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
    try:
        http_catalog = await http_gateway.validate_catalog()
        stdio_catalog = await stdio_gateway.validate_catalog()
        if {
            canonical_catalog_hash,
            http_catalog.content_hash,
            stdio_catalog.content_hash,
        } != {canonical_catalog_hash}:
            raise RuntimeError("data-tools 在直接调用、stdio 与 HTTP 间目录不等价")
        http_result = await http_gateway.execute(
            "get_data_profile",
            {"dataset_ref": dataset_ref},
            initial_context,
            timeout_seconds=20,
        )
        stdio_result = await stdio_gateway.execute(
            "get_data_profile",
            {"dataset_ref": dataset_ref},
            initial_context,
            timeout_seconds=20,
        )
        _assert_result(http_result.result, dataset_ref=dataset_ref)
        _assert_result(stdio_result.result, dataset_ref=dataset_ref)
        if {direct_hash, stable_hash(http_result.result), stable_hash(stdio_result.result)} != {
            direct_hash
        }:
            raise RuntimeError("get_data_profile 在直接调用、stdio 与 HTTP 间结果不等价")

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
            dataset_ref=dataset_ref,
        )
        recovered_catalog = await http_gateway.validate_catalog()
        if recovered_catalog.content_hash != canonical_catalog_hash:
            raise RuntimeError("data-tools 重连后的目录版本发生漂移")
        recovered_hash = stable_hash(recovered)
        if recovered_hash != direct_hash:
            raise RuntimeError("data-tools 重连后的角色/质量结果发生漂移")
        return {
            "schema": "chatbi-compose-data-role-recovery-probe-v1",
            "status": "passed",
            "tool": "get_data_profile",
            "tool_version": profile_descriptor.metadata.tool_version,
            "capabilities": list(profile_descriptor.metadata.capabilities),
            "catalog_hash": canonical_catalog_hash,
            "result_hash": direct_hash,
            "direct_stdio_http_equivalent": True,
            "recovered_result_equivalent": True,
            "connection_generation": http_gateway.health.generation,
            "quality_advice_mutates_data": False,
            "raw_data_in_report": False,
        }
    finally:
        await stdio_gateway.aclose()
        await http_gateway.aclose()


def main() -> int:
    report = asyncio.run(run_probe())
    print(json.dumps(report, ensure_ascii=False, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
