"""Independent intent/plan expectations, frozen before implementation.

NOT_RUN. These are semantic compiler/planner tests, not provenance or Agent
acceptance. The small context below is a capability descriptor, never Evidence.
Root separately runs actual Host/SQLite source-positive and stale-source cases.
"""

from __future__ import annotations

import pytest
from apps.orchestrator.control.task_outline import build_deterministic_outline
from apps.orchestrator.request_intent import requests_chart, requests_pdf, requests_report
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
)
from packages.orchestration.contracts import build_minimal_contract

METRIC = "播出时长（分钟）"
REF = "a" * 32
COLUMNS = ("月份", METRIC)
ROLES = (ColumnRoleBinding("月份", "time"), ColumnRoleBinding(METRIC, "metric"))
ORIGINAL = "用当前已生成的月度汇总表和折线图生成新的 PDF 报告，保留月度数据、总计及单位。"

# Each expected role is a human contract judgment, not a compiler-derived oracle.
CASES = [
    pytest.param(ORIGINAL, False, False, True, id="original-existing-table-chart-new-report"),
    pytest.param(
        "使用已有的月度汇总表生成新的 PDF 报告，保留总计。",
        False,
        False,
        True,
        id="existing-table-total-is-delivery",
    ),
    pytest.param(
        "重新按月份汇总播出时长（分钟），计算总计，生成折线图并生成新的 PDF 报告。",
        True,
        True,
        True,
        id="explicit-recompute-and-new-chart-still-required",
    ),
    pytest.param(
        "使用已有的月度汇总表，重新生成折线图，再生成新的 PDF 报告。",
        False,
        True,
        False,
        id="consume-table-but-create-chart",
    ),
    pytest.param(
        "使用已有的折线图，重新按月份汇总播出时长（分钟）并计算总计，生成新的 PDF 报告。",
        True,
        False,
        True,
        id="consume-chart-but-recompute-table",
    ),
    pytest.param(
        "按月份汇总播出时长（分钟），生成折线图并生成新的 PDF 报告。",
        True,
        True,
        False,
        id="new-productions-with-no-consumed-source",
    ),
]


def requirements(goal):
    return explicit_analysis_requirements(
        goal, columns=COLUMNS, dataset_refs=(REF,), column_roles=ROLES
    )


@pytest.mark.parametrize("goal,new_aggregate,new_chart,keep_total", CASES)
def test_report_source_and_creation_roles(goal, new_aggregate, new_chart, keep_total):
    req = requirements(goal)
    assert bool([x for x in req.active_operations() if x.kind == "aggregate"]) is new_aggregate
    assert req.total is keep_total
    if keep_total:
        assert req.total_column == METRIC
    assert requests_chart(goal) is new_chart
    assert requests_report(goal) and requests_pdf(goal)
    assert not req.needs_clarification(), req.to_dict()
    contract = build_minimal_contract(
        run_id="frozen-report-intent",
        user_text=goal,
        chart_required=requests_chart(goal),
        report_required=requests_report(goal),
        pdf_required=requests_pdf(goal),
        columns=COLUMNS,
        dataset_refs=(REF,),
        column_roles=ROLES,
    )
    produced = [x.artifact_type for x in contract.success_criteria if x.kind == "artifact"]
    assert ("chart" in produced) is new_chart
    assert "report" in produced


@pytest.mark.parametrize(
    "goal",
    [
        ORIGINAL,
        "使用已有的月度汇总表生成新的 PDF 报告，保留总计。",
    ],
)
def test_existing_total_requires_report_delivery_not_new_aggregate_plan(goal):
    req = requirements(goal)
    # This planning descriptor proves no data or artifact. No complete status,
    # invocation, Evidence, validation result or report file is fabricated.
    context = {
        "datasets": [{"ref": REF, "filename": "synthetic.csv", "columns": list(COLUMNS)}],
        "artifacts": [
            {
                "artifact_id": "table-descriptor",
                "type": "table",
                "source_tool": "aggregate_preview",
                "dataset_ref": REF,
            },
            {
                "artifact_id": "chart-descriptor",
                "type": "chart",
                "source_tool": "gen_chart",
                "dataset_ref": REF,
            },
        ],
    }
    plan = build_deterministic_outline(
        user_text=goal,
        context=context,
        route="template",
        available_capabilities={"data.aggregate", "visualization.chart", "report.generate"},
        analysis_requirements=req,
    )
    assert [step["capability"] for step in plan["steps"]] == ["report.generate"]
    assert req.total and req.total_column == METRIC
