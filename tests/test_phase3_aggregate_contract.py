"""Phase-3 standard answers for nullable aggregates and report units."""

from __future__ import annotations

import pandas as pd
import pytest
from mcp_servers.dataset_ops.tools import aggregate_preview
from mcp_servers.report import tools as report_tools
from packages.common.dataset_store import save_dataframe


@pytest.mark.parametrize("agg", ["sum", "mean"])
def test_all_null_aggregate_is_not_coerced_to_zero(agg: str) -> None:
    dataset_ref = save_dataframe(
        pd.DataFrame(
            {
                "地区": ["全空", "全空", "真零", "真零"],
                "销量": [None, None, 0.0, 0.0],
            }
        )
    )

    result = aggregate_preview(
        {
            "dataset_ref": dataset_ref,
            "group_col": "地区",
            "value_col": "销量",
            "agg": agg,
            "sort": "group",
        }
    )

    assert result["rows"] == [
        {
            "group": "全空",
            "value": None,
            "count": 0,
            "row_count": 2,
            "valid_value_count": 0,
        },
        {
            "group": "真零",
            "value": 0.0,
            "count": 2,
            "row_count": 2,
            "valid_value_count": 2,
        },
    ]


def test_count_uses_valid_values_and_reports_source_rows() -> None:
    dataset_ref = save_dataframe(pd.DataFrame({"地区": ["华东"] * 3, "销量": [1.0, None, 2.0]}))

    result = aggregate_preview(
        {
            "dataset_ref": dataset_ref,
            "group_col": "地区",
            "value_col": "销量",
            "agg": "count",
        }
    )

    assert result["rows"] == [
        {
            "group": "华东",
            "value": 2.0,
            "count": 2,
            "row_count": 3,
            "valid_value_count": 2,
        }
    ]


def test_trend_report_declares_slope_unit() -> None:
    markdown = "\n".join(
        report_tools._stat_md(
            {
                "kind": "trend_analysis",
                "result": {"direction": "up", "slope": 2.0, "slope_unit": "value_per_day"},
            }
        )
    )
    assert "斜率 2 数值/日" in markdown
