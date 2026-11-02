"""Two-metric controls distinguish proved field context from schema uniqueness.

New frozen contrast; does not replace the original six consumer cases.
Real local tools/SQLite, scripted model, no paid provider.
"""

import pytest

from tests.test_r4_context_consumers import assert_original_user_turn
from tests.test_r4_context_contract import (
    METRIC,
    ScriptedModel,
    execute,
    offline_and_isolated,  # noqa: F401
    upload,
    workspace,
    write_witness,
)

TWO_METRICS = (
    "月份,频道,状态,时长（分钟）,等待（秒）\n"
    "2026-01,A,已播出,10,1\n"
    "2026-01,A,已播出,20,3\n"
    "2026-02,A,待播,30,5\n"
    "2026-02,B,已播出,40,7\n"
    "2026-02,A,已播出,,9\n"
    "2026-02,A,已播出,50,11\n"
).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize("establish_metric", [False, True])
async def test_pronoun_requires_actual_unique_field_proof_not_unique_schema_guess(
    tmp_path, establish_metric
):
    store, project, conversation = workspace(tmp_path)
    dataset = upload(store, project, conversation, body=TWO_METRICS, name="two-metrics.csv")
    args = {"dataset_ref": dataset, "group_col": "月份", "value_col": METRIC, "agg": "sum"}
    previous = None
    if establish_metric:
        previous = await execute(
            store,
            project,
            conversation,
            ScriptedModel([("aggregate_preview", args)], "已完成汇总。"),
            f"dataset_ref={dataset}；按月份分组汇总时长（分钟）。",
        )
        write_witness(tmp_path, "two-metric-source", previous=previous)
        assert previous["run"]["status"] == "completed", previous
        assert len(previous["evidence"]) == 1 and len(previous["invocations"]) == 1
        assert previous["invocations"][0]["status"] == "succeeded"
        assert previous["invocations"][0]["args"] == args
    goal = "请按月份分组汇总该列。"
    result = await execute(
        store,
        project,
        conversation,
        ScriptedModel([("aggregate_preview", args)], "已完成汇总。"),
        goal,
    )
    write_witness(
        tmp_path,
        "two-metric-consumer",
        result=result,
        previous=previous,
        establish_metric=establish_metric,
    )
    req = result["contract"]["analysis_requirements"]
    if establish_metric:
        assert result["run"]["status"] == "completed", result
        assert req["value_col"] == METRIC and req["dataset_ref"] == dataset
        successful = [call for call in result["invocations"] if call["status"] == "succeeded"]
        assert len(successful) == 1 and successful[0]["args"] == args
    else:
        assert result["run"]["status"] == "waiting_user", result
        assert req["value_col"] is None
        assert any(question["slot"] == "value_col" for question in req["clarifications"])
        assert not any(call["status"] == "succeeded" for call in result["invocations"])
    assert_original_user_turn(store, result, goal)
