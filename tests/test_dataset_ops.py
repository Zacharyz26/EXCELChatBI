"""dataset_ops 工具测试：白名单变换、聚合预览与只读 Join 预检。

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

from mcp_servers.dataset_ops import tools as dataset_ops_tools  # noqa: E402
from mcp_servers.dataset_ops.server import build_server  # noqa: E402
from mcp_servers.dataset_ops.tools import (  # noqa: E402
    aggregate_preview,
    join_datasets,
    join_preflight,
    transform_dataset,
)
from packages.common.dataset_store import (  # noqa: E402
    duplicate_row_count,
    load_dataframe,
    load_metadata,
    save_dataframe,
    save_metadata,
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


# ── join_preflight：只读关联门禁 ──


def test_join_preflight_one_to_one_is_ready() -> None:
    left_ref = save_dataframe(pd.DataFrame({"订单号": [1, 2, 3], "金额": [10, 20, 30]}))
    right_ref = save_dataframe(pd.DataFrame({"订单ID": [1, 2, 4], "渠道": ["A", "B", "C"]}))

    out = build_server()._tools["join_preflight"].invoke(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "订单号",
            "right_key": "订单ID",
            "join_type": "inner",
        }
    )

    assert out["status"] == "ready"
    assert out["relationship"] == "one_to_one"
    assert out["matching_key_count"] == 2
    assert out["matched_left_rows"] == 2
    assert out["matched_right_rows"] == 2
    assert out["estimated_output_rows"] == 2
    assert out["risks"] == []
    assert out["mutates_data"] is False
    assert out["raw_rows_returned"] is False
    assert load_dataframe(left_ref).shape == (3, 2)
    assert load_dataframe(right_ref).shape == (3, 2)


def test_join_preflight_many_to_many_requires_confirmation() -> None:
    left_ref = save_dataframe(pd.DataFrame({"key": [1, 1, 2]}))
    right_ref = save_dataframe(pd.DataFrame({"key": [1, 1, 3]}))

    out = join_preflight(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "key",
            "right_key": "key",
            "join_type": "left",
        }
    )

    assert out["status"] == "requires_confirmation"
    assert out["relationship"] == "many_to_many"
    assert out["estimated_output_rows"] == 5
    assert out["requires_confirmation"] is True
    assert {risk["code"] for risk in out["risks"]} == {"many_to_many"}


def test_join_relationship_uses_matching_keys_not_unmatched_duplicates() -> None:
    left_ref = save_dataframe(pd.DataFrame({"key": [1, 2, 2]}))
    right_ref = save_dataframe(pd.DataFrame({"key": [1, 3, 3]}))

    out = join_preflight(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "key",
            "right_key": "key",
            "join_type": "inner",
        }
    )

    assert out["relationship"] == "one_to_one"
    assert out["status"] == "ready"


def test_join_preflight_blocks_incompatible_or_unmatched_keys() -> None:
    numeric_ref = save_dataframe(pd.DataFrame({"key": [1, 2]}))
    text_ref = save_dataframe(pd.DataFrame({"key": ["1", "2"]}))
    other_numeric_ref = save_dataframe(pd.DataFrame({"key": [7, 8]}))
    base_args = {
        "left_dataset_ref": numeric_ref,
        "left_key": "key",
        "right_key": "key",
        "join_type": "inner",
    }

    incompatible = join_preflight({**base_args, "right_dataset_ref": text_ref})
    unmatched = join_preflight({**base_args, "right_dataset_ref": other_numeric_ref})

    assert incompatible["status"] == "blocked"
    assert incompatible["relationship"] == "incompatible"
    assert incompatible["risks"][0]["code"] == "incompatible_key_types"
    assert unmatched["status"] == "blocked"
    assert unmatched["relationship"] == "no_matches"
    assert unmatched["risks"][0]["code"] == "no_matching_keys"


def test_join_preflight_warns_on_null_keys_and_blocks_fixed_row_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    left_ref = save_dataframe(pd.DataFrame({"key": [1.0, 1.0, np.nan]}))
    right_ref = save_dataframe(pd.DataFrame({"key": [1.0, 1.0]}))
    monkeypatch.setattr(dataset_ops_tools, "_JOIN_MAX_OUTPUT_ROWS", 3)

    out = join_preflight(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "key",
            "right_key": "key",
            "join_type": "left",
        }
    )

    assert out["status"] == "blocked"
    assert out["estimated_output_rows"] == 5
    assert {risk["code"] for risk in out["risks"]} == {
        "output_row_limit",
        "many_to_many",
        "left_null_keys",
    }


def test_join_preflight_rejects_protected_key_and_free_sql() -> None:
    left_ref = save_dataframe(pd.DataFrame({"secret_id": [1, 2]}))
    right_ref = save_dataframe(pd.DataFrame({"id": [1, 2]}))
    save_metadata(left_ref, {"policy": {"columns": {"secret_id": "mask"}}})
    args = {
        "left_dataset_ref": left_ref,
        "right_dataset_ref": right_ref,
        "left_key": "secret_id",
        "right_key": "id",
        "join_type": "inner",
    }

    with pytest.raises(ValueError, match="受数据策略保护"):
        join_preflight(args)
    with pytest.raises(SchemaValidationError):
        build_server()._tools["join_preflight"].invoke({**args, "sql": "SELECT *"})
    with pytest.raises(ValueError, match="两个不同的数据集"):
        join_preflight({**args, "right_dataset_ref": left_ref})


# ── join_datasets：固定执行与派生策略 ──


def test_join_datasets_materializes_fixed_inner_join_and_handles_collisions() -> None:
    left_ref = save_dataframe(
        pd.DataFrame({"客户ID": [1, 2, 3], "名称": ["甲", "乙", "丙"]})
    )
    right_ref = save_dataframe(
        pd.DataFrame({"id": [1, 2, 4], "名称": ["A", "B", "D"], "等级": [1, 2, 3]})
    )
    args = {
        "left_dataset_ref": left_ref,
        "right_dataset_ref": right_ref,
        "left_key": "客户ID",
        "right_key": "id",
        "join_type": "inner",
    }

    out = build_server()._tools["join_datasets"].invoke(args)
    joined = load_dataframe(out["dataset_ref"])

    assert out["schema"] == "chatbi-join-result-v1"
    assert out["parent_refs"] == [left_ref, right_ref]
    assert out["rows"] == 2
    assert list(joined.columns) == ["客户ID", "名称", "id", "名称_right", "等级"]
    assert joined["客户ID"].tolist() == [1, 2]
    assert out["mutates_data"] is True
    assert out["raw_rows_returned"] is False


def test_join_datasets_reruns_gate_and_rejects_blocked_join() -> None:
    left_ref = save_dataframe(pd.DataFrame({"id": [1, 2]}))
    right_ref = save_dataframe(pd.DataFrame({"id": [7, 8]}))

    with pytest.raises(ValueError, match="预检阻塞"):
        join_datasets(
            {
                "left_dataset_ref": left_ref,
                "right_dataset_ref": right_ref,
                "left_key": "id",
                "right_key": "id",
                "join_type": "inner",
            }
        )


def test_full_join_preserves_unmatched_shared_key() -> None:
    left_ref = save_dataframe(pd.DataFrame({"id": [1, 2], "left": ["a", "b"]}))
    right_ref = save_dataframe(pd.DataFrame({"id": [2, 3], "right": ["x", "y"]}))

    out = join_datasets(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "id",
            "right_key": "id",
            "join_type": "full",
        }
    )

    assert sorted(load_dataframe(out["dataset_ref"])["id"].tolist()) == [1, 2, 3]


def test_join_datasets_propagates_stricter_policy_to_renamed_columns() -> None:
    left_ref = save_dataframe(pd.DataFrame({"id": [1, 2], "金额": [10, 20]}))
    right_ref = save_dataframe(pd.DataFrame({"id": [1, 2], "金额": [30, 40]}))
    save_metadata(
        right_ref,
        {"policy": {"level": "restricted", "columns": {"金额": "exclude"}}},
    )

    out = join_datasets(
        {
            "left_dataset_ref": left_ref,
            "right_dataset_ref": right_ref,
            "left_key": "id",
            "right_key": "id",
            "join_type": "inner",
        }
    )

    assert load_metadata(out["dataset_ref"]) == {
        "policy": {
            "level": "restricted",
            "small_group_min_size": 5,
            "small_group_mode": "merge",
            "columns": {"金额_right": "exclude"},
        }
    }


# ── dataset_store 新增 ──


def test_duplicate_row_count(sales_ref: str) -> None:
    assert duplicate_row_count(sales_ref) == 1  # index 0/2 整行相同
