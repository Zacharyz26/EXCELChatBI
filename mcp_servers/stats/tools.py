"""统计分析工具实现（statsmodels / scikit-learn）。

红线2：所有数值结果均由本模块用 statsmodels/scikit-learn 从 dataset_ref 的**真实数据**
算出，函数内绝无 LLM 调用，LLM 仅负责事后解读（本切片暂不接解读）。
红线1：明细级输出（STL 逐行分量、异常点原值）随结果整体返回，供前端渲染（数据不出环境）；
将来接 LLM 解读时，须在编排层收敛为摘要再喂模型，不得下发逐行明细。
Prophet 预测按 CLAUDE 分阶段留后续（method 枚举预留）。
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from packages.common.dataset_store import load_dataframe
from sklearn.ensemble import IsolationForest
from statsmodels.tsa.seasonal import STL

_MIN_POINTS = 5  # 统计分析所需的最小有效样本量


# ── 共享工具 ──

def _f(value: Any) -> float | None:
    """numpy/pandas 标量 → JSON 安全 float；nan/inf → None。"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, 6)


def _require_columns(df: pd.DataFrame, cols: list[str]) -> None:
    """校验列存在，缺列抛 ValueError（→ 路由 422）。"""
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise ValueError(f"列不存在: {'、'.join(missing)}")


def _numeric(series: pd.Series, col: str) -> pd.Series:
    """把列转为数值型；无法转换（非数值列）抛 ValueError。"""
    out = pd.to_numeric(series, errors="coerce")
    if out.notna().sum() == 0:
        raise ValueError(f"列 {col} 不是数值型，无法做统计分析")
    return out


def _ordered_series(args: dict[str, Any], require_time: bool) -> tuple[pd.Series, list[str] | None]:
    """读取 value_col（可选按 time_col 升序），返回 (数值序列, 时间标签)。

    序列已丢弃缺失、重置为 0 基定位索引；时间标签与序列位置一一对应，供前端 x 轴。
    """
    df = load_dataframe(args["dataset_ref"])
    value_col: str = args["value_col"]
    time_col: str | None = args.get("time_col")
    if require_time and not time_col:
        raise ValueError("该分析需要 time_col（时间列）")

    cols = [value_col] + ([time_col] if time_col else [])
    _require_columns(df, cols)
    df = df[cols].copy()
    df[value_col] = _numeric(df[value_col], value_col)

    if time_col:
        df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
        df = df.dropna(subset=[time_col]).sort_values(time_col)
    df = df.dropna(subset=[value_col]).reset_index(drop=True)

    if len(df) < _MIN_POINTS:
        raise ValueError(f"有效样本量不足（{len(df)} < {_MIN_POINTS}），无法做统计分析")

    labels = [str(t) for t in df[time_col]] if time_col else None
    return df[value_col].astype(float), labels


def _linear_slope(y: np.ndarray) -> tuple[float, float]:
    """对序列做一元线性拟合，返回 (斜率, 截距)。"""
    x = np.arange(len(y), dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def _direction(slope: float, y: np.ndarray) -> str:
    """按拟合线端到端变化占均值绝对值的比例，判定 上升/下降/平稳。"""
    scale = float(np.mean(np.abs(y))) or 1.0
    rel = slope * (len(y) - 1) / scale
    if rel > 0.05:
        return "上升"
    if rel < -0.05:
        return "下降"
    return "平稳"


# ── 趋势分析 ──

def trend_analysis(args: dict[str, Any]) -> dict[str, Any]:
    """趋势分析：STL 时序分解 + 移动平均 + 线性外推预测。

    Args:
        args: {dataset_ref, value_col, time_col, method?("stl"|"ma"),
               period?, ma_window?, forecast_horizon?}。
            method 缺省：给了 period 走 stl，否则 ma。

    Returns:
        {method, direction, slope, seasonality_strength, ma_window, n,
         time?, points:{trend, seasonal, resid}, forecast}。
    """
    series, labels = _ordered_series(args, require_time=True)
    y = series.to_numpy()
    n = len(y)

    period: int | None = args.get("period")
    method: str = args.get("method") or ("stl" if period else "ma")

    slope, intercept = _linear_slope(y)
    direction = _direction(slope, y)

    ma_window: int = args.get("ma_window") or max(2, min(n // 4, 12))
    ma_window = min(ma_window, n)
    ma = pd.Series(y).rolling(window=ma_window, min_periods=1, center=True).mean().to_numpy()

    seasonality_strength: float | None = None
    if method == "stl":
        if not period:
            raise ValueError("method=stl 需要提供 period（季节周期，点数）")
        if n < 2 * period:
            raise ValueError(f"STL 需至少 2 个完整周期（样本 {n} < 2×{period}），请减小 period")
        res = STL(y, period=period, robust=True).fit()
        trend, seasonal, resid = res.trend, res.seasonal, res.resid
        # 季节强度 = max(0, 1 - Var(resid)/Var(seasonal+resid))（Hyndman 定义）
        denom = float(np.var(seasonal + resid))
        seasonality_strength = _f(max(0.0, 1 - float(np.var(resid)) / denom)) if denom else 0.0
    else:  # ma：移动平均作趋势，残差 = 原值 - 趋势，无季节项
        trend, seasonal, resid = ma, np.zeros(n), y - ma

    # 线性外推预测（红线2：预测值来自拟合，不经 LLM）
    horizon: int = args.get("forecast_horizon", 0)
    forecast = [_f(slope * (n + i) + intercept) for i in range(horizon)]

    return {
        "method": method,
        "direction": direction,
        "slope": _f(slope),
        "seasonality_strength": seasonality_strength,
        "ma_window": ma_window,
        "n": n,
        "time": labels,
        "points": {
            "trend": [_f(v) for v in trend],
            "seasonal": [_f(v) for v in seasonal],
            "resid": [_f(v) for v in resid],
        },
        "forecast": forecast,
    }


# ── 异常检测 ──

def anomaly_detect(args: dict[str, Any]) -> dict[str, Any]:
    """异常检测：IQR / 3σ / Isolation Forest / STL 残差。

    Args:
        args: {dataset_ref, value_col, method?, time_col?, contamination?, period?}。
            method 缺省 iqr。stl 需 time_col + period。

    Returns:
        {method, n_total, n_anomalies, anomalies:[{index, value, score, time?}]}。
        anomalies 按 score 降序，全量返回供前端渲染（红线1：明细仅到前端）。
    """
    method: str = args.get("method", "iqr")
    series, labels = _ordered_series(args, require_time=(method == "stl"))
    y = series.to_numpy()
    n = len(y)

    if method == "iqr":
        q1, q3 = np.percentile(y, [25, 75])
        iqr = q3 - q1
        lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
        mask = (y < lo) | (y > hi)
        scale = iqr or 1.0
        scores = np.maximum(lo - y, y - hi) / scale
    elif method == "3sigma":
        mu, sigma = float(np.mean(y)), float(np.std(y))
        scores = np.abs(y - mu) / (sigma or 1.0)
        mask = scores > 3
    elif method == "isolation_forest":
        contamination = args.get("contamination", 0.05)
        model = IsolationForest(contamination=contamination, random_state=0)
        pred = model.fit_predict(y.reshape(-1, 1))
        scores = -model.decision_function(y.reshape(-1, 1))  # 越大越异常
        mask = pred == -1
    elif method == "stl":
        period = args.get("period")
        if not period:
            raise ValueError("method=stl 需要提供 period（季节周期，点数）")
        if n < 2 * period:
            raise ValueError(f"STL 需至少 2 个完整周期（样本 {n} < 2×{period}）")
        resid = STL(y, period=period, robust=True).fit().resid
        rsigma = float(np.std(resid)) or 1.0
        scores = np.abs(resid) / rsigma
        mask = scores > 3
    else:  # schema 已限枚举，兜底防御
        raise ValueError(f"不支持的异常检测方法: {method}")

    idx = np.nonzero(mask)[0]
    anomalies: list[dict[str, Any]] = [
        {
            "index": int(i),
            "value": _f(y[i]),
            "score": _f(scores[i]),
            **({"time": labels[i]} if labels else {}),
        }
        for i in idx
    ]
    # 按异常分降序（None 分排最后）
    def _score_key(a: dict[str, Any]) -> float:
        s = a["score"]
        return -s if isinstance(s, int | float) else math.inf

    anomalies.sort(key=_score_key)
    return {
        "method": method,
        "n_total": n,
        "n_anomalies": len(anomalies),
        "anomalies": anomalies,
    }


# ── 回归分析 ──

def regression(args: dict[str, Any]) -> dict[str, Any]:
    """回归分析：statsmodels OLS / Logit，输出系数、标准误、p 值、R²、显著性。

    Args:
        args: {dataset_ref, target, features[], kind?("ols"|"logit")}。kind 缺省 ols。

    Returns:
        {kind, r_squared, adj_r_squared, n_obs, model_pvalue,
         coefficients:[{name, coef, std_err, p_value, significant}]}。
        Logit 的 r_squared 为 McFadden 伪 R²，model_pvalue 为似然比检验 p 值。
    """
    kind: str = args.get("kind", "ols")
    target: str = args["target"]
    features: list[str] = args["features"]

    df = load_dataframe(args["dataset_ref"])
    _require_columns(df, [target, *features])
    used = [target, *features]
    data = df[used].apply(pd.to_numeric, errors="coerce").dropna()
    if len(data) < _MIN_POINTS:
        raise ValueError(f"有效样本量不足（{len(data)} < {_MIN_POINTS}），无法拟合回归")

    x = sm.add_constant(data[features], has_constant="add")
    y = data[target]

    if kind == "ols":
        res = sm.OLS(y, x).fit()
        r_squared, adj = _f(res.rsquared), _f(res.rsquared_adj)
        model_pvalue = _f(res.f_pvalue)
    elif kind == "logit":
        if set(pd.unique(y)) - {0, 1}:
            raise ValueError("Logit 要求 target 为 0/1 二分类")
        res = sm.Logit(y, x).fit(disp=0)
        r_squared, adj = _f(res.prsquared), None  # McFadden 伪 R²
        model_pvalue = _f(res.llr_pvalue)
    else:  # schema 已限枚举，兜底防御
        raise ValueError(f"不支持的回归类型: {kind}")

    coefficients = [
        {
            "name": name,
            "coef": _f(res.params[name]),
            "std_err": _f(res.bse[name]),
            "p_value": _f(res.pvalues[name]),
            "significant": bool(res.pvalues[name] < 0.05),
        }
        for name in res.params.index
    ]
    return {
        "kind": kind,
        "r_squared": r_squared,
        "adj_r_squared": adj,
        "n_obs": int(res.nobs),
        "model_pvalue": model_pvalue,
        "coefficients": coefficients,
    }
