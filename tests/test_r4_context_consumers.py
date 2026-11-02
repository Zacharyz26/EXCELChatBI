"""Frozen R4 consumer contrasts, NOT_RUN until root's isolated runner executes.

Real upload/Host/in_process tools/SQLite; scripted model. No invented product API.
Original requests and safety expectations must not change to obtain a pass.
"""

import pytest

from tests.test_r4_context_contract import (
    FILTERS,
    METRIC,
    OTHER,
    ScriptedModel,
    execute,
    offline_and_isolated,  # noqa: F401
    upload,
    workspace,
    write_witness,
)
from tests.test_r4_field_context_draft import metric_binding


def assert_original_user_turn(store, result, expected):
    turn = next(
        item
        for item in store.list_messages(result["run"]["conversation_id"])
        if item.id == result["run"]["user_message_id"]
    )
    assert turn.role == "user" and turn.content == expected


@pytest.mark.asyncio
async def test_unique_verified_metric_is_consumed_without_rewriting_user_authorization(tmp_path):
    store, project, conversation = workspace(tmp_path)
    dataset, previous = await metric_binding(store, project, conversation)
    goal = "请按月份分组汇总该列。"
    result = await execute(
        store,
        project,
        conversation,
        ScriptedModel(
            [
                (
                    "aggregate_preview",
                    {
                        "dataset_ref": dataset,
                        "group_col": "月份",
                        "value_col": METRIC,
                        "agg": "sum",
                    },
                )
            ],
            "已完成汇总。",
        ),
        goal,
    )
    write_witness(tmp_path, "field-consumer", previous=previous, result=result, goal=goal)
    assert result["run"]["status"] == "completed", result
    requirements = result["contract"]["analysis_requirements"]
    assert requirements["dataset_ref"] == dataset and requirements["value_col"] == METRIC
    successful = [item for item in result["invocations"] if item["status"] == "succeeded"]
    assert len(successful) == 1 and successful[0]["args"]["value_col"] == METRIC
    assert_original_user_turn(store, result, goal)


@pytest.mark.asyncio
@pytest.mark.parametrize("perform_filter", [False, True])
async def test_explicit_selection_preserves_current_filter_obligation(tmp_path, perform_filter):
    store, project, conversation = workspace(tmp_path)
    dataset = upload(store, project, conversation)
    goal = (
        f"接下来的分析请使用数据集（dataset_ref: {dataset}）。"
        "现在筛选条件：频道=A，状态=已播出。说明筛选前后的行数。"
    )
    result = await execute(
        store,
        project,
        conversation,
        ScriptedModel(
            [("transform_dataset", {"dataset_ref": dataset, "filters": FILTERS})]
            if perform_filter
            else [],
            "筛选前6行，筛选后4行。",
        ),
        goal,
    )
    write_witness(tmp_path, "selection-with-filter", result=result, perform_filter=perform_filter)
    assert (result["run"]["status"] == "completed") is perform_filter, result
    successful = [item for item in result["invocations"] if item["status"] == "succeeded"]
    assert len(successful) == int(perform_filter), result
    if perform_filter:
        assert successful[0]["tool_name"] == "transform_dataset"
        assert successful[0]["args"]["filters"] == FILTERS
        records = [
            item for item in result["evidence"] if item["source"].get("tool") == "transform_dataset"
        ]
        assert len(records) == 1
        assert records[0]["summary"]["fact_projection"]["arguments"]["filters"] == FILTERS
    assert_original_user_turn(store, result, goal)


@pytest.mark.asyncio
async def test_same_named_metric_after_dataset_switch_requires_clarification(tmp_path):
    store, project, conversation = workspace(tmp_path)
    old, previous = await metric_binding(store, project, conversation)
    another = store.create_conversation(project.id)
    selected = upload(store, project, another, body=OTHER, name="other-source.csv")
    selection = await execute(
        store,
        project,
        conversation,
        ScriptedModel([], "已确认后续分析使用指定数据集。"),
        f"接下来的分析请使用数据集（dataset_ref: {selected}）。",
    )
    write_witness(tmp_path, "field-selection", previous=previous, selection=selection)
    assert selected != old and selection["run"]["status"] == "completed", selection
    goal = "请按月份分组汇总该列。"
    result = await execute(
        store,
        project,
        conversation,
        ScriptedModel(
            [
                (
                    "aggregate_preview",
                    {
                        "dataset_ref": selected,
                        "group_col": "月份",
                        "value_col": METRIC,
                        "agg": "sum",
                    },
                )
            ],
            "已完成汇总。",
        ),
        goal,
    )
    write_witness(tmp_path, "field-switch-consumer", selection=selection, result=result, goal=goal)
    assert result["run"]["status"] == "waiting_user", result
    requirements = result["contract"]["analysis_requirements"]
    assert requirements["dataset_ref"] == selected and requirements["value_col"] is None
    assert any(question["slot"] == "value_col" for question in requirements["clarifications"])
    assert not any(item["status"] == "succeeded" for item in result["invocations"])
    assert_original_user_turn(store, result, goal)


@pytest.mark.asyncio
@pytest.mark.parametrize("perform_profile", [False, True])
async def test_explicit_selection_does_not_cancel_same_request_profile_obligation(
    tmp_path, perform_profile
):
    store, project, conversation = workspace(tmp_path)
    dataset = upload(store, project, conversation)
    goal = f"接下来的分析请使用数据集（dataset_ref: {dataset}）。现在生成数据画像。"
    result = await execute(
        store,
        project,
        conversation,
        ScriptedModel(
            [("get_data_profile", {"dataset_ref": dataset})] if perform_profile else [],
            "已完成画像。",
        ),
        goal,
    )
    write_witness(tmp_path, "selection-with-duty", result=result, requested_profile=perform_profile)
    assert (result["run"]["status"] == "completed") is perform_profile, result
    successful = [item for item in result["invocations"] if item["status"] == "succeeded"]
    assert len(successful) == int(perform_profile), result
    if perform_profile:
        assert successful[0]["tool_name"] == "get_data_profile"
    assert_original_user_turn(store, result, goal)
