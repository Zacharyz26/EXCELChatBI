"""v2.5 阶段 3C-2 受治理 Memory 实体/字段映射测试。"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from packages.governance.audit import AuditEvent
from packages.governance.permissions import Principal
from packages.session.memory_models import MemoryDraft, MemoryRecord
from packages.session.memory_refs import (
    MemoryReferenceAccessDenied,
    MemoryReferenceResolver,
    find_memory_reference_assumptions,
    memory_reference_semantic_key,
    memory_reference_summary,
)
from packages.session.memory_store import MemoryStore
from packages.session.store import SessionStore

_OWNER = Principal(user_id="owner", tenant_id="tenant-a")
_OTHER = Principal(user_id="owner", tenant_id="tenant-b")


def _workspace(
    tmp_path: Path,
) -> tuple[SessionStore, MemoryStore, str, str, list[str], str]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project(
        "Memory reference",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    dataset_refs = ["1" * 32, "2" * 32]
    for index, dataset_ref in enumerate(dataset_refs, 1):
        session.register_dataset(
            ref=dataset_ref,
            project_id=project.id,
            filename=f"批次-{index}.xlsx",
            profile={"row_count": index},
        )
    message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已生成图表。",
    )
    artifact = session.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="chart",
        payload={"chart_type": "line"},
        source_tool="gen_chart",
        params={"analysis_id": "a" * 12},
        dataset_ref=dataset_refs[0],
    )
    return (
        session,
        MemoryStore(session, audit_recorder=lambda _event: None),
        project.id,
        conversation.id,
        dataset_refs,
        artifact.id,
    )


def _remember_mapping(
    session: SessionStore,
    memories: MemoryStore,
    *,
    project_id: str,
    conversation_id: str,
    alias: str,
    kind: str = "entity_mapping",
    target_type: str = "dataset",
    target_ref: str,
    scope: str = "project",
    confidence: float = 0.95,
    valid_from: str | None = None,
    expires_at: str | None = None,
    canonical_field: str | None = None,
    key_suffix: str,
) -> MemoryRecord:
    source = session.append_message(
        conversation_id=conversation_id,
        role="user",
        content=f"确认映射 {alias} {key_suffix}",
    )
    typed_kind = kind  # keep fixture call sites compact
    result = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=MemoryDraft(
            scope=scope,  # type: ignore[arg-type]
            kind=typed_kind,  # type: ignore[arg-type]
            semantic_key=memory_reference_semantic_key(
                kind=typed_kind,  # type: ignore[arg-type]
                alias=alias,
            ),
            content_summary=memory_reference_summary(
                kind=typed_kind,  # type: ignore[arg-type]
                alias=alias,
                canonical_field=canonical_field,
            ),
            source_type="user_confirmation",
            source_ref=source.id,
            source_hash=hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
            confidence=confidence,
            conversation_id=(conversation_id if scope == "conversation" else None),
            valid_from=valid_from,
            expires_at=expires_at,
        ),
        idempotency_key=f"mapping-{key_suffix}",
    )
    memories.add_link(
        result.record.memory_id,
        project_id=project_id,
        principal=_OWNER,
        target_type=target_type,  # type: ignore[arg-type]
        target_ref=target_ref,
    )
    return result.record


def _snapshot(
    memories: MemoryStore,
    *,
    project_id: str,
    conversation_id: str,
) -> str:
    snapshot, _ = memories.create_snapshot(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    return snapshot.memory_snapshot_id


def test_entity_mapping_resolves_and_restores_compact_proof(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    record = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="主数据",
        target_ref=refs[1],
        key_suffix="entity",
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    resolver = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    )

    result = resolver.resolve(
        "继续分析主数据",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    assert result.bindings[0].memory_id == record.memory_id
    assert result.targets[0].reference_id == refs[1]
    assert "memory-reference-v1" in result.rewritten_query
    assumptions = result.assumptions()
    assert len(assumptions) == 1
    assert len(assumptions[0]) <= 300
    assert find_memory_reference_assumptions(list(assumptions)) == assumptions

    restored = resolver.restore(
        assumptions,
        query="恢复任务",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assert restored.targets[0].reference_id == refs[1]
    assert restored.bindings[0].binding_hash == result.bindings[0].binding_hash


def test_field_alias_requires_structured_summary_and_dataset_link(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="请求 ID",
        kind="field_alias",
        canonical_field="工单编号",
        target_ref=refs[0],
        key_suffix="field",
    )
    plain_source = session.append_message(
        conversation_id=conversation_id,
        role="user",
        content="普通人类摘要",
    )
    memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=MemoryDraft(
            scope="project",
            kind="field_alias",
            semantic_key="field-alias.legacy",
            content_summary="旧名称是新名称",
            source_type="user_confirmation",
            source_ref=plain_source.id,
            source_hash=hashlib.sha256(plain_source.content.encode("utf-8")).hexdigest(),
            confidence=0.99,
        ),
        idempotency_key="plain-human-summary",
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )

    result = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    ).resolve(
        "按请求 ID 去重",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    assert result.bindings[0].canonical_field == "工单编号"
    assert result.targets[0].reference_id == refs[0]
    assert '"canonical_field":"工单编号"' in result.rewritten_query


def test_scoped_ambiguity_blocks_until_explicit_memory_id(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    project_record = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="当前批次",
        target_ref=refs[0],
        scope="project",
        key_suffix="project",
    )
    conversation_record = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="当前批次",
        target_ref=refs[1],
        scope="conversation",
        key_suffix="conversation",
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    resolver = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    )

    ambiguous = resolver.resolve(
        "分析当前批次",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    selected = resolver.resolve(
        f"分析当前批次\n用户澄清：memory_id={conversation_record.memory_id}",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )

    assert ambiguous.status == "ambiguous"
    assert {choice.memory_id for choice in ambiguous.choices} == {
        project_record.memory_id,
        conversation_record.memory_id,
    }
    assert selected.status == "resolved"
    assert selected.targets[0].reference_id == refs[1]


@pytest.mark.parametrize(
    ("mode", "reason_code"),
    [
        ("low", "memory_reference_low_confidence"),
        ("expired", "memory_reference_expired"),
        ("deleted", "memory_reference_deleted"),
    ],
)
def test_ineligible_mapping_fails_closed(
    tmp_path: Path,
    mode: str,
    reason_code: str,
) -> None:
    session, memories, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    kwargs: dict[str, object] = {}
    if mode == "low":
        kwargs["confidence"] = 0.4
    if mode == "expired":
        kwargs.update(
            {
                "valid_from": "2025-01-01T00:00:00Z",
                "expires_at": "2025-02-01T00:00:00Z",
            }
        )
    record = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="历史批次",
        target_ref=refs[0],
        key_suffix=mode,
        **kwargs,  # type: ignore[arg-type]
    )
    if mode == "deleted":
        memories.soft_delete(
            record.memory_id,
            project_id=project_id,
            principal=_OWNER,
            expected_version=record.version,
            idempotency_key="delete-mapping",
        )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )

    result = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    ).resolve(
        "分析历史批次",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )

    assert result.status == "unresolved"
    assert result.reason_code == reason_code
    assert result.targets == ()


def test_conflict_and_missing_target_fail_closed(tmp_path: Path) -> None:
    session, memories, project_id, conversation_id, refs, artifact_id = _workspace(tmp_path)
    active = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="确认图",
        kind="confirmed_decision",
        target_type="artifact",
        target_ref=artifact_id,
        key_suffix="active",
    )
    conflict_source = session.append_message(
        conversation_id=conversation_id,
        role="user",
        content="再次确认但来源不同",
    )
    conflict = memories.remember(
        project_id=project_id,
        principal=_OWNER,
        draft=MemoryDraft(
            scope="project",
            kind="confirmed_decision",
            semantic_key=active.semantic_key,
            content_summary=active.content_summary,
            source_type="user_confirmation",
            source_ref=conflict_source.id,
            source_hash=hashlib.sha256(conflict_source.content.encode("utf-8")).hexdigest(),
            confidence=0.95,
        ),
        idempotency_key="conflicting-mapping",
    ).record
    memories.add_link(
        conflict.memory_id,
        project_id=project_id,
        principal=_OWNER,
        target_type="dataset",
        target_ref=refs[1],
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    resolver = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    )

    conflicted = resolver.resolve(
        "使用确认图",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assert conflicted.status == "ambiguous"
    assert conflicted.reason_code == "memory_reference_conflict"

    selected = resolver.resolve(
        f"使用确认图\n用户澄清：memory_id={active.memory_id}",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assert selected.status == "resolved"
    assert selected.bindings[0].conflict_override is True
    restored = resolver.restore(
        selected.assumptions(),
        query="恢复已确认的一次性选择",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assert restored.targets[0].reference_id == artifact_id

    memories.soft_delete(
        conflict.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=conflict.version,
        idempotency_key="delete-conflict",
    )
    assert session.delete_artifact(artifact_id) is True
    missing = resolver.resolve(
        "使用确认图",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assert missing.status == "unresolved"
    assert missing.reason_code == "memory_reference_target_missing"


def test_restore_rejects_deleted_memory_and_cross_tenant_snapshot(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    record = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="固定数据",
        target_ref=refs[0],
        key_suffix="restore",
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    resolver = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    )
    result = resolver.resolve(
        "使用固定数据",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assumptions = result.assumptions()

    memories.soft_delete(
        record.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=record.version,
        idempotency_key="delete-before-restore",
    )
    with pytest.raises(MemoryReferenceAccessDenied):
        resolver.restore(
            assumptions,
            query="恢复",
            project_id=project_id,
            conversation_id=conversation_id,
            memory_snapshot_id=snapshot_id,
            principal=_OWNER,
        )
    with pytest.raises(MemoryReferenceAccessDenied):
        resolver.resolve(
            "使用固定数据",
            project_id=project_id,
            conversation_id=conversation_id,
            memory_snapshot_id=snapshot_id,
            principal=_OTHER,
        )


def test_fixed_snapshot_does_not_follow_superseding_mapping(
    tmp_path: Path,
) -> None:
    session, memories, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    original = _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="固定版本",
        target_ref=refs[0],
        key_suffix="superseded-original",
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )
    resolver = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=lambda _event: None,
    )
    resolved = resolver.resolve(
        "使用固定版本",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )

    source = session.append_message(
        conversation_id=conversation_id,
        role="user",
        content="确认固定版本改为第二个数据集",
    )
    revised = memories.revise(
        original.memory_id,
        project_id=project_id,
        principal=_OWNER,
        expected_version=original.version,
        draft=MemoryDraft(
            scope="project",
            kind="entity_mapping",
            semantic_key=original.semantic_key,
            content_summary=original.content_summary,
            source_type="user_confirmation",
            source_ref=source.id,
            source_hash=hashlib.sha256(source.content.encode("utf-8")).hexdigest(),
            confidence=0.95,
        ),
        idempotency_key="supersede-fixed-mapping",
    ).record
    memories.add_link(
        revised.memory_id,
        project_id=project_id,
        principal=_OWNER,
        target_type="dataset",
        target_ref=refs[1],
    )

    after_revision = resolver.resolve(
        "使用固定版本",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )
    assert after_revision.status == "unresolved"
    assert after_revision.reason_code == "memory_reference_superseded"
    with pytest.raises(MemoryReferenceAccessDenied):
        resolver.restore(
            resolved.assumptions(),
            query="恢复固定版本",
            project_id=project_id,
            conversation_id=conversation_id,
            memory_snapshot_id=snapshot_id,
            principal=_OWNER,
        )


def test_audit_does_not_include_query_or_memory_summary(tmp_path: Path) -> None:
    session, _, project_id, conversation_id, refs, _ = _workspace(tmp_path)
    events: list[AuditEvent] = []
    memories = MemoryStore(session, audit_recorder=lambda _event: None)
    _remember_mapping(
        session,
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
        alias="机密别名",
        target_ref=refs[0],
        key_suffix="audit",
    )
    snapshot_id = _snapshot(
        memories,
        project_id=project_id,
        conversation_id=conversation_id,
    )

    result = MemoryReferenceResolver(
        session,
        memories,
        audit_recorder=events.append,
    ).resolve(
        "使用机密别名 password=must-not-leak",
        project_id=project_id,
        conversation_id=conversation_id,
        memory_snapshot_id=snapshot_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    serialized = str(events[0].to_dict())
    assert "must-not-leak" not in serialized
    assert "机密别名" not in serialized
