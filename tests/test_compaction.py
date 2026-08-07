"""v2.5 阶段 3B 持久化上下文压缩、隔离与恢复测试。"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.governance.audit import AuditEvent
from packages.governance.permissions import Principal
from packages.session.compaction import (
    CompactionAccessDenied,
    CompactionResult,
    CompactionStore,
)
from packages.session.memory_store import MemoryAccessDenied, MemoryStore
from packages.session.migrations import CURRENT_SCHEMA_VERSION, v5, v6, v7, v8, v9, v10
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

_OWNER = Principal(user_id="owner", tenant_id="tenant-a")
_VIEWER = Principal(user_id="viewer", tenant_id="tenant-a")
_OTHER_TENANT = Principal(user_id="owner", tenant_id="tenant-b")


def _workspace(tmp_path: Path) -> tuple[SessionStore, str, str]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project(
        "压缩项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    return session, project.id, conversation.id


def _seed_long_history(session: SessionStore, conversation_id: str) -> list[str]:
    messages = [
        session.append_message(
            conversation_id=conversation_id,
            role="user",
            content=(
                "第一条需求需要保留，但凭据 api_key=top-secret-value 不得进入摘要。" + "甲" * 80
            ),
        ),
        session.append_message(
            conversation_id=conversation_id,
            role="assistant",
            content="旧答复引用 /home/alice/private.xlsx，路径不得进入摘要。" + "乙" * 80,
        ),
        session.append_message(
            conversation_id=conversation_id,
            role="assistant",
            content="我先调用工具。",
            tool_calls=[{"id": "call-1", "type": "function"}],
        ),
        session.append_message(
            conversation_id=conversation_id,
            role="tool",
            content='{"secret":"tool-result-must-not-enter-summary"}',
        ),
        session.append_message(
            conversation_id=conversation_id,
            role="user",
            content="第三条普通历史。" + "丙" * 80,
        ),
        session.append_message(
            conversation_id=conversation_id,
            role="assistant",
            content="最近答复必须保留原文。" + "丁" * 80,
        ),
        session.append_message(
            conversation_id=conversation_id,
            role="user",
            content="当前问题必须保留原文。" + "戊" * 80,
        ),
    ]
    return [message.id for message in messages]


def test_compaction_is_bounded_redacted_idempotent_and_versioned(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id = _workspace(tmp_path)
    message_ids = _seed_long_history(session, conversation_id)
    events: list[AuditEvent] = []
    compactions = CompactionStore(session, audit_recorder=events.append)

    created = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
        per_message_max_chars=160,
    )

    assert created.outcome == "created"
    assert created.view is not None
    first = created.view
    assert first.record.version == 1
    assert first.record.trigger_chars == 100
    assert first.record.keep_recent == 2
    assert first.record.summary_max_chars == 800
    assert first.record.per_message_max_chars == 160
    assert first.covered_message_ids == (
        message_ids[0],
        message_ids[1],
        message_ids[4],
    )
    assert first.record.source_message_count == 3
    assert first.record.redaction_count == 2
    assert len(first.record.summary_text) <= 800
    assert "top-secret-value" not in first.record.summary_text
    assert "/home/alice" not in first.record.summary_text
    assert "tool-result-must-not-enter-summary" not in first.record.summary_text
    assert "我先调用工具" not in first.record.summary_text
    assert "[REDACTED]" in first.record.summary_text
    assert "[REDACTED_PATH]" in first.record.summary_text
    assert "不能作为 Evidence" in first.record.summary_text

    replayed = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
        per_message_max_chars=160,
    )
    assert replayed.outcome == "replayed"
    assert replayed.view == first

    session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="新增最终答复。" + "己" * 80,
    )
    revised = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
        per_message_max_chars=160,
    )
    assert revised.outcome == "created"
    assert revised.view is not None
    assert revised.view.record.version == 2
    assert revised.view.record.supersedes_id == first.record.compaction_id
    assert revised.view.record.source_hash != first.record.source_hash

    reopened = CompactionStore(SessionStore(str(session.db_path)))
    restored = reopened.get_view(
        first.record.compaction_id,
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    assert restored == first
    assert [(event.action, event.outcome) for event in events] == [
        ("conversation.compact", "allowed"),
        ("conversation.compact", "allowed"),
        ("conversation.compact", "allowed"),
    ]
    assert all("top-secret-value" not in str(event.to_dict()) for event in events)


def test_compaction_integrity_drift_fails_closed(tmp_path: Path) -> None:
    session, project_id, conversation_id = _workspace(tmp_path)
    _seed_long_history(session, conversation_id)
    compactions = CompactionStore(session)
    result = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
    )
    assert result.view is not None
    compaction_id = result.view.record.compaction_id

    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            UPDATE conversation_compactions
            SET summary_text = summary_text || 'tampered'
            WHERE compaction_id = ?
            """,
            (compaction_id,),
        )

    with pytest.raises(RuntimeError, match="摘要完整性"):
        compactions.get_view(
            compaction_id,
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OWNER,
        )


def test_concurrent_compaction_retries_create_one_immutable_version(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id = _workspace(tmp_path)
    _seed_long_history(session, conversation_id)
    workers = 8
    barrier = threading.Barrier(workers)

    def compact(_: int) -> CompactionResult:
        barrier.wait()
        return CompactionStore(
            session,
            audit_recorder=lambda _event: None,
        ).compact_if_needed(
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OWNER,
            trigger_chars=100,
            keep_recent=2,
            summary_max_chars=800,
        )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(compact, range(workers)))

    assert [result.outcome for result in results].count("created") == 1
    assert [result.outcome for result in results].count("replayed") == workers - 1
    views = [result.view for result in results]
    assert all(view is not None for view in views)
    assert len({view.record.compaction_id for view in views if view is not None}) == 1
    with sqlite3.connect(session.db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM conversation_compactions").fetchone()
    assert count is not None and count[0] == 1


def test_compaction_respects_trigger_project_tenant_and_write_role(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id = _workspace(tmp_path)
    session.append_message(
        conversation_id=conversation_id,
        role="user",
        content="短对话",
    )
    compactions = CompactionStore(session)
    not_needed = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=1,
        summary_max_chars=256,
    )
    assert not_needed.outcome == "not_needed"
    assert not_needed.view is None

    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            INSERT INTO project_memberships(
                project_id, user_id, tenant_id, role, created_at
            ) VALUES (?, ?, ?, 'viewer', '2026-01-01T00:00:00Z')
            """,
            (project_id, _VIEWER.user_id, _VIEWER.tenant_scope),
        )
    with pytest.raises(CompactionAccessDenied):
        compactions.compact_if_needed(
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_VIEWER,
            trigger_chars=100,
            keep_recent=1,
            summary_max_chars=256,
        )
    with pytest.raises(CompactionAccessDenied):
        compactions.compact_if_needed(
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OTHER_TENANT,
            trigger_chars=100,
            keep_recent=1,
            summary_max_chars=256,
        )
    with pytest.raises(CompactionAccessDenied):
        compactions.get_latest(
            project_id="other-project",
            conversation_id=conversation_id,
            principal=_OWNER,
        )


def test_task_run_memory_snapshot_freezes_compaction_version(tmp_path: Path) -> None:
    session, project_id, conversation_id = _workspace(tmp_path)
    _seed_long_history(session, conversation_id)
    compactions = CompactionStore(session)
    first_result = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
    )
    assert first_result.view is not None
    first = first_result.view
    user_message = session.append_message(
        conversation_id=conversation_id,
        role="user",
        content="创建绑定压缩快照的任务",
    )
    contract = build_minimal_contract(
        run_id="compaction-bound-run",
        user_text=user_message.content,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = TaskStore(session.db_path).create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 1},
    )
    memory_store = MemoryStore(session)
    snapshot, _ = memory_store.create_snapshot(
        project_id=project_id,
        conversation_id=conversation_id,
        run_id=run.run_id,
        principal=_OWNER,
        compaction_id=first.record.compaction_id,
    )
    assert snapshot.compaction_id == first.record.compaction_id

    session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="推动滚动摘要产生新版本。" + "庚" * 100,
    )
    next_result = compactions.compact_if_needed(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
    )
    assert next_result.view is not None
    assert next_result.view.record.compaction_id != first.record.compaction_id

    replayed, _ = memory_store.create_snapshot(
        project_id=project_id,
        conversation_id=conversation_id,
        run_id=run.run_id,
        principal=_OWNER,
        compaction_id=next_result.view.record.compaction_id,
    )
    assert replayed.memory_snapshot_id == snapshot.memory_snapshot_id
    assert replayed.compaction_id == first.record.compaction_id
    assert replayed.content_hash == snapshot.content_hash

    other_project = session.create_project(
        "其他项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    other_conversation = session.create_conversation(other_project.id)
    other_message = session.append_message(
        conversation_id=other_conversation.id,
        role="user",
        content="其他项目任务",
    )
    other_contract = build_minimal_contract(
        run_id="cross-compaction-run",
        user_text=other_message.content,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    other_run, _ = TaskStore(session.db_path).create_run(
        project_id=other_project.id,
        conversation_id=other_conversation.id,
        user_message_id=other_message.id,
        contract=other_contract,
        budget={"max_tool_calls": 1},
    )
    with pytest.raises(MemoryAccessDenied):
        memory_store.create_snapshot(
            project_id=other_project.id,
            conversation_id=other_conversation.id,
            run_id=other_run.run_id,
            principal=_OWNER,
            compaction_id=first.record.compaction_id,
        )


def test_v4_database_is_backed_up_and_migrated_to_current(tmp_path: Path) -> None:
    db_path = tmp_path / "v4.db"
    SessionStore(str(db_path))
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for trigger in v10.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v10.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v10.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (v10.VERSION,))
        for trigger in v9.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v9.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v9.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v9.VERSION,),
        )
        for trigger in v8.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v8.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v8.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v8.VERSION,),
        )
        for trigger in v7.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v7.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v7.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v7.VERSION,),
        )
        for trigger in v6.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v6.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v6.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute("ALTER TABLE datasets DROP COLUMN lineage_parent_ref")
        connection.execute("ALTER TABLE artifacts DROP COLUMN lineage_dataset_ref")
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v6.VERSION,),
        )
        connection.execute('DROP TABLE "conversation_compaction_items"')
        connection.execute('DROP INDEX "idx_memory_snapshots_compaction"')
        connection.execute("ALTER TABLE memory_snapshots DROP COLUMN compaction_id")
        connection.execute('DROP TABLE "conversation_compactions"')
        for index in v5.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v5.VERSION,),
        )
        connection.execute("PRAGMA user_version = 4")

    migrated = SessionStore(str(db_path))

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    backups = list(tmp_path.glob("v4.db.v4-backup.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(db_path) as connection:
        migration = connection.execute(
            """
            SELECT checksum, source_version, backup_path, source_sha256
            FROM schema_migrations WHERE version = ?
            """,
            (v5.VERSION,),
        ).fetchone()
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory_snapshots)")}
        lineage_migration = connection.execute(
            """
            SELECT checksum, source_version
            FROM schema_migrations WHERE version = ?
            """,
            (v6.VERSION,),
        ).fetchone()
    assert migration is not None
    assert migration[0] == v5.CHECKSUM
    assert migration[1] == 4
    assert migration[2] == str(backups[0])
    assert migration[3] == hashlib.sha256(backups[0].read_bytes()).hexdigest()
    assert "compaction_id" in columns
    assert lineage_migration == (v6.CHECKSUM, v5.VERSION)
