"""Independent scope and original-source ownership examples, frozen before run."""

from dataclasses import asdict

import pytest
from apps.orchestrator.request_intent import requests_chart, requests_report
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
)

from tests import test_r4_context_contract as support
from tests import test_r4_report_consumption_real_frozen as report_support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_local_chain_draft import api_request, conversation_detail

actual_sources = report_support.actual_sources
METRIC = "播出时长（分钟）"
COLUMNS = ("月份", "频道", "状态", METRIC)
ROLES = (
    ColumnRoleBinding("月份", "time"),
    ColumnRoleBinding("频道", "dimension"),
    ColumnRoleBinding("状态", "dimension"),
    ColumnRoleBinding(METRIC, "metric"),
)


def requirements(goal):
    return explicit_analysis_requirements(
        goal, columns=COLUMNS, dataset_refs=("a" * 32,), column_roles=ROLES
    )


def test_keep_and_conditions_are_both_owned():
    req = requirements("只保留频道为A且状态为已播出的记录。")
    assert not req.needs_clarification(), req.to_dict()
    assert req.filters == (
        {"column": "频道", "op": "==", "value": "A"},
        {"column": "状态", "op": "==", "value": "已播出"},
    )


def test_quote_internal_relative_record_text_is_literal():
    req = requirements('只保留频道为"A的记录"的记录。')
    assert not req.needs_clarification(), req.to_dict()
    assert req.filters == ({"column": "频道", "op": "==", "value": "A的记录"},)


@pytest.mark.parametrize(
    "goal",
    [
        "只保留频道为A的记录，并且状态为已播出。",
        "只保留频道为A或状态为已播出的记录。",
    ],
)
def test_partial_or_disjunction_never_authorizes_only_first_filter(goal):
    req = requirements(goal)
    assert req.needs_clarification(), req.to_dict()


def test_materials_do_not_swallow_an_explicit_new_aggregate():
    goal = (
        "使用刚才的月度汇总表和折线图，重新按频道汇总播出时长（分钟），"
        "生成包含汇总表和期间总计的PDF报告。"
    )
    req = requirements(goal)
    assert not req.needs_clarification(), req.to_dict()
    assert [(op.kind, op.disposition) for op in req.active_operations()] == [
        ("aggregate", "current")
    ]
    assert req.group_col == "频道" and req.value_col == METRIC and req.total
    assert req.aggregation == "sum"
    assert requests_report(goal) and not requests_chart(goal)


async def add_newer_other_chart(source, tmp_path):
    # A real later upload and chart give the legacy whole-history resolver a
    # tempting but wrong source. No Artifact/Evidence is hand-authored.
    body, _rows, _monthly = report_support.synthetic_csv()
    uploaded = await api_request(
        source["store"],
        "POST",
        "/upload/excel",
        data={"project_id": source["project"].id, "conversation_id": source["conversation"].id},
        files={"file": ("other-independent-source.csv", body, "text/csv")},
    )
    assert uploaded.status_code == 200, uploaded.text
    other = uploaded.json()["dataset_ref"]
    assert other != source["ref"]
    await report_support.one_tool(
        source["store"],
        source["project"],
        source["conversation"],
        tmp_path,
        "other-real-chart",
        "gen_chart",
        {
            "dataset_ref": other,
            "chart_type": "line",
            "encoding": {"x": "月份", "y": METRIC, "agg": "sum"},
        },
        f"为当前数据按月份生成{METRIC}折线图。",
        "已生成图表。",
    )
    return other


@pytest.mark.asyncio
@pytest.mark.parametrize("selector", ["当前", "刚才的"])
async def test_shared_report_source_does_not_reenter_whole_history_fallback(
    actual_sources, tmp_path, selector
):
    source = actual_sources
    other = await add_newer_other_chart(source, tmp_path)
    selection = await support.execute(
        source["store"],
        source["project"],
        source["conversation"],
        support.ScriptedModel([], "已选择数据集。"),
        f'接下来的分析请使用数据集（dataset_ref: {source["ref"]}）。',
    )
    assert selection["run"]["status"] == "completed", selection
    context = support.execution_projection(
        await conversation_detail(source["store"], source["conversation"])
    )
    assert context["dataset_ref"] == source["ref"]
    goal = f"请使用{selector}月度汇总表和图表，生成一份PDF报告。"
    resolved = support.resolve(source["store"], source["project"], source["conversation"], goal)
    support.write_witness(
        tmp_path,
        "material-resolution",
        goal=goal,
        current=context,
        other=other,
        resolution=asdict(resolved),
    )
    assert resolved.status == "resolved", resolved
    assert {t.dataset_ref for t in resolved.targets} == {source["ref"]}
    assert {t.reference_id for t in resolved.targets if t.kind == "artifact"} == {
        source["table"].id,
        source["chart"].id,
    }


@pytest.mark.asyncio
async def test_missing_current_materials_never_borrow_latest_history(actual_sources, tmp_path):
    source = actual_sources
    other = await add_newer_other_chart(source, tmp_path)
    # B has a chart but no table. The request remains unresolved rather than
    # combining B's chart with A's genuine monthly table.
    goal = "请使用刚才的月度汇总表和图表，生成一份PDF报告。"
    resolved = support.resolve(source["store"], source["project"], source["conversation"], goal)
    support.write_witness(
        tmp_path, "missing-materials", goal=goal, current_ref=other, resolution=asdict(resolved)
    )
    assert resolved.status in {"unresolved", "ambiguous"}
    assert all(t.kind == "dataset" and t.dataset_ref == other for t in resolved.targets)
