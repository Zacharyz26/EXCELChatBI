"""统计分析工具入参 JSON Schema（红线3）。所有数值结果必来自工具（红线2）。"""

from __future__ import annotations

from typing import Any

_DATASET = {"dataset_ref": {"type": "string"}}

TREND_ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_DATASET,
        "value_col": {"type": "string"},
        "time_col": {"type": "string"},
        "method": {"type": "string", "enum": ["stl", "ma"]},
        # 季节周期（点数）；给出才做 STL 季节分解，否则退化为移动平均 + 线性趋势。
        "period": {"type": "integer", "minimum": 2},
        # 移动平均窗口；缺省由数据量自适应。
        "ma_window": {"type": "integer", "minimum": 2},
        # 线性外推预测步数（0 表示不预测）。
        "forecast_horizon": {"type": "integer", "minimum": 0},
    },
    "required": ["dataset_ref", "value_col", "time_col"],
    "additionalProperties": False,
}

ANOMALY_DETECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_DATASET,
        "value_col": {"type": "string"},
        "method": {"type": "string", "enum": ["3sigma", "iqr", "isolation_forest", "stl"]},
        # 时间列可选：给出则异常点带时间戳，STL 方法需要它排序。
        "time_col": {"type": "string"},
        # isolation_forest 预期异常占比；STL 残差需要季节周期。
        "contamination": {"type": "number", "exclusiveMinimum": 0, "maximum": 0.5},
        "period": {"type": "integer", "minimum": 2},
    },
    "required": ["dataset_ref", "value_col"],
    "additionalProperties": False,
}

REGRESSION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        **_DATASET,
        "target": {"type": "string"},
        "features": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "kind": {"type": "string", "enum": ["ols", "logit"]},
    },
    "required": ["dataset_ref", "target", "features"],
    "additionalProperties": False,
}
