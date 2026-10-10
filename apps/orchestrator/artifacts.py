"""Artifact persistence projections for orchestrator tool results."""

from __future__ import annotations

from typing import Any

from packages.common.analysis_kinds import ANALYSIS_KIND_BY_TOOL
from packages.session.models import JsonObject


def artifact_payload_for(tool: str, result: dict[str, Any]) -> JsonObject:
    """Build the compact payload persisted for a tool-generated artifact."""
    if tool == "generate_report":
        report_id = result.get("report_id", "")
        payload: JsonObject = {
            "report_id": report_id,
            "md_url": f"/analyze/report/{report_id}.md",
            "skipped_charts": result.get("skipped_charts", 0),
        }
        if result.get("pdf_path"):
            payload["pdf_url"] = f"/analyze/report/{report_id}.pdf"
        if isinstance(result.get("validation"), dict):
            payload["validation"] = result["validation"]
        return payload
    if tool in {
        "trend_analysis",
        "forecast",
        "anomaly_detect",
        "regression",
        "correlation",
        "dimension_contribution",
        "group_compare",
    }:
        return {"kind": ANALYSIS_KIND_BY_TOOL[tool], "result": result}
    return dict(result)


def artifact_file_ref(tool: str, result: dict[str, Any]) -> str | None:
    """Return the concrete generated file used by deterministic verification."""
    if tool != "generate_report":
        return None
    pdf_path = result.get("pdf_path")
    if isinstance(pdf_path, str) and pdf_path.strip():
        return pdf_path
    markdown_path = result.get("md_path")
    if isinstance(markdown_path, str) and markdown_path.strip():
        return markdown_path
    return None
