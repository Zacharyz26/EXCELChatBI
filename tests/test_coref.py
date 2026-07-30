"""v2.5 阶段 3C-1 确定性指代消解、隔离与恢复契约测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.governance.audit import AuditEvent
from packages.governance.permissions import Principal
from packages.session.coref import (
    ReferenceAccessDenied,
    ReferenceResolver,
)
from packages.session.models import Artifact
from packages.session.store import SessionStore

_OWNER = Principal(user_id="owner", tenant_id="tenant-a")
_OTHER = Principal(user_id="owner", tenant_id="tenant-b")


def _workspace(
    tmp_path: Path,
) -> tuple[SessionStore, str, str, list[Artifact], list[str]]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project(
        "引用项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    dataset_refs = ["1" * 32, "2" * 32]
    for index, dataset_ref in enumerate(dataset_refs, 1):
        session.register_dataset(
            ref=dataset_ref,
            project_id=project.id,
            filename=f"文档批次-{index}.xlsx",
            profile={"row_count": index},
        )
    message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已生成两个图表和一个趋势结果。",
    )
    artifacts = [
        session.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="chart",
            payload={"chart_type": "line"},
            source_tool="gen_chart",
            params={"analysis_id": "chart-ref-001"},
            dataset_ref=dataset_refs[0],
        ),
        session.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="stats",
            payload={"result": {"direction": "stable"}},
            source_tool="trend_analysis",
            params={"analysis_id": "trend-ref-01"},
            dataset_ref=dataset_refs[0],
        ),
        session.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="chart",
            payload={"chart_type": "bar"},
            source_tool="gen_chart",
            params={"analysis_id": "chart-ref-002"},
            dataset_ref=dataset_refs[1],
        ),
    ]
    return session, project.id, conversation.id, artifacts, dataset_refs


def test_resolves_exact_ordinal_and_restores_host_binding(tmp_path: Path) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)
    resolver = ReferenceResolver(session, audit_recorder=lambda _event: None)

    result = resolver.resolve(
        "把第二张图改成按周展示",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    assert [target.reference_id for target in result.targets] == [artifacts[2].id]
    assert result.targets[0].analysis_id == "chart-ref-002"
    assert result.targets[0].dataset_ref == "2" * 32
    assert "[Host 已验证引用 coref-v1]" in result.rewritten_query
    assert artifacts[2].id in result.rewritten_query
    assumption = result.assumption()
    assert assumption is not None
    assert len(assumption) <= 300

    restored = resolver.restore(
        assumption,
        query=result.original_query,
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    assert [target.reference_id for target in restored.targets] == [
        target.reference_id for target in result.targets
    ]
    assert restored.resolution_hash == result.resolution_hash
    assert "第二张图" not in assumption


def test_ordinal_out_of_range_does_not_fallback_to_latest(tmp_path: Path) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)

    result = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        "把第三张图导出",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "unresolved"
    assert result.reason_code == "reference_ordinal_out_of_range"
    assert result.targets == ()
    assert [choice.reference_id for choice in result.choices] == [
        artifacts[0].id,
        artifacts[2].id,
    ]
    clarification = result.clarification()
    assert clarification is not None
    assert clarification["blocking"] is True


def test_deictic_reference_is_ambiguous_but_recent_reference_is_deterministic(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)
    resolver = ReferenceResolver(session, audit_recorder=lambda _event: None)

    ambiguous = resolver.resolve(
        "把这个图放进报告",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    recent = resolver.resolve(
        "把刚才的这个图改成横向",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert ambiguous.status == "ambiguous"
    assert ambiguous.targets == ()
    assert len(ambiguous.choices) == 2
    assert recent.status == "resolved"
    assert [target.reference_id for target in recent.targets] == [artifacts[2].id]


def test_explicit_clarification_dominates_original_deictic_reference(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)

    result = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        f"把这个图放进报告\n\n用户澄清：analysis_id={artifacts[2].id}",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    assert [target.reference_id for target in result.targets] == [artifacts[2].id]


def test_resolves_recent_trend_and_chart_without_copying_payload(tmp_path: Path) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)

    result = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        "把刚才的趋势和图表生成报告",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    assert {target.reference_id for target in result.targets} == {
        artifacts[1].id,
        artifacts[2].id,
    }
    assert "stable" not in result.rewritten_query
    assert all(target.kind == "artifact" for target in result.targets)


def test_resolves_dataset_filename_ordinal_and_explicit_ref(tmp_path: Path) -> None:
    session, project_id, conversation_id, _, dataset_refs = _workspace(tmp_path)
    resolver = ReferenceResolver(session, audit_recorder=lambda _event: None)

    filename = resolver.resolve(
        "继续使用文档批次-1.xlsx",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    ordinal = resolver.resolve(
        "比较第二个数据集",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    explicit = resolver.resolve(
        f"继续使用 dataset_ref={dataset_refs[0]}",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert filename.targets[0].reference_id == dataset_refs[0]
    assert ordinal.targets[0].reference_id == dataset_refs[1]
    assert explicit.targets[0].reference_id == dataset_refs[0]


def test_cross_tenant_project_and_forged_reference_fail_closed(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id, _, _ = _workspace(tmp_path)
    other_project = session.create_project(
        "其他项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    other_conversation = session.create_conversation(other_project.id)
    other_ref = "f" * 32
    session.register_dataset(
        ref=other_ref,
        project_id=other_project.id,
        filename="其他.xlsx",
        profile={},
    )
    resolver = ReferenceResolver(session, audit_recorder=lambda _event: None)

    with pytest.raises(ReferenceAccessDenied):
        resolver.resolve(
            "这个数据集",
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OTHER,
        )
    with pytest.raises(ReferenceAccessDenied):
        resolver.resolve(
            "这个数据集",
            project_id=project_id,
            conversation_id=other_conversation.id,
            principal=_OWNER,
        )
    forged = resolver.resolve(
        f"继续使用 dataset_ref={other_ref}",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    assert forged.status == "unresolved"
    assert forged.targets == ()


def test_audit_contains_only_ids_hashes_and_counts(tmp_path: Path) -> None:
    session, project_id, conversation_id, _, _ = _workspace(tmp_path)
    events: list[AuditEvent] = []
    secret_query = "把第二张图改成按周，password=must-not-leak"

    result = ReferenceResolver(session, audit_recorder=events.append).resolve(
        secret_query,
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "resolved"
    assert len(events) == 1
    serialized = str(events[0].to_dict())
    assert "must-not-leak" not in serialized
    assert events[0].detail["resolution_hash"] == result.resolution_hash


def test_restore_rejects_tampered_or_deleted_target(tmp_path: Path) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)
    resolver = ReferenceResolver(session, audit_recorder=lambda _event: None)
    result = resolver.resolve(
        "第一张图",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    assumption = result.assumption()
    assert assumption is not None

    with pytest.raises(ReferenceAccessDenied, match="hash"):
        resolver.restore(
            assumption.replace(result.resolution_hash, "0" * 64),
            query=result.original_query,
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OWNER,
        )
    with pytest.raises(ReferenceAccessDenied, match="hash"):
        resolver.restore(
            assumption.replace(artifacts[0].id, artifacts[2].id),
            query=result.original_query,
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OWNER,
        )
    assert session.delete_artifact(artifacts[0].id) is True
    with pytest.raises(ReferenceAccessDenied, match="不存在"):
        resolver.restore(
            assumption,
            query=result.original_query,
            project_id=project_id,
            conversation_id=conversation_id,
            principal=_OWNER,
        )


def test_reference_binding_is_bounded_by_planner_assumption_contract(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id, artifacts, _ = _workspace(tmp_path)
    message = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="补充更多图表。",
    )
    for _ in range(4):
        artifacts.append(
            session.create_artifact(
                conversation_id=conversation_id,
                message_id=message.id,
                type="chart",
                payload={"chart_type": "line"},
                source_tool="gen_chart",
                params={},
                dataset_ref="1" * 32,
            )
        )
    explicit_ids = " ".join(artifact.id for artifact in artifacts if artifact.type == "chart")

    result = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        explicit_ids,
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "unresolved"
    assert result.reason_code == "reference_limit_exceeded"
    assert result.assumption() is None
    clarification = result.clarification()
    assert clarification is not None
    assert "最多可绑定 5 个" in str(clarification["question"])

    ambiguous = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        "把这个图放进报告",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )
    ambiguous_question = ambiguous.clarification()
    assert ambiguous_question is not None
    assert len(str(ambiguous_question["question"])) <= 500


def test_memory_id_is_not_misclassified_as_artifact_or_dataset(
    tmp_path: Path,
) -> None:
    session, project_id, conversation_id, _, _ = _workspace(tmp_path)

    result = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        f"用户澄清：memory_id={'f' * 32}",
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_OWNER,
    )

    assert result.status == "no_reference"
