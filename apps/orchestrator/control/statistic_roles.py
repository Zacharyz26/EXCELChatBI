"""数值字段的统计角色：业务指标相同不表示样本、参数与统计量可互换。"""

from __future__ import annotations

import re
from functools import lru_cache

# Only semantic aliases share an identity; source paths remain in Evidence refs.
_CANONICAL = {
    "n": "sample_size",
    "n_obs": "sample_size",
    "n_total": "sample_size",
    "valid_rows": "sample_size",
}
ROLE_LABELS: dict[str, tuple[str, ...]] = {
    "slope": ("斜率",),
    "intercept": ("截距",),
    "sample_size": ("样本量", "样本数", "有效样本量", "有效样本数"),
    "total_rows": ("原始行数", "原始样本数"),
    "ma_window": ("移动平均窗口", "窗口大小", "窗口"),
    "seasonality_strength": ("季节强度", "季节性强度"),
    "seasonality_period_days": ("季节周期天数",),
    "corr": ("相关系数",),
    "p_value": ("p值", "p 值", "显著性"),
    "adjusted_p_value": ("调整后p值", "校正后p值", "调整后p 值", "校正后p 值"),
    "statistic": ("检验统计量",),
    "r_squared": ("决定系数", "拟合优度", "R²"),
    "coef": ("回归系数",),
    "score": ("异常分数", "异常得分"),
    "n_anomalies": ("异常数", "异常点", "异常记录"),
    "mean": ("均值", "平均值", "平均"),
    "median": ("中位数",),
    "std": ("标准差",),
    "min": ("最小值",),
    "max": ("最大值",),
    "df1": ("分子自由度",),
    "df2": ("分母自由度",),
    "mean_difference": ("均值差", "平均值差"),
    "effect_size_hedges_g": ("效应量",),
    "forecast_horizon": ("预测步数",),
    "period": ("周期点数",),
    "tests_count": ("检验次数",),
    "alpha": ("显著性水平",),
    "adj_r_squared": ("调整后决定系数",),
    "model_pvalue": ("模型p值",),
    "std_err": ("系数标准误",),
    "mae": ("平均绝对误差", "MAE"),
    "rmse": ("均方根误差", "RMSE"),
    "mape": ("平均绝对百分比误差", "MAPE"),
    "smape": ("对称平均绝对百分比误差", "SMAPE"),
    "share": ("贡献占比",),
    "rank": ("贡献排名",),
    "group_count": ("分组数量",),
    "ci95_low": ("均值置信下限",),
    "ci95_high": ("均值置信上限",),
    "point": ("预测值",),
    "lower": ("预测下限",),
    "upper": ("预测上限",),
    "total_observations": ("总观测数",),
    "training_observations": ("训练观测数",),
    "validation_observations": ("验证观测数",),
    "horizon": ("预测期数",),
    "seasonal_period": ("季节周期",),
}

STATISTICAL_TOOLS = frozenset(
    {
        "trend_analysis",
        "correlation",
        "anomaly_detect",
        "regression",
        "group_compare",
        "forecast",
        "dimension_contribution",
    }
)


def statistic_role(path: str) -> str:
    """始终保留字段角色，未知角色也不降格成通用业务指标。"""
    leaf = re.sub(r"\[\d+\]", "[]", path.rsplit(".", 1)[-1])
    if ".option.series[" in path:
        return "value"
    return _CANONICAL.get(leaf, leaf)


def statistic_scope(path: str) -> str | None:
    """不同诊断/检验的同名统计量仍有独立身份；重复样本元数据共用角色。"""
    role = statistic_role(path)
    if role not in ROLE_LABELS or role in {
        "sample_size",
        "total_rows",
        "n_anomalies",
        "tests_count",
    }:
        return None
    return path.rsplit(".", 1)[0]


def label_pattern(label: str) -> str:
    """英文标识符遵守词法边界；中文标签允许与正文相连。"""
    left = r"(?<![A-Za-z0-9_])" if re.match(r"[A-Za-z0-9_]", label) else ""
    right = r"(?![A-Za-z0-9_])" if re.search(r"[A-Za-z0-9_]$", label) else ""
    return left + re.escape(label) + right


def role_mentions(text: str, available: set[str]) -> list[tuple[int, int, str]]:
    """先隔离工具标识符，再按统一大小写和边界提取统计角色位置。"""
    return list(_role_mentions(text, frozenset(available)))


@lru_cache(maxsize=512)
def _role_mentions(text: str, available: frozenset[str]) -> tuple[tuple[int, int, str], ...]:
    masked = text
    for tool in STATISTICAL_TOOLS:
        masked = re.sub(
            label_pattern(tool), lambda match: " " * len(match.group()), masked, flags=re.I
        )
    labels = [
        (match.start(), match.end(), role)
        for role, aliases in ROLE_LABELS.items()
        for label in (role, *aliases)
        if not (label == "样本数" and "sample_size" not in available)
        for match in re.finditer(label_pattern(label), masked, re.I)
    ]
    labels.extend(
        (match.start(), match.end(), match[1].casefold())
        for match in re.finditer(r"统计量\s*=\s*([A-Za-z_][A-Za-z_0-9]*)", masked)
        if match[1].casefold() in available
    )
    return tuple(
        sorted(
            set(
                item
                for item in labels
                if not any(
                    other[0] <= item[0]
                    and other[1] >= item[1]
                    and other[1] - other[0] > item[1] - item[0]
                    for other in labels
                )
            )
        )
    )


def mentioned_roles(text: str, available: set[str]) -> set[str]:
    """从完整子句提取角色；工具名内部的短词不构成统计量。"""
    return {role for _, _, role in role_mentions(text, available)}
