"""数据集变换与聚合预览工具实现——纯数据操作，零 LLM（5.3 正式条款）。

- transform_dataset：结构化白名单变换（决策 3 修订），产出**衍生数据集**
  （新 parquet + 血缘信息由调用方登记，见 agent_tools.register_derived_dataset）。
- aggregate_preview：分组聚合出表格（封装 dataset_store.aggregate，DuckDB 下推）。

所有数字来自真实数据计算（红线2）；本模块只被 Tool.invoke 调用（红线3）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from packages.common.dataset_store import aggregate, load_dataframe, save_dataframe

# 变换操作的确定性执行顺序（文档化，模型与用户可预期）
_OPERATION_ORDER = ("exclude_row_indices", "filters", "drop_nulls", "drop_duplicates", "sort")


def transform_dataset(args: dict[str, Any]) -> dict[str, Any]:
    """对源数据集执行白名单变换，落盘为新数据集并返回血缘信息。

    执行顺序固定：排除行 → 过滤 → 去空 → 去重 → 排序。

    Args:
        args: 见 TRANSFORM_DATASET_SCHEMA；除 dataset_ref 外至少需一个操作。

    Returns:
        {dataset_ref(新), parent_ref, rows_before, rows_after, columns, transform}
        transform 为实际生效的操作回显，供血缘登记与前端展示。

    Raises:
        ValueError: 未提供任何操作 / 列不存在 / 条件值类型不合法。
        FileNotFoundError: 源数据集不存在。
    """
    source_ref: str = args["dataset_ref"]
    operations = {k: args[k] for k in _OPERATION_ORDER if k in args}
    if not operations:
        raise ValueError("transform_dataset 需要至少一个变换操作（filters/drop_nulls/…）")

    df = load_dataframe(source_ref)
    rows_before = len(df)

    if "exclude_row_indices" in operations:
        df = _exclude_rows(df, operations["exclude_row_indices"], rows_before)
    if "filters" in operations:
        for cond in operations["filters"]:
            df = _apply_filter(df, cond)
    if "drop_nulls" in operations:
        df = _drop_nulls(df, operations["drop_nulls"])
    if "drop_duplicates" in operations:
        df = _drop_duplicates(df, operations["drop_duplicates"])
    if "sort" in operations:
        df = _sort(df, operations["sort"])

    if df.empty:
        raise ValueError("变换后数据集为空：请放宽过滤条件后重试")

    new_ref = save_dataframe(df.reset_index(drop=True))
    return {
        "dataset_ref": new_ref,
        "parent_ref": source_ref,
        "rows_before": rows_before,
        "rows_after": len(df),
        "columns": [str(c) for c in df.columns],
        "transform": operations,
    }


def aggregate_preview(args: dict[str, Any]) -> dict[str, Any]:
    """分组聚合出表格（DuckDB 下推），回答"各 X 的 Y 是多少"类取数问题。

    注意：本工具供 /chat 助手通道使用，按红线1 例外不做小分组门控，
    只做行数截断（token 经济，13.5）。

    Args:
        args: 见 AGGREGATE_PREVIEW_SCHEMA。

    Returns:
        {rows: [{group, value, count}], group_total, truncated, agg, group_col, value_col}
    """
    group_col: str = args["group_col"]
    agg: str = args["agg"]
    value_col: str = args.get("value_col") or group_col  # count 可省略度量列
    if agg != "count" and not args.get("value_col"):
        raise ValueError(f"agg={agg} 需要提供 value_col")

    tuples = aggregate(args["dataset_ref"], group_col, value_col, agg)

    sort = args.get("sort", "value_desc")
    if sort == "group":
        tuples.sort(key=lambda t: str(t[0]))
    else:
        tuples.sort(key=lambda t: t[1], reverse=(sort == "value_desc"))

    limit = int(args.get("limit", 20))
    rows = [
        {"group": _plain(g), "value": v, "count": c} for g, v, c in tuples[:limit]
    ]
    return {
        "rows": rows,
        "group_total": len(tuples),
        "truncated": len(tuples) > limit,
        "agg": agg,
        "group_col": group_col,
        "value_col": None if agg == "count" and not args.get("value_col") else value_col,
    }


# ── 内部：各变换操作 ──


def _require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"列不存在: {', '.join(missing)}")


def _exclude_rows(df: pd.DataFrame, indices: list[int], rows_before: int) -> pd.DataFrame:
    out_of_range = [i for i in indices if i >= rows_before]
    if out_of_range:
        raise ValueError(f"排除行号超出范围（共 {rows_before} 行）: {out_of_range[:5]}")
    return df.drop(index=df.index[indices])


def _apply_filter(df: pd.DataFrame, cond: dict[str, Any]) -> pd.DataFrame:
    column, op = cond["column"], cond["op"]
    _require_columns(df, [column])
    series = df[column]

    if op in ("is_null", "not_null"):
        mask = series.isna() if op == "is_null" else series.notna()
        return df[mask]

    if "value" not in cond:
        raise ValueError(f"过滤条件 {column} {op} 缺少 value")
    value = cond["value"]

    if op in ("in", "not_in"):
        if not isinstance(value, list):
            raise ValueError(f"{op} 的 value 必须是数组")
        mask = series.isin(value)
        return df[mask if op == "in" else ~mask]
    if op == "contains":
        if not isinstance(value, str):
            raise ValueError("contains 的 value 必须是字符串")
        return df[series.astype(str).str.contains(value, na=False, regex=False)]

    try:
        if op == "==":
            mask = series == value
        elif op == "!=":
            mask = series != value
        elif op == ">":
            mask = series > value
        elif op == ">=":
            mask = series >= value
        elif op == "<":
            mask = series < value
        else:  # op == "<="；其余值已由 schema 枚举拒绝
            mask = series <= value
    except TypeError as exc:  # 如字符串列与数字比大小
        raise ValueError(f"列 {column} 与给定值无法做 {op} 比较: {exc}") from exc
    return df[mask]


def _drop_nulls(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if columns:
        _require_columns(df, columns)
        return df.dropna(subset=columns)
    return df.dropna()


def _drop_duplicates(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    if columns:
        _require_columns(df, columns)
        return df.drop_duplicates(subset=columns, keep="first")
    return df.drop_duplicates(keep="first")


def _sort(df: pd.DataFrame, keys: list[dict[str, Any]]) -> pd.DataFrame:
    columns = [k["column"] for k in keys]
    _require_columns(df, columns)
    ascending = [k.get("order", "asc") == "asc" for k in keys]
    return df.sort_values(by=columns, ascending=ascending, kind="mergesort")


def _plain(value: Any) -> Any:
    """把 numpy/pandas 标量转为可 JSON 序列化的原生类型。"""
    if hasattr(value, "item"):
        return value.item()
    return value
