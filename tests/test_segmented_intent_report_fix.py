"""Frozen M1/M8–M12 contracts. Real tools/SQLite; only model turns scripted."""

import json
from dataclasses import asdict

import pytest
from apps.api.deps import model_gateway_dep
from apps.orchestrator.request_intent import requests_chart, requests_pdf, requests_report
from fastapi.testclient import TestClient
from packages.common.dataset_store import load_dataframe
from packages.models.types import ModelResponse, ToolCall
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
    resolve_analysis_slot,
)
from packages.session.task_store import TaskStore

from tests import test_r4_context_contract as support
from tests import test_r4_report_consumption_real_frozen as report_support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_local_chain_draft import artifact_id
from tests.test_r4_report_consumption_real_frozen import (
    one_tool,
    report_attempt,
)

actual_sources = report_support.actual_sources

METRIC = "播出时长（分钟）"
COLUMNS = ("月份", "频道", "状态", METRIC)
ROLES = (
    ColumnRoleBinding("月份", "time"),
    ColumnRoleBinding("频道", "dimension"),
    ColumnRoleBinding("状态", "dimension"),
    ColumnRoleBinding(METRIC, "metric"),
)
FILTER_GOAL = "请只保留频道为A的节目播出记录，并说明筛选结果。"
SOURCE_GOAL = (
    "请使用刚才的月度汇总表和折线图，生成一份PDF报告，写明播出时长的单位和整个期间的总计。"
)
NEW_GOAL = (
    "请基于当前文件按月份汇总播出时长（分钟），生成包含月度汇总表、期间总计和折线图的PDF报告。"
)


def compile_goal(goal):
    return explicit_analysis_requirements(
        goal, columns=COLUMNS, dataset_refs=("a" * 32,), column_roles=ROLES
    )


@pytest.mark.parametrize("prefix,current", [("", True), ("不要", False), ("稍后", False)])
def test_keep_condition_has_current_local_owner(prefix, current):
    goal = FILTER_GOAL if not prefix else f"{prefix}保留频道为A的节目播出记录，并说明筛选结果。"
    req = compile_goal(goal)
    active = req.active_operations()
    assert bool(active) is current
    if current:
        assert not req.needs_clarification(), req.to_dict()
        assert req.filters == ({"column": "频道", "op": "==", "value": "A"},)


@pytest.mark.parametrize("goal", [SOURCE_GOAL, NEW_GOAL])
def test_report_creation_survives_material_clause_and_required_total(goal):
    req = compile_goal(goal)
    assert requests_report(goal) and requests_pdf(goal)
    assert not req.needs_clarification(), req.to_dict()
    assert req.total and req.total_column == METRIC
    active = req.active_operations()
    assert len(active) == (1 if goal == NEW_GOAL else 0)
    assert requests_chart(goal) is (goal == NEW_GOAL)


@pytest.mark.parametrize(
    "goal",
    [
        "不要生成包含月度汇总表、总计和折线图的PDF报告。",
        "稍后生成包含月度汇总表、总计和折线图的PDF报告。",
        "说明‘生成包含月度汇总表、总计和折线图的PDF报告’的意思。",
    ],
)
def test_non_current_report_does_not_authorize_creation(goal):
    assert not requests_report(goal)


def test_explicit_recompute_is_not_a_consumed_report_operand():
    goal = "使用刚才的折线图，重新按月份汇总播出时长（分钟），生成包含月度汇总表和总计的PDF报告。"
    req = compile_goal(goal)
    assert len(req.active_operations()) == 1
    assert req.requires_operation("aggregate") and req.total
    assert not requests_chart(goal)
    assert requests_report(goal)


def test_operation_choice_is_a_real_resolvable_frozen_slot():
    req = compile_goal("清洗播出时长（分钟）。")
    pending = next(q for q in req.clarifications if q.slot == "operation")
    assert set(pending.candidates) == {"drop_nulls", "drop_duplicates"}
    resolved = resolve_analysis_slot(
        req,
        slot="operation",
        answer="drop_nulls",
        operation_id=pending.operation_id,
        expected_revision=req.revision,
        columns=COLUMNS,
    )
    assert not resolved.needs_clarification(), resolved.to_dict()
    assert resolved.active_operations()[0].kind == "drop_nulls"
    assert resolved.active_operations()[0].parameters.columns == (METRIC,)
    assert resolved.dataset_ref == req.dataset_ref and resolved.source_hash == req.source_hash
    with pytest.raises(ValueError):
        resolve_analysis_slot(
            req, slot="operation", answer="filter", operation_id=pending.operation_id
        )


@pytest.mark.asyncio
async def test_original_keep_goal_reaches_actual_tool_and_frozen_child(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    source = support.upload(store, project, conversation)
    model = support.ScriptedModel(
        [
            (
                "transform_dataset",
                {"dataset_ref": source, "filters": [{"column": "频道", "op": "==", "value": "A"}]},
            )
        ],
        "已完成筛选。",
    )
    result = await support.execute(store, project, conversation, model, FILTER_GOAL)
    support.write_witness(tmp_path, "original-keep", result=result, model=model.messages)
    assert result["run"]["status"] == "completed", result
    calls = [c for c in result["invocations"] if c["status"] == "succeeded"]
    assert len(calls) == 1 and calls[0]["tool_name"] == "transform_dataset"
    child = result["evidence"][0]["summary"]["fact_projection"]["output_ref"]
    frame = load_dataframe(child)
    assert len(frame) == 5 and set(frame["频道"]) == {"A"}
    assert store.get_dataset(child).parent_ref == source
    assert (
        TaskStore(store.db_path).get_verified_contract(result["run"]["run_id"])["goal"]
        == FILTER_GOAL
    )


@pytest.mark.asyncio
async def test_original_recent_material_report_uses_actual_proven_sources(actual_sources, tmp_path):
    result = await report_attempt(actual_sources, tmp_path, goal=SOURCE_GOAL)
    assert result["run"]["status"] == "completed", result
    calls = [c for c in result["invocations"] if c["status"] == "succeeded"]
    assert [c["tool_name"] for c in calls] == ["generate_report"]
    assert set(calls[0]["args"]["analysis_ids"]) == set(actual_sources["ids"])
    assert result["contract"]["analysis_requirements"]["dataset_ref"] == actual_sources["ref"]
    assert result["contract"]["analysis_requirements"]["total"]


@pytest.mark.asyncio
async def test_recent_materials_do_not_borrow_another_conversation(actual_sources, tmp_path):
    source = actual_sources
    other = source["store"].create_conversation(source["project"].id)
    resolved = support.resolve(source["store"], source["project"], other, SOURCE_GOAL)
    support.write_witness(tmp_path, "foreign-materials", result=asdict(resolved))
    assert resolved.status in {"unresolved", "ambiguous"}
    assert not resolved.targets


@pytest.mark.asyncio
async def test_formal_reference_answer_rebinds_original_report_without_losing_total(
    actual_sources, tmp_path
):
    source = actual_sources
    # A second real table creates an actual ambiguous deictic reference. No
    # Artifact, Evidence, waiting state or completion marker is hand-authored.
    await one_tool(
        source["store"],
        source["project"],
        source["conversation"],
        tmp_path,
        "second-month-table",
        "aggregate_preview",
        {"dataset_ref": source["ref"], "group_col": "月份", "value_col": METRIC, "agg": "sum"},
        f"按月份汇总{METRIC}。",
        "已完成汇总。",
    )
    goal = SOURCE_GOAL.replace("刚才的", "上述")
    model = support.ScriptedModel(
        [
            (
                "generate_report",
                {"analysis_ids": source["ids"], "title": "澄清后的合成报告", "include_pdf": True},
            )
        ],
        "已生成新的分析报告。",
    )
    initial = await support.execute(
        source["store"], source["project"], source["conversation"], model, goal
    )
    tasks = TaskStore(source["store"].db_path)
    run = tasks.get_run(initial["run"]["run_id"])
    assert run.status == "waiting_user" and model.calls == 0, initial
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert waiting["question_id"] == "reference_target", waiting
    answer = (
        "月度汇总表使用 analysis_id="
        + source["ids"][0]
        + "；折线图使用 analysis_id="
        + source["ids"][1]
        + "。请把这两个已生成的产物纳入报告。"
    )
    app = support.application(source["store"])
    app.dependency_overrides[model_gateway_dep] = lambda: model
    with TestClient(app) as client:
        response = client.post(
            f'/agent/runs/{run.run_id}/clarifications/{waiting["question_id"]}/answer/stream',
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "segmented-report-reference-answer",
            },
            json={"answer": answer, "resume_token": waiting["resume_token"]},
        )
    final = tasks.get_run(run.run_id)
    saved = tasks.get_verified_contract(run.run_id)
    support.write_witness(
        tmp_path,
        "reference-answer",
        initial=initial,
        waiting=waiting,
        response_status=response.status_code,
        response=response.text,
        final=asdict(final),
        contract=saved,
        calls=[asdict(x) for x in tasks.list_invocations(run.run_id)],
    )
    assert response.status_code == 200 and final.status == "completed"
    assert saved["goal"] == goal
    req = saved["analysis_requirements"]
    assert req["dataset_ref"] == source["ref"] and req["total_column"] == METRIC and req["total"]
    assert not req["clarifications"]
    assert len(tasks.list_invocations(run.run_id)) == 1


@pytest.mark.asyncio
async def test_prior_defer_does_not_override_later_current_filter(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    source = support.upload(store, project, conversation)
    first = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel([("get_data_profile", {"dataset_ref": source})], "已完成画像。"),
        "请查看数据画像，暂不筛选或清洗。",
    )
    assert first["run"]["status"] == "completed", first
    second = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                (
                    "transform_dataset",
                    {
                        "dataset_ref": source,
                        "filters": [{"column": "频道", "op": "==", "value": "A"}],
                    },
                )
            ],
            "已完成筛选。",
        ),
        FILTER_GOAL,
    )
    support.write_witness(tmp_path, "later-explicit-filter", first=first, second=second)
    assert second["run"]["status"] == "completed", second
    assert len([x for x in second["invocations"] if x["status"] == "succeeded"]) == 1


@pytest.mark.asyncio
async def test_operation_choice_real_api_preserves_bound_column(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    ref = support.upload(store, project, conversation)
    goal = "清洗时长（分钟）。"
    model = support.ScriptedModel(
        [("transform_dataset", {"dataset_ref": ref, "drop_nulls": [support.METRIC]})],
        "已完成清洗。",
    )
    initial = await support.execute(store, project, conversation, model, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(initial["run"]["run_id"])
    assert run.status == "waiting_user" and model.calls == 0, initial
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert waiting["analysis_slot_request"]["slot"] == "operation"
    assert set(waiting["answer_schema"]["enum"]) == {"drop_nulls", "drop_duplicates"}
    app = support.application(store)
    app.dependency_overrides[model_gateway_dep] = lambda: model
    with TestClient(app) as client:
        response = client.post(
            f'/agent/runs/{run.run_id}/clarifications/{waiting["question_id"]}/answer/stream',
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "segmented-operation-answer",
            },
            json={"answer": "drop_nulls", "resume_token": waiting["resume_token"]},
        )
    final = tasks.get_run(run.run_id)
    support.write_witness(
        tmp_path,
        "operation-api-answer",
        waiting=waiting,
        response=response.text,
        final=asdict(final),
        contract=tasks.get_verified_contract(run.run_id),
        calls=[asdict(x) for x in tasks.list_invocations(run.run_id)],
    )
    assert response.status_code == 200 and final.status == "completed"
    calls = tasks.list_invocations(run.run_id)
    assert len(calls) == 1 and calls[0].status == "succeeded"
    assert calls[0].args == {"dataset_ref": ref, "drop_nulls": [support.METRIC]}


@pytest.mark.asyncio
async def test_global_operation_without_executable_answer_is_read_only(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    support.upload(store, project, conversation)
    goal = f"按月份汇总{support.METRIC}；按频道计算{support.METRIC}的平均值。"
    model = support.ScriptedModel([], "不应开始模型调用。")
    initial = await support.execute(store, project, conversation, model, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(initial["run"]["run_id"])
    assert run.status == "waiting_user" and model.calls == 0, initial
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert waiting["question_id"] == "analysis:operation:general", waiting
    assert waiting["analysis_slot_request"]["candidates"] == []
    app = support.application(store)
    app.dependency_overrides[model_gateway_dep] = lambda: model
    with TestClient(app) as client:
        response = client.post(
            f'/agent/runs/{run.run_id}/clarifications/{waiting["question_id"]}/answer/stream',
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "segmented-operation-unsupported-answer",
            },
            json={"answer": "aggregate", "resume_token": waiting["resume_token"]},
        )
    support.write_witness(
        tmp_path,
        "operation-read-only",
        initial=initial,
        waiting=waiting,
        response_status=response.status_code,
        response=response.text,
        final=asdict(tasks.get_run(run.run_id)),
    )
    assert response.status_code == 409
    assert tasks.get_run(run.run_id).state_version == run.state_version
    assert model.calls == 0 and tasks.list_invocations(run.run_id) == []
    assert waiting["answer_schema"].get("readOnly") is True, waiting
    assert "取消" in waiting["question"]


@pytest.mark.asyncio
async def test_original_new_report_goal_executes_real_aggregate_chart_and_pdf(
    actual_sources, tmp_path
):
    source = actual_sources
    store, conversation = source["store"], source["conversation"]
    original_ids = {a.id for a in store.list_artifacts(conversation.id)}

    class Pipeline:
        calls = 0

        async def stream_turn(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                name, args = (
                    "aggregate_preview",
                    {
                        "dataset_ref": source["ref"],
                        "group_col": "月份",
                        "value_col": METRIC,
                        "agg": "sum",
                    },
                )
            elif self.calls == 2:
                name, args = (
                    "gen_chart",
                    {
                        "dataset_ref": source["ref"],
                        "chart_type": "line",
                        "encoding": {"x": "月份", "y": METRIC, "agg": "sum"},
                    },
                )
            elif self.calls == 3:
                artifacts = [
                    a for a in store.list_artifacts(conversation.id) if a.id not in original_ids
                ]
                name, args = (
                    "generate_report",
                    {
                        "analysis_ids": [artifact_id(a) for a in artifacts],
                        "title": "独立原任务合成报告",
                        "include_pdf": True,
                    },
                )
            else:
                yield ModelResponse(
                    content=f"{METRIC}总计为78。已生成新的分析报告。",
                    model="offline-report-pipeline",
                )
                return
            yield ModelResponse(
                content="",
                model="offline-report-pipeline",
                tool_calls=[ToolCall(f"pipeline-{self.calls}", name, json.dumps(args))],
            )

    result = await support.execute(store, source["project"], conversation, Pipeline(), NEW_GOAL)
    support.write_witness(tmp_path, "original-new-report-pipeline", result=result)
    assert result["run"]["status"] == "completed", result
    assert [x["tool_name"] for x in result["invocations"] if x["status"] == "succeeded"] == [
        "aggregate_preview",
        "gen_chart",
        "generate_report",
    ]
    assert result["contract"]["goal"] == NEW_GOAL
    assert any(
        x["artifact_format"] == "pdf" and x["required"]
        for x in result["contract"]["success_criteria"]
    )
