"""Independent explicit governed-memory source versus current-source priority.

Both datasets are actually uploaded; MemoryStore receives an actual user
confirmation and governed dataset link.  No Artifact, Evidence or completion is
fabricated.  Root runs this offline real-Host pair.
"""

from __future__ import annotations

import json

import pytest

from tests import test_r4_context_contract as support
from tests.test_agent_loop import _remember_agent_reference
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("use_named_source", [False, True], ids=["wrong-current", "correct-memory"])
async def test_explicit_memory_dataset_is_not_replaced_by_implicit_current(
    tmp_path, use_named_source
):
    store, project, conversation = support.workspace(tmp_path)
    # The first upload is the uniquely named governed target.  The later upload
    # is the valid default source, so neither missing-file nor project denial can
    # explain a rejection.  Both groups have enough rows for unchanged policy.
    named_body = (
        "月份,频道,状态,时长（分钟）\n"
        + "2026-03,B,已播出,900\n" * 6
        + "2026-04,B,已播出,901\n" * 6
    ).encode()
    current_body = (
        "月份,频道,状态,时长（分钟）\n" + "2026-01,A,已播出,10\n" * 6 + "2026-02,A,已播出,20\n" * 6
    ).encode()
    named = support.upload(store, project, conversation, body=named_body, name="named-source.csv")
    current = support.upload(
        store, project, conversation, body=current_body, name="implicit-current.csv"
    )
    assert named != current
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="主数据",
        target_ref=named,
        key="independent-r4-explicit-source",
    )
    selected = named if use_named_source else current
    goal = "使用主数据生成折线图"
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
        "已生成主数据的折线图。",
    )
    result = await support.execute(store, project, conversation, model, goal)
    support.write_witness(
        tmp_path,
        "independent-memory-source",
        goal=goal,
        named_dataset=named,
        implicit_current_dataset=current,
        model_dataset=selected,
        memory_id=memory_id,
        result=result,
        model_messages=model.messages,
    )
    meta = json.loads(next(item["data"] for item in result["events"] if item["event"] == "meta"))
    assert meta["memory_reference_status"] == "resolved", result
    calls = [x for x in result["invocations"] if x["status"] == "succeeded"]
    assert not any(x["args"].get("dataset_ref") == current for x in calls), result
    if use_named_source:
        assert result["run"]["status"] == "completed", result
        assert result["contract"]["analysis_requirements"]["dataset_ref"] == named, result
        assert len(calls) == 1 and calls[0]["args"]["dataset_ref"] == named, result
    elif result["run"]["status"] == "completed":
        # A Host may explicitly bind/repair to the named source or reject the
        # wrong model parameter, but cannot execute/complete on the other source.
        assert calls and all(x["args"].get("dataset_ref") == named for x in calls), result
