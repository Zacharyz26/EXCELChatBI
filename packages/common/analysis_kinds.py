"""Canonical production analysis kinds shared by artifacts and reports."""

from __future__ import annotations

ANALYSIS_KIND_BY_TOOL: dict[str, str] = {
    "trend_analysis": "trend",
    "forecast": "forecast",
    "anomaly_detect": "anomaly",
    "regression": "regression",
    "correlation": "correlation",
    "dimension_contribution": "contribution",
    "group_compare": "group_compare",
}
REPORT_ANALYSIS_KINDS = frozenset(ANALYSIS_KIND_BY_TOOL.values())


def canonical_analysis_kind(value: object) -> str:
    """Normalize a production tool name or canonical kind; reject unknown kinds."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError("统计 Artifact 缺少 kind")
    clean = value.strip()
    canonical = ANALYSIS_KIND_BY_TOOL.get(clean, clean)
    if canonical not in REPORT_ANALYSIS_KINDS:
        raise ValueError(f"不支持的统计 Artifact kind: {clean}")
    return canonical
