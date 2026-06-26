"""统计分析工具入参 JSON Schema（红线3）。所有数值结果必来自工具（红线2）。"""

from __future__ import annotations

from typing import Any

_DATASET = {"dataset_ref": {"type": "string"}}

TREND_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {**_DATASET, "value_col": {"type": "string"}, "time_col": {"type": "string"}},
    "required": ["dataset_ref", "value_col", "time_col"],
    "additionalProperties": False,
}

ANOMALY_DETECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_DATASET,
        "value_col": {"type": "string"},
        "method": {"type": "string", "enum": ["3sigma", "iqr", "isolation_forest", "stl"]},
    },
    "required": ["dataset_ref", "value_col"],
    "additionalProperties": False,
}

REGRESSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_DATASET,
        "target": {"type": "string"},
        "features": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["ols", "logit"]},
    },
    "required": ["dataset_ref", "target", "features"],
    "additionalProperties": False,
}
