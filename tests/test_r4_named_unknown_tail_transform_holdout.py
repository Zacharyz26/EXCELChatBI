"""One bounded same-mechanism unknown remainder; real Host/tool boundary."""

from __future__ import annotations

import pytest

from tests import test_r4_context_contract as support
from tests import test_r4_memory_conflict_projection_holdout_v2 as _FIXTURE
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
async def test_unknown_tail_does_not_authorize_a_data_transform(tmp_path):
    store, project, conversation, named, current, memory_id = _FIXTURE._sources(tmp_path)
    goal = "使用主数据；待定。"
    model = support.ScriptedModel(
        [
            (
                "transform_dataset",
                {
                    "dataset_ref": named,
                    "filters": [{"column": "月份", "op": "==", "value": "2026-03"}],
                },
            )
        ],
        "已选择主数据。",
    )
    result = await support.execute(store, project, conversation, model, goal)
    support.write_witness(
        tmp_path,
        "named-unknown-tail",
        goal=goal,
        named=named,
        current=current,
        memory_id=memory_id,
        result=result,
        messages=model.messages,
    )
    assert not any(
        call["tool_name"] == "transform_dataset" and call["status"] == "succeeded"
        for call in result["invocations"]
    ), result
    assert result["run"]["status"] != "completed", result
