"""v2.5 3A 工作区一致备份、校验与恢复测试。"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.governance.permissions import Principal
from packages.session.compaction import CompactionStore
from packages.session.memory_models import MemoryDraft
from packages.session.memory_store import MemoryStore
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from packages.session.workspace_backup import (
    backup_workspace,
    restore_workspace,
    verify_workspace_backup,
)


def _seed_workspace(
    tmp_path: Path,
) -> tuple[Path, Path, Path, Path, str, str, str, str]:
    database = tmp_path / "db" / "chatbi.db"
    datasets = tmp_path / "datasets"
    artifacts = tmp_path / "artifacts"
    backups = tmp_path / "backups"
    datasets.mkdir()
    artifacts.mkdir()
    session = SessionStore(str(database))
    principal = Principal(user_id="owner", tenant_id="tenant-a")
    project = session.create_project(
        "恢复项目",
        owner_user_id=principal.user_id,
        tenant_id=principal.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    message = session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="把工单编号显示为请求 ID",
    )
    session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="我会在后续分析中使用这个已确认的展示名称。" + "甲" * 80,
    )
    session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="现在继续处理新的工单数据。" + "乙" * 80,
    )
    dataset_ref = "d" * 32
    session.register_dataset(
        ref=dataset_ref,
        project_id=project.id,
        filename="tickets.xlsx",
        profile={"row_count": 2, "column_count": 1},
    )
    (datasets / f"{dataset_ref}.parquet").write_bytes(b"dataset-v1")
    report_file = artifacts / "reports" / "recovery.md"
    report_file.parent.mkdir()
    report_file.write_text("# recovery artifact", encoding="utf-8")
    artifact = session.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="report",
        file_ref="reports/recovery.md",
        source_tool="generate_report",
        dataset_ref=dataset_ref,
    )
    memory = MemoryStore(session).remember(
        project_id=project.id,
        principal=principal,
        draft=MemoryDraft(
            scope="project",
            kind="field_alias",
            semantic_key="field-alias.ticket-id",
            content_summary="工单编号的展示名称是请求 ID",
            source_type="user_confirmation",
            source_ref=message.id,
            source_hash=hashlib.sha256(message.content.encode("utf-8")).hexdigest(),
            confidence=0.95,
        ),
        idempotency_key="backup-memory",
    ).record
    contract = build_minimal_contract(
        run_id="workspace-backup-run",
        user_text="验证恢复",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = TaskStore(database).create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 1},
    )
    compaction_result = CompactionStore(session).compact_if_needed(
        project_id=project.id,
        conversation_id=conversation.id,
        principal=principal,
        trigger_chars=100,
        keep_recent=1,
        summary_max_chars=512,
    )
    assert compaction_result.view is not None
    compaction_id = compaction_result.view.record.compaction_id
    snapshot, records = MemoryStore(session).create_snapshot(
        project_id=project.id,
        principal=principal,
        conversation_id=conversation.id,
        run_id=run.run_id,
        compaction_id=compaction_id,
    )
    assert records == (memory,)
    return (
        database,
        datasets,
        artifacts,
        backups,
        project.id,
        artifact.id,
        snapshot.memory_snapshot_id,
        compaction_id,
    )


def test_offline_backup_verify_and_exact_restore(tmp_path: Path) -> None:
    (
        database,
        datasets,
        artifacts,
        backups,
        project_id,
        artifact_id,
        snapshot_id,
        compaction_id,
    ) = _seed_workspace(tmp_path)
    result = backup_workspace(
        db_path=database,
        dataset_dir=datasets,
        artifact_dir=artifacts,
        backup_root=backups,
        service_stopped=True,
    )
    backup_path = Path(str(result["path"]))
    manifest = verify_workspace_backup(backup_path)
    database_manifest = manifest["database"]
    assert isinstance(database_manifest, dict)
    counts = database_manifest["table_counts"]
    assert counts["task_runs"] == 1
    assert counts["task_events"] == 1
    assert counts["memory_records"] == 1
    assert counts["memory_snapshots"] == 1
    assert counts["memory_snapshot_items"] == 1
    assert counts["conversation_compactions"] == 1
    assert counts["conversation_compaction_items"] == 2
    assert counts["artifacts"] == 1
    assert counts["datasets"] == 1

    SessionStore(str(database)).update_project(project_id, "已污染")
    (datasets / f"{'d' * 32}.parquet").write_bytes(b"mutated")
    (datasets / "extra.parquet").write_bytes(b"extra")
    (artifacts / "reports" / "recovery.md").write_text(
        "mutated",
        encoding="utf-8",
    )
    restored = restore_workspace(
        input_dir=backup_path,
        db_path=database,
        dataset_dir=datasets,
        artifact_dir=artifacts,
        backup_root=backups,
        service_stopped=True,
        confirmed=True,
        replace_files=True,
    )

    assert restored["status"] == "restored"
    assert Path(str(restored["pre_restore_backup"])).is_dir()
    reopened = SessionStore(str(database))
    project = reopened.get_project(project_id)
    assert project is not None and project.name == "恢复项目"
    assert reopened.list_report_artifacts()[0].id == artifact_id
    snapshot = MemoryStore(reopened).get_snapshot(
        snapshot_id,
        principal=Principal(user_id="owner", tenant_id="tenant-a"),
    )
    assert snapshot is not None and snapshot[0].record_count == 1
    compaction = CompactionStore(reopened).get_view(
        compaction_id,
        project_id=project_id,
        conversation_id=snapshot[0].conversation_id or "",
        principal=Principal(user_id="owner", tenant_id="tenant-a"),
    )
    assert compaction is not None
    assert snapshot[0].compaction_id == compaction.record.compaction_id
    assert (datasets / f"{'d' * 32}.parquet").read_bytes() == b"dataset-v1"
    assert not (datasets / "extra.parquet").exists()
    assert (
        artifacts / "reports" / "recovery.md"
    ).read_text(encoding="utf-8") == "# recovery artifact"


def test_backup_and_restore_require_explicit_offline_acknowledgement(
    tmp_path: Path,
) -> None:
    database, datasets, artifacts, backups, *_ = _seed_workspace(tmp_path)
    with pytest.raises(RuntimeError, match="停止"):
        backup_workspace(
            db_path=database,
            dataset_dir=datasets,
            artifact_dir=artifacts,
            backup_root=backups,
            service_stopped=False,
        )
    result = backup_workspace(
        db_path=database,
        dataset_dir=datasets,
        artifact_dir=artifacts,
        backup_root=backups,
        service_stopped=True,
    )
    with pytest.raises(RuntimeError, match="--yes"):
        restore_workspace(
            input_dir=str(result["path"]),
            db_path=database,
            dataset_dir=datasets,
            artifact_dir=artifacts,
            backup_root=backups,
            service_stopped=True,
            confirmed=False,
            replace_files=True,
        )
    with pytest.raises(RuntimeError, match="--replace-files"):
        restore_workspace(
            input_dir=str(result["path"]),
            db_path=database,
            dataset_dir=datasets,
            artifact_dir=artifacts,
            backup_root=backups,
            service_stopped=True,
            confirmed=True,
            replace_files=False,
        )
    external_backup = tmp_path / "external-backup"
    shutil.copytree(Path(str(result["path"])), external_backup)
    with pytest.raises(RuntimeError, match="WORKSPACE_BACKUP_DIR"):
        restore_workspace(
            input_dir=external_backup,
            db_path=database,
            dataset_dir=datasets,
            artifact_dir=artifacts,
            backup_root=backups,
            service_stopped=True,
            confirmed=True,
            replace_files=True,
        )


def test_tampered_backup_is_rejected_before_restore(tmp_path: Path) -> None:
    database, datasets, artifacts, backups, *_ = _seed_workspace(tmp_path)
    result = backup_workspace(
        db_path=database,
        dataset_dir=datasets,
        artifact_dir=artifacts,
        backup_root=backups,
        service_stopped=True,
    )
    backup_path = Path(str(result["path"]))
    backed_up_dataset = backup_path / "datasets" / f"{'d' * 32}.parquet"
    backed_up_dataset.write_bytes(b"tampered")
    original_database_hash = hashlib.sha256(database.read_bytes()).hexdigest()

    with pytest.raises(RuntimeError, match="大小不匹配|hash 不匹配"):
        restore_workspace(
            input_dir=backup_path,
            db_path=database,
            dataset_dir=datasets,
            artifact_dir=artifacts,
            backup_root=backups,
            service_stopped=True,
            confirmed=True,
            replace_files=True,
        )
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_database_hash
