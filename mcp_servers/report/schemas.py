"""报告工具入参 JSON Schema（红线3）。"""

from __future__ import annotations

from typing import Any

GEN_REPORT_MD_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "chart_ids": {"type": "array", "items": {"type": "string"}},
        "analysis_ref": {"type": "string"},
    },
    "required": ["title"],
    "additionalProperties": False,
}

INSIGHT_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"analysis_ref": {"type": "string"}},
    "required": ["analysis_ref"],
    "additionalProperties": False,
}

EXPORT_PDF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"report_ref": {"type": "string"}},
    "required": ["report_ref"],
    "additionalProperties": False,
}
