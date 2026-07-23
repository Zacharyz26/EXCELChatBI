"""Static, administrator-owned capability metadata for project MCP tools."""

from __future__ import annotations

from typing import Any

from mcp_servers.common.contracts import RiskLevel, ToolCapabilityMetadata

JsonSchema = dict[str, Any]


def _object(properties: JsonSchema, *required: str) -> JsonSchema:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": True,
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER_OR_NULL = {"type": ["number", "null"]}
_ARRAY = {"type": "array"}
_OBJECT = {"type": "object"}

_OUTPUT_SCHEMAS: dict[str, JsonSchema] = {
    "parse_excel": _object(
        {"dataset_ref": _STRING, "row_count": _INTEGER, "column_count": _INTEGER},
        "dataset_ref",
        "row_count",
        "column_count",
    ),
    "infer_schema": _object(
        {
            "dataset_ref": _STRING,
            "row_count": _INTEGER,
            "column_count": _INTEGER,
            "columns": _ARRAY,
            "sample_rows": _ARRAY,
        },
        "dataset_ref",
        "row_count",
        "column_count",
        "columns",
        "sample_rows",
    ),
    "data_preview": _object({"rows": _ARRAY}, "rows"),
    "trend_analysis": _object(
        {
            "method": _STRING,
            "direction": _STRING,
            "slope": _NUMBER_OR_NULL,
            "n": _INTEGER,
            "points": _OBJECT,
            "forecast": _ARRAY,
        },
        "method",
        "direction",
        "slope",
        "n",
        "points",
        "forecast",
    ),
    "anomaly_detect": _object(
        {
            "method": _STRING,
            "n_total": _INTEGER,
            "n_anomalies": _INTEGER,
            "anomalies": _ARRAY,
        },
        "method",
        "n_total",
        "n_anomalies",
        "anomalies",
    ),
    "regression": _object(
        {
            "kind": _STRING,
            "r_squared": _NUMBER_OR_NULL,
            "n_obs": _INTEGER,
            "coefficients": _ARRAY,
        },
        "kind",
        "r_squared",
        "n_obs",
        "coefficients",
    ),
    "correlation": _object(
        {
            "method": _STRING,
            "columns": _ARRAY,
            "n_obs": _INTEGER,
            "matrix": _ARRAY,
            "top_pairs": _ARRAY,
        },
        "method",
        "columns",
        "n_obs",
        "matrix",
        "top_pairs",
    ),
    "gen_chart": _object(
        {"chart_id": _STRING, "chart_type": _STRING, "option": _OBJECT},
        "chart_id",
        "chart_type",
        "option",
    ),
    "chart_screenshot": _object(
        {
            "image_path": _STRING,
            "width": _INTEGER,
            "height": _INTEGER,
            "bytes": _INTEGER,
        },
        "image_path",
        "width",
        "height",
        "bytes",
    ),
    # multi_layout remains unavailable to the Agent; its implementation still
    # raises NotImplementedError, so no successful result is advertised here.
    "multi_layout": {"type": "object"},
    "transform_dataset": _object(
        {
            "dataset_ref": _STRING,
            "parent_ref": _STRING,
            "rows_before": _INTEGER,
            "rows_after": _INTEGER,
            "columns": _ARRAY,
            "transform": _OBJECT,
        },
        "dataset_ref",
        "parent_ref",
        "rows_before",
        "rows_after",
        "columns",
        "transform",
    ),
    "aggregate_preview": _object(
        {
            "rows": _ARRAY,
            "group_total": _INTEGER,
            "truncated": {"type": "boolean"},
            "agg": _STRING,
            "group_col": _STRING,
            "value_col": {"type": ["string", "null"]},
        },
        "rows",
        "group_total",
        "truncated",
        "agg",
        "group_col",
        "value_col",
    ),
    "gen_report_md": _object(
        {"report_id": _STRING, "md_path": _STRING, "markdown": _STRING},
        "report_id",
        "md_path",
        "markdown",
    ),
    "insight_summary": _object({"summary_md": _STRING}, "summary_md"),
    "export_pdf": _object(
        {"report_id": _STRING, "pdf_path": _STRING, "bytes": _INTEGER},
        "report_id",
        "pdf_path",
        "bytes",
    ),
    "get_data_profile": _object(
        {"profile": _OBJECT, "quality": _OBJECT}, "profile", "quality"
    ),
    "kb_search": _object(
        {"is_empty": {"type": "boolean"}, "hits": _ARRAY}, "is_empty", "hits"
    ),
    "generate_report": _object(
        {
            "report_id": _STRING,
            "md_path": _STRING,
            "analysis_ids": _ARRAY,
            "skipped_charts": _INTEGER,
        },
        "report_id",
        "md_path",
        "analysis_ids",
        "skipped_charts",
    ),
}


def tool_output_schema(tool_name: str) -> JsonSchema:
    """Return the reviewed output schema for one project/Agent tool."""
    try:
        return _OUTPUT_SCHEMAS[tool_name]
    except KeyError as exc:
        raise ValueError(f"missing output schema for tool: {tool_name}") from exc


def tool_metadata(
    capability: str,
    *artifact_types: str,
    read_only: bool = True,
    idempotent: bool = True,
    risk_level: str = "low",
) -> ToolCapabilityMetadata:
    """Construct reviewed metadata; callers cannot obtain it from model arguments."""
    risk: RiskLevel
    if risk_level == "low":
        risk = "low"
    elif risk_level == "medium":
        risk = "medium"
    elif risk_level == "high":
        risk = "high"
    else:
        raise ValueError(f"invalid risk level: {risk_level}")
    return ToolCapabilityMetadata(
        capabilities=(capability,),
        artifact_types=tuple(artifact_types),
        read_only=read_only,
        destructive=False,
        idempotent=idempotent,
        risk_level=risk,
    )
