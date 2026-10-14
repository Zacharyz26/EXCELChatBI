"""报告工具入参 JSON Schema（红线3）。所有工具经 Tool.invoke 校验后执行。"""

from __future__ import annotations

from typing import Any

from packages.common.exact_numbers import EXACT_NUMBER_SCHEMA

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
        "tables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "analysis_id": {"type": "string"},
                    "group_col": {"type": "string"},
                    "value_col": {"type": ["string", "null"]},
                    "agg": {"enum": ["sum", "mean", "count"]},
                    "group_total": {"type": "integer"},
                    "truncated": {"type": "boolean"},
                    "total_value": {
                        "anyOf": [{"type": ["number", "null"]}, EXACT_NUMBER_SCHEMA]
                    },
                    "rows": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["group", "value"],
                            "properties": {
                                "group": {},
                                "value": {
                                    "anyOf": [
                                        {"type": ["number", "null"]},
                                        EXACT_NUMBER_SCHEMA,
                                    ]
                                },
                            },
                        },
                    },
                },
                "required": ["rows", "group_col", "agg", "truncated"],
            },
        },
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
