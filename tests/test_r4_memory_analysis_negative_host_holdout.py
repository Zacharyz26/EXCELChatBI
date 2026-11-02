"""Independent negative governed-source mention is not source authorization.

The alias is a genuine MemoryStore mapping, not model-provided metadata.  The
existing explicit-negative source contract must still govern its use.
"""

from __future__ import annotations

import pytest

from tests import test_r4_context_contract as support
from tests.test_agent_loop import _remember_agent_reference
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "use_forbidden_alias", [False, True], ids=["current-allowed", "alias-forbidden"]
)
async def test_negative_analysis_predicate_does_not_authorize_governed_source(
    tmp_path, use_forbidden_alias
):
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
        name="governed-source.csv",
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
        name="current-source.csv",
    )
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="主数据",
        target_ref=named,
        key="independent-r4-negative-analysis-source",
    )
    selected = named if use_forbidden_alias else current
    goal = "不要分析主数据；请对当前数据集生成折线图。"
    model = support.ScriptedModel(
        [
            (
                "gen_chart",
                {
                    "dataset_ref": selected,
                    "chart_type": "line",
                    "encoding": {"x": "月份", "y": support.METRIC, "agg": "sum"},
                },
            )
        ],
        "已生成当前数据集的折线图。",
    )
    result = await support.execute(store, project, conversation, model, goal)
    support.write_witness(
        tmp_path,
        "negative-memory-source",
        goal=goal,
        named=named,
        current=current,
        model_dataset=selected,
        memory_id=memory_id,
        result=result,
        model_messages=model.messages,
    )
    calls = [call for call in result["invocations"] if call["status"] == "succeeded"]
    assert not any(call["args"].get("dataset_ref") == named for call in calls), result
    if not use_forbidden_alias:
        assert result["run"]["status"] == "completed", result
        assert result["contract"]["analysis_requirements"]["dataset_ref"] == current, result
        assert len(calls) == 1 and calls[0]["args"]["dataset_ref"] == current, result
