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
_NUMBER = {"type": "number"}
_NUMBER_OR_NULL = {"type": ["number", "null"]}
_ARRAY = {"type": "array"}
_OBJECT = {"type": "object"}
_SCALAR = {"type": ["string", "number", "boolean", "null"]}

_STATISTICAL_EVIDENCE_SCHEMA = _closed_object(
    {
        "schema": {"const": "chatbi-statistical-evidence-v1"},
        "analysis_kind": {
            "type": "string",
            "enum": [
                "trend",
                "anomaly",
                "regression",
                "correlation",
                "contribution",
                "group_comparison",
                "forecast",
            ],
        },
        "method": _STRING,
        "sample": _closed_object(
            {
                "total_rows": _INTEGER,
                "valid_rows": _INTEGER,
                "excluded_rows": _INTEGER,
                "missing_policy": {"const": "complete_case_drop"},
                "minimum_required": _INTEGER,
                "meets_minimum": {"type": "boolean"},
            },
            "total_rows",
            "valid_rows",
            "excluded_rows",
            "missing_policy",
            "minimum_required",
            "meets_minimum",
        ),
        "inference": _closed_object(
            {
                "alpha": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
                "tests_count": _INTEGER,
                "multiple_testing_method": {"type": "string", "enum": ["none", "holm"]},
                "causal_claim_allowed": {"const": False},
            },
            "alpha",
            "tests_count",
            "multiple_testing_method",
            "causal_claim_allowed",
        ),
        "assumptions": {"type": "array", "items": _STRING, "minItems": 1},
        "limitations": {"type": "array", "items": _STRING, "minItems": 1},
    },
    "schema",
    "analysis_kind",
    "method",
    "sample",
    "inference",
    "assumptions",
    "limitations",
)

_SMALL_GROUP_PROTECTION_SCHEMA = _closed_object(
    {
        "minimum_group_size": _INTEGER,
        "mode": {"type": "string", "enum": ["merge", "drop"]},
        "protected_group_count": _INTEGER,
        "protected_row_count": _INTEGER,
    },
    "minimum_group_size",
    "mode",
    "protected_group_count",
    "protected_row_count",
)

_DIAGNOSTIC_TEST_SCHEMA = _closed_object(
    {
        "test": _STRING,
        "statistic": _NUMBER_OR_NULL,
        "p_value": _NUMBER_OR_NULL,
        "passed": {"type": "boolean"},
    },
    "test",
    "statistic",
    "p_value",
    "passed",
)
_AUTOCORRELATION_DIAGNOSTIC_SCHEMA = _closed_object(
    {
        "test": _STRING,
        "statistic": _NUMBER_OR_NULL,
        "passed": {"type": "boolean"},
    },
    "test",
    "statistic",
    "passed",
)
_VIF_SCHEMA = _closed_object(
    {"name": _STRING, "vif": _NUMBER_OR_NULL},
    "name",
    "vif",
)
_REGRESSION_DIAGNOSTICS_SCHEMA = _closed_object(
    {
        "residual_normality": {
            "oneOf": [_DIAGNOSTIC_TEST_SCHEMA, {"type": "null"}]
        },
        "heteroskedasticity": {
            "oneOf": [_DIAGNOSTIC_TEST_SCHEMA, {"type": "null"}]
        },
        "autocorrelation": {
            "oneOf": [_AUTOCORRELATION_DIAGNOSTIC_SCHEMA, {"type": "null"}]
        },
        "multicollinearity": _closed_object(
            {
                "condition_number": _NUMBER_OR_NULL,
                "max_vif": _NUMBER_OR_NULL,
                "vif": {"type": "array", "items": _VIF_SCHEMA},
                "rank_deficient": {"type": "boolean"},
            },
            "condition_number",
            "max_vif",
            "vif",
            "rank_deficient",
        ),
        "warnings": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "residual_non_normal",
                    "heteroskedasticity_detected",
                    "residual_autocorrelation",
                    "multicollinearity_risk",
                ],
            },
        },
    },
    "residual_normality",
    "heteroskedasticity",
    "autocorrelation",
    "multicollinearity",
    "warnings",
)

_CONTRIBUTION_GROUP_SCHEMA = _closed_object(
    {
        "dimension": _SCALAR,
        "value": _NUMBER_OR_NULL,
        "count": _INTEGER,
        "share": _NUMBER_OR_NULL,
        "rank": _INTEGER,
        "protected": {"type": "boolean"},
    },
    "dimension",
    "value",
    "count",
    "share",
    "rank",
    "protected",
)
_GROUP_SUMMARY_SCHEMA = _closed_object(
    {
        "group": _SCALAR,
        "count": _INTEGER,
        "mean": _NUMBER_OR_NULL,
        "std": _NUMBER_OR_NULL,
        "median": _NUMBER_OR_NULL,
        "ci95_low": _NUMBER_OR_NULL,
        "ci95_high": _NUMBER_OR_NULL,
    },
    "group",
    "count",
    "mean",
    "std",
    "median",
    "ci95_low",
    "ci95_high",
)
_GROUP_OVERALL_SCHEMA = _closed_object(
    {
        "test": {"type": "string", "enum": ["welch_t", "welch_anova"]},
        "statistic": _NUMBER_OR_NULL,
        "p_value": _NUMBER_OR_NULL,
        "df1": _NUMBER_OR_NULL,
        "df2": _NUMBER_OR_NULL,
        "significant": {"type": "boolean"},
    },
    "test",
    "statistic",
    "p_value",
    "df1",
    "df2",
    "significant",
)
_PAIRWISE_COMPARISON_SCHEMA = _closed_object(
    {
        "left": _SCALAR,
        "right": _SCALAR,
        "mean_difference": _NUMBER_OR_NULL,
        "statistic": _NUMBER_OR_NULL,
        "p_value": _NUMBER_OR_NULL,
        "adjusted_p_value": _NUMBER_OR_NULL,
        "significant": {"type": "boolean"},
        "effect_size_hedges_g": _NUMBER_OR_NULL,
    },
    "left",
    "right",
    "mean_difference",
    "statistic",
    "p_value",
    "adjusted_p_value",
    "significant",
    "effect_size_hedges_g",
)

_FORECAST_METRICS_SCHEMA = _closed_object(
    {
        "mae": _NUMBER_OR_NULL,
        "rmse": _NUMBER_OR_NULL,
        "smape": _NUMBER_OR_NULL,
        "mape": _NUMBER_OR_NULL,
    },
    "mae",
    "rmse",
    "smape",
    "mape",
)
_FORECAST_SPLIT_SCHEMA = _closed_object(
    {
        "total_observations": _INTEGER,
        "training_observations": _INTEGER,
        "validation_observations": _INTEGER,
        "training_start": _STRING,
        "training_end": _STRING,
        "validation_start": _STRING,
        "validation_end": _STRING,
    },
    "total_observations",
    "training_observations",
    "validation_observations",
    "training_start",
    "training_end",
    "validation_start",
    "validation_end",
)
_FORECAST_BASELINE_SCHEMA = _closed_object(
    {
        "method": {"const": "naive"},
        "metrics": _FORECAST_METRICS_SCHEMA,
        "beats_baseline": {"type": "boolean"},
        "mae_improvement": _NUMBER_OR_NULL,
        "mae_improvement_percent": _NUMBER_OR_NULL,
    },
    "method",
    "metrics",
    "beats_baseline",
    "mae_improvement",
    "mae_improvement_percent",
)
_FORECAST_INTERVAL_SCHEMA = _closed_object(
    {
        "level": {"const": 0.95},
        "method": {"const": "empirical_absolute_error"},
        "radius": _NUMBER_OR_NULL,
        "validation_coverage": _NUMBER_OR_NULL,
    },
    "level",
    "method",
    "radius",
    "validation_coverage",
)
_FORECAST_LEAKAGE_SCHEMA = _closed_object(
    {
        "passed": {"const": True},
        "chronological_split": {"const": True},
        "duplicate_timestamps": {"const": False},
        "regular_frequency": {"const": True},
        "future_target_rows_used": {"const": False},
        "preprocessing_fit_on_training_only": {"const": True},
    },
    "passed",
    "chronological_split",
    "duplicate_timestamps",
    "regular_frequency",
    "future_target_rows_used",
    "preprocessing_fit_on_training_only",
)
_FORECAST_POINT_SCHEMA = _closed_object(
    {
        "time": _STRING,
        "point": _NUMBER_OR_NULL,
        "lower": _NUMBER_OR_NULL,
        "upper": _NUMBER_OR_NULL,
    },
    "time",
    "point",
    "lower",
    "upper",
)
_FORECAST_CANDIDATE_METRICS_SCHEMA = _closed_object(
    {
        "naive": _FORECAST_METRICS_SCHEMA,
        "drift": _FORECAST_METRICS_SCHEMA,
        "seasonal_naive": _FORECAST_METRICS_SCHEMA,
    }
)

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
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "method",
        "direction",
        "slope",
        "n",
        "points",
        "forecast",
        "statistical_evidence",
    ),
    "forecast": _closed_object(
        {
            "requested_method": {
                "type": "string",
                "enum": ["auto", "naive", "drift", "seasonal_naive"],
            },
            "selected_method": {
                "type": "string",
                "enum": ["naive", "drift", "seasonal_naive"],
            },
            "reliability": {"type": "string", "enum": ["moderate", "limited"]},
            "frequency": _STRING,
            "horizon": _INTEGER,
            "seasonal_period": {"type": ["integer", "null"]},
            "split": _FORECAST_SPLIT_SCHEMA,
            "validation_metrics": _FORECAST_METRICS_SCHEMA,
            "baseline": _FORECAST_BASELINE_SCHEMA,
            "prediction_interval": _FORECAST_INTERVAL_SCHEMA,
            "leakage_checks": _FORECAST_LEAKAGE_SCHEMA,
            "candidate_metrics": _FORECAST_CANDIDATE_METRICS_SCHEMA,
            "predictions": {"type": "array", "items": _FORECAST_POINT_SCHEMA},
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "requested_method",
        "selected_method",
        "reliability",
        "frequency",
        "horizon",
        "seasonal_period",
        "split",
        "validation_metrics",
        "baseline",
        "prediction_interval",
        "leakage_checks",
        "candidate_metrics",
        "predictions",
        "statistical_evidence",
    ),
    "anomaly_detect": _object(
        {
            "method": _STRING,
            "n_total": _INTEGER,
            "n_anomalies": _INTEGER,
            "anomalies": _ARRAY,
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "method",
        "n_total",
        "n_anomalies",
        "anomalies",
        "statistical_evidence",
    ),
    "regression": _object(
        {
            "kind": _STRING,
            "r_squared": _NUMBER_OR_NULL,
            "n_obs": _INTEGER,
            "coefficients": _ARRAY,
            "diagnostics": _REGRESSION_DIAGNOSTICS_SCHEMA,
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "kind",
        "r_squared",
        "n_obs",
        "coefficients",
        "diagnostics",
        "statistical_evidence",
    ),
    "correlation": _object(
        {
            "method": _STRING,
            "columns": _ARRAY,
            "n_obs": _INTEGER,
            "matrix": _ARRAY,
            "top_pairs": _ARRAY,
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "method",
        "columns",
        "n_obs",
        "matrix",
        "top_pairs",
        "statistical_evidence",
    ),
    "dimension_contribution": _closed_object(
        {
            "method": {"type": "string", "enum": ["sum", "count"]},
            "dimension_col": _STRING,
            "value_col": _STRING,
            "total_value": _NUMBER_OR_NULL,
            "groups": {"type": "array", "items": _CONTRIBUTION_GROUP_SCHEMA},
            "group_count": _INTEGER,
            "truncated": {"type": "boolean"},
            "returned_share": _NUMBER_OR_NULL,
            "small_group_protection": _SMALL_GROUP_PROTECTION_SCHEMA,
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "method",
        "dimension_col",
        "value_col",
        "total_value",
        "groups",
        "group_count",
        "truncated",
        "returned_share",
        "small_group_protection",
        "statistical_evidence",
    ),
    "group_compare": _closed_object(
        {
            "method": {"type": "string", "enum": ["welch_t", "welch_anova"]},
            "group_col": _STRING,
            "value_col": _STRING,
            "groups": {"type": "array", "items": _GROUP_SUMMARY_SCHEMA},
            "overall": _GROUP_OVERALL_SCHEMA,
            "pairwise": {"type": "array", "items": _PAIRWISE_COMPARISON_SCHEMA},
            "small_group_protection": _SMALL_GROUP_PROTECTION_SCHEMA,
            "statistical_evidence": _STATISTICAL_EVIDENCE_SCHEMA,
        },
        "method",
        "group_col",
        "value_col",
        "groups",
        "overall",
        "pairwise",
        "small_group_protection",
        "statistical_evidence",
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
    "join_preflight": _closed_object(
        {
            "schema": {"const": "chatbi-join-preflight-v1"},
            "status": {
                "type": "string",
                "enum": ["ready", "requires_confirmation", "blocked"],
            },
            "join_type": {
                "type": "string",
                "enum": ["inner", "left", "right", "full"],
            },
            "relationship": {
                "type": "string",
                "enum": [
                    "one_to_one",
                    "one_to_many",
                    "many_to_one",
                    "many_to_many",
                    "no_matches",
                    "incompatible",
                ],
            },
            "left": _closed_object(
                {
                    "key": _STRING,
                    "dtype": _STRING,
                    "row_count": _INTEGER,
                    "non_null_count": _INTEGER,
                    "null_count": _INTEGER,
                    "distinct_count": _INTEGER,
                    "duplicate_key_rows": _INTEGER,
                    "unique_non_null": {"type": "boolean"},
                },
                "key",
                "dtype",
                "row_count",
                "non_null_count",
                "null_count",
                "distinct_count",
                "duplicate_key_rows",
                "unique_non_null",
            ),
            "right": _closed_object(
                {
                    "key": _STRING,
                    "dtype": _STRING,
                    "row_count": _INTEGER,
                    "non_null_count": _INTEGER,
                    "null_count": _INTEGER,
                    "distinct_count": _INTEGER,
                    "duplicate_key_rows": _INTEGER,
                    "unique_non_null": {"type": "boolean"},
                },
                "key",
                "dtype",
                "row_count",
                "non_null_count",
                "null_count",
                "distinct_count",
                "duplicate_key_rows",
                "unique_non_null",
            ),
            "matching_key_count": _INTEGER,
            "matched_left_rows": _INTEGER,
            "matched_right_rows": _INTEGER,
            "estimated_output_rows": _INTEGER,
            "expansion_ratio": _NUMBER,
            "max_output_rows": _INTEGER,
            "risks": {
                "type": "array",
                "items": _closed_object(
                    {
                        "code": {
                            "type": "string",
                            "enum": [
                                "incompatible_key_types",
                                "no_matching_keys",
                                "output_row_limit",
                                "many_to_many",
                                "row_expansion",
                                "left_null_keys",
                                "right_null_keys",
                            ],
                        },
                        "severity": {
                            "type": "string",
                            "enum": ["warning", "blocking"],
                        },
                        "message": _STRING,
                    },
                    "code",
                    "severity",
                    "message",
                ),
            },
            "requires_confirmation": {"type": "boolean"},
            "executable": {"type": "boolean"},
            "mutates_data": {"const": False},
            "raw_rows_returned": {"const": False},
        },
        "schema",
        "status",
        "join_type",
        "relationship",
        "left",
        "right",
        "matching_key_count",
        "matched_left_rows",
        "matched_right_rows",
        "estimated_output_rows",
        "expansion_ratio",
        "max_output_rows",
        "risks",
        "requires_confirmation",
        "executable",
        "mutates_data",
        "raw_rows_returned",
    ),
    "join_datasets": _closed_object(
        {
            "schema": {"const": "chatbi-join-result-v1"},
            "dataset_ref": _STRING,
            "parent_ref": _STRING,
            "parent_refs": {
                "type": "array",
                "items": _STRING,
                "minItems": 2,
                "maxItems": 2,
            },
            "join_type": {
                "type": "string",
                "enum": ["inner", "left", "right", "full"],
            },
            "left_key": _STRING,
            "right_key": _STRING,
            "rows": _INTEGER,
            "columns": {"type": "array", "items": _STRING},
            "relationship": {
                "type": "string",
                "enum": [
                    "one_to_one",
                    "one_to_many",
                    "many_to_one",
                    "many_to_many",
                ],
            },
            "preflight_status": {
                "type": "string",
                "enum": ["ready", "requires_confirmation"],
            },
            "risks": {
                "type": "array",
                "items": _closed_object(
                    {
                        "code": {
                            "type": "string",
                            "enum": [
                                "many_to_many",
                                "row_expansion",
                                "left_null_keys",
                                "right_null_keys",
                            ],
                        },
                        "severity": {"const": "warning"},
                        "message": _STRING,
                    },
                    "code",
                    "severity",
                    "message",
                ),
            },
            "mutates_data": {"const": True},
            "raw_rows_returned": {"const": False},
            "registered": {"type": "boolean"},
        },
        "schema",
        "dataset_ref",
        "parent_ref",
        "parent_refs",
        "join_type",
        "left_key",
        "right_key",
        "rows",
        "columns",
        "relationship",
        "preflight_status",
        "risks",
        "mutates_data",
        "raw_rows_returned",
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
