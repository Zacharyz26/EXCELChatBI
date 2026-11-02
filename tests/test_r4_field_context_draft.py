"""DRAFT, NOT_RUN: limited R4 field binding and read-only API contract.

No new product API is assumed beyond the approved execution_context projection.
Root must freeze this draft before execution. All tools/SQLite are real; model
turns are scripted. This file does not cover arbitrary pronoun interpretation.
"""

import sqlite3

import pytest

from tests.test_r4_context_contract import (
    METRIC,
    OTHER,
    ScriptedModel,
    conversation_detail,
    execute,
    execution_projection,
    offline_and_isolated,  # noqa: F401
    upload,
    workspace,
    write_witness,
)


def database_dump(store):
    with sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True) as connection:
        return list(connection.iterdump())


def require_field_context(projection):
    value = projection.get("field_context")
    assert isinstance(value, dict) and "column" in value, {
        "interface_status": "R4_FIELD_CONTEXT_NOT_IMPLEMENTED_OR_INVALID",
        "projection": projection,
    }
    return value


def test_upload_profile_is_read_only_and_does_not_guess_first_column(tmp_path):
    store, project, conversation = workspace(tmp_path)
    dataset = upload(store, project, conversation)
    before = database_dump(store)
    detail = conversation_detail(store, conversation)
    after = database_dump(store)
    write_witness(tmp_path, "whole-profile", detail=detail, database_unchanged=before == after)
    context = execution_projection(detail)
    assert context["dataset_ref"] == dataset
    assert context["run_id"] is None and context["run_status"] is None
    assert require_field_context(context)["column"] is None
    assert before == after


async def metric_binding(store, project, conversation):
    dataset = upload(store, project, conversation)
    goal = f"dataset_ref={dataset}；按月份分组汇总时长（分钟）。"
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
    # Real tool computation has independent fixture total10+20+30+40+50=150.
    assert result["run"]["status"] == "completed", result
    invocations = [call for call in result["invocations"] if call["status"] == "succeeded"]
    assert len(invocations) == 1 and invocations[0]["args"]["value_col"] == METRIC, result
    records = [
        record
        for record in result["evidence"]
        if record["source"].get("tool") == "aggregate_preview"
    ]
    assert len(records) == 1, result
    assert records[0]["summary"]["fact_projection"]["output_ref"] == dataset, result
    assert any(
        item.get("path") == "$.total_value" and str(item.get("value")) in {"150", "150.0"}
        for item in records[0]["summary"]["value_index"]
    ), result
    return dataset, result


@pytest.mark.asyncio
async def test_current_dataset_metric_binding_has_exact_origin(tmp_path):
    store, project, conversation = workspace(tmp_path)
    dataset, result = await metric_binding(store, project, conversation)
    before = database_dump(store)
    detail = conversation_detail(store, conversation)
    write_witness(tmp_path, "unique-metric", result=result, detail=detail)
    projection = execution_projection(detail)
    field = require_field_context(projection)
    assert projection["dataset_ref"] == dataset
    assert field["column"] == METRIC
    assert field["source_run_id"] == result["run"]["run_id"]
    assert before == database_dump(store)


@pytest.mark.asyncio
async def test_same_named_metric_cannot_transfer_to_another_dataset(tmp_path):
    store, project, conversation = workspace(tmp_path)
    old, previous = await metric_binding(store, project, conversation)
    another = store.create_conversation(project.id)
    selected = upload(store, project, another, body=OTHER, name="new-source.csv")
    assert selected != old
    selection = await execute(
        store,
        project,
        conversation,
        ScriptedModel([], "已确认后续分析使用指定数据集。"),
        f"接下来的分析请使用数据集（dataset_ref: {selected}）。",
    )
    assert selection["run"]["status"] == "completed", selection
    assert not selection["invocations"] and not selection["evidence"], selection
    detail = conversation_detail(store, conversation)
    write_witness(
        tmp_path, "field-dataset-switch", previous=previous, selection=selection, detail=detail
    )
    projection = execution_projection(detail)
    assert projection["dataset_ref"] == selected
    # OTHER also has 时长（分钟）; same spelling alone is not a current binding.
    assert require_field_context(projection)["column"] is None
