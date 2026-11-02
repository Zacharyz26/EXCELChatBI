"""Independent governed source priority and persisted shared-context proof.

Actual uploads/MemoryStore/Host/tool/SQLite, scripted model only.  Frozen oracle
is the explicitly selected source, never a value returned by the resolver.
"""

from __future__ import annotations

import pytest

from tests import test_r4_context_contract as support
from tests.test_agent_loop import _remember_agent_reference
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


def _sources(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    named = support.upload(
        store,
        project,
        conversation,
        body=(
            "月份,频道,状态,时长（分钟）\n"
            + "2026-03,B,已播出,900\n" * 6
            + "2026-04,B,已播出,901\n" * 6
        ).encode(),
        name="governed-input.csv",
    )
    current = support.upload(
        store,
        project,
        conversation,
        body=(
            "月份,频道,状态,时长（分钟）\n"
            + "2026-01,A,已播出,10\n" * 6
            + "2026-02,A,已播出,20\n" * 6
        ).encode(),
        name="implicit-current.csv",
    )
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="主数据",
        target_ref=named,
        key="independent-r4-memory-projection",
    )
    return store, project, conversation, named, current, memory_id


def _model(dataset):
    return support.ScriptedModel(
        [
            (
                "gen_chart",
                {
                    "dataset_ref": dataset,
                    "chart_type": "line",
                    "encoding": {"x": "月份", "y": support.METRIC, "agg": "sum"},
                },
            )
        ],
        "已生成折线图。",
    )


@pytest.mark.asyncio
async def test_conflicting_explicit_dataset_and_governed_memory_do_not_union_scope(tmp_path):
    store, project, conversation, named, current, memory_id = _sources(tmp_path)
    goal = f"请使用数据集（dataset_ref={current}）；使用主数据生成折线图。"
    model = _model(current)
    result = await support.execute(store, project, conversation, model, goal)
    support.write_witness(
        tmp_path,
        "memory-explicit-conflict",
        goal=goal,
        named=named,
        current=current,
        memory_id=memory_id,
        result=result,
        messages=model.messages,
    )
    assert result["run"]["status"] != "completed", result
    assert not any(call["status"] == "succeeded" for call in result["invocations"]), result


@pytest.mark.asyncio
async def test_governed_dataset_source_is_retained_in_api_and_resolver_after_completion(tmp_path):
    store, project, conversation, named, current, memory_id = _sources(tmp_path)
    goal = "使用主数据生成折线图"
    model = _model(named)
    result = await support.execute(store, project, conversation, model, goal)
    detail = support.conversation_detail(store, conversation)
    projection = support.execution_projection(detail)
    resolution = support.resolve(store, project, conversation, "处理当前数据集")
    support.write_witness(
        tmp_path,
        "memory-persisted-source",
        goal=goal,
        named=named,
        current=current,
        memory_id=memory_id,
        result=result,
        detail=detail,
        resolver_status=resolution.status,
        resolver_targets=resolution.targets,
    )
    assert result["run"]["status"] == "completed", result
    assert result["contract"]["analysis_requirements"]["dataset_ref"] == named, result
    calls = [call for call in result["invocations"] if call["status"] == "succeeded"]
    assert len(calls) == 1 and calls[0]["args"]["dataset_ref"] == named, result
    assert projection["availability"] == "available", projection
    assert projection["dataset_ref"] == named and projection["dataset_ref"] != current, projection
    assert support.ref_set(resolution) == {named}, resolution
