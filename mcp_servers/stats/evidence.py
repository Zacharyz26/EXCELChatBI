"""Deterministic statistical Evidence guardrails shared by governed stats tools."""

from __future__ import annotations

import math
from typing import Literal, TypedDict

AnalysisKind = Literal[
    "trend",
    "anomaly",
    "regression",
    "correlation",
    "contribution",
    "group_comparison",
    "forecast",
]


class StatisticalEvidence(TypedDict):
    """Stable output shape attached to each governed statistical result."""

    schema: str
    analysis_kind: AnalysisKind
    method: str
    sample: dict[str, int | str | bool]
    inference: dict[str, int | float | str | bool]
    assumptions: list[str]
    limitations: list[str]

_MINIMUM_SAMPLE_SIZE: dict[AnalysisKind, int] = {
    "trend": 5,
    "anomaly": 5,
    "regression": 5,
    "correlation": 5,
    "contribution": 5,
    "group_comparison": 10,
    "forecast": 12,
}


def holm_adjust(p_values: list[float]) -> list[float]:
    """Return Holm-adjusted p-values in their original order."""
    if not p_values:
        return []
    normalized = [value if math.isfinite(value) and 0 <= value <= 1 else 1.0 for value in p_values]
    ordered = sorted(enumerate(normalized), key=lambda item: item[1])
    adjusted = [0.0] * len(p_values)
    running_max = 0.0
    count = len(p_values)
    for rank, (original_index, value) in enumerate(ordered):
        running_max = max(running_max, min(1.0, (count - rank) * value))
        adjusted[original_index] = running_max
    return adjusted


def build_statistical_evidence(
    *,
    analysis_kind: AnalysisKind,
    method: str,
    total_rows: int,
    valid_rows: int,
    tests_count: int = 0,
    multiple_testing_method: Literal["none", "holm"] = "none",
    minimum_required: int | None = None,
    assumptions: list[str],
    limitations: list[str],
) -> StatisticalEvidence:
    """Build the strict, model-independent ``chatbi-statistical-evidence-v1`` block."""
    minimum = (
        _MINIMUM_SAMPLE_SIZE[analysis_kind]
        if minimum_required is None
        else minimum_required
    )
    return {
        "schema": "chatbi-statistical-evidence-v1",
        "analysis_kind": analysis_kind,
        "method": method,
        "sample": {
            "total_rows": total_rows,
            "valid_rows": valid_rows,
            "excluded_rows": total_rows - valid_rows,
            "missing_policy": "complete_case_drop",
            "minimum_required": minimum,
            "meets_minimum": valid_rows >= minimum,
        },
        "inference": {
            "alpha": 0.05,
            "tests_count": tests_count,
            "multiple_testing_method": multiple_testing_method,
            "causal_claim_allowed": False,
        },
        "assumptions": assumptions,
        "limitations": limitations,
    }
