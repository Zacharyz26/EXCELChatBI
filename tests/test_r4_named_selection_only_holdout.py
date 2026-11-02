"""Focused existing selection-only contract for a governed dataset alias.

Real upload, governed MemoryStore link, Host, SQLite, and post-run GET; the
model is scripted and does not provide any tool or Evidence.  A source-only
request cannot silently acquire a profile/analysis obligation.
"""

from __future__ import annotations

import json

import pytest

from tests import test_r4_context_contract as support
from tests import test_r4_memory_conflict_projection_holdout_v2 as _FIXTURE
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
async def test_pure_governed_dataset_selection_has_no_analysis_obligation(tmp_path):
    store, project, conversation, named, current, memory_id = _FIXTURE._sources(tmp_path)
    goal = "使用主数据"
    model = support.ScriptedModel([], "已选择主数据。")
    result = await support.execute(store, project, conversation, model, goal)
    detail = support.conversation_detail(store, conversation)
    projection = support.execution_projection(detail)
    resolution = support.resolve(store, project, conversation, "处理当前数据集")
    support.write_witness(
        tmp_path,
        "named-selection-only",
        goal=goal,
        named=named,
        current=current,
        memory_id=memory_id,
        result=result,
        messages=model.messages,
        detail=detail,
        resolver_status=resolution.status,
        resolver_targets=resolution.targets,
    )
    assert result["invocations"] == [], result
    if result["run"]["status"] == "completed":
        assert result["contract"]["analysis_requirements"]["dataset_ref"] == named, result
        assert projection["availability"] == "available", projection
        assert projection["dataset_ref"] == named and named != current, projection
        assert support.ref_set(resolution) == {named}, resolution
    else:
        # An explicitly unsupported alias-only selection may ask for a concrete
        # source selection. It must not invent a profile or silently complete.
        assert result["run"]["status"] == "waiting_user", result
        assert any(
            event["event"] == "waiting_user"
            and bool(json.loads(event["data"]).get("payload", {}).get("question"))
            for event in result["events"]
        ), result
