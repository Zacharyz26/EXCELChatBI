"""v2.5 阶段 3E 血缘图、不可变锚点和隔离边界测试。"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.governance.audit import AuditEvent
from packages.governance.permissions import Principal
from packages.session.lineage import LineageAccessDenied, LineageStore
from packages.session.migrations import CURRENT_SCHEMA_VERSION, v6, v7, v8, v9, v10
from packages.session.models import ArtifactDraft
from packages.session.store import SessionStore
from packages.session.task_models import ClaimDraft
from packages.session.task_store import TaskStore

_OWNER = Principal(user_id="owner", tenant_id="tenant-a")
_VIEWER = Principal(user_id="viewer", tenant_id="tenant-a")
_OTHER_TENANT = Principal(user_id="owner", tenant_id="tenant-b")
_PARENT_REF = "a" * 32
_CHILD_REF = "b" * 32


def test_complete_lineage_graph_and_deleted_dataset_anchors(tmp_path: Path) -> None:
    session, project_id, conversation_id, _, _, artifact_id = _seed_lineage(tmp_path)
    events: list[AuditEvent] = []
    lineage = LineageStore(session, audit_recorder=events.append)

    graph = lineage.build_graph(
        project_id=project_id,
        principal=_OWNER,
    )

    nodes = {node.node_id: node for node in graph.nodes}
    assert graph.integrity_status == "ok"
    assert graph.truncated is False
    assert {
        "dataset",
        "analysis",
        "artifact",
        "evidence",
        "claim",
    }.issubset({node.node_type for node in graph.nodes})
    assert {edge.relation for edge in graph.edges} >= {
        "derived_from",
        "used_by",
        "produced",
        "substantiates",
        "supports",
    }
    artifact = nodes[f"artifact:{artifact_id}"]
    assert "file_ref" not in artifact.metadata
    assert "payload" not in artifact.metadata
    analysis = next(node for node in graph.nodes if node.node_type == "analysis")
    assert "args" not in analysis.metadata
    assert events[-1].action == "lineage.read"
    assert events[-1].outcome == "allowed"
    assert _CHILD_REF not in str(events[-1].to_dict())

    assert session.delete_dataset(_PARENT_REF) is True
    with sqlite3.connect(session.db_path) as connection:
        child_anchor = connection.execute(
            """
            SELECT parent_ref, lineage_parent_ref
            FROM datasets WHERE ref = ?
            """,
            (_CHILD_REF,),
        ).fetchone()
    assert child_anchor == (None, _PARENT_REF)

    assert session.delete_dataset(_CHILD_REF) is True
    with sqlite3.connect(session.db_path) as connection:
        artifact_anchor = connection.execute(
            """
            SELECT dataset_ref, lineage_dataset_ref
            FROM artifacts WHERE id = ?
            """,
            (artifact_id,),
        ).fetchone()
    assert artifact_anchor == (None, _CHILD_REF)

    restored_graph = lineage.build_graph(
        project_id=project_id,
        principal=_OWNER,
        conversation_id=conversation_id,
    )
    restored_nodes = {node.node_id: node for node in restored_graph.nodes}
    assert restored_nodes[f"dataset:{_PARENT_REF}"].status == "deleted"
    assert restored_nodes[f"dataset:{_CHILD_REF}"].status == "deleted"
    assert any(
        edge.source == f"dataset:{_PARENT_REF}"
        and edge.target == f"dataset:{_CHILD_REF}"
        and edge.relation == "derived_from"
        for edge in restored_graph.edges
    )


def test_lineage_scope_is_project_and_tenant_isolated(tmp_path: Path) -> None:
    session, project_id, conversation_id, *_ = _seed_lineage(tmp_path)
    unrelated_ref = "c" * 32
    session.register_dataset(
        ref=unrelated_ref,
        project_id=project_id,
        filename="unrelated.csv",
        profile={"row_count": 1},
    )
    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            INSERT INTO project_memberships(
                project_id, user_id, tenant_id, role, created_at
            ) VALUES (?, ?, ?, 'viewer', '2026-01-01T00:00:00Z')
            """,
            (project_id, _VIEWER.user_id, _VIEWER.tenant_scope),
        )
    lineage = LineageStore(session, audit_recorder=lambda _event: None)

    viewer = lineage.build_graph(
        project_id=project_id,
        conversation_id=conversation_id,
        principal=_VIEWER,
    )
    assert viewer.nodes
    assert unrelated_ref not in {node.resource_ref for node in viewer.nodes}
    with pytest.raises(LineageAccessDenied):
        lineage.build_graph(
            project_id=project_id,
            principal=_OTHER_TENANT,
        )

    other_project = session.create_project(
        "其他项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    other_conversation = session.create_conversation(other_project.id)
    with pytest.raises(LineageAccessDenied):
        lineage.build_graph(
            project_id=project_id,
            conversation_id=other_conversation.id,
            principal=_OWNER,
        )


def test_lineage_anchor_columns_reject_rewrite(tmp_path: Path) -> None:
    session, _, _, _, _, artifact_id = _seed_lineage(tmp_path)
    with sqlite3.connect(session.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE datasets SET lineage_parent_ref = ?
                WHERE ref = ?
                """,
                ("c" * 32, _CHILD_REF),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE artifacts SET lineage_dataset_ref = ?
                WHERE id = ?
                """,
                ("c" * 32, artifact_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE dataset_lineage_anchors SET parent_ref = ?
                WHERE ref = ?
                """,
                ("c" * 32, _CHILD_REF),
            )
        with pytest.raises(sqlite3.IntegrityError, match="tombstone"):
            connection.execute(
                """
                UPDATE dataset_lineage_anchors SET deleted_at = ?
                WHERE ref = ?
                """,
                ("2026-01-01T00:00:00Z", _CHILD_REF),
            )

    session.update_dataset_filename(_CHILD_REF, "objects-current.parquet")
    with sqlite3.connect(session.db_path) as connection:
        anchor_name = connection.execute(
            "SELECT filename FROM dataset_lineage_anchors WHERE ref = ?",
            (_CHILD_REF,),
        ).fetchone()
    assert anchor_name == ("objects-current.parquet",)


def test_v5_database_is_backed_up_and_backfills_lineage_anchors(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v5.db"
    session = SessionStore(str(db_path))
    project = session.create_project("升级血缘")
    conversation = session.create_conversation(project.id)
    message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="画像完成",
    )
    session.register_dataset(
        ref=_PARENT_REF,
        project_id=project.id,
        filename="objects.xlsx",
        profile={"row_count": 3},
    )
    artifact = session.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="profile",
        payload={"row_count": 3},
        dataset_ref=_PARENT_REF,
    )
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
        connection.execute(f"PRAGMA user_version = {v6.VERSION - 1}")

    migrated = SessionStore(str(db_path))

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    backups = list(tmp_path.glob("v5.db.v5-backup.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(db_path) as connection:
        migration = connection.execute(
            """
            SELECT checksum, source_version, backup_path, source_sha256
            FROM schema_migrations WHERE version = ?
            """,
            (v6.VERSION,),
        ).fetchone()
        anchor = connection.execute(
            """
            SELECT project_id, filename, parent_ref, deleted_at
            FROM dataset_lineage_anchors WHERE ref = ?
            """,
            (_PARENT_REF,),
        ).fetchone()
        artifact_anchor = connection.execute(
            "SELECT lineage_dataset_ref FROM artifacts WHERE id = ?",
            (artifact.id,),
        ).fetchone()
    assert migration is not None
    assert migration[0] == v6.CHECKSUM
    assert migration[1] == v6.VERSION - 1
    assert migration[2] == str(backups[0])
    assert migration[3] == hashlib.sha256(backups[0].read_bytes()).hexdigest()
    assert anchor == (project.id, "objects.xlsx", None, None)
    assert artifact_anchor == (_PARENT_REF,)


def _seed_lineage(
    tmp_path: Path,
) -> tuple[SessionStore, str, str, str, str, str]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project(
        "血缘项目",
        owner_user_id=_OWNER.user_id,
        tenant_id=_OWNER.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    user_message = session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="检查对象数据并形成结论",
    )
    session.register_dataset(
        ref=_PARENT_REF,
        project_id=project.id,
        filename="objects.xlsx",
        profile={"row_count": 3},
    )
    session.register_dataset(
        ref=_CHILD_REF,
        project_id=project.id,
        filename="objects-clean.parquet",
        profile={"row_count": 2},
        parent_ref=_PARENT_REF,
        transform={"operation": "drop_missing"},
    )
    contract = build_minimal_contract(
        run_id="lineage-run",
        user_text=user_message.content,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    tasks = TaskStore(session.db_path)
    planning, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    invocation, _ = tasks.start_invocation(
        run_id=running.run_id,
        tool_call_id="profile-call",
        tool_name="get_data_profile",
        arguments={"dataset_ref": _CHILD_REF},
        idempotency_key="lineage-invocation",
    )
    assistant = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已生成对象画像",
    )
    _, _, evidence, artifact, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=running.state_version,
        assistant_message_id=assistant.id,
        result={"row_count": 2},
        evidence_kind="tool_result",
        evidence_source={"tool": "get_data_profile"},
        evidence_summary={"summary": "共 2 行"},
        artifact_draft=ArtifactDraft(
            type="profile",
            payload={"row_count": 2},
            file_ref=None,
            source_tool="get_data_profile",
            params={"analysis_id": "object-profile-v1"},
            dataset_ref=_CHILD_REF,
        ),
    )
    assert artifact is not None
    claims = tasks.replace_claims(
        running.run_id,
        [
            ClaimDraft(
                statement="清洗后的对象数据共有 2 行。",
                claim_kind="numeric",
                value_refs=(
                    {
                        "token": "2",
                        "supported": True,
                        "evidence_id": evidence.evidence_id,
                        "path": "$.row_count",
                    },
                ),
                evidence_ids=(evidence.evidence_id,),
            )
        ],
    )
    return (
        session,
        project.id,
        conversation.id,
        running.run_id,
        claims[0].claim_id,
        artifact.id,
    )
