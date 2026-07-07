"""图表工具入参 JSON Schema（红线3）。"""

from __future__ import annotations

from typing import Any

GEN_CHART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_ref": {"type": "string"},
        "chart_type": {"type": "string", "enum": ["line", "bar", "pie", "scatter"]},
        "encoding": {
            "type": "object",
            "properties": {
                "x": {"type": "string", "description": "维度列（类目/时间轴）"},
                "y": {"type": "string", "description": "度量列"},
                "agg": {
                    "type": "string",
                    "enum": ["sum", "mean", "count", "none"],
                    "description": "聚合方式；scatter 用 none",
                },
                "top_n": {"type": "integer", "minimum": 1, "description": "可选，限制类目数"},
            },
            "required": ["x", "y"],
            "additionalProperties": False,
        },
    },
    "required": ["dataset_ref", "chart_type", "encoding"],
    "additionalProperties": False,
}

# 直接吃 ECharts option（gen_chart 的产出），不依赖 chart registry 持久化。
CHART_SCREENSHOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "option": {"type": "object"},
        "width": {"type": "integer", "minimum": 100, "maximum": 4000},
        "height": {"type": "integer", "minimum": 100, "maximum": 4000},
    },
    "required": ["option"],
    "additionalProperties": False,
}

MULTI_LAYOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"chart_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["chart_ids"],
    "additionalProperties": False,
}
