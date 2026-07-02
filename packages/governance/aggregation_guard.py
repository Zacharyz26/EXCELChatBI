"""小分组保护（第3层：防聚合结果泄露原始明细）。

当某个 groupby 分组样本量过低（如 1 行）时，聚合值≈原始明细，等于变相泄露。
本模块把样本量低于阈值的分组按配置**合并到"其他"**（默认）或**丢弃**；
合并后若仍不足阈值，则整体丢弃，避免"其他"本身泄露单个小组。

场景无关：阈值与行为均由策略驱动，不含任何业务假设。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class GroupAgg:
    """一个聚合分组：类目键、聚合值、分组样本量（行数）。"""

    key: object
    value: float
    count: int


def _combine(small: list[GroupAgg], agg: str, total: int) -> float:
    """按聚合方式合并多个小分组的值。"""
    if agg == "sum":
        return sum(g.value for g in small)
    if agg == "count":
        return float(total)
    if agg == "mean":
        # 加权平均：Σ(mean_i · count_i) / Σ count_i
        return sum(g.value * g.count for g in small) / total
    raise ValueError(f"不支持的聚合方式: {agg}")


def guard_small_groups(
    groups: list[GroupAgg],
    agg: str,
    min_size: int,
    *,
    mode: str = "merge",
    other_label: str = "其他",
) -> list[GroupAgg]:
    """对聚合结果施加小分组保护。

    Args:
        groups: 原始聚合分组。
        agg: 聚合方式（sum/mean/count）。
        min_size: 分组最小样本量阈值，低于则受保护。
        mode: "merge" 合并到 other_label；"drop" 直接丢弃。
        other_label: 合并桶的类目名。

    Returns:
        处理后的分组列表（顺序未排序，由调用方排序/截断）。
    """
    if min_size <= 1:
        return groups
    kept = [g for g in groups if g.count >= min_size]
    small = [g for g in groups if g.count < min_size]
    if not small or mode == "drop":
        return kept

    total = sum(g.count for g in small)
    if total < min_size:
        # 合并后仍不足阈值，无法安全展示 → 丢弃
        return kept
    return [*kept, GroupAgg(other_label, _combine(small, agg, total), total)]
