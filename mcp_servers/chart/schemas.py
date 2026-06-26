"""图表工具入参 JSON Schema（红线3）。"""

from __future__ import annotations

from typing import Any

GEN_CHART_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "dataset_ref": {"type": "string"},
        "chart_type": {"type": "string", "enum": ["line", "bar", "pie", "scatter", "heatmap"]},
        "encoding": {"type": "object", "description": "x/y/series 等字段映射"},
    },
    "required": ["dataset_ref", "chart_type", "encoding"],
    "additionalProperties": False,
}

CHART_SCREENSHOT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"chart_id": {"type": "string"}},
    "required": ["chart_id"],
    "additionalProperties": False,
}

MULTI_LAYOUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"chart_ids": {"type": "array", "items": {"type": "string"}}},
    "required": ["chart_ids"],
    "additionalProperties": False,
}
