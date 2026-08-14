"""数据集变换、聚合预览与 Join 预检工具实现——纯数据操作，零 LLM。

- transform_dataset：结构化白名单变换（决策 3 修订），产出**衍生数据集**
  （新 parquet + 血缘信息由调用方登记，见 agent_tools.register_derived_dataset）。
- aggregate_preview：分组聚合出表格（封装 dataset_store.aggregate，DuckDB 下推）。
- join_preflight：只读评估双数据集关联风险，不执行 Join、不返回原始行。
- join_datasets：在同一固定预检门禁后物化等值 Join，不接受自由 SQL。

所有数字来自真实数据计算（红线2）；本模块只被 Tool.invoke 调用（红线3）。
"""

from __future__ import annotations

from typing import Any

import pandas as pd
from packages.common.dataset_store import (
    aggregate,
    delete_dataset,
    join_key_statistics,
    load_dataframe,
    materialize_join,
    save_dataframe,
    save_metadata,
)
from packages.governance.data_boundary import ColumnRule, SensitivityLevel, resolve_policy

# 变换操作的确定性执行顺序（文档化，模型与用户可预期）
_OPERATION_ORDER = ("exclude_row_indices", "filters", "drop_nulls", "drop_duplicates", "sort")
_JOIN_MAX_OUTPUT_ROWS = 500_000
_JOIN_EXPANSION_CONFIRM_RATIO = 2.0


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


def join_preflight(args: dict[str, Any]) -> dict[str, Any]:
    """只读评估两个已登记数据集的 Join 可行性、基数关系和行数风险。"""
    left_ref: str = args["left_dataset_ref"]
    right_ref: str = args["right_dataset_ref"]
    left_key: str = args["left_key"]
    right_key: str = args["right_key"]
    join_type: str = args["join_type"]
    if left_ref == right_ref:
        raise ValueError("Join 左右数据集必须是两个不同的数据集")
    _require_join_key_visible(left_ref, left_key, side="左侧")
    _require_join_key_visible(right_ref, right_key, side="右侧")

    stats = join_key_statistics(
        left_ref,
        right_ref,
        left_key,
        right_key,
        join_type,
    )
    left = {"key": left_key, **stats["left"]}
    right = {"key": right_key, **stats["right"]}
    compatible = bool(stats["compatible_key_types"])
    matching_key_count = int(stats["matching_key_count"])
    estimated_rows = int(stats["estimated_output_rows"])
    denominator = max(int(left["row_count"]), int(right["row_count"]), 1)
    expansion_ratio = round(estimated_rows / denominator, 6)
    relationship = _join_relationship(
        compatible,
        matching_key_count,
        left_matching_max_rows_per_key=int(stats["left_matching_max_rows_per_key"]),
        right_matching_max_rows_per_key=int(stats["right_matching_max_rows_per_key"]),
    )

    risks: list[dict[str, str]] = []
    if not compatible:
        risks.append(
            _join_risk("incompatible_key_types", "blocking", "左右关联键的数据类型不兼容。")
        )
    elif matching_key_count == 0:
        risks.append(_join_risk("no_matching_keys", "blocking", "两个关联键没有可匹配值。"))
    if estimated_rows > _JOIN_MAX_OUTPUT_ROWS:
        risks.append(
            _join_risk(
                "output_row_limit",
                "blocking",
                f"预估结果超过固定上限 {_JOIN_MAX_OUTPUT_ROWS} 行。",
            )
        )
    if relationship == "many_to_many":
        risks.append(
            _join_risk("many_to_many", "warning", "关联键为多对多关系，执行前必须人工确认。")
        )
    if expansion_ratio > _JOIN_EXPANSION_CONFIRM_RATIO:
        risks.append(
            _join_risk("row_expansion", "warning", "预估结果行数存在明显膨胀。")
        )
    if int(left["null_count"]) > 0:
        risks.append(_join_risk("left_null_keys", "warning", "左侧关联键包含空值。"))
    if int(right["null_count"]) > 0:
        risks.append(_join_risk("right_null_keys", "warning", "右侧关联键包含空值。"))

    blocked = any(risk["severity"] == "blocking" for risk in risks)
    requires_confirmation = not blocked and any(
        risk["severity"] == "warning" for risk in risks
    )
    status = (
        "blocked"
        if blocked
        else ("requires_confirmation" if requires_confirmation else "ready")
    )
    return {
        "schema": "chatbi-join-preflight-v1",
        "status": status,
        "join_type": join_type,
        "relationship": relationship,
        "left": left,
        "right": right,
        "matching_key_count": matching_key_count,
        "matched_left_rows": int(stats["matched_left_rows"]),
        "matched_right_rows": int(stats["matched_right_rows"]),
        "estimated_output_rows": estimated_rows,
        "expansion_ratio": expansion_ratio,
        "max_output_rows": _JOIN_MAX_OUTPUT_ROWS,
        "risks": risks,
        "requires_confirmation": requires_confirmation,
        "executable": not blocked,
        "mutates_data": False,
        "raw_rows_returned": False,
    }


def join_datasets(args: dict[str, Any]) -> dict[str, Any]:
    """通过固定预检后物化等值 Join；外层高风险契约负责显式用户授权。"""
    preflight = join_preflight(args)
    if preflight["status"] == "blocked":
        codes = ", ".join(str(risk["code"]) for risk in preflight["risks"])
        raise ValueError(f"Join 预检阻塞，禁止执行: {codes}")

    left_ref = str(args["left_dataset_ref"])
    right_ref = str(args["right_dataset_ref"])
    materialized = materialize_join(
        left_ref,
        right_ref,
        str(args["left_key"]),
        str(args["right_key"]),
        str(args["join_type"]),
    )
    actual_rows = int(materialized["rows"])
    if actual_rows != int(preflight["estimated_output_rows"]):
        # Parquet 版本应不可变；不一致说明执行输入已漂移，不能登记不可信结果。
        delete_dataset(str(materialized["dataset_ref"]))
        raise RuntimeError("Join 执行结果与预检行数不一致，已撤销输出")

    try:
        _save_join_policy(
            output_ref=str(materialized["dataset_ref"]),
            left_ref=left_ref,
            right_ref=right_ref,
            right_column_mapping=materialized["right_column_mapping"],
        )
    except Exception:
        delete_dataset(str(materialized["dataset_ref"]))
        raise
    return {
        "schema": "chatbi-join-result-v1",
        "dataset_ref": materialized["dataset_ref"],
        "parent_ref": left_ref,
        "parent_refs": [left_ref, right_ref],
        "join_type": args["join_type"],
        "left_key": args["left_key"],
        "right_key": args["right_key"],
        "rows": actual_rows,
        "columns": materialized["columns"],
        "relationship": preflight["relationship"],
        "preflight_status": preflight["status"],
        "risks": preflight["risks"],
        "mutates_data": True,
        "raw_rows_returned": False,
    }


def _save_join_policy(
    *,
    output_ref: str,
    left_ref: str,
    right_ref: str,
    right_column_mapping: dict[str, str],
) -> None:
    """把两侧更严格的数据边界映射到 Join 输出，避免派生后策略降级。"""
    left = resolve_policy(left_ref)
    right = resolve_policy(right_ref)
    levels = {
        SensitivityLevel.OPEN: 0,
        SensitivityLevel.INTERNAL: 1,
        SensitivityLevel.RESTRICTED: 2,
    }
    level = max((left.level, right.level), key=levels.__getitem__)
    columns = {
        name: rule.value
        for name, rule in left.columns.items()
        if rule is not ColumnRule.NORMAL
    }
    columns.update(
        {
            right_column_mapping.get(name, name): rule.value
            for name, rule in right.columns.items()
            if rule is not ColumnRule.NORMAL
            and name in right_column_mapping
        }
    )
    policy: dict[str, Any] = {
        "level": level.value,
        "small_group_min_size": max(
            left.small_group_min_size, right.small_group_min_size
        ),
        "small_group_mode": (
            "drop"
            if "drop" in {left.small_group_mode, right.small_group_mode}
            else "merge"
        ),
    }
    if columns:
        policy["columns"] = columns
    save_metadata(output_ref, {"policy": policy})


def _require_join_key_visible(dataset_ref: str, key: str, *, side: str) -> None:
    rule = resolve_policy(dataset_ref).rule_of(key)
    if rule in {ColumnRule.MASK, ColumnRule.EXCLUDE}:
        raise ValueError(f"{side}关联键受数据策略保护，不能用于 Join: {key}")


def _join_relationship(
    compatible: bool,
    matching_key_count: int,
    *,
    left_matching_max_rows_per_key: int,
    right_matching_max_rows_per_key: int,
) -> str:
    if not compatible:
        return "incompatible"
    if matching_key_count == 0:
        return "no_matches"
    left_unique = left_matching_max_rows_per_key <= 1
    right_unique = right_matching_max_rows_per_key <= 1
    if left_unique and right_unique:
        return "one_to_one"
    if left_unique:
        return "one_to_many"
    if right_unique:
        return "many_to_one"
    return "many_to_many"


def _join_risk(code: str, severity: str, message: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "message": message}


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
