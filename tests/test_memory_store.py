"""v2.5 阶段 3A 记忆 Repository、Policy 和隔离边界测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.governance.audit import AuditEvent
from packages.governance.permissions import Principal
from packages.session.memory_models import MemoryDraft
from packages.session.memory_policy import MemoryPolicyViolation
from packages.session.memory_store import (
    MemoryAccessDenied,
    MemoryIdempotencyConflict,
    MemoryStore,
    MemoryVersionConflict,
)
from packages.session.migrations import v4
from packages.session.store import SessionStore
from packages.session.task_models import ClaimDraft
from packages.session.task_store import TaskStore

_OWNER = Principal(user_id="alice", tenant_id="tenant-a")
_VIEWER = Principal(user_id="bob", tenant_id="tenant-a")
_OTHER_TENANT = Principal(user_id="alice", tenant_id="tenant-b")


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workspace(
    tmp_path: Path,
) -> tuple[SessionStore, MemoryStore, str, str, str, str]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project(
        "Memory 项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    message = session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="以后将客户编号称为客户 ID",
    )
    return (
        session,
        MemoryStore(session),
        project.id,
        conversation.id,
        message.id,
        message.content,
    )


def _draft(
    *,
    source_ref: str,
    source_content: str,
    scope: str = "project",
    conversation_id: str | None = None,
    semantic_key: str = "field-alias.customer-id",
    content_summary: str = "客户编号的展示名称是客户 ID",
    valid_from: str | None = None,
    expires_at: str | None = None,
) -> MemoryDraft:
    return MemoryDraft(
        scope=scope,  # type: ignore[arg-type]
        kind="field_alias",
        semantic_key=semantic_key,
        content_summary=content_summary,
        source_type="user_confirmation",
        source_ref=source_ref,
        source_hash=_sha256(source_content),
        confidence=0.95,
        conversation_id=conversation_id,
        valid_from=valid_from,
        expires_at=expires_at,
    )


def _add_member(
    session: SessionStore,
    *,
    project_id: str,
    principal: Principal,
    role: str,
) -> None:
    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            INSERT INTO project_memberships(
                project_id, user_id, tenant_id, role, created_at
            ) VALUES (?, ?, ?, ?, '2026-01-01T00:00:00.000000Z')
            """,
            (project_id, principal.user_id, principal.tenant_scope, role),
        )


def test_remember_is_idempotent_and_reuses_same_semantic_content(
    tmp_path: Path,
) -> None:
    _, memories, project_id, _, message_id, content = _workspace(tmp_path)
    draft = _draft(source_ref=message_id, source_content=content)

    created = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=draft,
        idempotency_key="remember-1",
    )
    replayed = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=draft,
        idempotency_key="remember-1",
    )
    reused = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=draft,
        idempotency_key="remember-2",
    )

    assert created.outcome == "created"
    assert replayed.outcome == "replayed"
    assert reused.outcome == "reused"
    assert created.record.memory_id == replayed.record.memory_id
    assert created.record.memory_id == reused.record.memory_id
    assert created.record.version == 1

    changed = replace(draft, content_summary="客户编号的展示名称是客户标识")
    with pytest.raises(MemoryIdempotencyConflict):
        memories.remember(
            project_id=project_id,
            principal=_OWNER,
            draft=changed,
            idempotency_key="remember-1",
        )


def test_conflict_is_excluded_until_explicit_revision(tmp_path: Path) -> None:
    _, memories, project_id, _, message_id, content = _workspace(tmp_path)
    original = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(source_ref=message_id, source_content=content),
        idempotency_key="original",
    ).record
    conflicting = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            content_summary="客户编号的展示名称是客户代码",
        ),
        idempotency_key="conflict",
    )

    assert conflicting.outcome == "conflict"
    assert conflicting.record.status == "conflict"
    assert [item.memory_id for item in memories.list_records(
        project_id=project_id,
        principal=_OWNER,
    )] == [original.memory_id]
    assert len(
        memories.list_records(
            project_id=project_id,
            principal=_OWNER,
            include_conflicts=True,
        )
    ) == 2

    revised = memories.revise(
        original.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=original.version,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            content_summary="客户编号统一显示为客户标识",
        ),
        idempotency_key="revision",
    )

    assert revised.outcome == "revised"
    assert revised.record.version == 3
    assert revised.record.supersedes_id == original.memory_id
    selected = memories.list_records(project_id=project_id, principal=_OWNER)
    assert [item.memory_id for item in selected] == [revised.record.memory_id]
    with pytest.raises(MemoryVersionConflict):
        memories.revise(
            original.memory_id,
            project_id=project_id,
            principal=_OWNER,
            expected_version=original.version,
            draft=_draft(source_ref=message_id, source_content=content),
            idempotency_key="stale-revision",
        )


def test_memory_governance_audits_writes_conflicts_snapshots_and_refusals(
    tmp_path: Path,
) -> None:
    session, _, project_id, conversation_id, message_id, content = _workspace(
        tmp_path
    )
    events: list[AuditEvent] = []
    memories = MemoryStore(session, audit_recorder=events.append)
    original = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(source_ref=message_id, source_content=content),
        idempotency_key="audit-create",
    ).record
    memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            content_summary="客户编号统一显示为客户代码",
        ),
        idempotency_key="audit-conflict",
    )
    memories.create_snapshot(
        project_id=project_id,
        principal=_OWNER,
        conversation_id=conversation_id,
    )
    with pytest.raises(MemoryPolicyViolation):
        memories.remember(
            project_id=project_id,
            principal=_OWNER,
            draft=_draft(
                source_ref=message_id,
                source_content=content,
                content_summary="Authorization: Bearer top-secret-token",
            ),
            idempotency_key="audit-denied",
        )
    memories.soft_delete(
        original.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=original.version,
        idempotency_key="audit-delete",
    )

    assert [(event.action, event.outcome) for event in events] == [
        ("memory.remember", "allowed"),
        ("memory.conflict", "allowed"),
        ("memory.snapshot", "allowed"),
        ("memory.remember", "denied"),
        ("memory.delete", "allowed"),
    ]
    assert events[3].detail == {"reason_code": "MemoryPolicyViolation"}
    assert all("top-secret-token" not in str(event.to_dict()) for event in events)


def test_project_subject_and_conversation_scopes_are_isolated(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, message_id, content = _workspace(
        tmp_path
    )
    _add_member(
        session,
        project_id=project_id,
        principal=_VIEWER,
        role="viewer",
    )
    project_memory = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(source_ref=message_id, source_content=content),
        idempotency_key="project",
    ).record
    subject_memory = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            scope="subject",
            semantic_key="preference.customer-id",
        ),
        idempotency_key="subject",
    ).record
    conversation_memory = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            scope="conversation",
            conversation_id=conversation_id,
            semantic_key="conversation.customer-id",
        ),
        idempotency_key="conversation",
    ).record

    owner_ids = {
        item.memory_id
        for item in memories.list_records(
            project_id=project_id,
            principal=_OWNER,
            conversation_id=conversation_id,
        )
    }
    viewer_ids = {
        item.memory_id
        for item in memories.list_records(
            project_id=project_id,
            principal=_VIEWER,
            conversation_id=conversation_id,
        )
    }
    assert owner_ids == {
        project_memory.memory_id,
        subject_memory.memory_id,
        conversation_memory.memory_id,
    }
    assert viewer_ids == {project_memory.memory_id, conversation_memory.memory_id}
    assert memories.get_record(subject_memory.memory_id, principal=_VIEWER) is None

    with pytest.raises(MemoryAccessDenied):
        memories.remember(
            project_id=project_id,
            principal=_VIEWER,
            draft=_draft(source_ref=message_id, source_content=content),
            idempotency_key="viewer-write",
        )
    with pytest.raises(MemoryAccessDenied):
        memories.list_records(
            project_id=project_id,
            principal=_OTHER_TENANT,
        )


def test_cross_project_sources_and_links_are_rejected(tmp_path: Path) -> None:
    session, memories, project_id, _, message_id, content = _workspace(tmp_path)
    other_project = session.create_project(
        "另一个项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    other_conversation = session.create_conversation(other_project.id)
    other_message = session.append_message(
        conversation_id=other_conversation.id,
        role="user",
        content="另一个项目的口径",
    )
    with pytest.raises(ValueError, match="跨项目"):
        memories.remember(
            project_id=project_id,
            principal=_OWNER,
            draft=_draft(
                source_ref=other_message.id,
                source_content=other_message.content,
            ),
            idempotency_key="cross-project-source",
        )

    record = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(source_ref=message_id, source_content=content),
        idempotency_key="link-source",
    ).record
    dataset = session.register_dataset(
        ref="a" * 64,
        project_id=project_id,
        filename="customers.xlsx",
        profile={},
    )
    link = memories.add_link(
        record.memory_id,
        project_id=project_id,
        principal=_OWNER,
        target_type="dataset",
        target_ref=dataset.ref,
    )
    replayed = memories.add_link(
        record.memory_id,
        project_id=project_id,
        principal=_OWNER,
        target_type="dataset",
        target_ref=dataset.ref,
    )
    assert replayed.link_id == link.link_id
    with pytest.raises(ValueError, match="跨项目"):
        memories.add_link(
            record.memory_id,
            project_id=project_id,
            principal=_OWNER,
            target_type="conversation",
            target_ref=other_conversation.id,
        )


def test_expiry_soft_delete_and_snapshots_have_stable_contents(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, message_id, content = _workspace(
        tmp_path
    )
    active = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(source_ref=message_id, source_content=content),
        idempotency_key="active",
    ).record
    memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            semantic_key="expired",
            valid_from="2025-01-01T00:00:00Z",
            expires_at="2025-02-01T00:00:00Z",
        ),
        idempotency_key="expired",
    )
    memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(
            source_ref=message_id,
            source_content=content,
            semantic_key="future",
            valid_from="2099-01-01T00:00:00Z",
        ),
        idempotency_key="future",
    )
    memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=replace(
            _draft(
                source_ref=message_id,
                source_content=content,
                semantic_key="low-confidence",
            ),
            confidence=0.69,
        ),
        idempotency_key="low-confidence",
    )

    contract = build_minimal_contract(
        run_id="memory-snapshot-run",
        user_text=content,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = TaskStore(session.db_path).create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    snapshot, records = memories.create_snapshot(
        project_id=project_id,
        principal=_OWNER,
        run_id=run.run_id,
    )
    assert [item.memory_id for item in records] == [active.memory_id]
    assert snapshot.record_count == 1
    with pytest.raises(ValueError, match="Evidence"):
        TaskStore(session.db_path).replace_claims(
            run.run_id,
            [
                ClaimDraft(
                    statement="记忆不能替代 Evidence",
                    claim_kind="numeric",
                    value_refs=(),
                    evidence_ids=(active.memory_id,),
                )
            ],
        )

    deleted = memories.soft_delete(
        active.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=active.version,
        idempotency_key="delete-active",
    )
    replayed_delete = memories.soft_delete(
        active.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=active.version,
        idempotency_key="delete-active",
    )
    assert deleted.status == "deleted"
    assert replayed_delete == deleted
    replayed_snapshot, replayed_records = memories.create_snapshot(
        project_id=project_id,
        principal=_OWNER,
        run_id=run.run_id,
    )
    assert replayed_snapshot == snapshot
    assert replayed_records == records
    reopened = MemoryStore(SessionStore(str(session.db_path))).get_snapshot(
        snapshot.memory_snapshot_id,
        principal=_OWNER,
    )
    assert reopened == (snapshot, records)

    new_snapshot, new_records = memories.create_snapshot(
        project_id=project_id,
        principal=_OWNER,
        conversation_id=conversation_id,
    )
    assert new_snapshot.content_hash != snapshot.content_hash
    assert new_records == ()


def test_memory_policy_rejects_secrets_host_paths_and_invalid_source_hash(
    tmp_path: Path,
) -> None:
    _, memories, project_id, _, message_id, content = _workspace(tmp_path)
    for summary in (
        r"导出到 C:\Users\alice\secret.xlsx",
        "Authorization: Bearer top-secret-token",
    ):
        with pytest.raises(MemoryPolicyViolation):
            memories.remember(
                project_id=project_id,
                principal=_OWNER,
                draft=_draft(
                    source_ref=message_id,
                    source_content=content,
                    content_summary=summary,
                ),
                idempotency_key=_sha256(summary),
            )
    with pytest.raises(MemoryPolicyViolation, match="source_hash"):
        memories.remember(
            project_id=project_id,
            principal=_OWNER,
            draft=replace(
                _draft(source_ref=message_id, source_content=content),
                source_hash="not-a-hash",
            ),
            idempotency_key="bad-hash",
        )
    with pytest.raises(ValueError, match="hash 不匹配"):
        memories.remember(
            project_id=project_id,
            principal=_OWNER,
            draft=replace(
                _draft(source_ref=message_id, source_content=content),
                source_hash="f" * 64,
            ),
            idempotency_key="wrong-source-hash",
        )


def test_v3_database_is_backed_up_and_migrated_to_v4(tmp_path: Path) -> None:
    db_path = tmp_path / "v3.db"
    SessionStore(str(db_path))
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for table in v4.ADDED_TABLES:
            connection.execute(f'DROP TABLE "{table}"')
        for index in v4.ADDED_INDEXES_ON_LEGACY_TABLES:
            connection.execute(f'DROP INDEX "{index}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v4.VERSION,),
        )
        connection.execute("PRAGMA user_version = 3")

    store = SessionStore(str(db_path))

    assert store.schema_version == 4
    backups = list(tmp_path.glob("v3.db.v3-backup.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(db_path) as connection:
        migration = connection.execute(
            """
            SELECT checksum, source_version, backup_path, source_sha256
            FROM schema_migrations WHERE version = 4
            """
        ).fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
    assert migration is not None
    assert migration[0] == v4.CHECKSUM
    assert migration[1] == 3
    assert migration[2] == str(backups[0])
    assert migration[3] == hashlib.sha256(backups[0].read_bytes()).hexdigest()
    assert set(v4.ADDED_TABLES).issubset(tables)


def test_project_delete_cascades_memory_control_plane(tmp_path: Path) -> None:
    session, memories, project_id, conversation_id, message_id, content = _workspace(
        tmp_path
    )
    record = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=_draft(source_ref=message_id, source_content=content),
        idempotency_key="delete-project-memory",
    ).record
    memories.create_snapshot(
        project_id=project_id,
        principal=_OWNER,
        conversation_id=conversation_id,
    )
    memories.add_link(
        record.memory_id,
        project_id=project_id,
        principal=_OWNER,
        target_type="conversation",
        target_ref=conversation_id,
    )

    assert session.delete_project(project_id)
    with sqlite3.connect(session.db_path) as connection:
        counts = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in v4.ADDED_TABLES
        }
    assert counts == {table: 0 for table in v4.ADDED_TABLES}
