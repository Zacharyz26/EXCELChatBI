"""Stage-5A versioned definition, conflict and formula compilation contracts."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from apps.orchestrator.control.contracts import build_minimal_contract
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.common.contracts import MCPProtocolError, MCPRequestContext
from packages.common.config import Settings
from packages.governance.permissions import Principal
from packages.knowledge.domain_models import DomainDefinitionDraft
from packages.knowledge.domain_store import (
    DomainAccessDenied,
    DomainDefinitionStore,
    DomainIdempotencyConflict,
    DomainMappingConflict,
)
from packages.session.models import ArtifactDraft
from packages.session.store import SessionStore
from packages.session.task_models import ClaimDraft
from packages.session.task_store import TaskStore, invocation_arguments_hash

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
            rag_embedder="hashing",
            rag_reranker="lexical",
            rag_store="local",
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
    assert result.structured_content["compiled_invocation"]["tool_name"] == ("aggregate_preview")

    denied = runtime.adapter.call_tool(
        "domain_definition_lookup",
        {"semantic_key": definition.semantic_key},
        _mcp_context(project_id, conversation.id, subject_id="mallory"),
    )
    assert denied.is_error is True


def test_knowledge_resources_are_project_and_subject_scoped(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
    tmp_path: Path,
) -> None:
    session, store, project_id = workspace
    conversation = session.create_conversation(project_id, "领域定义 Resource")
    definition = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="resource-definition-v1",
    ).definition
    other_project = session.create_project(
        "另一匿名项目",
        owner_user_id=_ALICE.user_id,
        tenant_id=_ALICE.tenant_scope,
    )
    other_conversation = session.create_conversation(other_project.id, "另一项目")
    other_definition = store.create_definition(
        project_id=other_project.id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="other-resource-definition-v1",
    ).definition
    runtime = AgentServiceRuntime(
        "knowledge-tools",
        Settings(
            process_role="mcp_server",
            chat_db_path=str(session.db_path),
            dataset_dir=str(tmp_path / "datasets"),
            report_dir=str(tmp_path / "reports"),
            kb_index_dir=str(tmp_path / "kb"),
            rag_embedder="hashing",
            rag_reranker="lexical",
            rag_store="local",
        ),
    )
    alice_context = _mcp_context(
        project_id,
        conversation.id,
        subject_id=_ALICE.user_id,
    )

    resources = runtime.adapter.list_resources(alice_context)
    assert runtime.adapter.has_resources is True
    assert [item.uri for item in resources] == [
        f"chatbi://domain-definitions/catalog/{project_id}",
        definition.resource_uri,
    ]
    assert resources[0].metadata is not None
    assert resources[0].metadata["com.chatbi/resource-kind"] == (
        "domain-definition-catalog"
    )
    assert len(str(resources[0].metadata["com.chatbi/catalog-version"])) == 64
    assert resources[1].metadata == {
        "com.chatbi/definition-id": definition.definition_id,
        "com.chatbi/semantic-key": definition.semantic_key,
        "com.chatbi/definition-version": 1,
        "com.chatbi/formula-hash": definition.formula_hash,
    }

    bob_resources = runtime.adapter.list_resources(
        _mcp_context(project_id, conversation.id, subject_id=_BOB.user_id)
    )
    assert [item.uri for item in bob_resources] == [
        f"chatbi://domain-definitions/catalog/{project_id}",
        definition.resource_uri,
    ]
    catalog = runtime.adapter.read_resource(
        f"chatbi://domain-definitions/catalog/{project_id}",
        alice_context,
    )
    catalog_payload = json.loads(catalog.text)
    assert catalog_payload["project_id"] == project_id
    assert catalog_payload["definitions"] == [
        {
            "definition_id": definition.definition_id,
            "semantic_key": definition.semantic_key,
            "version": 1,
            "formula_hash": definition.formula_hash,
            "resource_uri": definition.resource_uri,
        }
    ]
    assert catalog_payload["catalog_version"] == catalog.metadata[
        "com.chatbi/catalog-version"
    ]
    contents = runtime.adapter.read_resource(definition.resource_uri, alice_context)
    payload = json.loads(contents.text)
    assert payload["definition_id"] == definition.definition_id
    assert payload["version"] == definition.version
    assert payload["formula_hash"] == definition.formula_hash
    assert "tenant_id" not in payload
    assert "created_by_user_id" not in payload
    assert "idempotency_key" not in payload
    catalog_before = runtime.adapter.resource_subscription_snapshot(
        f"chatbi://domain-definitions/catalog/{project_id}",
        alice_context,
    )
    immutable_before = runtime.adapter.resource_subscription_snapshot(
        definition.resource_uri,
        alice_context,
    )
    assert catalog_before.catalog_version == catalog_payload["catalog_version"]

    store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=replace(
            _draft(),
            version=2,
            title="匿名分组度量（目录更新）",
            source_ref="urn:domain-definition:grouped-measure:v2",
            effective_from="2027-01-01T00:00:00Z",
        ),
        idempotency_key="resource-definition-v2",
    )
    catalog_after = runtime.adapter.resource_subscription_snapshot(
        f"chatbi://domain-definitions/catalog/{project_id}",
        alice_context,
    )
    immutable_after = runtime.adapter.resource_subscription_snapshot(
        definition.resource_uri,
        alice_context,
    )
    assert catalog_after.catalog_version != catalog_before.catalog_version
    assert catalog_after.content_hash != catalog_before.content_hash
    assert immutable_after.content_hash == immutable_before.content_hash
    assert immutable_after.catalog_version == catalog_after.catalog_version

    with pytest.raises(MCPProtocolError) as cross_project:
        runtime.adapter.read_resource(other_definition.resource_uri, alice_context)
    assert cross_project.value.code == "resource_not_found"
    with pytest.raises(MCPProtocolError) as cross_catalog:
        runtime.adapter.read_resource(
            f"chatbi://domain-definitions/catalog/{other_project.id}",
            alice_context,
        )
    assert cross_catalog.value.code == "resource_not_found"
    with pytest.raises(MCPProtocolError) as unknown_subject:
        runtime.adapter.list_resources(
            _mcp_context(project_id, conversation.id, subject_id="mallory")
        )
    assert unknown_subject.value.code == "resource_not_found"
    with pytest.raises(MCPProtocolError) as crossed_conversation:
        runtime.adapter.list_resources(
            _mcp_context(project_id, other_conversation.id, subject_id=_ALICE.user_id)
        )
    assert crossed_conversation.value.code == "resource_not_found"


def test_old_report_review_preserves_exact_definition_version(
    workspace: tuple[SessionStore, DomainDefinitionStore, str],
) -> None:
    session, store, project_id = workspace
    definition = store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=_draft(),
        idempotency_key="report-lineage-definition-v1",
    ).definition
    conversation = session.create_conversation(project_id, "历史报告口径复核")
    _, user_message = session.start_user_turn(
        conversation_id=conversation.id,
        content="生成受控汇总报告",
        suggested_title="历史报告口径复核",
    )
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="report-definition-lineage-run",
        user_text=user_message.content,
        chart_required=False,
        report_required=True,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation.id,
        user_message_id=user_message.id,
        contract=contract,
        budget={"max_tool_calls": 3},
    )
    run, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    compiled_arguments = {
        "dataset_ref": _DATASET_REF,
        "group_col": "bucket_code",
        "agg": "sum",
        "value_col": "measure_value",
        "sort": "group",
        "limit": 100,
    }
    definition_resource = {
        "definition_id": definition.definition_id,
        "definition_version": definition.version,
        "semantic_key": definition.semantic_key,
        "formula_hash": definition.formula_hash,
        "resource_uri": definition.resource_uri,
        "source_ref": definition.source_ref,
    }
    definition_invocation, _ = tasks.start_invocation(
        run_id=run.run_id,
        tool_call_id="definition-call",
        tool_name="domain_definition_lookup",
        arguments={"semantic_key": definition.semantic_key},
        idempotency_key="report-definition-call",
    )
    definition_message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="解析受控口径",
    )
    run, _, definition_evidence, _, _, _ = tasks.commit_tool_success(
        definition_invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=definition_message.id,
        result={"status": "resolved"},
        evidence_kind="tool_result",
        evidence_source={
            "tool": "domain_definition_lookup",
            "definition_resource": definition_resource,
            "compiled_invocation": {
                "definition_id": definition.definition_id,
                "definition_version": definition.version,
                "formula_hash": definition.formula_hash,
                "tool_name": "aggregate_preview",
                "arguments_hash": invocation_arguments_hash(compiled_arguments),
                "definition_match": True,
            },
        },
        evidence_summary={"summary": "领域定义 v1 已解析"},
        artifact_draft=None,
    )
    definition_execution = {
        **definition_resource,
        "definition_evidence_id": definition_evidence.evidence_id,
        "compiled_tool_name": "aggregate_preview",
        "compiled_arguments_hash": invocation_arguments_hash(compiled_arguments),
    }
    data_invocation, _ = tasks.start_invocation(
        run_id=run.run_id,
        tool_call_id="data-call",
        tool_name="aggregate_preview",
        arguments=compiled_arguments,
        idempotency_key="report-data-call",
    )
    data_message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="执行受控汇总",
    )
    run, _, data_evidence, data_artifact, _, _ = tasks.commit_tool_success(
        data_invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=data_message.id,
        result={"rows": [{"bucket_code": "A", "value": 25.0}]},
        evidence_kind="tool_result",
        evidence_source={
            "tool": "aggregate_preview",
            "definition_execution": definition_execution,
        },
        evidence_summary={
            "summary": "受控汇总完成",
            "value_index": [{"path": "$.rows[0].value", "value": "25"}],
        },
        artifact_draft=ArtifactDraft(
            type="table",
            payload={"rows": [{"bucket_code": "A", "value": 25.0}]},
            file_ref=None,
            source_tool="aggregate_preview",
            params={"analysis_id": "controlled-analysis-v1"},
            dataset_ref=_DATASET_REF,
        ),
    )
    assert data_artifact is not None
    claims = tasks.replace_claims(
        run.run_id,
        [
            ClaimDraft(
                statement="受控汇总值为 25。",
                claim_kind="numeric",
                value_refs=(
                    {
                        "token": "25",
                        "supported": True,
                        "evidence_id": data_evidence.evidence_id,
                        "path": "$.rows[0].value",
                    },
                    {
                        "kind": "definition_execution",
                        "supported": True,
                        "data_evidence_id": data_evidence.evidence_id,
                        "evidence_id": definition_evidence.evidence_id,
                        **definition_resource,
                    },
                ),
                evidence_ids=(
                    data_evidence.evidence_id,
                    definition_evidence.evidence_id,
                ),
            )
        ],
    )
    report_id = "e" * 32
    report_invocation, _ = tasks.start_invocation(
        run_id=run.run_id,
        tool_call_id="report-call",
        tool_name="generate_report",
        arguments={"analysis_ids": ["controlled-analysis-v1"]},
        idempotency_key="report-generation-call",
    )
    report_message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="生成历史报告",
    )
    tasks.commit_tool_success(
        report_invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=report_message.id,
        result={"report_id": report_id, "md_url": "/reports/old.md"},
        evidence_kind="tool_result",
        evidence_source={"tool": "generate_report"},
        evidence_summary={"summary": "报告生成完成"},
        artifact_draft=ArtifactDraft(
            type="report",
            payload={"report_id": report_id, "md_url": "/reports/old.md"},
            file_ref="old.md",
            source_tool="generate_report",
            params={"analysis_ids": ["controlled-analysis-v1"]},
            dataset_ref=None,
        ),
    )

    review = store.review_report_definition_lineage(
        project_id=project_id,
        report_id=report_id,
        principal=_ALICE,
    )

    assert review is not None
    assert review.status == "verified"
    assert review.issues == ()
    assert len(review.bindings) == 1
    binding = review.bindings[0]
    assert binding.definition_id == definition.definition_id
    assert binding.definition_version == 1
    assert binding.formula_hash == definition.formula_hash
    assert binding.claim_ids == (claims[0].claim_id,)

    store.create_definition(
        project_id=project_id,
        principal=_ALICE,
        draft=replace(
            _draft(),
            version=2,
            title="匿名分组度量（新版本）",
            source_ref="urn:domain-definition:grouped-measure:v2",
            effective_from="2027-01-01T00:00:00Z",
        ),
        idempotency_key="report-lineage-definition-v2",
    )
    historical_review = store.review_report_definition_lineage(
        project_id=project_id,
        report_id=report_id,
        principal=_ALICE,
    )
    assert historical_review is not None
    assert historical_review.status == "verified"
    assert historical_review.bindings[0].definition_version == 1
    with pytest.raises(DomainAccessDenied):
        store.review_report_definition_lineage(
            project_id=project_id,
            report_id=report_id,
            principal=_OTHER_TENANT,
        )


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
