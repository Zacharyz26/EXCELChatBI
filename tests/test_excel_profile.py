"""DataProfile 生成测试：类型推断、空值率、统计摘要、样本行。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.excel_parser.tools import infer_schema, parse_excel  # noqa: E402


@pytest.fixture
def sales_xlsx(tmp_path: Path) -> Path:
    """造一个含空值、数值列、类目列、时间列的 Excel。"""
    df = pd.DataFrame(
        {
            "月份": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01", "2024-04-01"]),
            "区域": ["华东", "华东", "华北", "华北"],
            "销售额": [1200.5, 1800.0, None, 900.0],  # 25% 空值
            "订单数": [30, 45, 22, 18],
        }
    )
    path = tmp_path / "sales.xlsx"
    df.to_excel(path, index=False)
    return path


def test_profile_generation(sales_xlsx: Path) -> None:
    parsed = parse_excel({"file_ref": str(sales_xlsx)})
    assert parsed["row_count"] == 4
    assert parsed["column_count"] == 4

    profile = infer_schema({"dataset_ref": parsed["dataset_ref"]})
    by_name = {c.name: c for c in profile.columns}

    # 类型推断
    assert by_name["月份"].dtype == "datetime"
    assert by_name["区域"].dtype == "str"
    assert by_name["销售额"].dtype == "float"
    assert by_name["订单数"].dtype == "int"

    # 空值率
    assert by_name["销售额"].null_ratio == 0.25
    assert by_name["订单数"].null_ratio == 0.0

    # 数值列统计摘要（describe）
    sales = by_name["销售额"]
    assert sales.min == 900.0
    assert sales.max == 1800.0
    assert sales.median == 1200.5

    # 样本行（前5，此处共4行），且类目列不带统计摘要
    assert len(profile.sample_rows) == 4
    assert by_name["区域"].mean is None


def test_nrows_limits_rows(sales_xlsx: Path) -> None:
    parsed = parse_excel({"file_ref": str(sales_xlsx), "nrows": 2})
    assert parsed["row_count"] == 2
