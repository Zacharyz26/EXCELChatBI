"""v2.5 3A TaskRun/MemorySnapshot/Artifact 联合恢复门禁测试。"""

from __future__ import annotations

from pathlib import Path

from apps.api.memory_recovery_probe import (
    disturb_probe_state,
    seed_probe,
    verify_probe,
)
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.common.config import Settings
from packages.governance.permissions import Principal
from packages.session.models import ArtifactDraft
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from packages.session.workspace_backup import backup_workspace, restore_workspace


def _settings(tmp_path: Path) -> Settings:
    dataset_dir = tmp_path / "datasets"
    report_dir = tmp_path / "reports"
    backup_dir = tmp_path / "backups"
    dataset_dir.mkdir()
    report_dir.mkdir()
    backup_dir.mkdir()
    return Settings(
        _env_file=None,
        chat_db_path=str(tmp_path / "db" / "chatbi.db"),
        dataset_dir=str(dataset_dir),
        report_dir=str(report_dir),
        workspace_backup_dir=str(backup_dir),
        model_registry_path="config/models.example.yaml",
    )


def _completed_report_run(settings: Settings, principal: Principal) -> str:
    session = SessionStore(settings.chat_db_path)
    project = session.create_project(
        "联合恢复",
        owner_user_id=principal.user_id,
        tenant_id=principal.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    user_message = session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="生成恢复报告",
    )
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="compose-original-run",
        user_text=user_message.content,
        chart_required=False,
        report_required=True,
        pdf_required=True,
    )
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 1},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    invocation, created = tasks.start_invocation(
        run_id=run.run_id,
        tool_call_id="report-call",
        tool_name="generate_report",
        arguments={"format": "pdf"},
        idempotency_key="recovery-report-once",
    )
    assert created is True
    assistant = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="报告已生成",
    )
    report_id = "a" * 32
    report_root = Path(settings.report_dir)
    (report_root / f"{report_id}.md").write_text("# recovered", encoding="utf-8")
    (report_root / f"{report_id}.pdf").write_bytes(b"%PDF-1.4 recovered")
    run, _, _, artifact, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result={"report_id": report_id},
        evidence_kind="tool_result",
        evidence_source={"tool": "generate_report"},
        evidence_summary={"summary": "报告生成完成"},
        artifact_draft=ArtifactDraft(
            type="report",
            payload={
                "report_id": report_id,
                "md_url": f"/reports/{report_id}.md",
                "pdf_url": f"/reports/{report_id}.pdf",
            },
            file_ref=f"{report_id}.md",
            source_tool="generate_report",
            params=None,
            dataset_ref=None,
        ),
    )
    assert artifact is not None
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="verifying",
        event_type="verification.started",
        payload={},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="completed",
        event_type="run.completed",
        payload={},
    )
    return run.run_id


def test_joint_recovery_probe_survives_exact_workspace_restore(
    tmp_path: Path,
    monkeypatch,
) -> None:
    settings = _settings(tmp_path)
    principal = Principal(user_id="owner", tenant_id="tenant-a")
    original_run_id = _completed_report_run(settings, principal)
    seeded = seed_probe(
        settings,
        original_run_id=original_run_id,
        principal=principal,
    )
    verified = verify_probe(
        settings,
        original_run_id=original_run_id,
        probe_run_id=str(seeded["probe_run_id"]),
        memory_snapshot_id=str(seeded["memory_snapshot_id"]),
        memory_content_hash=str(seeded["memory_content_hash"]),
        compaction_id=str(seeded["compaction_id"]),
        compaction_summary_hash=str(seeded["compaction_summary_hash"]),
        latest_compaction_id=str(seeded["latest_compaction_id"]),
        memory_id=str(seeded["memory_id"]),
        artifact_id=str(seeded["artifact_id"]),
        plan_id=str(seeded["plan_id"]),
        plan_version=int(seeded["plan_version"]),
        plan_hash=str(seeded["plan_hash"]),
        reference_resolution_hash=str(seeded["reference_resolution_hash"]),
        memory_reference_resolution_hash=str(
            seeded["memory_reference_resolution_hash"]
        ),
        lineage_graph_hash=str(seeded["lineage_graph_hash"]),
        lineage_node_count=int(seeded["lineage_node_count"]),
        lineage_edge_count=int(seeded["lineage_edge_count"]),
        principal=principal,
    )
    assert verified["report_completion_count"] == 1
    assert verified["memory_records"] == 1
    assert seeded["compaction_id"] != seeded["latest_compaction_id"]
    assert verified["compaction_id"] == seeded["compaction_id"]
    assert verified["latest_compaction_id"] == seeded["latest_compaction_id"]
    assert verified["plan_id"] == seeded["plan_id"]
    assert verified["plan_hash"] == seeded["plan_hash"]
    assert verified["reference_resolution_hash"] == seeded["reference_resolution_hash"]
    assert (
        verified["memory_reference_resolution_hash"]
        == seeded["memory_reference_resolution_hash"]
    )
    assert verified["probe_invocation_count"] == 0
    assert verified["lineage_graph_hash"] == seeded["lineage_graph_hash"]
    assert verified["lineage_node_count"] == seeded["lineage_node_count"]
    assert verified["lineage_edge_count"] == seeded["lineage_edge_count"]

    backed_up = backup_workspace(
        db_path=settings.chat_db_path,
        dataset_dir=settings.dataset_dir,
        artifact_dir=settings.report_dir,
        backup_root=settings.workspace_backup_dir,
        service_stopped=True,
    )
    monkeypatch.setenv("CHATBI_RECOVERY_PROBE_ALLOW_DESTRUCTIVE", "1")
    disturbed = disturb_probe_state(
        settings,
        artifact_id=str(seeded["artifact_id"]),
    )
    assert "chatbi.db" in disturbed["moved"]
    assert not Path(settings.chat_db_path).exists()

    restore_workspace(
        input_dir=str(backed_up["path"]),
        db_path=settings.chat_db_path,
        dataset_dir=settings.dataset_dir,
        artifact_dir=settings.report_dir,
        backup_root=settings.workspace_backup_dir,
        service_stopped=True,
        confirmed=True,
        replace_files=True,
    )
    restored = verify_probe(
        settings,
        original_run_id=original_run_id,
        probe_run_id=str(seeded["probe_run_id"]),
        memory_snapshot_id=str(seeded["memory_snapshot_id"]),
        memory_content_hash=str(seeded["memory_content_hash"]),
        compaction_id=str(seeded["compaction_id"]),
        compaction_summary_hash=str(seeded["compaction_summary_hash"]),
        latest_compaction_id=str(seeded["latest_compaction_id"]),
        memory_id=str(seeded["memory_id"]),
        artifact_id=str(seeded["artifact_id"]),
        plan_id=str(seeded["plan_id"]),
        plan_version=int(seeded["plan_version"]),
        plan_hash=str(seeded["plan_hash"]),
        reference_resolution_hash=str(seeded["reference_resolution_hash"]),
        memory_reference_resolution_hash=str(
            seeded["memory_reference_resolution_hash"]
        ),
        lineage_graph_hash=str(seeded["lineage_graph_hash"]),
        lineage_node_count=int(seeded["lineage_node_count"]),
        lineage_edge_count=int(seeded["lineage_edge_count"]),
        principal=principal,
    )
    assert restored["status"] == "verified"
    assert restored["probe_run_status"] == "paused"
    assert restored["report_completion_count"] == 1
    assert restored["compaction_id"] == seeded["compaction_id"]
    assert restored["latest_compaction_id"] == seeded["latest_compaction_id"]
    assert restored["plan_id"] == seeded["plan_id"]
    assert restored["plan_hash"] == seeded["plan_hash"]
    assert restored["probe_invocation_count"] == 0
    assert restored["lineage_graph_hash"] == seeded["lineage_graph_hash"]
    assert restored["lineage_node_count"] == seeded["lineage_node_count"]
    assert restored["lineage_edge_count"] == seeded["lineage_edge_count"]
