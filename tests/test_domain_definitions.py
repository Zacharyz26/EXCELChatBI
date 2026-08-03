"""Stage-5A versioned definition, conflict and formula compilation contracts."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.common.contracts import MCPRequestContext
from packages.common.config import Settings
from packages.governance.permissions import Principal
from packages.knowledge.domain_models import DomainDefinitionDraft
from packages.knowledge.domain_store import (
    DomainAccessDenied,
    DomainDefinitionStore,
    DomainIdempotencyConflict,
    DomainMappingConflict,
)
from packages.session.store import SessionStore

_ALICE = Principal(user_id="alice", tenant_id="tenant-a")
_BOB = Principal(user_id="bob", tenant_id="tenant-a")
_OTHER_TENANT = Principal(user_id="alice", tenant_id="tenant-b")
_DATASET_REF = "d" * 32


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[SessionStore, DomainDefinitionStore, str]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project(
        "匿名领域定义",
        owner_user_id=_ALICE.user_id,
        tenant_id=_ALICE.tenant_scope,
    )
    session.register_dataset(
        ref=_DATASET_REF,
        project_id=project.id,
        filename="anonymous.xlsx",
        profile={
            "columns": [
                {"name": "bucket_code", "dtype": "string"},
                {"name": "measure_value", "dtype": "number"},
            ]
        },
    )
    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            INSERT INTO project_memberships(
                project_id, user_id, tenant_id, role, created_at
            ) VALUES (?, ?, ?, 'viewer', '2026-01-01T00:00:00Z')
            """,
            (project.id, _BOB.user_id, _BOB.tenant_scope),
        )
    return session, DomainDefinitionStore(session), project.id


def test_definition_is_versioned_idempotent_and_compiles_to_allowlisted_tool(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
) -> None:
    _, store, project_id = workspace
    created = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="definition-v1",
    )
    replayed = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="definition-v1",
    )

    assert created.outcome == "created"
    assert replayed.outcome == "replayed"
    assert replayed.definition.definition_id == created.definition.definition_id
    assert created.definition.resource_uri == (
        f"chatbi://domain-definitions/{created.definition.definition_id}"
    )
    assert len(created.definition.formula_hash) == 64

    store.register_field_mapping(
        project_id=project_id,
        dataset_ref=_DATASET_REF,
        concept_key="dimension.bucket",
        field_name="bucket_code",
        source_ref="urn:field-map:bucket",
        principal=_ALICE,
    )
    store.register_field_mapping(
        project_id=project_id,
        dataset_ref=_DATASET_REF,
        concept_key="measure.value",
        field_name="measure_value",
        source_ref="urn:field-map:value",
        principal=_ALICE,
    )
    resolution = store.resolve(
        project_id=project_id,
        semantic_key="metric.grouped_measure",
        principal=_ALICE,
        as_of="2026-06-01T00:00:00Z",
    )
    assert resolution.status == "resolved"
    assert resolution.requires_clarification is False
    assert resolution.definition == created.definition
    invocation = store.compile(
        definition=created.definition,
        dataset_ref=_DATASET_REF,
        principal=_ALICE,
    )
    assert invocation.tool_name == "aggregate_preview"
    assert invocation.arguments == {
        "dataset_ref": _DATASET_REF,
        "group_col": "bucket_code",
        "agg": "sum",
        "value_col": "measure_value",
        "sort": "group",
        "limit": 100,
    }
    serialized = str(invocation.arguments).lower()
    assert "sql" not in serialized
    assert "/home/" not in serialized


def test_overlapping_versions_require_clarification_and_history_stays_addressable(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
) -> None:
    _, store, project_id = workspace
    first = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="definition-v1",
    ).definition
    second_result = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=replace(
            _draft(),
            version=2,
            title="匿名分组度量（修订）",
            effective_from="2026-06-01T00:00:00Z",
            formula={
                "tool": "aggregate_preview",
                "arguments": {
                    "group_concept": "dimension.bucket",
                    "value_concept": "measure.value",
                    "agg": "mean",
                },
            },
        ),
        idempotency_key="definition-v2",
    )

    assert second_result.outcome == "conflict"
    conflict = store.resolve(
        project_id=project_id,
        semantic_key="metric.grouped_measure",
        principal=_ALICE,
        as_of="2026-07-01T00:00:00Z",
    )
    assert conflict.status == "conflict"
    assert conflict.definition is None
    assert conflict.requires_clarification is True
    assert [item.version for item in conflict.candidates] == [1, 2]
    assert store.get_definition(first.definition_id, principal=_ALICE) == first


def test_resolution_distinguishes_expired_future_missing_and_mapping_gap(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
) -> None:
    _, store, project_id = workspace
    definition = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=replace(_draft(), effective_to="2026-07-01T00:00:00Z"),
        idempotency_key="definition-v1",
    ).definition

    expired = store.resolve(
        project_id=project_id,
        semantic_key=definition.semantic_key,
        principal=_ALICE,
        as_of="2026-08-01T00:00:00Z",
    )
    future = store.resolve(
        project_id=project_id,
        semantic_key=definition.semantic_key,
        principal=_ALICE,
        as_of="2025-12-01T00:00:00Z",
    )
    missing = store.resolve(
        project_id=project_id,
        semantic_key="metric.not_registered",
        principal=_ALICE,
    )
    assert expired.status == "expired"
    assert future.status == "missing"
    assert missing.status == "missing"
    with pytest.raises(ValueError, match="缺少领域字段映射"):
        store.compile(
            definition=definition,
            dataset_ref=_DATASET_REF,
            principal=_ALICE,
        )


def test_writes_are_editor_only_tenant_scoped_and_immutable(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
) -> None:
    session, store, project_id = workspace
    with pytest.raises(DomainAccessDenied):
        store.create_definition(
            project_id=project_id,
            principal=_BOB,
            draft=_draft(),
            idempotency_key="viewer-write",
        )
    with pytest.raises(DomainAccessDenied):
        store.list_definitions(project_id=project_id, principal=_OTHER_TENANT)

    record = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="definition-v1",
    ).definition
    with sqlite3.connect(session.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                "UPDATE domain_definitions SET title = 'tampered' WHERE definition_id = ?",
                (record.definition_id,),
            )


def test_formula_source_idempotency_and_mapping_conflicts_fail_closed(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
) -> None:
    _, store, project_id = workspace
    store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="definition-v1",
    )
    with pytest.raises(DomainIdempotencyConflict):
        store.create_definition(
            project_id=project_id,
            principal=_ALICE,
            draft=replace(_draft(), title="different"),
            idempotency_key="definition-v1",
        )
    with pytest.raises(ValueError, match="受控契约"):
        store.create_definition(
            project_id=project_id,
            principal=_ALICE,
            draft=replace(
                _draft(),
                version=2,
                formula={"tool": "sql", "arguments": {"query": "SELECT 1"}},
            ),
            idempotency_key="unsafe-formula",
        )
    with pytest.raises(ValueError, match="仅允许"):
        store.create_definition(
            project_id=project_id,
            principal=_ALICE,
            draft=replace(_draft(), version=2, source_ref="/home/private/spec.md"),
            idempotency_key="unsafe-source",
        )

    store.register_field_mapping(
        project_id=project_id,
        dataset_ref=_DATASET_REF,
        concept_key="dimension.bucket",
        field_name="bucket_code",
        source_ref="urn:field-map:bucket",
        principal=_ALICE,
    )
    with pytest.raises(DomainMappingConflict):
        store.register_field_mapping(
            project_id=project_id,
            dataset_ref=_DATASET_REF,
            concept_key="dimension.bucket",
            field_name="measure_value",
            source_ref="urn:field-map:changed",
            principal=_ALICE,
        )


def test_knowledge_service_filters_signed_subject_and_returns_compiled_plan(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
    tmp_path: Path,
) -> None:
    session, store, project_id = workspace
    conversation = session.create_conversation(project_id, "领域定义工具")
    definition = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="definition-v1",
    ).definition
    for concept, field in (
        ("dimension.bucket", "bucket_code"),
        ("measure.value", "measure_value"),
    ):
        store.register_field_mapping(
            project_id=project_id,
            dataset_ref=_DATASET_REF,
            concept_key=concept,
            field_name=field,
            source_ref=f"urn:mcp-map:{concept}",
            principal=_ALICE,
        )
    runtime = AgentServiceRuntime(
        "knowledge-tools",
        Settings(
            process_role="mcp_server",
            chat_db_path=str(session.db_path),
            dataset_dir=str(tmp_path / "datasets"),
            report_dir=str(tmp_path / "reports"),
            kb_index_dir=str(tmp_path / "kb"),
        ),
    )
    context = _mcp_context(project_id, conversation.id, subject_id="alice")
    result = runtime.adapter.call_tool(
        "domain_definition_lookup",
        {
            "semantic_key": definition.semantic_key,
            "as_of": "2026-06-01T00:00:00Z",
            "dataset_ref": _DATASET_REF,
        },
        context,
    )
    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "resolved"
    assert result.structured_content["compiled_invocation"]["tool_name"] == (
        "aggregate_preview"
    )

    denied = runtime.adapter.call_tool(
        "domain_definition_lookup",
        {"semantic_key": definition.semantic_key},
        _mcp_context(project_id, conversation.id, subject_id="mallory"),
    )
    assert denied.is_error is True


def _mcp_context(
    project_id: str,
    conversation_id: str,
    *,
    subject_id: str,
) -> MCPRequestContext:
    return MCPRequestContext(
        subject_id=subject_id,
        project_id=project_id,
        conversation_id=conversation_id,
        run_id="run-stage5",
        plan_version=1,
        step_id="resolve-definition",
        invocation_id="invocation-stage5",
        idempotency_key="stage5-tool-call",
        permission_snapshot_id="permission-stage5",
        memory_snapshot_id="0" * 32,
        evidence_ledger_version=1,
        trace_id="trace-stage5",
        deadline_at=(datetime.now(UTC) + timedelta(minutes=1)).isoformat(),
    )


def _draft() -> DomainDefinitionDraft:
    return DomainDefinitionDraft(
        semantic_key="metric.grouped_measure",
        version=1,
        title="匿名分组度量",
        description="按匿名分组汇总匿名度量。",
        formula={
            "tool": "aggregate_preview",
            "arguments": {
                "group_concept": "dimension.bucket",
                "value_concept": "measure.value",
                "agg": "sum",
                "sort": "group",
                "limit": 100,
            },
        },
        grain=("dimension.bucket",),
        scope={"dataset_type": "tabular"},
        owner="domain-owner",
        source_ref="urn:domain-definition:grouped-measure:v1",
        effective_from="2026-01-01T00:00:00Z",
    )
