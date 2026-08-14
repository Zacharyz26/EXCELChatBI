"""Stage 2E service partition, routing, readiness and isolation tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from apps.orchestrator.agent_tools import AgentContext, build_registry
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.chart.server import build_server as build_chart
from mcp_servers.common.client_gateway import MCPClientConfig
from mcp_servers.common.contracts import MCPRequestContext, stable_hash
from mcp_servers.common.service_catalog import (
    AGENT_MCP_SERVICE_TOOLS,
    AGENT_MCP_SERVICES,
)
from mcp_servers.dataset_ops.server import build_server as build_ops
from mcp_servers.excel_parser.server import build_server as build_excel
from mcp_servers.report.server import build_server as build_report
from mcp_servers.stats.server import build_server as build_stats
from packages.common.config import Settings, get_settings
from packages.common.dataset_store import save_dataframe
from packages.rag.embedding import HashingEmbedder
from packages.rag.rerank import LexicalReranker
from packages.rag.retriever import HybridRetriever
from packages.rag.store import LocalKnowledgeStore
from packages.session.store import SessionStore


def _context(project_id: str, conversation_id: str) -> MCPRequestContext:
    return MCPRequestContext(
        subject_id="stage2e-user",
        project_id=project_id,
        conversation_id=conversation_id,
        run_id="run-stage2e",
        plan_version=1,
        step_id="profile",
        invocation_id="invocation-stage2e",
        idempotency_key="idempotency-stage2e",
        permission_snapshot_id="permission-stage2e",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=0,
        data_version_hash="0" * 64,
        cancellation_node_id="0" * 32,
        trace_id="trace-stage2e",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
    )


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        process_role="mcp_server",
        chat_db_path=str(tmp_path / "chatbi.db"),
        dataset_dir=str(tmp_path / "datasets"),
        report_dir=str(tmp_path / "artifacts"),
        kb_index_dir=str(tmp_path / "kb" / "index"),
        kb_backup_dir=str(tmp_path / "kb" / "backups"),
    )


def test_five_service_catalog_is_complete_and_disjoint() -> None:
    assert tuple(AGENT_MCP_SERVICE_TOOLS) == AGENT_MCP_SERVICES
    flattened = [
        tool_name for tool_names in AGENT_MCP_SERVICE_TOOLS.values() for tool_name in tool_names
    ]
    assert len(flattened) == 17
    assert len(flattened) == len(set(flattened))


def test_production_routes_resolve_six_secret_files(tmp_path: Path) -> None:
    urls = {service: f"http://{service}:8000/mcp/" for service in AGENT_MCP_SERVICES}
    token_files: dict[str, str] = {}
    for service in AGENT_MCP_SERVICES:
        path = tmp_path / f"{service}.token"
        path.write_text(f"secret-{service}", encoding="utf-8")
        token_files[service] = str(path)
    signing_key = tmp_path / "context.key"
    signing_key.write_text("separate-context-key", encoding="utf-8")

    settings = Settings(
        _env_file=None,
        app_env="production",
        auth_mode="bearer",
        auth_tokens_json='{"api":{"user_id":"u","tenant_id":"t"}}',
        agent_mcp_transport="streamable_http",
        agent_mcp_server_urls_json=json.dumps(urls),
        agent_mcp_service_token_files_json=json.dumps(token_files),
        agent_mcp_context_signing_key_file=str(signing_key),
    )

    assert json.loads(settings.agent_mcp_service_tokens_json) == {
        service: f"secret-{service}" for service in AGENT_MCP_SERVICES
    }
    assert settings.agent_mcp_context_signing_key == "separate-context-key"


async def test_registry_routes_each_partition_through_an_independent_gateway(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("2E")
    conversation = store.create_conversation(project.id)
    dataset_ref = save_dataframe(pd.DataFrame({"x": [1, 2], "y": [2, 4]}))
    store.register_dataset(
        ref=dataset_ref,
        project_id=project.id,
        filename="routed.xlsx",
        profile={},
    )
    retriever = HybridRetriever(
        HashingEmbedder(),
        LocalKnowledgeStore(str(tmp_path / "kb")),
        LexicalReranker(),
    )
    routes = {service: f"http://{service}:8000/mcp/" for service in AGENT_MCP_SERVICES}
    tokens = {service: f"token-{service}" for service in AGENT_MCP_SERVICES}
    registry = build_registry(
        excel=build_excel(),
        stats=build_stats(),
        chart=build_chart(),
        dataset_ops=build_ops(),
        report=build_report(),
        retriever=retriever,
        context=AgentContext(store, project.id, conversation.id),
        # in_process deliberately exercises the same five subset catalogs without
        # opening sockets; HTTP transport behavior remains covered by the 2D probe.
        mcp_config=MCPClientConfig(
            transport="in_process",
            service_urls=routes,
            service_tokens=tokens,
        ),
    )
    try:
        result = await registry.execute_mcp(
            "get_data_profile",
            {"dataset_ref": dataset_ref},
            _context(project.id, conversation.id),
            timeout_seconds=5,
        )
    finally:
        await registry.aclose()
        get_settings.cache_clear()
    assert result.result["profile"]["row_count"] == 2
    assert result.health.generation == 1
    assert result.service_name == "data-tools"


def test_service_rejects_cross_project_dataset_and_reports_readiness(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("CHAT_DB_PATH", settings.chat_db_path)
    monkeypatch.setenv("DATASET_DIR", settings.dataset_dir)
    monkeypatch.setenv("REPORT_DIR", settings.report_dir)
    monkeypatch.setenv("KB_INDEX_DIR", settings.kb_index_dir)
    get_settings.cache_clear()
    writer = SessionStore(settings.chat_db_path)
    project = writer.create_project("allowed")
    conversation = writer.create_conversation(project.id)
    other_project = writer.create_project("denied")
    dataset_ref = save_dataframe(pd.DataFrame({"value": [1, 2]}))
    writer.register_dataset(
        ref=dataset_ref,
        project_id=other_project.id,
        filename="other.xlsx",
        profile={},
    )
    allowed_ref = save_dataframe(pd.DataFrame({"id": [1, 2]}))
    writer.register_dataset(
        ref=allowed_ref,
        project_id=project.id,
        filename="allowed.xlsx",
        profile={},
    )
    runtime = AgentServiceRuntime("data-tools", settings)

    result = runtime.adapter.call_tool(
        "get_data_profile",
        {"dataset_ref": dataset_ref},
        _context(project.id, conversation.id),
    )
    join_result = runtime.adapter.call_tool(
        "join_preflight",
        {
            "left_dataset_ref": allowed_ref,
            "right_dataset_ref": dataset_ref,
            "left_key": "id",
            "right_key": "value",
            "join_type": "inner",
        },
        _context(project.id, conversation.id),
    )
    ready, details = runtime.readiness()
    get_settings.cache_clear()

    assert result.is_error is True
    assert result.error_code == "project_scope_violation"
    assert join_result.is_error is True
    assert join_result.error_code == "project_scope_violation"
    assert ready is True
    assert details == {"tool_count": 5, "storage": "ready"}


def test_join_execution_requires_exact_high_risk_approval(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setenv("CHAT_DB_PATH", settings.chat_db_path)
    monkeypatch.setenv("DATASET_DIR", settings.dataset_dir)
    get_settings.cache_clear()
    store = SessionStore(settings.chat_db_path)
    project = store.create_project("join approval")
    conversation = store.create_conversation(project.id)
    left_ref = save_dataframe(pd.DataFrame({"id": [1, 2]}))
    right_ref = save_dataframe(pd.DataFrame({"id": [1, 2], "value": [3, 4]}))
    for ref in (left_ref, right_ref):
        store.register_dataset(
            ref=ref,
            project_id=project.id,
            filename=f"{ref}.xlsx",
            profile={},
        )
    runtime = AgentServiceRuntime("data-tools", settings)
    arguments = {
        "left_dataset_ref": left_ref,
        "right_dataset_ref": right_ref,
        "left_key": "id",
        "right_key": "id",
        "join_type": "inner",
    }
    descriptor = next(
        item for item in runtime.adapter.list_tools() if item.name == "join_datasets"
    )
    base_context = _context(project.id, conversation.id)

    denied = runtime.adapter.call_tool("join_datasets", arguments, base_context)
    approved_context = replace(
        base_context,
        approval_id="a" * 32,
        approval_version=1,
        approval_contract_hash=descriptor.contract_hash,
        approval_parameter_hash=stable_hash(arguments),
    )
    approved = runtime.adapter.call_tool(
        "join_datasets",
        arguments,
        approved_context,
    )
    get_settings.cache_clear()

    assert denied.is_error is True
    assert denied.error_code == "approval_required"
    assert approved.is_error is False
    assert approved.structured_content is not None
    assert approved.structured_content["parent_refs"] == [left_ref, right_ref]
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        runtime.store.create_project("must-not-write")
