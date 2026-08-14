"""v2.5 6E governed-Join dual-transport deployment gate contract."""

from __future__ import annotations

from pathlib import Path

import apps.api.join_recovery_probe as recovery_probe
import pytest
from apps.api.join_recovery_probe import (
    _approved_context,
    _arguments,
    _assert_catalog,
    _assert_execution,
    _assert_preflight,
    _context,
    _gateway,
    _seed,
)
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.common.client_gateway import MCPClientConfig
from mcp_servers.common.contracts import MCPToolDescriptor
from packages.common.config import Settings, get_settings
from packages.common.dataset_store import delete_dataset
from packages.session.store import SessionStore

_ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        process_role="mcp_server",
        chat_db_path=str(tmp_path / "chatbi.db"),
        dataset_dir=str(tmp_path / "datasets"),
        report_dir=str(tmp_path / "artifacts"),
        kb_index_dir=str(tmp_path / "kb" / "index"),
        kb_backup_dir=str(tmp_path / "kb" / "backups"),
    )


def test_data_tools_exports_reviewed_join_contracts(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    SessionStore(settings.chat_db_path)
    runtime = AgentServiceRuntime("data-tools", settings)

    preflight, execute = _assert_catalog(runtime.adapter.list_tools())

    assert preflight.output_schema["additionalProperties"] is False
    assert execute.output_schema["additionalProperties"] is False
    assert preflight.metadata.read_only is True
    assert execute.metadata.read_only is False
    assert execute.metadata.idempotent is False
    assert execute.metadata.risk_level == "high"


@pytest.mark.asyncio
async def test_join_probe_validates_complete_data_service_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    SessionStore(settings.chat_db_path)
    descriptors = AgentServiceRuntime("data-tools", settings).adapter.list_tools()

    class CatalogTransport:
        async def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
            return descriptors

        async def aclose(self) -> None:
            return None

    transport = CatalogTransport()
    monkeypatch.setattr(
        recovery_probe,
        "OfficialSDKClientTransport",
        lambda _config: transport,
    )
    gateway = _gateway(MCPClientConfig(), descriptors)
    try:
        catalog = await gateway.validate_catalog()
    finally:
        await gateway.aclose()

    assert catalog.report.healthy is True
    assert catalog.report.expected_count == 5
    assert catalog.report.discovered_count == 5
    assert catalog.report.unexpected == ()
    assert {descriptor.name for descriptor in catalog.descriptors} == {
        "aggregate_preview",
        "get_data_profile",
        "join_datasets",
        "join_preflight",
        "transform_dataset",
    }


def test_compose_probe_fixture_exercises_join_governance_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    settings = _settings(tmp_path)
    fixture = _seed(settings)
    runtime = AgentServiceRuntime("data-tools", settings)
    _preflight_descriptor, execute_descriptor = _assert_catalog(
        runtime.adapter.list_tools()
    )
    context = _context(
        project_id=fixture.project_id,
        conversation_id=fixture.conversation_id,
        run_id="stage-6e-test",
    )
    arguments = _arguments(fixture)

    preflight = runtime.adapter.call_tool("join_preflight", arguments, context)
    cross_project = runtime.adapter.call_tool(
        "join_preflight",
        _arguments(fixture, right_ref=fixture.foreign_ref),
        context,
    )
    protected = runtime.adapter.call_tool(
        "join_preflight",
        _arguments(fixture, right_ref=fixture.protected_ref),
        context,
    )
    unapproved = runtime.adapter.call_tool("join_datasets", arguments, context)
    approved = runtime.adapter.call_tool(
        "join_datasets",
        arguments,
        _approved_context(context, execute_descriptor, arguments),
    )

    assert preflight.is_error is False
    assert preflight.structured_content is not None
    _assert_preflight(preflight.structured_content)
    assert cross_project.is_error is True
    assert cross_project.error_code == "project_scope_violation"
    assert protected.is_error is True
    assert protected.error_code == "tool_business_error"
    assert unapproved.is_error is True
    assert unapproved.error_code == "approval_required"
    assert approved.is_error is False
    assert approved.structured_content is not None
    _assert_execution(approved.structured_content, fixture=fixture)
    delete_dataset(str(approved.structured_content["dataset_ref"]))
    get_settings.cache_clear()


def test_compose_gate_checks_join_dual_transport_restart_and_browser() -> None:
    gate = (_ROOT / "scripts" / "run_compose_e2e.sh").read_text(encoding="utf-8")
    probe = (_ROOT / "apps" / "api" / "join_recovery_probe.py").read_text(
        encoding="utf-8"
    )
    browser = (
        _ROOT / "apps" / "web" / "e2e" / "workspace-artifacts.spec.ts"
    ).read_text(encoding="utf-8")

    assert "apps.api.join_recovery_probe" in gate
    assert 'restart data-tools' in gate
    assert '"status":"ready"' in gate
    assert '"status":"passed"' in gate
    assert "direct_stdio_http_equivalent" in probe
    assert "recovered_result_equivalent" in probe
    assert "cross_project_rejected" in probe
    assert "sensitive_key_rejected" in probe
    assert "missing_approval_rejected" in probe
    assert "approved_execution_passed" in probe
    assert "two_parent_result_verified" in probe
    assert '"raw_data_in_report": False' in probe
    assert "6E-4 Join 发布浏览器门禁" in browser
    assert "完整双父血缘已登记" in browser
