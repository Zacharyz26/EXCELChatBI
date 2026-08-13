"""v2.5 6D governed-forecast dual-transport deployment gate contract."""

from __future__ import annotations

from pathlib import Path

import apps.api.forecast_recovery_probe as recovery_probe
import pytest
from apps.api.forecast_recovery_probe import (
    _arguments,
    _assert_catalog,
    _assert_result,
    _context,
    _gateway,
    _seed,
)
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.common.client_gateway import MCPClientConfig
from mcp_servers.common.contracts import MCPToolDescriptor
from mcp_servers.common.service_catalog import parse_capability_profiles
from packages.common.config import Settings, get_settings
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


def test_stats_tools_exports_the_reviewed_forecast_contract(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    SessionStore(settings.chat_db_path)
    runtime = AgentServiceRuntime("stats-tools", settings)

    descriptor = _assert_catalog(runtime.adapter.list_tools())

    assert "forecast" not in parse_capability_profiles(
        settings.agent_capability_profiles
    )
    assert descriptor.input_schema["additionalProperties"] is False
    assert descriptor.output_schema["additionalProperties"] is False
    assert descriptor.output_schema["properties"]["leakage_checks"][
        "additionalProperties"
    ] is False
    assert descriptor.output_schema["properties"]["prediction_interval"][
        "additionalProperties"
    ] is False


@pytest.mark.asyncio
async def test_forecast_probe_validates_the_complete_stats_service_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = _settings(tmp_path)
    SessionStore(settings.chat_db_path)
    descriptors = AgentServiceRuntime("stats-tools", settings).adapter.list_tools()

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
    assert catalog.report.expected_count == 7
    assert catalog.report.discovered_count == 7
    assert catalog.report.unexpected == ()
    assert {descriptor.name for descriptor in catalog.descriptors} == {
        "anomaly_detect",
        "correlation",
        "dimension_contribution",
        "forecast",
        "group_compare",
        "regression",
        "trend_analysis",
    }


def test_compose_probe_fixture_exercises_forecast_quality_guards(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    settings = _settings(tmp_path)
    project_id, conversation_id, dataset_ref = _seed(settings)
    runtime = AgentServiceRuntime("stats-tools", settings)

    result = runtime.adapter.call_tool(
        "forecast",
        _arguments(dataset_ref),
        _context(
            project_id=project_id,
            conversation_id=conversation_id,
            run_id="stage-6d-test",
        ),
    )
    get_settings.cache_clear()

    assert result.is_error is False
    assert result.structured_content is not None
    _assert_result(result.structured_content)


def test_compose_gate_checks_forecast_dual_transport_and_restart() -> None:
    gate = (_ROOT / "scripts" / "run_compose_e2e.sh").read_text(encoding="utf-8")
    probe = (_ROOT / "apps" / "api" / "forecast_recovery_probe.py").read_text(
        encoding="utf-8"
    )
    compose = (_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "apps.api.forecast_recovery_probe" in gate
    assert 'restart stats-tools' in gate
    assert '"status":"ready"' in gate
    assert '"status":"passed"' in gate
    assert "direct_stdio_http_equivalent" in probe
    assert "recovered_result_equivalent" in probe
    assert "leakage_checks_passed" in probe
    assert "baseline_gate_passed" in probe
    assert "forecast_profile_enabled" in probe
    assert '"raw_data_in_report": False' in probe
    assert '"predictions_in_report": False' in probe
    assert "${AGENT_CAPABILITY_PROFILES:-stats,browser}" in compose
