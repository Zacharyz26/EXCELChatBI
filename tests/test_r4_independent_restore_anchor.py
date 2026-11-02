"""Three independent R4 restore/anchor contrasts; NOT_RUN until root executes.

Real API uploads, real Host/tools/SQLite, no fabricated successful state. The
invalid field-proof test changes only the supplied proof argument in memory,
not persisted Evidence, ledger, run status or the source file.
"""

from __future__ import annotations

import importlib
from dataclasses import asdict

import pytest
from packages.session.coref import ReferenceResolver
from packages.session.store import SessionStore

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_field_proof_consumers import TWO_METRICS
from tests.test_r4_local_chain_draft import conversation_detail


async def metric(store, project, conversation, dataset, column):
    result = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                (
                    "aggregate_preview",
                    {
                        "dataset_ref": dataset,
                        "group_col": "月份",
                        "value_col": column,
                        "agg": "sum",
                    },
                )
            ],
            "已完成汇总。",
        ),
        f"dataset_ref={dataset}；按月份分组汇总{column}。",
    )
    assert result["run"]["status"] == "completed", result
    succeeded = [row for row in result["invocations"] if row["status"] == "succeeded"]
    assert len(succeeded) == 1 and succeeded[0]["args"]["value_col"] == column
    return result


async def proved_field(store, project, conversation):
    dataset = support.upload(store, project, conversation, body=TWO_METRICS, name="field-proof.csv")
    first = await metric(store, project, conversation, dataset, support.METRIC)
    query = "请按月份分组汇总该列。"
    resolution = support.resolve(store, project, conversation, query)
    field = getattr(resolution, "field_binding", None)
    assert field is not None, {
        "interface": "frozen R4 field proof missing",
        "resolution": asdict(resolution),
    }
    invocation = next(i for i in first["invocations"] if i["status"] == "succeeded")
    evidence = next(
        e for e in first["evidence"] if e["invocation_id"] == invocation["invocation_id"]
    )
    assert field.dataset_ref == dataset and field.column == support.METRIC
    assert field.source_run_id == first["run"]["run_id"]
    assert field.source_invocation_id == invocation["invocation_id"]
    assert field.source_evidence_id == evidence["evidence_id"]
    assert field.source_result_hash == invocation["result_hash"] == evidence["result_hash"]
    return dataset, first, query, resolution, field


@pytest.mark.asyncio
async def test_original_frozen_field_proof_restores_after_different_later_metric(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    dataset, first, query, resolution, field = await proved_field(store, project, conversation)
    assumption = resolution.assumption()
    assert assumption is not None
    second = await metric(store, project, conversation, dataset, "等待（秒）")
    reopened = SessionStore(store.db_path)
    detail = await conversation_detail(reopened, conversation)
    support.write_witness(
        tmp_path,
        "field-restore-before",
        first=first,
        second=second,
        frozen=asdict(resolution),
        detail=detail,
    )
    projection = support.execution_projection(detail)
    assert projection["field_context"]["column"] == "等待（秒）"
    restored = ReferenceResolver(reopened, audit_recorder=lambda _event: None).restore(
        assumption,
        query=query,
        project_id=project.id,
        conversation_id=conversation.id,
        principal=support.PRINCIPAL,
    )
    support.write_witness(
        tmp_path,
        "field-restore-after",
        first=first,
        second=second,
        frozen=asdict(resolution),
        restored=asdict(restored),
        detail=detail,
    )
    assert restored.field_binding == field
    assert restored.resolution_hash == resolution.resolution_hash
    assert restored.field_binding.column != projection["field_context"]["column"]


@pytest.mark.asyncio
async def test_field_restore_rejects_mismatched_original_result_hash_without_db_write(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    _dataset, first, _query, resolution, field = await proved_field(store, project, conversation)
    from tests.test_r4_independent_context_events import snapshot

    before = snapshot(store.db_path)
    module = importlib.import_module("packages.session.execution_context")
    raw = field.to_dict()
    raw["source_result_hash"] = "0" * 64 if field.source_result_hash != "0" * 64 else "1" * 64
    support.write_witness(
        tmp_path,
        "invalid-field-proof",
        first=first,
        frozen=asdict(resolution),
        supplied=raw,
        fault="in-memory proof input only; persisted records unchanged",
    )
    with pytest.raises(ValueError):
        module.validate_field_binding(store, conversation_id=conversation.id, raw=raw)
    assert snapshot(store.db_path) == before
    assert (
        module.validate_field_binding(store, conversation_id=conversation.id, raw=field.to_dict())
        == field
    )


@pytest.mark.asyncio
async def test_explicit_external_derived_anchor_is_usable_without_inventing_its_lineage(tmp_path):
    store, project, origin = support.workspace(tmp_path)
    parent, child, source_run = await support.filtered_source(store, project, origin)
    target = store.create_conversation(project.id)
    selection = await support.execute(
        store,
        project,
        target,
        support.ScriptedModel([], "已确认后续分析使用指定数据集。"),
        f"接下来的分析请使用数据集（dataset_ref: {child}）。",
    )
    detail = await conversation_detail(store, target)
    support.write_witness(
        tmp_path,
        "external-anchor-before",
        origin=origin.id,
        target=target.id,
        parent=parent,
        child=child,
        source_run=source_run,
        selection=selection,
        detail=detail,
    )
    assert selection["run"]["status"] == "completed", selection
    assert selection["invocations"] == [] and selection["evidence"] == []
    context = support.execution_projection(detail)
    assert context["availability"] == "available" and context["dataset_ref"] == child
    assert context["lineage"] == [] and context["lineage_state"] == "unknown"
    assert context["lineage_boundary_ref"] == child
    assert context["analysis_ids"] == [] and context["field_context"]["column"] is None
    assert context["source_run_id"] == selection["run"]["run_id"]
    actual = await support.execute(
        store,
        project,
        target,
        support.ScriptedModel([("get_data_profile", {"dataset_ref": child})], "已完成画像。"),
        "请查看当前数据集的画像。",
    )
    reopened_detail = await conversation_detail(SessionStore(store.db_path), target)
    support.write_witness(
        tmp_path,
        "external-anchor-used",
        actual=actual,
        detail=reopened_detail,
        foreign_source_run_id=source_run["run"]["run_id"],
    )
    assert actual["run"]["status"] == "completed", actual
    assert all(e["run_id"] == actual["run"]["run_id"] for e in actual["evidence"])
    after = support.execution_projection(reopened_detail)
    assert after["dataset_ref"] == child and after["lineage_state"] == "unknown"
    assert after["lineage"] == [] and after["lineage_boundary_ref"] == child
