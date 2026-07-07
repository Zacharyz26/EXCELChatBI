"""报告工具入参 JSON Schema（红线3）。所有工具经 Tool.invoke 校验后执行。"""

from __future__ import annotations

from typing import Any

GEN_REPORT_MD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "profile": {"type": "object"},
        "charts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "caption": {"type": "string"},
                    "image_path": {"type": "string"},
                },
                "required": ["image_path"],
                "additionalProperties": True,
            },
        },
        "stats": {"type": "array", "items": {"type": "object"}},
        "insights": {"type": "string"},
    },
    "required": ["title", "profile"],
    "additionalProperties": False,
}

INSIGHT_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"label": {"type": "string"}, "text": {"type": "string"}},
                "additionalProperties": True,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

EXPORT_PDF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"report_id": {"type": "string"}},
    "required": ["report_id"],
    "additionalProperties": False,
}
