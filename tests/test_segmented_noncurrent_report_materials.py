"""The author's unchanged noncurrent request must not create child duties."""

import pytest
from apps.orchestrator.request_intent import requests_chart, requests_report
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
)


@pytest.mark.parametrize("prefix", ["不要", "稍后"])
def test_noncurrent_report_materials_do_not_become_current_children(prefix):
    goal = f"{prefix}生成包含月度汇总表、总计和折线图的PDF报告。"
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
    assert not requests_chart(
        goal
    ), "A material of a deferred/prohibited report is not a chart request"
    assert not requirements.active_operations(), requirements.to_dict()
    assert not requirements.total, "A deferred/prohibited report cannot require a current total"
