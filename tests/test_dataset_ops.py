"""dataset_ops 工具测试：白名单变换（决策3修订）+ 聚合预览。

红线2：所有数字来自真实数据计算；红线3：经 Tool.invoke 的 schema 校验拒绝越界入参。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.dataset_ops.server import build_server  # noqa: E402
from mcp_servers.dataset_ops.tools import (  # noqa: E402
    aggregate_preview,
    transform_dataset,
)
from packages.common.dataset_store import (  # noqa: E402
    duplicate_row_count,
    load_dataframe,
    save_dataframe,
)
from packages.governance.schema_validator import SchemaValidationError  # noqa: E402


@pytest.fixture
def sales_ref() -> str:
    """6 行销售数据：含空值、重复行、可过滤维度。"""
    df = pd.DataFrame(
        {
            "地区": ["华东", "华南", "华东", "华北", "华东", "华南"],
            "销量": [100.0, 200.0, 100.0, np.nan, 300.0, 250.0],
            "渠道": ["线上", "线下", "线上", "线上", "线下", None],
        }
    )
    # index 0 与 2 整行完全相同（重复行）
    df.loc[2] = df.loc[0]
    return save_dataframe(df)


# ── transform_dataset：各操作 ──


def test_filter_eq_and_gt(sales_ref: str) -> None:
    out = transform_dataset(
        {
            "dataset_ref": sales_ref,
            "filters": [
                {"column": "地区", "op": "==", "value": "华东"},
                {"column": "销量", "op": ">", "value": 100},
            ],
        }
    )
    df = load_dataframe(out["dataset_ref"])
    assert out["rows_before"] == 6
    assert out["rows_after"] == len(df) == 1
    assert df.iloc[0]["销量"] == 300.0
    assert out["parent_ref"] == sales_ref  # 血缘字段


def test_filter_in_contains_null(sales_ref: str) -> None:
    assert transform_dataset(
        {"dataset_ref": sales_ref, "filters": [{"column": "地区", "op": "in", "value": ["华南"]}]}
    )["rows_after"] == 2
    contains = {"column": "渠道", "op": "contains", "value": "线上"}
    assert transform_dataset(
        {"dataset_ref": sales_ref, "filters": [contains]}
    )["rows_after"] == 3
    assert transform_dataset(
        {"dataset_ref": sales_ref, "filters": [{"column": "渠道", "op": "is_null"}]}
    )["rows_after"] == 1


def test_drop_nulls_and_duplicates(sales_ref: str) -> None:
    # 指定列去空：销量为 NaN 的 1 行被去
    assert transform_dataset({"dataset_ref": sales_ref, "drop_nulls": ["销量"]})["rows_after"] == 5
    # 空数组 = 任一列为空即去（销量 NaN + 渠道 None 共 2 行）
    assert transform_dataset({"dataset_ref": sales_ref, "drop_nulls": []})["rows_after"] == 4
    # 整行去重：index 0/2 相同，去掉 1 行
    assert transform_dataset({"dataset_ref": sales_ref, "drop_duplicates": []})["rows_after"] == 5


def test_sort_and_exclude_rows(sales_ref: str) -> None:
    out = transform_dataset(
        {
            "dataset_ref": sales_ref,
            "exclude_row_indices": [3],  # 排除 NaN 行（模拟"排除异常值"）
            "sort": [{"column": "销量", "order": "desc"}],
        }
    )
    df = load_dataframe(out["dataset_ref"])
    assert out["rows_after"] == 5
    assert df.iloc[0]["销量"] == 300.0  # 降序首行
    assert list(out["transform"].keys()) == ["exclude_row_indices", "sort"]


def test_derived_dataset_is_new_parquet(sales_ref: str) -> None:
    """衍生数据集是独立落盘的新 parquet，源数据集不被修改。"""
    out = transform_dataset({"dataset_ref": sales_ref, "drop_nulls": []})
    assert out["dataset_ref"] != sales_ref
    assert len(load_dataframe(sales_ref)) == 6  # 源不变


# ── transform_dataset：拒绝路径 ──


def test_no_operation_rejected(sales_ref: str) -> None:
    with pytest.raises(ValueError, match="至少一个变换操作"):
        transform_dataset({"dataset_ref": sales_ref})


def test_unknown_column_rejected(sales_ref: str) -> None:
    with pytest.raises(ValueError, match="列不存在"):
        transform_dataset(
            {"dataset_ref": sales_ref, "filters": [{"column": "不存在", "op": "==", "value": 1}]}
        )


def test_incompatible_comparison_returns_actionable_error(sales_ref: str) -> None:
    with pytest.raises(ValueError, match="无法做 > 比较"):
        transform_dataset(
            {"dataset_ref": sales_ref, "filters": [{"column": "地区", "op": ">", "value": 1}]}
        )


def test_empty_result_rejected(sales_ref: str) -> None:
    with pytest.raises(ValueError, match="为空"):
        transform_dataset(
            {"dataset_ref": sales_ref, "filters": [{"column": "销量", "op": ">", "value": 9999}]}
        )


def test_exclude_out_of_range_rejected(sales_ref: str) -> None:
    with pytest.raises(ValueError, match="超出范围"):
        transform_dataset({"dataset_ref": sales_ref, "exclude_row_indices": [99]})


def test_schema_rejects_unknown_op_and_extra_keys(sales_ref: str) -> None:
    """红线3：白名单外的算子/字段在 Tool.invoke 即被拒，触不到执行体。"""
    tool = build_server()._tools["transform_dataset"]
    with pytest.raises(SchemaValidationError):
        tool.invoke(
            {"dataset_ref": sales_ref, "filters": [{"column": "销量", "op": "regex", "value": "."}]}
        )
    with pytest.raises(SchemaValidationError):
        tool.invoke({"dataset_ref": sales_ref, "sql": "DROP TABLE x"})  # 无自由 SQL 入口


# ── aggregate_preview ──


def test_aggregate_sum_sorted(sales_ref: str) -> None:
    out = aggregate_preview(
        {"dataset_ref": sales_ref, "group_col": "地区", "value_col": "销量", "agg": "sum"}
    )
    assert out["rows"][0] == {"group": "华东", "value": 500.0, "count": 3}  # 默认 value_desc
    assert out["group_total"] == 3
    assert out["truncated"] is False


def test_aggregate_count_without_value_col(sales_ref: str) -> None:
    out = aggregate_preview({"dataset_ref": sales_ref, "group_col": "地区", "agg": "count"})
    by_group = {r["group"]: r["value"] for r in out["rows"]}
    assert by_group == {"华东": 3.0, "华南": 2.0, "华北": 1.0}
    assert out["value_col"] is None


def test_aggregate_limit_truncates(sales_ref: str) -> None:
    out = aggregate_preview(
        {"dataset_ref": sales_ref, "group_col": "地区", "agg": "count", "limit": 2, "sort": "group"}
    )
    assert len(out["rows"]) == 2
    assert out["truncated"] is True
    assert [r["group"] for r in out["rows"]] == ["华东", "华北"]  # sort=group 按组名


def test_aggregate_sum_requires_value_col(sales_ref: str) -> None:
    with pytest.raises(ValueError, match="value_col"):
        aggregate_preview({"dataset_ref": sales_ref, "group_col": "地区", "agg": "sum"})


def test_aggregate_schema_rejects_unknown_agg(sales_ref: str) -> None:
    tool = build_server()._tools["aggregate_preview"]
    with pytest.raises(SchemaValidationError):
        tool.invoke({"dataset_ref": sales_ref, "group_col": "地区", "agg": "median"})


# ── dataset_store 新增 ──


def test_duplicate_row_count(sales_ref: str) -> None:
    assert duplicate_row_count(sales_ref) == 1  # index 0/2 整行相同
