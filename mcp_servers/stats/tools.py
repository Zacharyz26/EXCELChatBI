"""统计分析工具实现（statsmodels / scikit-learn）。

红线2：所有数值结果均由本模块用 statsmodels/scikit-learn 从 dataset_ref 的**真实数据**
算出，函数内绝无 LLM 调用，LLM 仅负责事后解读（本切片暂不接解读）。
红线1：明细级输出（STL 逐行分量、异常点原值）随结果整体返回，供前端渲染（数据不出环境）；
将来接 LLM 解读时，须在编排层收敛为摘要再喂模型，不得下发逐行明细。
趋势支持 STL / 移动平均 / Prophet（prophet 惰性导入，需 .[stats]）。
"""

from __future__ import annotations

import math
import warnings as py_warnings
from itertools import combinations
from typing import Any

import numpy as np
import pandas as pd
import statsmodels.api as sm
from packages.common.dataset_store import load_dataframe
from packages.governance.aggregation_guard import GroupAgg, guard_small_groups
from packages.governance.data_boundary import ColumnRule, resolve_policy
from scipy import stats as scipy_stats
from sklearn.ensemble import IsolationForest
from statsmodels.stats.diagnostic import het_breuschpagan
from statsmodels.stats.outliers_influence import variance_inflation_factor
from statsmodels.stats.stattools import durbin_watson, jarque_bera
from statsmodels.tsa.seasonal import STL

from mcp_servers.stats.evidence import build_statistical_evidence, holm_adjust

_MIN_POINTS = 5  # 统计分析所需的最小有效样本量
_MAX_COMPARISON_GROUPS = 10


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


def _plain(value: Any) -> Any:
    """Convert numpy/pandas scalars to JSON-safe native values."""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    converted = value.item() if hasattr(value, "item") else value
    if converted is None or isinstance(converted, str | int | float | bool):
        return converted
    return str(converted)


def _require_model_visible_columns(dataset_ref: str, columns: list[str]) -> None:
    """Prevent new stats tools from returning protected labels/aggregates to the model."""
    policy = resolve_policy(dataset_ref)
    blocked = [
        column
        for column in columns
        if policy.rule_of(column) in {ColumnRule.MASK, ColumnRule.EXCLUDE}
    ]
    if blocked:
        raise ValueError("列受数据策略保护，不能进入统计模型结果: " + "、".join(blocked))


def _ordered_series(
    args: dict[str, Any], require_time: bool
) -> tuple[pd.Series, list[str] | None, int]:
    """读取 value_col（可选按 time_col 升序），返回 (数值序列, 时间标签)。

    序列已丢弃缺失、重置为 0 基定位索引；时间标签与序列位置一一对应，供前端 x 轴。
    """
    df = load_dataframe(args["dataset_ref"])
    total_rows = len(df)
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
    return df[value_col].astype(float), labels, total_rows


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

def _prophet_decompose(
    y: np.ndarray, labels: list[str], period: int | None, horizon: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray, list[float | None], float | None]:
    """用 Prophet 拟合，返回 (趋势, 季节, 残差, 预测, 季节强度)。

    红线2：趋势/预测值均由 Prophet 从真实数据拟合，非 LLM 产出。惰性导入 Prophet，
    使未装/未用 prophet 时本模块其余功能不受影响。
    """
    import logging

    from prophet import Prophet

    # 静音 cmdstanpy/prophet 的 INFO 噪声
    logging.getLogger("cmdstanpy").setLevel(logging.WARNING)
    logging.getLogger("prophet").setLevel(logging.WARNING)

    ds = pd.to_datetime(labels)
    df = pd.DataFrame({"ds": ds, "y": y})
    model = Prophet(
        weekly_seasonality=False, daily_seasonality=False, yearly_seasonality=False
    )
    if period:  # 与 STL 一致：显式周期才做季节项
        model.add_seasonality(name="seasonal", period=period, fourier_order=3)
    model.fit(df)

    # 历史：对原始每行的 ds 预测，保证与 y 逐行对齐（Prophet 会把历史折叠成唯一日期，
    # 直接切片会和含重复日期的原始行数对不上，故显式按原 ds 预测）。
    hist = model.predict(df[["ds"]])
    trend = hist["trend"].to_numpy()
    yhat_hist = hist["yhat"].to_numpy()
    seasonal = yhat_hist - trend
    resid = y - yhat_hist

    # 未来：单独外推 horizon 期（不含历史）
    forecast: list[float | None] = []
    if horizon > 0:
        freq = pd.infer_freq(ds.sort_values()) or "D"
        future = model.make_future_dataframe(periods=horizon, freq=freq, include_history=False)
        forecast = [_f(v) for v in model.predict(future)["yhat"].to_numpy()[:horizon]]

    denom = float(np.var(seasonal + resid))
    strength = _f(max(0.0, 1 - float(np.var(resid)) / denom)) if denom else 0.0
    return trend, seasonal, resid, forecast, strength


def trend_analysis(args: dict[str, Any]) -> dict[str, Any]:
    """趋势分析：STL 时序分解 / 移动平均 / Prophet + 预测。

    Args:
        args: {dataset_ref, value_col, time_col, method?("stl"|"ma"|"prophet"),
               period?, ma_window?, forecast_horizon?}。
            method 缺省：给了 period 走 stl，否则 ma；prophet 需显式指定。
            stl/ma 用线性外推预测，prophet 用其自身预测。

    Returns:
        {method, direction, slope, seasonality_strength, ma_window, n,
         time?, points:{trend, seasonal, resid}, forecast}。
    """
    series, labels, total_rows = _ordered_series(args, require_time=True)
    y = series.to_numpy()
    n = len(y)

    period: int | None = args.get("period")
    method: str = args.get("method") or ("stl" if period else "ma")

    slope, intercept = _linear_slope(y)
    direction = _direction(slope, y)

    ma_window: int = args.get("ma_window") or max(2, min(n // 4, 12))
    ma_window = min(ma_window, n)
    ma = pd.Series(y).rolling(window=ma_window, min_periods=1, center=True).mean().to_numpy()

    horizon: int = args.get("forecast_horizon", 0)
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
        # 线性外推预测（红线2：预测值来自拟合，不经 LLM）
        forecast = [_f(slope * (n + i) + intercept) for i in range(horizon)]
    elif method == "prophet":
        if period and n < 2 * period:
            raise ValueError(f"Prophet 季节分解需至少 2 个完整周期（样本 {n} < 2×{period}）")
        assert labels is not None  # require_time=True 保证有时间列
        trend, seasonal, resid, forecast, seasonality_strength = _prophet_decompose(
            y, labels, period, horizon
        )
    else:  # ma：移动平均作趋势，残差 = 原值 - 趋势，无季节项
        trend, seasonal, resid = ma, np.zeros(n), y - ma
        forecast = [_f(slope * (n + i) + intercept) for i in range(horizon)]

    limitations = [
        "趋势方向描述统计关联，不证明时间变化导致指标变化。",
        "未执行训练/验证隔离、时间泄漏检测和预测区间评估。",
    ]
    if horizon > 0:
        limitations.append(
            "forecast 仅为探索性外推，不属于受治理 stats.forecast 预测结论。"
        )
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
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="trend",
            method=method,
            total_rows=total_rows,
            valid_rows=n,
            assumptions=[
                "有效记录按时间升序排列，缺失的时间或指标记录按完整案例剔除。",
                "趋势方法假定当前时间粒度和用户指定周期适用于所选序列。",
            ],
            limitations=limitations,
        ),
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
    series, labels, total_rows = _ordered_series(args, require_time=(method == "stl"))
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
    threshold_limit = {
        "iqr": "异常阈值固定为 Q1-1.5×IQR 或 Q3+1.5×IQR。",
        "3sigma": "异常阈值固定为距均值超过 3 个总体标准差。",
        "isolation_forest": "异常数量受 contamination 参数和固定 random_state=0 影响。",
        "stl": "异常阈值固定为 STL 残差绝对值超过 3 个残差标准差。",
    }[method]
    return {
        "method": method,
        "n_total": n,
        "n_anomalies": len(anomalies),
        "anomalies": anomalies,
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="anomaly",
            method=method,
            total_rows=total_rows,
            valid_rows=n,
            assumptions=[
                "缺失指标记录按完整案例剔除。",
                "所选异常检测方法及其阈值适用于当前数据分布。",
            ],
            limitations=[
                threshold_limit,
                "异常点仅表示统计偏离，根因只能作为待验证的候选解释因素。",
            ],
        ),
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

    if target in features:
        raise ValueError("因变量不能同时作为自变量")
    if len(features) != len(set(features)):
        raise ValueError("自变量不能重复")

    df = load_dataframe(args["dataset_ref"])
    _require_columns(df, [target, *features])
    used = [target, *features]
    total_rows = len(df)
    data = df[used].apply(pd.to_numeric, errors="coerce").dropna()
    minimum_required = max(10, 5 * (len(features) + 1))
    if len(data) < minimum_required:
        raise ValueError(
            f"有效样本量不足（{len(data)} < {minimum_required}），无法拟合回归"
        )

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

    coefficient_names = list(res.params.index)
    tested_names = [name for name in coefficient_names if name != "const"]
    raw_test_pvalues = [float(res.pvalues[name]) for name in tested_names]
    adjusted_by_name = dict(zip(tested_names, holm_adjust(raw_test_pvalues), strict=True))
    coefficients = [
        {
            "name": name,
            "coef": _f(res.params[name]),
            "std_err": _f(res.bse[name]),
            "p_value": _f(res.pvalues[name]),
            "adjusted_p_value": (
                _f(adjusted_by_name[name]) if name in adjusted_by_name else None
            ),
            "significant": bool(
                adjusted_by_name.get(name, float(res.pvalues[name])) < 0.05
            ),
        }
        for name in coefficient_names
    ]
    raw_condition_number = float(np.linalg.cond(np.asarray(x, dtype=float)))
    condition_number = _f(raw_condition_number)
    vif_items: list[dict[str, Any]] = []
    non_finite_vif = False
    if len(features) == 1:
        vif_items.append({"name": features[0], "vif": 1.0})
    else:
        design = np.asarray(x, dtype=float)
        for index, name in enumerate(x.columns):
            if name == "const":
                continue
            with py_warnings.catch_warnings():
                py_warnings.simplefilter("ignore", RuntimeWarning)
                raw_vif = float(variance_inflation_factor(design, index))
            non_finite_vif = non_finite_vif or not math.isfinite(raw_vif)
            vif_items.append({"name": str(name), "vif": _f(raw_vif)})
    finite_vifs = [
        float(item["vif"])
        for item in vif_items
        if isinstance(item.get("vif"), int | float)
    ]
    max_vif = _f(max(finite_vifs)) if finite_vifs else None
    warnings: list[str] = []
    normality: dict[str, Any] | None = None
    heteroskedasticity: dict[str, Any] | None = None
    autocorrelation: dict[str, Any] | None = None
    if kind == "ols":
        jb_stat, jb_pvalue, _, _ = jarque_bera(res.resid)
        bp_stat, bp_pvalue, _, _ = het_breuschpagan(res.resid, x)
        dw_stat = float(durbin_watson(res.resid))
        normality = {
            "test": "jarque_bera",
            "statistic": _f(jb_stat),
            "p_value": _f(jb_pvalue),
            "passed": bool(jb_pvalue >= 0.05),
        }
        heteroskedasticity = {
            "test": "breusch_pagan",
            "statistic": _f(bp_stat),
            "p_value": _f(bp_pvalue),
            "passed": bool(bp_pvalue >= 0.05),
        }
        autocorrelation = {
            "test": "durbin_watson",
            "statistic": _f(dw_stat),
            "passed": bool(1.5 <= dw_stat <= 2.5),
        }
        if not normality["passed"]:
            warnings.append("residual_non_normal")
        if not heteroskedasticity["passed"]:
            warnings.append("heteroskedasticity_detected")
        if not autocorrelation["passed"]:
            warnings.append("residual_autocorrelation")
    rank_deficient = bool(
        non_finite_vif
        or not math.isfinite(raw_condition_number)
        or np.linalg.matrix_rank(np.asarray(x, dtype=float)) < x.shape[1]
    )
    if rank_deficient or (max_vif is not None and max_vif >= 5) or (
        condition_number is not None and condition_number >= 30
    ):
        warnings.append("multicollinearity_risk")
    return {
        "kind": kind,
        "r_squared": r_squared,
        "adj_r_squared": adj,
        "n_obs": int(res.nobs),
        "model_pvalue": model_pvalue,
        "coefficients": coefficients,
        "diagnostics": {
            "residual_normality": normality,
            "heteroskedasticity": heteroskedasticity,
            "autocorrelation": autocorrelation,
            "multicollinearity": {
                "condition_number": condition_number,
                "max_vif": max_vif,
                "vif": vif_items,
                "rank_deficient": rank_deficient,
            },
            "warnings": warnings,
        },
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="regression",
            method=kind,
            total_rows=total_rows,
            valid_rows=int(res.nobs),
            minimum_required=minimum_required,
            tests_count=len(tested_names),
            multiple_testing_method="holm" if len(tested_names) > 1 else "none",
            assumptions=[
                "目标列和全部特征均为数值，含缺失的记录按完整案例剔除。",
                (
                    "OLS 假定线性、独立、同方差且残差设定合理。"
                    if kind == "ols"
                    else "Logit 假定二元目标、独立观测和正确的 logit 线性设定。"
                ),
            ],
            limitations=[
                (
                    "特征系数显著性按 Holm 方法控制同一模型内的多重检验。"
                    if len(tested_names) > 1
                    else "当前仅检验一个特征系数，无需额外多重检验校正。"
                ),
                "回归关系不证明因果，遗漏变量、共线性和选择偏差仍可能影响结果。",
                "诊断告警只检查既定阈值，不会自动修改模型或选择其他特征。",
            ],
        ),
    }


# ── 相关性分析 ──

def correlation(args: dict[str, Any]) -> dict[str, Any]:
    """相关性分析：Pearson/Spearman 相关矩阵 + 最强相关对（含 p 值、显著性）。

    Args:
        args: {dataset_ref, columns[]（≥2）, method?("pearson"|"spearman")}。

    Returns:
        {method, columns, n_obs, matrix, top_pairs:[{a,b,corr,p_value,significant}]}。
        matrix 为 n×n 相关系数（供前端热力图）；top_pairs 为上三角按 |corr| 降序的聚合摘要。
    """
    method: str = args.get("method", "pearson")
    columns: list[str] = args["columns"]

    df = load_dataframe(args["dataset_ref"])
    _require_columns(df, columns)
    total_rows = len(df)
    data = df[columns].apply(pd.to_numeric, errors="coerce")
    for col in columns:
        if data[col].notna().sum() == 0:
            raise ValueError(f"列 {col} 不是数值型，无法做相关性分析")
    data = data.dropna()
    if len(data) < _MIN_POINTS:
        raise ValueError(f"有效样本量不足（{len(data)} < {_MIN_POINTS}），无法做相关性分析")

    corr = data.corr(method=method)
    n = len(columns)
    matrix = [[_f(corr.iat[i, j]) for j in range(n)] for i in range(n)]

    # 上三角所有列对逐对补 p 值（scipy），按 |corr| 降序取前 5 作摘要
    pair_fn = scipy_stats.pearsonr if method == "pearson" else scipy_stats.spearmanr
    raw_pairs: list[tuple[str, str, float, float]] = []
    for i in range(n):
        for j in range(i + 1, n):
            r, p = pair_fn(data[columns[i]], data[columns[j]])
            raw_pairs.append((columns[i], columns[j], float(r), float(p)))
    adjusted = holm_adjust([pair[3] for pair in raw_pairs])
    pairs: list[dict[str, Any]] = []
    for (left, right, r, p), adjusted_p in zip(raw_pairs, adjusted, strict=True):
        pairs.append(
            {
                "a": left,
                "b": right,
                "corr": _f(r),
                "p_value": _f(p),
                "adjusted_p_value": _f(adjusted_p),
                "significant": bool(adjusted_p < 0.05),
            }
        )
    pairs.sort(key=lambda d: abs(d["corr"]) if d["corr"] is not None else -1.0, reverse=True)

    return {
        "method": method,
        "columns": columns,
        "n_obs": int(len(data)),
        "matrix": matrix,
        "top_pairs": pairs[:5],
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="correlation",
            method=method,
            total_rows=total_rows,
            valid_rows=int(len(data)),
            tests_count=len(raw_pairs),
            multiple_testing_method="holm" if len(raw_pairs) > 1 else "none",
            assumptions=[
                "所有所选列均为数值，任一列缺失的记录按完整案例剔除。",
                (
                    "Pearson 相关用于衡量线性关系并依赖独立观测。"
                    if method == "pearson"
                    else "Spearman 相关用于衡量单调关系并依赖独立观测。"
                ),
            ],
            limitations=[
                (
                    "列对显著性按 Holm 方法控制同一分析内的多重检验。"
                    if len(raw_pairs) > 1
                    else "当前仅检验一个列对，无需额外多重检验校正。"
                ),
                "相关不等于因果，未观测混杂和共同趋势可能产生表面关系。",
            ],
        ),
    }


# ── 维度贡献 ──

def dimension_contribution(args: dict[str, Any]) -> dict[str, Any]:
    """Compute additive dimension contribution with policy-driven small-group protection."""
    dataset_ref: str = args["dataset_ref"]
    dimension_col: str = args["dimension_col"]
    value_col: str = args["value_col"]
    method: str = args.get("method", "sum")
    limit = int(args.get("limit", 20))

    _require_model_visible_columns(dataset_ref, [dimension_col, value_col])
    df = load_dataframe(dataset_ref)
    total_rows = len(df)
    _require_columns(df, [dimension_col, value_col])
    data = df[[dimension_col, value_col]].copy()
    data[value_col] = _numeric(data[value_col], value_col)
    data = data.dropna(subset=[dimension_col, value_col])
    if len(data) < _MIN_POINTS:
        raise ValueError(f"有效样本量不足（{len(data)} < {_MIN_POINTS}），无法计算维度贡献")
    if method == "sum" and bool((data[value_col] < 0).any()):
        raise ValueError("sum 贡献要求度量值非负；含负值时贡献份额不可解释")

    raw_groups: list[GroupAgg] = []
    for key, frame in data.groupby(dimension_col, dropna=False, sort=False):
        value = float(frame[value_col].sum()) if method == "sum" else float(len(frame))
        raw_groups.append(GroupAgg(_plain(key), value, len(frame)))
    total_value = sum(group.value for group in raw_groups)
    if total_value <= 0:
        raise ValueError("贡献总量必须大于 0")

    policy = resolve_policy(dataset_ref)
    protected = guard_small_groups(
        raw_groups,
        method,
        policy.small_group_min_size,
        mode=policy.small_group_mode,
        other_label=policy.other_label,
    )
    protected.sort(key=lambda group: group.value, reverse=True)
    shown = protected[:limit]
    groups = [
        {
            "dimension": _plain(group.key),
            "value": _f(group.value),
            "count": group.count,
            "share": _f(group.value / total_value),
            "rank": rank,
            "protected": not any(group is raw_group for raw_group in raw_groups),
        }
        for rank, group in enumerate(shown, 1)
    ]
    small = [group for group in raw_groups if group.count < policy.small_group_min_size]
    returned_share = sum(group.value for group in shown) / total_value
    return {
        "method": method,
        "dimension_col": dimension_col,
        "value_col": value_col,
        "total_value": _f(total_value),
        "groups": groups,
        "group_count": len(raw_groups),
        "truncated": len(protected) > limit,
        "returned_share": _f(returned_share),
        "small_group_protection": {
            "minimum_group_size": policy.small_group_min_size,
            "mode": policy.small_group_mode,
            "protected_group_count": len(small),
            "protected_row_count": sum(group.count for group in small),
        },
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="contribution",
            method=method,
            total_rows=total_rows,
            valid_rows=len(data),
            assumptions=[
                "维度和度量缺失记录按完整案例剔除。",
                "贡献份额仅对可加总的非负 sum 或非空记录 count 定义。",
            ],
            limitations=[
                "小于策略阈值的群体已按数据策略合并或抑制，展示份额可能小于完整总量。",
                "维度贡献是描述性构成，不证明该维度导致结果变化。",
            ],
        ),
    }


# ── 分群比较 ──

def _welch_anova(samples: list[np.ndarray]) -> tuple[float, float, float, float]:
    """Return Welch ANOVA statistic, p-value and degrees of freedom."""
    count = len(samples)
    sizes = np.asarray([len(sample) for sample in samples], dtype=float)
    means = np.asarray([np.mean(sample) for sample in samples], dtype=float)
    variances = np.asarray([np.var(sample, ddof=1) for sample in samples], dtype=float)
    if bool(np.any(variances <= 0)):
        raise ValueError("分群比较要求每个纳入群体都具有非零组内方差")
    weights = sizes / variances
    weight_total = float(np.sum(weights))
    weighted_mean = float(np.sum(weights * means) / weight_total)
    term = float(np.sum(((1 - weights / weight_total) ** 2) / (sizes - 1)))
    df1 = float(count - 1)
    df2 = float((count**2 - 1) / (3 * term))
    numerator = float(np.sum(weights * ((means - weighted_mean) ** 2)) / df1)
    denominator = 1 + (2 * (count - 2) / (count**2 - 1)) * term
    statistic = numerator / denominator
    return statistic, float(scipy_stats.f.sf(statistic, df1, df2)), df1, df2


def _hedges_g(left: np.ndarray, right: np.ndarray) -> float | None:
    """Return bias-corrected standardized mean difference for two groups."""
    left_n, right_n = len(left), len(right)
    pooled_numerator = (left_n - 1) * np.var(left, ddof=1) + (right_n - 1) * np.var(
        right, ddof=1
    )
    pooled_variance = float(pooled_numerator / (left_n + right_n - 2))
    if pooled_variance <= 0:
        return None
    correction = 1 - 3 / (4 * (left_n + right_n) - 9)
    difference = float(np.mean(left)) - float(np.mean(right))
    return _f(correction * difference / math.sqrt(pooled_variance))


def group_compare(args: dict[str, Any]) -> dict[str, Any]:
    """Compare governed cohorts with fixed Welch tests and Holm pairwise correction."""
    dataset_ref: str = args["dataset_ref"]
    group_col: str = args["group_col"]
    value_col: str = args["value_col"]
    _require_model_visible_columns(dataset_ref, [group_col, value_col])
    df = load_dataframe(dataset_ref)
    total_rows = len(df)
    _require_columns(df, [group_col, value_col])
    data = df[[group_col, value_col]].copy()
    data[value_col] = _numeric(data[value_col], value_col)
    data = data.dropna(subset=[group_col, value_col])

    policy = resolve_policy(dataset_ref)
    comparison_minimum = max(2, policy.small_group_min_size)
    raw_groups = [
        (_plain(key), frame[value_col].to_numpy(dtype=float))
        for key, frame in data.groupby(group_col, dropna=False, sort=False)
    ]
    eligible = [
        (key, sample)
        for key, sample in raw_groups
        if len(sample) >= comparison_minimum
    ]
    protected = [
        (key, sample)
        for key, sample in raw_groups
        if len(sample) < comparison_minimum
    ]
    if len(eligible) < 2:
        raise ValueError("小群体保护后不足两个可比较群体")
    if len(eligible) > _MAX_COMPARISON_GROUPS:
        raise ValueError(
            f"可比较群体过多（{len(eligible)} > {_MAX_COMPARISON_GROUPS}），请先明确分析范围"
        )
    samples = [sample for _, sample in eligible]
    if any(float(np.var(sample, ddof=1)) <= 0 for sample in samples):
        raise ValueError("分群比较要求每个纳入群体都具有非零组内方差")
    used_rows = sum(len(sample) for sample in samples)

    summaries: list[dict[str, Any]] = []
    for key, sample in eligible:
        mean = float(np.mean(sample))
        std = float(np.std(sample, ddof=1))
        sem = std / math.sqrt(len(sample))
        critical = float(scipy_stats.t.ppf(0.975, len(sample) - 1))
        summaries.append(
            {
                "group": key,
                "count": len(sample),
                "mean": _f(mean),
                "std": _f(std),
                "median": _f(np.median(sample)),
                "ci95_low": _f(mean - critical * sem),
                "ci95_high": _f(mean + critical * sem),
            }
        )

    if len(samples) == 2:
        statistic, p_value = scipy_stats.ttest_ind(samples[0], samples[1], equal_var=False)
        overall = {
            "test": "welch_t",
            "statistic": _f(statistic),
            "p_value": _f(p_value),
            "df1": None,
            "df2": None,
            "significant": bool(p_value < 0.05),
        }
    else:
        statistic, p_value, df1, df2 = _welch_anova(samples)
        overall = {
            "test": "welch_anova",
            "statistic": _f(statistic),
            "p_value": _f(p_value),
            "df1": _f(df1),
            "df2": _f(df2),
            "significant": bool(p_value < 0.05),
        }

    raw_pairs: list[tuple[Any, Any, np.ndarray, np.ndarray, float, float]] = []
    for (left_key, left), (right_key, right) in combinations(eligible, 2):
        statistic, p_value = scipy_stats.ttest_ind(left, right, equal_var=False)
        raw_pairs.append(
            (left_key, right_key, left, right, float(statistic), float(p_value))
        )
    adjusted = holm_adjust([pair[5] for pair in raw_pairs])
    pairwise = [
        {
            "left": left_key,
            "right": right_key,
            "mean_difference": _f(float(np.mean(left)) - float(np.mean(right))),
            "statistic": _f(statistic),
            "p_value": _f(p_value),
            "adjusted_p_value": _f(adjusted_p),
            "significant": bool(adjusted_p < 0.05),
            "effect_size_hedges_g": _hedges_g(left, right),
        }
        for (left_key, right_key, left, right, statistic, p_value), adjusted_p in zip(
            raw_pairs, adjusted, strict=True
        )
    ]
    return {
        "method": str(overall["test"]),
        "group_col": group_col,
        "value_col": value_col,
        "groups": summaries,
        "overall": overall,
        "pairwise": pairwise,
        "small_group_protection": {
            "minimum_group_size": comparison_minimum,
            "mode": "drop",
            "protected_group_count": len(protected),
            "protected_row_count": sum(len(sample) for _, sample in protected),
        },
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="group_comparison",
            method=str(overall["test"]),
            total_rows=total_rows,
            valid_rows=used_rows,
            tests_count=len(pairwise),
            multiple_testing_method="holm" if len(pairwise) > 1 else "none",
            minimum_required=2 * comparison_minimum,
            assumptions=[
                "群体和度量缺失记录按完整案例剔除。",
                "群体观测相互独立；Welch 方法不要求各群体方差相等。",
            ],
            limitations=[
                "小于策略阈值的群体已完全抑制，未合并为统计上无意义的混合群体。",
                "成对比较显著性按 Holm 方法校正；群体差异不证明群体属性导致结果变化。",
            ],
        ),
    }
