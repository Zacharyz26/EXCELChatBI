"""Cross-layer standard answer: cached-formula workbook through BI outputs."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, cast

from mcp_servers.chart.server import build_server as build_chart_server
from mcp_servers.dataset_ops.tools import aggregate_preview
from mcp_servers.excel_parser.tools import infer_schema, parse_excel
from mcp_servers.report.server import build_server as build_report_server
from mcp_servers.stats.tools import trend_analysis
from openpyxl import Workbook

from tests.xlsx_fixtures import inject_formula_caches


def _standard_workbook() -> bytes:
    source = io.BytesIO()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "sales"
    sheet.append(["日期", "地区", "销售额", "销售额翻倍"])
    for index in range(10):
        row = index + 2
        amount = 10 + index * 2
        sheet.append(
            [
                f"2025-01-{index + 1:02d}",
                "A" if index % 2 == 0 else "B",
                amount,
                f"=C{row}*2",
            ]
        )
    workbook.save(source)

    return inject_formula_caches(
        source.getvalue(),
        worksheet_name="sales",
        cached_values={
            f"D{index + 2}": (10 + index * 2) * 2 for index in range(10)
        },
    )


def test_workbook_to_stats_chart_and_report_standard_answer(tmp_path: Path) -> None:
    workbook_path = tmp_path / "standard-answer.xlsx"
    workbook_path.write_bytes(_standard_workbook())

    parsed = parse_excel({"file_ref": str(workbook_path), "sheet": "sales"})
    dataset_ref = str(parsed["dataset_ref"])
    profile = infer_schema({"dataset_ref": dataset_ref}).to_dict()
    aggregate = aggregate_preview(
        {
            "dataset_ref": dataset_ref,
            "group_col": "地区",
            "value_col": "销售额翻倍",
            "agg": "sum",
            "sort": "group",
        }
    )
    trend = trend_analysis(
        {
            "dataset_ref": dataset_ref,
            "value_col": "销售额翻倍",
            "time_col": "日期",
            "method": "ma",
            "forecast_horizon": 2,
        }
    )
    chart = cast(
        dict[str, Any],
        build_chart_server()
        ._tools["gen_chart"]
        .invoke(
            {
                "dataset_ref": dataset_ref,
                "chart_type": "bar",
                "encoding": {"x": "地区", "y": "销售额翻倍", "agg": "sum"},
            }
        ),
    )
    report = cast(
        dict[str, Any],
        build_report_server()
        ._tools["gen_report_md"]
        .invoke(
            {
                "title": "标准答案",
                "profile": profile,
                "stats": [{"kind": "trend_analysis", "result": trend}],
            }
        ),
    )

    assert parsed["row_count"] == 10
    assert profile["row_count"] == 10
    assert aggregate["rows"] == [
        {"group": "A", "value": 180.0, "count": 5, "row_count": 5, "valid_value_count": 5},
        {"group": "B", "value": 200.0, "count": 5, "row_count": 5, "valid_value_count": 5},
    ]
    assert trend["slope"] == 4.0
    assert trend["slope_unit"] == "value_per_day"
    assert chart["option"]["xAxis"]["data"] == ["B", "A"]
    assert chart["option"]["series"][0]["data"] == [200.0, 180.0]
    assert "斜率 4 数值/日" in report["markdown"]
