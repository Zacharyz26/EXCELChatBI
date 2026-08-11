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


def _closed_object(properties: JsonSchema, *required: str) -> JsonSchema:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_NUMBER_OR_NULL = {"type": ["number", "null"]}
_ARRAY = {"type": "array"}
_OBJECT = {"type": "object"}

_ROLE_CANDIDATE_SCHEMA = _closed_object(
    {
        "role": {
            "type": "string",
            "enum": ["time", "metric", "dimension", "identifier", "unknown"],
        },
        "score": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "array", "items": _STRING},
    },
    "role",
    "score",
    "evidence",
)
_ROLE_COLUMN_SCHEMA = _closed_object(
    {
        "column": _STRING,
        "primary_role": {
            "type": "string",
            "enum": ["time", "metric", "dimension", "identifier", "unknown"],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "ambiguous": {"type": "boolean"},
        "candidates": {
            "type": "array",
            "items": _ROLE_CANDIDATE_SCHEMA,
            "minItems": 1,
        },
        "profile_evidence": _closed_object(
            {
                "dtype": _STRING,
                "null_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                "distinct_count": _INTEGER,
                "non_null_count": _INTEGER,
                "unique_ratio": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "dtype",
            "null_ratio",
            "distinct_count",
            "non_null_count",
            "unique_ratio",
        ),
    },
    "column",
    "primary_role",
    "confidence",
    "ambiguous",
    "candidates",
    "profile_evidence",
)
_ROLES_OUTPUT_SCHEMA = _closed_object(
    {
        "schema": {"const": "chatbi-data-roles-v1"},
        "method": {"const": "deterministic-profile-heuristics-v1"},
        "columns": {"type": "array", "items": _ROLE_COLUMN_SCHEMA},
        "summary": _closed_object(
            {
                "time": _INTEGER,
                "metric": _INTEGER,
                "dimension": _INTEGER,
                "identifier": _INTEGER,
                "unknown": _INTEGER,
                "ambiguous": _INTEGER,
            },
            "time",
            "metric",
            "dimension",
            "identifier",
            "unknown",
            "ambiguous",
        ),
        "ambiguous_columns": {"type": "array", "items": _STRING},
        "requires_confirmation": {"type": "boolean"},
    },
    "schema",
    "method",
    "columns",
    "summary",
    "ambiguous_columns",
    "requires_confirmation",
)
_QUALITY_ISSUE_SCHEMA = _closed_object(
    {
        "issue_id": _STRING,
        "code": _STRING,
        "severity": {"type": "string", "enum": ["low", "medium", "high"]},
        "columns": {"type": "array", "items": _STRING},
        "evidence": _OBJECT,
        "suggested_action": _STRING,
        "recommendation": _STRING,
    },
    "issue_id",
    "code",
    "severity",
    "columns",
    "evidence",
    "suggested_action",
    "recommendation",
)
_QUALITY_RECOMMENDATION_SCHEMA = _closed_object(
    {
        "issue_id": _STRING,
        "action": _STRING,
        "columns": {"type": "array", "items": _STRING},
        "message": _STRING,
        "automatic": {"const": False},
    },
    "issue_id",
    "action",
    "columns",
    "message",
    "automatic",
)
_QUALITY_OUTPUT_SCHEMA = _closed_object(
    {
        "schema": {"const": "chatbi-data-quality-v1"},
        "mutates_data": {"const": False},
        "duplicate_rows": _INTEGER,
        "high_null_columns": {
            "type": "array",
            "items": _closed_object(
                {
                    "name": _STRING,
                    "null_ratio": {"type": "number", "minimum": 0, "maximum": 1},
                },
                "name",
                "null_ratio",
            ),
        },
        "constant_columns": {"type": "array", "items": _STRING},
        "issues": {"type": "array", "items": _QUALITY_ISSUE_SCHEMA},
        "recommendations": {
            "type": "array",
            "items": _QUALITY_RECOMMENDATION_SCHEMA,
        },
        "summary": _closed_object(
            {
                "issue_count": _INTEGER,
                "high": _INTEGER,
                "medium": _INTEGER,
                "low": _INTEGER,
                "requires_confirmation": {"type": "boolean"},
            },
            "issue_count",
            "high",
            "medium",
            "low",
            "requires_confirmation",
        ),
    },
    "schema",
    "mutates_data",
    "duplicate_rows",
    "high_null_columns",
    "constant_columns",
    "issues",
    "recommendations",
    "summary",
)

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
        {
            "profile": _OBJECT,
            "roles": _ROLES_OUTPUT_SCHEMA,
            "quality": _QUALITY_OUTPUT_SCHEMA,
        },
        "profile",
        "roles",
        "quality",
    ),
    "kb_search": _object(
        {"is_empty": {"type": "boolean"}, "hits": _ARRAY}, "is_empty", "hits"
    ),
    "domain_definition_lookup": _object(
        {
            "status": _STRING,
            "is_empty": {"type": "boolean"},
            "requires_clarification": {"type": "boolean"},
            "semantic_key": _STRING,
            "as_of": _STRING,
            "definition": {"type": ["object", "null"]},
            "candidates": _ARRAY,
            "compilation_status": _STRING,
            "compiled_invocation": {"type": ["object", "null"]},
        },
        "status",
        "is_empty",
        "requires_clarification",
        "semantic_key",
        "as_of",
        "definition",
        "candidates",
        "compilation_status",
        "compiled_invocation",
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
    capability: str | tuple[str, ...],
    *artifact_types: str,
    read_only: bool = True,
    idempotent: bool = True,
    risk_level: str = "low",
    tool_version: str = "1.0.0",
) -> ToolCapabilityMetadata:
    """Construct reviewed metadata; callers cannot obtain it from model arguments."""
    risk: RiskLevel
    if risk_level == "low":
        risk = "low"
    elif risk_level == "medium":
        risk = "medium"
    elif risk_level == "high":
        risk = "high"
    elif risk_level == "critical":
        risk = "critical"
    else:
        raise ValueError(f"invalid risk level: {risk_level}")
    capabilities = (capability,) if isinstance(capability, str) else capability
    if not capabilities or any(not item.strip() for item in capabilities):
        raise ValueError("capability 不能为空")
    if not tool_version.strip():
        raise ValueError("tool_version 不能为空")
    return ToolCapabilityMetadata(
        capabilities=capabilities,
        tool_version=tool_version,
        artifact_types=tuple(artifact_types),
        read_only=read_only,
        destructive=False,
        idempotent=idempotent,
        risk_level=risk,
    )
