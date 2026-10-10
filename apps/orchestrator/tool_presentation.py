"""Human-readable presentation of tool invocation arguments."""

from __future__ import annotations

import json
from typing import Any

# 参数键 → 中文标签（人话参数摘要用；未列出的键按原名展示）
_ARG_LABELS = {
    "dataset_ref": "数据集",
    "left_dataset_ref": "左数据集",
    "right_dataset_ref": "右数据集",
    "left_key": "左关联键",
    "right_key": "右关联键",
    "join_type": "Join 类型",
    "value_col": "数值列",
    "time_col": "时间列",
    "method": "方法",
    "period": "周期",
    "ma_window": "窗口",
    "forecast_horizon": "预测步数",
    "horizon": "预测期数",
    "validation_size": "验证样本数",
    "seasonal_period": "季节周期",
    "contamination": "异常比例",
    "target": "目标列",
    "features": "自变量",
    "columns": "列",
    "dimension_col": "维度列",
    "chart_type": "图型",
    "group_col": "分组列",
    "agg": "聚合",
    "sort": "排序",
    "limit": "行数上限",
    "query": "检索词",
    "top_k": "条数",
    "title": "标题",
    "analysis_ids": "纳入分析",
    "insights": "要点",
    "include_pdf": "导出PDF",
    "filters": "过滤",
    "drop_nulls": "去空列",
    "drop_duplicates": "去重列",
    "exclude_row_indices": "排除行",
    "x": "X轴",
    "y": "Y轴",
    "top_n": "取前N",
}

# 摘要里不展示的大值参数（原始 JSON 仍在 args_preview 里供调参表单用）
_ARG_SKIP = {"option", "encoding", "sample_rows"}


def humanize_args(tool: str, args: dict[str, Any]) -> str:
    """把工具入参翻译成一行中文摘要（14.5.3：涉及字段/筛选条件，非原始 JSON）。"""
    if tool == "chart_screenshot":
        return "渲染当前图表为 PNG"
    parts: list[str] = []
    flat = dict(args)
    # gen_chart 的列映射摊平成普通键
    encoding = flat.get("encoding")
    if isinstance(encoding, dict):
        flat.update(encoding)
    for key, value in flat.items():
        if key in _ARG_SKIP or value is None:
            continue
        parts.append(f"{_ARG_LABELS.get(key, key)}: {_humanize_value(key, value)}")
    return " · ".join(parts) if parts else "无参数"


def _humanize_value(key: str, value: Any) -> str:
    """单个参数值的人话展示：短标识、列表截断、布尔汉化。"""
    if key.endswith("dataset_ref") and isinstance(value, str):
        return value[:8]
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        shown = [_filter_condition_text(v) if isinstance(v, dict) else str(v) for v in value[:5]]
        suffix = f" 等 {len(value)} 项" if len(value) > 5 else ""
        return "、".join(shown) + suffix
    if isinstance(value, dict):
        return _compact_json(value, 60)
    return str(value)


def _filter_condition_text(cond: dict[str, Any]) -> str:
    """过滤/排序条件的紧凑人话（如 "地区 in [华东,华南]"、"销售额 desc"）。"""
    if "op" in cond:
        column, op = cond.get("column", "?"), cond.get("op", "?")
        if op in ("is_null", "not_null"):
            return f"{column} {'为空' if op == 'is_null' else '非空'}"
        value = cond.get("value")
        value_text = "、".join(str(v) for v in value) if isinstance(value, list) else str(value)
        return f"{column} {op} {value_text}"
    if "column" in cond:  # 排序键
        return f"{cond['column']} {cond.get('order', 'asc')}"
    return _compact_json(cond, 40)



def _compact_json(value: Any, max_chars: int) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    return f"{text[:max_chars]}…（已截断）" if len(text) > max_chars else text
