"""A noncurrent report cannot erase an independently authorized outer task."""

from apps.orchestrator.request_intent import requests_chart, requests_report
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
)


def test_deferred_report_does_not_erase_current_outer_aggregate_total():
    goal = (
        "稍后生成包含月度汇总表、总计和折线图的PDF报告。"
        "现在按频道汇总播出时长（分钟），并给出总计。"
    )
    requirements = explicit_analysis_requirements(
        goal,
        columns=("月份", "频道", "播出时长（分钟）"),
        column_roles=(
            ColumnRoleBinding("月份", "time"),
            ColumnRoleBinding("频道", "dimension"),
            ColumnRoleBinding("播出时长（分钟）", "metric"),
        ),
        dataset_refs=("a" * 32,),
    )
    assert not requests_report(goal)
    assert not requests_chart(goal)
    assert not requirements.needs_clarification(), requirements.to_dict()
    assert [op.kind for op in requirements.active_operations()] == ["aggregate"]
    assert requirements.group_col == "频道"
    assert requirements.value_col == "播出时长（分钟）" and requirements.total


def test_prohibited_report_does_not_erase_current_outer_chart():
    goal = "不要生成包含月度汇总表、总计和折线图的PDF报告。现在生成折线图。"
    assert not requests_report(goal)
    assert requests_chart(goal)
