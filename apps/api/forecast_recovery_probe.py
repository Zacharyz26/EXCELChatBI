"""Stage 6D Compose gate for governed-forecast transport equivalence and recovery."""

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
from mcp_servers.common.service_catalog import parse_capability_profiles
from packages.common.config import Settings, get_settings
from packages.common.dataset_store import save_dataframe
from packages.governance.permissions import Principal
from packages.session.store import SessionStore

_PRINCIPAL = Principal(user_id="local-user", tenant_id="local")
_EXPECTED_TOOL_NAMES = (
    "anomaly_detect",
    "correlation",
    "dimension_contribution",
    "forecast",
    "group_compare",
    "regression",
    "trend_analysis",
)
_FORECAST_ARGUMENTS = {
    "time_col": "event_time",
    "value_col": "signal_value",
    "horizon": 3,
    "method": "auto",
    "validation_size": 6,
}


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
        step_id="compose-governed-forecast",
        invocation_id=f"compose-forecast-invocation:{run_id}",
        idempotency_key=f"compose-forecast-idempotency:{run_id}",
        permission_snapshot_id="compose-forecast-permissions",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
        trace_id=f"compose-forecast-trace:{run_id}",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
    )


def _seed(settings: Settings) -> tuple[str, str, str]:
    frame = pd.DataFrame(
        {
            "event_time": pd.date_range("2025-01-01", periods=30, freq="D"),
            "signal_value": [float(index) for index in range(1, 31)],
        }
    )
    dataset_ref = save_dataframe(frame)
    sessions = SessionStore(settings.chat_db_path)
    project = sessions.create_project(
        f"Stage 6D forecast probe {uuid.uuid4().hex[:8]}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = sessions.create_conversation(project.id, "Forecast reconnect")
    sessions.register_dataset(
        ref=dataset_ref,
        project_id=project.id,
        filename="stage-6d-anonymous-forecast-probe.parquet",
        profile={},
    )
    return project.id, conversation.id, dataset_ref


def _arguments(dataset_ref: str) -> dict[str, Any]:
    return {"dataset_ref": dataset_ref, **_FORECAST_ARGUMENTS}


def _gateway(
    config: MCPClientConfig,
    expected: tuple[MCPToolDescriptor, ...],
) -> ManagedMCPClientGateway:
    return ManagedMCPClientGateway(
        config=config,
        expected=expected,
        # Discovery validates the complete service catalog. Limiting this to the
        # one exercised tool would incorrectly classify every other reviewed
        # stats tool returned by ``tools/list`` as unexpected catalog drift.
        allowed_tools=frozenset(descriptor.name for descriptor in expected),
        transport_factory=lambda: OfficialSDKClientTransport(config),
    )


def _catalog_hash(descriptors: tuple[MCPToolDescriptor, ...]) -> str:
    ordered = sorted(descriptors, key=lambda item: item.name)
    return stable_hash([item.to_protocol_dict() for item in ordered])


def _assert_catalog(descriptors: tuple[MCPToolDescriptor, ...]) -> MCPToolDescriptor:
    names = tuple(sorted(item.name for item in descriptors))
    if names != _EXPECTED_TOOL_NAMES:
        raise RuntimeError(f"stats-tools 目录不完整: {names}")
    descriptor = next(item for item in descriptors if item.name == "forecast")
    metadata = descriptor.metadata
    if (
        metadata.tool_version != "1.0.0"
        or metadata.capabilities != ("stats.forecast",)
        or not metadata.read_only
        or not metadata.idempotent
        or metadata.destructive
        or metadata.open_world
    ):
        raise RuntimeError("forecast 受治理元数据漂移")
    if descriptor.input_schema.get("required") != [
        "dataset_ref",
        "value_col",
        "time_col",
        "horizon",
    ]:
        raise RuntimeError("forecast 输入契约漂移")
    required = descriptor.output_schema.get("required")
    if not isinstance(required, list) or not {
        "selected_method",
        "reliability",
        "validation_metrics",
        "baseline",
        "prediction_interval",
        "leakage_checks",
        "predictions",
        "statistical_evidence",
    }.issubset(required):
        raise RuntimeError("forecast 输出契约缺少质量或泄漏字段")
    return descriptor


def _assert_result(result: dict[str, Any]) -> None:
    baseline = result.get("baseline")
    interval = result.get("prediction_interval")
    leakage = result.get("leakage_checks")
    evidence = result.get("statistical_evidence")
    split = result.get("split")
    predictions = result.get("predictions")
    if (
        result.get("requested_method") != "auto"
        or result.get("selected_method") != "drift"
        or result.get("reliability") != "moderate"
        or result.get("frequency") != "D"
        or result.get("horizon") != 3
    ):
        raise RuntimeError("forecast 固定方法选择或可靠性发生漂移")
    if (
        not isinstance(split, dict)
        or split.get("training_observations") != 24
        or split.get("validation_observations") != 6
    ):
        raise RuntimeError("forecast 时间留出边界发生漂移")
    if (
        not isinstance(baseline, dict)
        or baseline.get("method") != "naive"
        or baseline.get("beats_baseline") is not True
    ):
        raise RuntimeError("forecast 未通过朴素基线门禁")
    if (
        not isinstance(interval, dict)
        or interval.get("level") != 0.95
        or interval.get("method") != "empirical_absolute_error"
        or interval.get("radius") != 0.0
    ):
        raise RuntimeError("forecast 经验区间契约发生漂移")
    if leakage != {
        "passed": True,
        "chronological_split": True,
        "duplicate_timestamps": False,
        "regular_frequency": True,
        "future_target_rows_used": False,
        "preprocessing_fit_on_training_only": True,
    }:
        raise RuntimeError("forecast 时间泄漏防线未通过")
    if (
        not isinstance(predictions, list)
        or len(predictions) != 3
        or any(point.get("lower") != point.get("point") for point in predictions)
        or any(point.get("upper") != point.get("point") for point in predictions)
    ):
        raise RuntimeError("forecast 固定预测输出发生漂移")
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != "chatbi-statistical-evidence-v1"
        or evidence.get("analysis_kind") != "forecast"
        or evidence.get("method") != "drift"
        or not evidence.get("limitations")
    ):
        raise RuntimeError("forecast Statistical Evidence 不完整")


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
                "forecast",
                _arguments(dataset_ref),
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
            _assert_result(execution.result)
            return execution.result
        await asyncio.sleep(0.25)
    raise TimeoutError("Compose stats-tools 重启后未恢复受治理预测调用")


async def run_probe() -> dict[str, Any]:
    settings = get_settings()
    enabled_profiles = parse_capability_profiles(settings.agent_capability_profiles)
    project_id, conversation_id, dataset_ref = _seed(settings)
    initial_context = _context(
        project_id=project_id,
        conversation_id=conversation_id,
        run_id="compose-forecast-run-1",
    )
    recovered_context = replace(
        _context(
            project_id=project_id,
            conversation_id=conversation_id,
            run_id="compose-forecast-run-2",
        ),
        data_version_hash=stable_hash({"dataset_ref": dataset_ref}),
    )

    canonical_runtime = AgentServiceRuntime("stats-tools", settings)
    expected = canonical_runtime.adapter.list_tools()
    descriptor = _assert_catalog(expected)
    canonical_catalog_hash = _catalog_hash(expected)
    direct = canonical_runtime.adapter.call_tool(
        "forecast",
        _arguments(dataset_ref),
        initial_context,
    )
    if direct.is_error or direct.structured_content is None:
        raise RuntimeError(f"stats-tools 直接预测失败: {direct.error_code}")
    _assert_result(direct.structured_content)
    direct_hash = stable_hash(direct.structured_content)

    raw_tokens = json.loads(settings.agent_mcp_service_tokens_json)
    service_token = raw_tokens.get("stats-tools") if isinstance(raw_tokens, dict) else None
    if not service_token or not settings.agent_mcp_context_signing_key:
        raise RuntimeError("Compose forecast 探针缺少 stats-tools 内部凭据")
    http_config = MCPClientConfig(
        transport="streamable_http",
        http_url=os.getenv(
            "MCP_FORECAST_PROBE_HTTP_URL",
            "http://stats-tools:8000/mcp/",
        ),
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
            "MCP_AGENT_SERVICE": "stats-tools",
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
            raise RuntimeError("stats-tools 在直接调用、stdio 与 HTTP 间目录不等价")
        http_result = await http_gateway.execute(
            "forecast",
            _arguments(dataset_ref),
            initial_context,
            timeout_seconds=20,
        )
        stdio_result = await stdio_gateway.execute(
            "forecast",
            _arguments(dataset_ref),
            initial_context,
            timeout_seconds=20,
        )
        _assert_result(http_result.result)
        _assert_result(stdio_result.result)
        if {
            direct_hash,
            stable_hash(http_result.result),
            stable_hash(stdio_result.result),
        } != {direct_hash}:
            raise RuntimeError("forecast 在直接调用、stdio 与 HTTP 间结果不等价")

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
            raise RuntimeError("stats-tools 重连后的目录版本发生漂移")
        recovered_hash = stable_hash(recovered)
        if recovered_hash != direct_hash:
            raise RuntimeError("stats-tools 重连后的预测结果发生漂移")
        return {
            "schema": "chatbi-compose-forecast-recovery-probe-v1",
            "status": "passed",
            "tool": "forecast",
            "tool_version": descriptor.metadata.tool_version,
            "capabilities": list(descriptor.metadata.capabilities),
            "catalog_hash": canonical_catalog_hash,
            "result_hash": direct_hash,
            "direct_stdio_http_equivalent": True,
            "recovered_result_equivalent": True,
            "connection_generation": http_gateway.health.generation,
            "leakage_checks_passed": True,
            "baseline_gate_passed": True,
            "forecast_profile_enabled": "forecast" in enabled_profiles,
            "raw_data_in_report": False,
            "predictions_in_report": False,
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
