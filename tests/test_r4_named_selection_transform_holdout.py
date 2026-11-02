"""Same source-only alias input must not authorize a data-changing tool.

This single scenario uses actual upload/MemoryStore/Host/tools/SQLite; no fake
Evidence or completion. It complements the unchanged no-tool false-reject case.
"""

from __future__ import annotations

import pytest

from tests import test_r4_context_contract as support
from tests import test_r4_memory_conflict_projection_holdout_v2 as _FIXTURE
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
async def test_pure_governed_selection_does_not_authorize_filtering(tmp_path):
    store, project, conversation, named, current, memory_id = _FIXTURE._sources(tmp_path)
    goal = "使用主数据"
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
    detail = support.conversation_detail(store, conversation)
    support.write_witness(
        tmp_path,
        "named-selection-transform",
        goal=goal,
        named=named,
        current=current,
        memory_id=memory_id,
        result=result,
        messages=model.messages,
        detail=detail,
    )
    succeeded = [call for call in result["invocations"] if call["status"] == "succeeded"]
    assert not any(call["tool_name"] == "transform_dataset" for call in succeeded), result
    # If the source-only request is successfully delivered, it has not consumed
    # an unsolicited data operation or progressed to its derived version.
    if result["run"]["status"] == "completed":
        assert succeeded == [], result
        assert result["contract"]["analysis_requirements"]["dataset_ref"] == named, result
        assert support.execution_projection(detail)["dataset_ref"] == named, detail
