"""Independent same-source wrong-chart-kind check; root executes offline.

The original existing-line-chart request is unchanged.  Both line and bar are
real current-source products; the model deliberately selects only table + bar.
This is a consumer contract check, not a fabricated Artifact/Evidence fixture.
"""

from __future__ import annotations

import pytest

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_report_consumption_real_frozen import (
    METRIC,
    ORIGINAL,
    actual_sources,  # noqa: F401
    artifact_id,
    full_verdict,
    one_tool,
    report_attempt,
)


@pytest.mark.asyncio
async def test_existing_line_chart_cannot_be_replaced_by_real_current_bar(
    actual_sources,  # noqa: F811 - imported pytest fixture
    tmp_path,
):
    source = actual_sources
    _, call = await one_tool(
        source["store"],
        source["project"],
        source["conversation"],
        tmp_path,
        "setup-independent-real-bar",
        "gen_chart",
        {
            "dataset_ref": source["ref"],
            "chart_type": "bar",
            "encoding": {"x": "月份", "y": METRIC, "agg": "sum"},
        },
        f"请为当前数据生成按月份展示{METRIC}的柱状图。",
        "已生成当前数据的月度播出时长柱状图。",
    )
    artifacts = source["store"].list_artifacts(source["conversation"].id)
    bar = next(a for a in artifacts if a.id == call["artifact_id"])
    line = source["chart"]
    assert bar.id != line.id
    assert bar.dataset_ref == line.dataset_ref == source["ref"]
    assert {item["type"] for item in line.payload["option"]["series"]} == {"line"}
    assert {item["type"] for item in bar.payload["option"]["series"]} == {"bar"}
    chosen = [artifact_id(source["table"]), artifact_id(bar)]
    assert artifact_id(line) not in chosen

    result = await report_attempt(source, tmp_path, goal=ORIGINAL, ids=chosen)
    verdict, reports, *_ = full_verdict(source, result)
    support.write_witness(
        tmp_path,
        "independent-existing-chart-kind",
        request=ORIGINAL,
        required_existing_line=artifact_id(line),
        selected_existing_bar=artifact_id(bar),
        chosen_analysis_ids=chosen,
        actual_run_status=result["run"]["status"],
        full_verdict={
            "passed": verdict.passed,
            "issues": [issue.code for issue in verdict.issues],
        },
        report_ids=[report.id for report in reports],
    )
    assert result["model_calls"] > 0, result
    assert result["run"]["status"] != "completed", result
    assert not verdict.passed, verdict
