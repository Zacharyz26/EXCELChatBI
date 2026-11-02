"""Independent current-source table-grain mismatch; only root executes."""

from __future__ import annotations

from decimal import Decimal

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
async def test_existing_monthly_table_cannot_be_replaced_by_real_channel_table(
    actual_sources,  # noqa: F811 - imported pytest fixture
    tmp_path,
):
    source = actual_sources
    _, call = await one_tool(
        source["store"],
        source["project"],
        source["conversation"],
        tmp_path,
        "setup-independent-real-channel-aggregate",
        "aggregate_preview",
        {
            "dataset_ref": source["ref"],
            "group_col": "频道",
            "value_col": METRIC,
            "agg": "sum",
        },
        f"请按频道汇总{METRIC}并给出总计。",
        f"{METRIC}总计为78。",
    )
    artifacts = source["store"].list_artifacts(source["conversation"].id)
    channel_table = next(a for a in artifacts if a.id == call["artifact_id"])
    monthly_table = source["table"]
    assert channel_table.id != monthly_table.id
    assert channel_table.dataset_ref == monthly_table.dataset_ref == source["ref"]
    assert channel_table.params["group_col"] == "频道"
    assert monthly_table.params["group_col"] == "月份"
    assert len(channel_table.payload["rows"]) == 2
    assert {row["group"]: Decimal(str(row["value"])) for row in channel_table.payload["rows"]} == {
        "A": Decimal(60),
        "B": Decimal(18),
    }
    assert Decimal(str(channel_table.payload["total_value"])) == Decimal(78)
    assert Decimal(str(monthly_table.payload["total_value"])) == Decimal(78)
    chosen = [artifact_id(channel_table), artifact_id(source["chart"])]
    assert artifact_id(monthly_table) not in chosen

    result = await report_attempt(source, tmp_path, goal=ORIGINAL, ids=chosen)
    verdict, reports, *_ = full_verdict(source, result)
    support.write_witness(
        tmp_path,
        "independent-existing-table-grain",
        request=ORIGINAL,
        required_monthly_table=artifact_id(monthly_table),
        selected_channel_table=artifact_id(channel_table),
        chosen_analysis_ids=chosen,
        equal_total=78,
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
