"""图表工具实现。

gen_chart 输出 ECharts JSON（前端渲染）；chart_screenshot 用 Playwright 无头浏览器
服务端渲染截图（实例池化复用，设计文档 5.3）。图表生成时持久化底层 data_ref +
生成参数 + chart_id，写入会话 chart_registry，供追问（设计文档 6.2）。
"""

from __future__ import annotations

import uuid
from typing import Any

import pandas as pd
from packages.common.dataset_store import aggregate as pushdown_aggregate
from packages.common.dataset_store import load_dataframe
from packages.governance.aggregation_guard import GroupAgg, guard_small_groups
from packages.governance.data_boundary import resolve_policy


def gen_chart(args: dict[str, Any]) -> dict[str, Any]:
    """生成 ECharts JSON 配置。

    红线2：图表里的所有数值都由本函数读取 dataset_ref 的**真实数据**聚合得到，
    LLM 只提供 chart_type 与列映射（encoding），绝不提供数字。
    第2层：聚合下推到 DuckDB 执行（数据不出环境）。
    第3层：聚合结果经小分组保护后才用于出图。

    Args:
        args: {dataset_ref, chart_type, encoding:{x, y, agg, top_n?}}。

    Returns:
        {chart_id, chart_type, option}  —— option 为 ECharts 配置（前端直接渲染）。
    """
    dataset_ref: str = args["dataset_ref"]
    chart_type: str = args["chart_type"]
    enc: dict[str, Any] = args["encoding"]
    x_col, y_col = enc["x"], enc["y"]
    agg: str = enc.get("agg", "sum")
    top_n: int | None = enc.get("top_n")

    if chart_type == "scatter":
        # TODO（大表）：散点非聚合，超大表应下推采样，避免整表入内存。
        df = load_dataframe(dataset_ref)
        for col in (x_col, y_col):
            if col not in df.columns:
                raise ValueError(f"列不存在: {col}")
        option = _scatter_option(df, x_col, y_col)
    else:
        cats, values = _aggregate(dataset_ref, x_col, y_col, agg, chart_type, top_n)
        option = _categorical_option(chart_type, x_col, y_col, cats, values)

    return {"chart_id": uuid.uuid4().hex, "chart_type": chart_type, "option": option}


# ── 内部：聚合与 option 组装（数值全部来自真实数据）──

def _aggregate(
    dataset_ref: str, x_col: str, y_col: str, agg: str, chart_type: str, top_n: int | None
) -> tuple[list, list]:
    """按 x 分组聚合 y（DuckDB 下推）+ 小分组保护，返回 (类目, 数值)。"""
    if agg == "none":
        # none：不聚合（每个 x 取其 y），退化情形走整表读回，不做小分组保护。
        df = load_dataframe(dataset_ref)
        for col in (x_col, y_col):
            if col not in df.columns:
                raise ValueError(f"列不存在: {col}")
        grouped = df.set_index(x_col)[y_col]
        groups = [GroupAgg(k, v, 1) for k, v in grouped.items()]
    else:
        rows = pushdown_aggregate(dataset_ref, x_col, y_col, agg)  # (key,value,count)
        policy = resolve_policy(dataset_ref)
        groups = guard_small_groups(
            [GroupAgg(k, v, c) for k, v, c in rows],
            agg,
            policy.small_group_min_size,
            mode=policy.small_group_mode,
            other_label=policy.other_label,
        )

    # 折线/时间序列按 x 升序；柱/饼按值降序更直观
    if chart_type == "line":
        groups = sorted(groups, key=lambda g: str(g.key))
    else:
        groups = sorted(groups, key=lambda g: g.value, reverse=True)
    if top_n:
        groups = groups[:top_n]

    cats = [_coerce(g.key) for g in groups]
    values = [_coerce(g.value) for g in groups]
    return cats, values


def _categorical_option(
    chart_type: str, x_col: str, y_col: str, cats: list, values: list
) -> dict[str, Any]:
    """组装类目型图表（line/bar/pie）的 ECharts option。"""
    title = f"{y_col} by {x_col}"
    if chart_type == "pie":
        return {
            "title": {"text": title},
            "tooltip": {"trigger": "item"},
            "series": [
                {
                    "type": "pie",
                    "data": [
                        {"name": str(c), "value": v}
                        for c, v in zip(cats, values, strict=False)
                    ],
                }
            ],
        }
    return {
        "title": {"text": title},
        "tooltip": {"trigger": "axis"},
        "xAxis": {"type": "category", "data": [str(c) for c in cats]},
        "yAxis": {"type": "value"},
        "series": [{"name": y_col, "type": chart_type, "data": values}],
    }


def _scatter_option(df: pd.DataFrame, x_col: str, y_col: str) -> dict[str, Any]:
    """散点图 option：原始 (x, y) 点对。"""
    pairs = [
        [_coerce(x), _coerce(y)]
        for x, y in zip(df[x_col].tolist(), df[y_col].tolist(), strict=False)
    ]
    return {
        "title": {"text": f"{y_col} vs {x_col}"},
        "tooltip": {"trigger": "item"},
        "xAxis": {"type": "value", "name": x_col},
        "yAxis": {"type": "value", "name": y_col},
        "series": [{"type": "scatter", "data": pairs}],
    }


def _coerce(value: Any) -> Any:
    """numpy/pandas 标量 → JSON 安全的原生类型。"""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if hasattr(value, "item"):  # numpy 标量
        value = value.item()
    if isinstance(value, pd.Timestamp):
        return str(value)
    return value


def chart_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    """用 Playwright 无头浏览器渲染 ECharts 并截图（实例池化）。"""
    raise NotImplementedError("TODO: Playwright 加载 ECharts 配置渲染截图，返回图片引用")


def multi_layout(args: dict[str, Any]) -> dict[str, Any]:
    """多图组合布局。"""
    raise NotImplementedError("TODO: 组合多个图表为面板布局")
