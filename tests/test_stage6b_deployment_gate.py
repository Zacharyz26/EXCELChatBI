"""v2.5 6B data-role/quality dual-transport deployment gate contract."""

from __future__ import annotations

from pathlib import Path

import pytest
from apps.api.data_role_recovery_probe import (
    _assert_catalog,
    _assert_result,
    _context,
    _seed,
)
from mcp_servers.agent_service.server import AgentServiceRuntime
from packages.common.config import Settings, get_settings
from packages.session.store import SessionStore

_ROOT = Path(__file__).resolve().parents[1]


def test_data_tools_exports_the_reviewed_data_role_contract(tmp_path: Path) -> None:
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        process_role="mcp_server",
        chat_db_path=str(tmp_path / "chatbi.db"),
        dataset_dir=str(tmp_path / "datasets"),
        report_dir=str(tmp_path / "artifacts"),
        kb_index_dir=str(tmp_path / "kb" / "index"),
        kb_backup_dir=str(tmp_path / "kb" / "backups"),
    )
    SessionStore(settings.chat_db_path)
    runtime = AgentServiceRuntime("data-tools", settings)

    descriptor = _assert_catalog(runtime.adapter.list_tools())

    assert descriptor.output_schema["required"] == ["profile", "roles", "quality"]
    assert descriptor.output_schema["properties"]["roles"]["additionalProperties"] is False
    assert descriptor.output_schema["properties"]["quality"]["additionalProperties"] is False


def test_compose_probe_fixture_exercises_roles_and_read_only_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATASET_DIR", str(tmp_path / "datasets"))
    get_settings.cache_clear()
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        process_role="mcp_server",
        chat_db_path=str(tmp_path / "chatbi.db"),
        dataset_dir=str(tmp_path / "datasets"),
        report_dir=str(tmp_path / "artifacts"),
        kb_index_dir=str(tmp_path / "kb" / "index"),
        kb_backup_dir=str(tmp_path / "kb" / "backups"),
    )
    project_id, conversation_id, dataset_ref = _seed(settings)
    runtime = AgentServiceRuntime("data-tools", settings)

    result = runtime.adapter.call_tool(
        "get_data_profile",
        {"dataset_ref": dataset_ref},
        _context(
            project_id=project_id,
            conversation_id=conversation_id,
            run_id="stage-6b-test",
        ),
    )
    get_settings.cache_clear()

    assert result.is_error is False
    assert result.structured_content is not None
    _assert_result(result.structured_content, dataset_ref=dataset_ref)


def test_compose_gate_checks_data_role_dual_transport_and_restart() -> None:
    gate = (_ROOT / "scripts" / "run_compose_e2e.sh").read_text(encoding="utf-8")
    probe = (_ROOT / "apps" / "api" / "data_role_recovery_probe.py").read_text(
        encoding="utf-8"
    )

    assert "apps.api.data_role_recovery_probe" in gate
    assert 'restart data-tools' in gate
    assert '"status":"ready"' in gate
    assert '"status":"passed"' in gate
    assert "direct_stdio_http_equivalent" in probe
    assert "recovered_result_equivalent" in probe
    assert '"raw_data_in_report": False' in probe
