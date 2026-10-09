"""统计分析工具实现（statsmodels / scikit-learn）。

红线2：所有数值结果均由本模块用 statsmodels/scikit-learn 从 dataset_ref 的**真实数据**
算出，函数内绝无 LLM 调用，LLM 仅负责事后解读（本切片暂不接解读）。
红线1：明细级输出（STL 逐行分量、异常点原值）随结果整体返回，供前端渲染（数据不出环境）；
将来接 LLM 解读时，须在编排层收敛为摘要再喂模型，不得下发逐行明细。
趋势支持 STL / 移动平均 / Prophet（prophet 惰性导入，需 .[forecast]）。
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
from packages.governance.data_boundary import (
    ColumnAccess,
    authorize_columns,
    resolve_policy,
)
from scipy import stats as scipy_stats
from sklearn.ensemble import IsolationForest
from statsmodels.stats.diagnostic import het_breuschpagan
from statsmodels.stats.outliers_influence import variance_inflation_factor
from statsmodels.stats.stattools import durbin_watson, jarque_bera
from statsmodels.tsa.seasonal import STL

from mcp_servers.stats.evidence import build_statistical_evidence, holm_adjust

_MIN_POINTS = 5  # 统计分析所需的最小有效样本量
_MAX_COMPARISON_GROUPS = 10
_FORECAST_MIN_POINTS = 12
_FORECAST_MIN_TRAINING_POINTS = 8
_FORECAST_INTERVAL_LEVEL = 0.95


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
    out = out.replace([np.inf, -np.inf], np.nan)
    if out.notna().sum() == 0:
        if series.isna().all():
            raise ValueError(f"列 {col} 没有有效数值（全部为空）")
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
    """Require columns whose row/category values appear in the result."""
    authorize_columns(
        dataset_ref, columns, access=ColumnAccess.VALUES, operation="stats"
    )


def _require_summary_columns(dataset_ref: str, columns: list[str]) -> None:
    """Allow masked statistical summaries while always rejecting excluded columns."""
    authorize_columns(
        dataset_ref, columns, access=ColumnAccess.SUMMARY, operation="stats"
    )


def _ordered_series(
    args: dict[str, Any], require_time: bool
) -> tuple[pd.Series, list[str] | None, int, dict[str, Any] | None]:
    """读取 value_col（可选按 time_col 升序），返回 (数值序列, 时间标签)。

    序列已丢弃缺失、重置为 0 基定位索引；时间标签与序列位置一一对应，供前端 x 轴。
    """
    dataset_ref = str(args["dataset_ref"])
    value_col: str = args["value_col"]
    time_col: str | None = args.get("time_col")
    if require_time and not time_col:
        raise ValueError("该分析需要 time_col（时间列）")

    cols = [value_col] + ([time_col] if time_col else [])
    _require_model_visible_columns(dataset_ref, cols)
    df = load_dataframe(dataset_ref)
    total_rows = len(df)
    _require_columns(df, cols)
    df = df[cols].copy()
    df[value_col] = _numeric(df[value_col], value_col)

    time_axis: dict[str, Any] | None = None
    if time_col:
        df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
        invalid_timestamps = int(df[time_col].isna().sum())
        df = df.dropna(subset=[time_col]).sort_values(time_col)
    df = df.dropna(subset=[value_col]).reset_index(drop=True)

    if len(df) < _MIN_POINTS:
        raise ValueError(f"有效样本量不足（{len(df)} < {_MIN_POINTS}），无法做统计分析")

    labels = [t.isoformat() for t in df[time_col]] if time_col else None
    if time_col:
        timestamps = pd.DatetimeIndex(df[time_col])
        origin = timestamps.min()
        elapsed_days = np.asarray(
            (timestamps - origin).total_seconds()
            / pd.Timedelta(days=1).total_seconds(),
            dtype=float,
        )
        unique_days = np.unique(elapsed_days)
        positive_deltas = np.diff(unique_days)
        unique_timestamps = pd.DatetimeIndex(timestamps.unique()).sort_values()
        forecast_frequency: str | None = None
        if len(unique_timestamps) >= 3:
            try:
                forecast_frequency = pd.infer_freq(unique_timestamps)
            except (TypeError, ValueError):
                forecast_frequency = None
        median_step_days = (
            float(np.median(positive_deltas)) if len(positive_deltas) else None
        )
        time_axis = {
            "elapsed_days": elapsed_days,
            "median_step_days": median_step_days,
            "unit": "day",
            "origin": origin.isoformat(),
            "observed_span_days": float(elapsed_days.max()),
            "duplicate_timestamps": int(timestamps.duplicated().sum()),
            "invalid_timestamps": invalid_timestamps,
            "forecast_frequency": forecast_frequency,
            "forecast_step_days": (
                None if forecast_frequency is not None else median_step_days
            ),
        }
    return df[value_col].astype(float), labels, total_rows, time_axis


def _linear_slope(y: np.ndarray, x: np.ndarray) -> tuple[float, float]:
    """Fit values against an explicit numeric axis and return slope/intercept."""
    slope, intercept = np.polyfit(x, y, 1)
    return float(slope), float(intercept)


def _direction(slope: float, y: np.ndarray, x_span: float) -> str:
    """按拟合线端到端变化占均值绝对值的比例，判定 上升/下降/平稳。"""
    scale = float(np.mean(np.abs(y))) or 1.0
    rel = slope * x_span / scale
    if rel > 0.05:
        return "上升"
    if rel < -0.05:
        return "下降"
    return "平稳"


# ── 趋势分析 ──

def _future_timestamps(
    labels: list[str],
    horizon: int,
    *,
    frequency: str | None,
    median_step_days: float,
) -> pd.DatetimeIndex:
    """Build future timestamps from a calendar cadence or a fixed median-day step."""
    if horizon <= 0:
        return pd.DatetimeIndex([])
    last = pd.DatetimeIndex(pd.to_datetime(labels)).max()
    if frequency is not None:
        return pd.DatetimeIndex(
            pd.date_range(start=last, periods=horizon + 1, freq=frequency)[1:]
        )
    step = pd.Timedelta(days=median_step_days)
    return pd.DatetimeIndex([last + step * offset for offset in range(1, horizon + 1)])


def _seasonality_period_in_days(labels: list[str], period: int) -> float:
    """Convert an observation-count period into Prophet's elapsed-day unit."""
    timestamps = pd.DatetimeIndex(pd.to_datetime(labels)).unique().sort_values()
    if len(timestamps) < 2 * period:
        raise ValueError(
            "Prophet 季节周期按唯一时间观测点计算，至少需要两个完整周期"
            f"（唯一时间点 {len(timestamps)} < 2×{period}）"
        )
    spans = (timestamps[period:] - timestamps[:-period]).total_seconds()
    period_days = float(np.median(spans) / pd.Timedelta(days=1).total_seconds())
    if not math.isfinite(period_days) or period_days <= 0:
        raise ValueError("Prophet 季节周期无法换算为正的自然日跨度")
    return period_days

def _prophet_decompose(
    y: np.ndarray,
    labels: list[str],
    seasonality_period_days: float | None,
    future_timestamps: pd.DatetimeIndex,
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
    if seasonality_period_days is not None:
        model.add_seasonality(
            name="seasonal",
            period=seasonality_period_days,
            fourier_order=3,
        )
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
    if len(future_timestamps) > 0:
        future = pd.DataFrame({"ds": future_timestamps})
        forecast = [
            _f(v)
            for v in model.predict(future)["yhat"].to_numpy()[
                : len(future_timestamps)
            ]
        ]

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
    series, labels, total_rows, time_axis = _ordered_series(args, require_time=True)
    assert time_axis is not None
    y = series.to_numpy()
    n = len(y)
    elapsed_days = time_axis.pop("elapsed_days")
    median_step_days = time_axis.pop("median_step_days")
    observed_span_days = float(time_axis["observed_span_days"])
    forecast_frequency = time_axis["forecast_frequency"]
    if observed_span_days <= 0 or median_step_days is None:
        raise ValueError("时间跨度必须大于 0，无法按真实时间单位计算趋势斜率")

    period: int | None = args.get("period")
    method: str = args.get("method") or ("stl" if period else "ma")

    slope, intercept = _linear_slope(y, elapsed_days)
    direction = _direction(slope, y, observed_span_days)

    ma_window: int = args.get("ma_window") or max(2, min(n // 4, 12))
    ma_window = min(ma_window, n)
    ma = pd.Series(y).rolling(window=ma_window, min_periods=1, center=True).mean().to_numpy()

    horizon: int = args.get("forecast_horizon", 0)
    assert labels is not None
    future_timestamps = _future_timestamps(
        labels,
        horizon,
        frequency=forecast_frequency,
        median_step_days=median_step_days,
    )
    origin = pd.Timestamp(str(time_axis["origin"]))
    future_elapsed_days = np.asarray(
        (future_timestamps - origin).total_seconds()
        / pd.Timedelta(days=1).total_seconds(),
        dtype=float,
    )
    seasonality_strength: float | None = None
    seasonality_period_days: float | None = None
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
        forecast = [_f(slope * x + intercept) for x in future_elapsed_days]
    elif method == "prophet":
        if period:
            seasonality_period_days = _seasonality_period_in_days(labels, period)
        trend, seasonal, resid, forecast, seasonality_strength = _prophet_decompose(
            y,
            labels,
            seasonality_period_days,
            future_timestamps,
        )
    else:  # ma：移动平均作趋势，残差 = 原值 - 趋势，无季节项
        trend, seasonal, resid = ma, np.zeros(n), y - ma
        forecast = [_f(slope * x + intercept) for x in future_elapsed_days]

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
        "slope_unit": "value_per_day",
        "time_axis": time_axis,
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
        "forecast_time": [timestamp.isoformat() for timestamp in future_timestamps],
        "seasonality_period_days": _f(seasonality_period_days),
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="trend",
            method=method,
            total_rows=total_rows,
            valid_rows=n,
            assumptions=[
                "有效记录按时间升序排列，缺失的时间或指标记录按完整案例剔除。",
                "斜率按距首个有效时间点的自然日数拟合，重复时间点保留为同一时间上的独立观测。",
                "探索性外推优先沿用可推断的日历频率；不规则序列使用相邻唯一时间点间隔的中位数作为固定步长。",
                "趋势方法假定当前时间粒度和用户指定周期适用于所选序列。",
            ],
            limitations=limitations,
        ),
    }


# ── 受治理预测 ──

def _forecast_values(
    method: str,
    history: np.ndarray,
    horizon: int,
    seasonal_period: int | None,
) -> np.ndarray:
    """Generate deterministic univariate predictions without reading future targets."""
    if method == "naive":
        return np.repeat(float(history[-1]), horizon)
    if method == "drift":
        slope = (float(history[-1]) - float(history[0])) / (len(history) - 1)
        return np.asarray(
            [float(history[-1]) + slope * step for step in range(1, horizon + 1)]
        )
    if method == "seasonal_naive":
        if seasonal_period is None:
            raise ValueError("seasonal_naive 需要提供 seasonal_period")
        if len(history) < 2 * seasonal_period:
            raise ValueError("seasonal_naive 的训练集至少需要两个完整季节周期")
        season = history[-seasonal_period:]
        return np.asarray([float(season[index % seasonal_period]) for index in range(horizon)])
    raise ValueError(f"不支持的预测方法: {method}")


def _forecast_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float | None]:
    """Return fixed validation metrics; MAPE is unavailable when any target is zero."""
    errors = actual - predicted
    absolute = np.abs(errors)
    denominator = np.abs(actual) + np.abs(predicted)
    smape_terms = np.divide(
        2 * absolute,
        denominator,
        out=np.zeros_like(absolute, dtype=float),
        where=denominator > 0,
    )
    mape = None
    if bool(np.all(np.abs(actual) > 1e-12)):
        mape = _f(float(np.mean(absolute / np.abs(actual)) * 100))
    return {
        "mae": _f(float(np.mean(absolute))),
        "rmse": _f(float(np.sqrt(np.mean(errors**2)))),
        "smape": _f(float(np.mean(smape_terms) * 100)),
        "mape": mape,
    }


def _regular_frequency(times: pd.Series) -> str:
    """Require a regular datetime index so future timestamps are not guessed."""
    try:
        frequency = pd.infer_freq(pd.DatetimeIndex(times))
    except (TypeError, ValueError) as exc:
        raise ValueError("无法从时间列推断规则频率，不能生成受治理预测") from exc
    if frequency is None:
        raise ValueError("时间间隔不规则，不能生成受治理预测；请先按固定粒度整理数据")
    return str(frequency)


def forecast(args: dict[str, Any]) -> dict[str, Any]:
    """Forecast a regular univariate series with chronological holdout validation."""
    dataset_ref: str = args["dataset_ref"]
    time_col: str = args["time_col"]
    value_col: str = args["value_col"]
    horizon = int(args["horizon"])
    requested_method: str = args.get("method", "auto")
    seasonal_period_raw = args.get("seasonal_period")
    seasonal_period = int(seasonal_period_raw) if seasonal_period_raw is not None else None

    _require_summary_columns(dataset_ref, [time_col, value_col])
    df = load_dataframe(dataset_ref)
    total_rows = len(df)
    _require_columns(df, [time_col, value_col])
    data = df[[time_col, value_col]].copy()
    data[time_col] = pd.to_datetime(data[time_col], errors="coerce")
    data[value_col] = pd.to_numeric(data[value_col], errors="coerce")
    data[value_col] = data[value_col].replace([np.inf, -np.inf], np.nan)
    data = data.dropna(subset=[time_col, value_col]).sort_values(time_col).reset_index(drop=True)
    if len(data) < _FORECAST_MIN_POINTS:
        raise ValueError(
            f"有效时间点不足（{len(data)} < {_FORECAST_MIN_POINTS}），无法执行预测"
        )
    if bool(data[time_col].duplicated().any()):
        raise ValueError("时间列包含重复时间点，训练/验证边界不唯一；请先按固定粒度聚合")

    frequency = _regular_frequency(data[time_col])
    if requested_method == "seasonal_naive" and seasonal_period is None:
        raise ValueError("seasonal_naive 需要提供 seasonal_period")
    if seasonal_period is not None and len(data) < 3 * seasonal_period:
        raise ValueError(
            "带季节周期的预测至少需要三个完整周期"
            f"（{len(data)} < {3 * seasonal_period}）"
        )

    # 留出窗口至少覆盖用户要求的预测步数，否则给出的误差与区间并未在同等
    # 长度上接受验证，不能作为该 horizon 的受治理证据。
    default_validation = max(4, horizon, math.ceil(len(data) * 0.2))
    if seasonal_period is not None:
        default_validation = max(default_validation, seasonal_period)
    validation_size = int(args.get("validation_size", default_validation))
    minimum_training = max(
        _FORECAST_MIN_TRAINING_POINTS,
        2 * seasonal_period if seasonal_period is not None else 0,
    )
    if validation_size >= len(data) or len(data) - validation_size < minimum_training:
        raise ValueError(
            "训练/验证切分后训练样本不足"
            f"（训练 {len(data) - validation_size} < {minimum_training}）"
        )
    if validation_size < horizon:
        raise ValueError("validation_size 至少覆盖 horizon")
    if seasonal_period is not None and validation_size < seasonal_period:
        raise ValueError("validation_size 至少覆盖一个 seasonal_period")

    values = data[value_col].to_numpy(dtype=float)
    times = data[time_col]
    training = values[:-validation_size]
    validation = values[-validation_size:]
    if not bool(times.iloc[-validation_size - 1] < times.iloc[-validation_size]):
        raise ValueError("训练结束时间必须严格早于验证开始时间")

    candidates = ["naive", "drift"]
    if seasonal_period is not None:
        candidates.append("seasonal_naive")
    if requested_method != "auto":
        candidates = [requested_method]

    validation_predictions: dict[str, np.ndarray] = {}
    candidate_metrics: dict[str, dict[str, float | None]] = {}
    for candidate in candidates:
        predicted = _forecast_values(
            candidate,
            training,
            validation_size,
            seasonal_period,
        )
        validation_predictions[candidate] = predicted
        candidate_metrics[candidate] = _forecast_metrics(validation, predicted)

    baseline_prediction = _forecast_values("naive", training, validation_size, None)
    baseline_metrics = _forecast_metrics(validation, baseline_prediction)
    if requested_method == "auto":
        preference = {"naive": 0, "seasonal_naive": 1, "drift": 2}

        def candidate_mae(name: str) -> float:
            value = candidate_metrics[name]["mae"]
            return float(value) if value is not None else math.inf

        selected_method = min(
            candidates,
            key=lambda name: (
                candidate_mae(name),
                preference[name],
            ),
        )
    else:
        selected_method = requested_method

    selected_validation_prediction = validation_predictions[selected_method]
    selected_metrics = candidate_metrics[selected_method]
    selected_mae_value = selected_metrics["mae"]
    baseline_mae_value = baseline_metrics["mae"]
    selected_mae = (
        float(selected_mae_value) if selected_mae_value is not None else math.inf
    )
    baseline_mae = (
        float(baseline_mae_value) if baseline_mae_value is not None else math.inf
    )
    beats_baseline = selected_mae < baseline_mae - 1e-12
    mae_improvement = baseline_mae - selected_mae
    improvement_percent = (
        _f(mae_improvement / baseline_mae * 100)
        if math.isfinite(baseline_mae) and baseline_mae > 0
        else None
    )

    residuals = validation - selected_validation_prediction
    absolute_residuals = np.abs(residuals)
    interval_radius = float(
        np.quantile(absolute_residuals, _FORECAST_INTERVAL_LEVEL, method="higher")
    )
    validation_coverage = float(np.mean(absolute_residuals <= interval_radius))
    future_values = _forecast_values(selected_method, values, horizon, seasonal_period)
    future_times = pd.date_range(
        start=times.iloc[-1],
        periods=horizon + 1,
        freq=frequency,
    )[1:]
    predictions = [
        {
            "time": timestamp.isoformat(),
            "point": _f(point),
            "lower": _f(point - interval_radius),
            "upper": _f(point + interval_radius),
        }
        for timestamp, point in zip(future_times, future_values, strict=True)
    ]

    limitations = [
        "模型只使用单变量历史值，未纳入外部驱动因素、结构突变或未来事件。",
        "95% 区间由单次时间留出集的经验绝对误差构造，不等同于已校准概率区间。",
        "预测表现只代表当前留出窗口，不能保证未来误差保持不变。",
    ]
    if requested_method == "auto":
        limitations.append(
            "同一时间留出窗口用于有限候选方法选择和结果评估，误差可能偏乐观。"
        )
    if selected_metrics["mape"] is None:
        limitations.append("验证目标包含零值，因此 MAPE 不可定义，使用 MAE/RMSE/sMAPE。")
    if not beats_baseline:
        limitations.append("所选方法未优于最后值朴素基线，应把预测视为低置信参考。")
    return {
        "requested_method": requested_method,
        "selected_method": selected_method,
        "reliability": "moderate" if beats_baseline else "limited",
        "frequency": frequency,
        "horizon": horizon,
        "seasonal_period": seasonal_period,
        "split": {
            "total_observations": len(data),
            "training_observations": len(training),
            "validation_observations": len(validation),
            "training_start": times.iloc[0].isoformat(),
            "training_end": times.iloc[-validation_size - 1].isoformat(),
            "validation_start": times.iloc[-validation_size].isoformat(),
            "validation_end": times.iloc[-1].isoformat(),
        },
        "validation_metrics": selected_metrics,
        "baseline": {
            "method": "naive",
            "metrics": baseline_metrics,
            "beats_baseline": beats_baseline,
            "mae_improvement": _f(mae_improvement),
            "mae_improvement_percent": improvement_percent,
        },
        "prediction_interval": {
            "level": _FORECAST_INTERVAL_LEVEL,
            "method": "empirical_absolute_error",
            "radius": _f(interval_radius),
            "validation_coverage": _f(validation_coverage),
        },
        "leakage_checks": {
            "passed": True,
            "chronological_split": True,
            "duplicate_timestamps": False,
            "regular_frequency": True,
            "future_target_rows_used": False,
            "preprocessing_fit_on_training_only": True,
        },
        "candidate_metrics": candidate_metrics,
        "predictions": predictions,
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="forecast",
            method=selected_method,
            total_rows=total_rows,
            valid_rows=len(data),
            minimum_required=max(
                _FORECAST_MIN_POINTS,
                minimum_training + validation_size,
            ),
            assumptions=[
                "时间列严格唯一且等间隔，训练记录全部早于验证记录。",
                "固定时间留出集可代表近期预测误差，未来延续当前数据生成机制。",
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
    series, labels, total_rows, _ = _ordered_series(
        args, require_time=(method == "stl")
    )
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

    _require_summary_columns(str(args["dataset_ref"]), [target, *features])
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

    _require_summary_columns(str(args["dataset_ref"]), columns)
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

    _require_model_visible_columns(dataset_ref, [dimension_col])
    _require_summary_columns(dataset_ref, [value_col])
    df = load_dataframe(dataset_ref)
    total_rows = len(df)
    _require_columns(df, [dimension_col, value_col])
    data = df[[dimension_col, value_col]].copy()
    data[value_col] = _numeric(data[value_col], value_col)
    data = data.dropna(subset=[dimension_col])
    valid_value_count = int(data[value_col].notna().sum())
    if valid_value_count < _MIN_POINTS:
        raise ValueError(
            f"有效样本量不足（{valid_value_count} < {_MIN_POINTS}），无法计算维度贡献"
        )
    if method == "sum" and bool((data[value_col].dropna() < 0).any()):
        raise ValueError("sum 贡献要求度量值非负；含负值时贡献份额不可解释")

    raw_groups: list[GroupAgg] = []
    for key, frame in data.groupby(dimension_col, dropna=False, sort=False):
        valid = frame[value_col].dropna()
        value = float(valid.sum()) if method == "sum" else float(len(valid))
        raw_groups.append(
            GroupAgg(_plain(key), value, len(valid), row_count=len(frame))
        )
    total_value = sum(_required_group_value(group) for group in raw_groups)
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
    protected.sort(key=_required_group_value, reverse=True)
    shown = protected[:limit]
    groups = [
        {
            "dimension": _plain(group.key),
            "value": _f(_required_group_value(group)),
            "count": group.count,
            "row_count": group.source_row_count,
            "valid_value_count": group.count,
            "share": _f(_required_group_value(group) / total_value),
            "rank": rank,
            "protected": not any(group is raw_group for raw_group in raw_groups),
        }
        for rank, group in enumerate(shown, 1)
    ]
    small = [group for group in raw_groups if group.count < policy.small_group_min_size]
    returned_share = sum(_required_group_value(group) for group in shown) / total_value
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
            "protected_row_count": sum(group.source_row_count for group in small),
        },
        "statistical_evidence": build_statistical_evidence(
            analysis_kind="contribution",
            method=method,
            total_rows=total_rows,
            valid_rows=valid_value_count,
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
def _required_group_value(group: GroupAgg) -> float:
    """Narrow protected stats groups, which are never allowed to carry NULL values."""
    if group.value is None:
        raise ValueError("统计分组缺少有效聚合值")
    return group.value




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
    _require_model_visible_columns(dataset_ref, [group_col])
    _require_summary_columns(dataset_ref, [value_col])
    df = load_dataframe(dataset_ref)
    total_rows = len(df)
    _require_columns(df, [group_col, value_col])
    data = df[[group_col, value_col]].copy()
    data[value_col] = _numeric(data[value_col], value_col)
    data = data.dropna(subset=[group_col])

    policy = resolve_policy(dataset_ref)
    comparison_minimum = max(2, policy.small_group_min_size)
    raw_groups = [
        (
            _plain(key),
            frame[value_col].dropna().to_numpy(dtype=float),
            len(frame),
        )
        for key, frame in data.groupby(group_col, dropna=False, sort=False)
    ]
    eligible = [
        (key, sample, row_count)
        for key, sample, row_count in raw_groups
        if len(sample) >= comparison_minimum
    ]
    protected = [
        (key, sample, row_count)
        for key, sample, row_count in raw_groups
        if len(sample) < comparison_minimum
    ]
    if len(eligible) < 2:
        raise ValueError("小群体保护后不足两个可比较群体")
    if len(eligible) > _MAX_COMPARISON_GROUPS:
        raise ValueError(
            f"可比较群体过多（{len(eligible)} > {_MAX_COMPARISON_GROUPS}），请先明确分析范围"
        )
    samples = [sample for _, sample, _ in eligible]
    if any(float(np.var(sample, ddof=1)) <= 0 for sample in samples):
        raise ValueError("分群比较要求每个纳入群体都具有非零组内方差")
    used_rows = sum(len(sample) for sample in samples)

    summaries: list[dict[str, Any]] = []
    for key, sample, row_count in eligible:
        mean = float(np.mean(sample))
        std = float(np.std(sample, ddof=1))
        sem = std / math.sqrt(len(sample))
        critical = float(scipy_stats.t.ppf(0.975, len(sample) - 1))
        summaries.append(
            {
                "group": key,
                "count": len(sample),
                "row_count": row_count,
                "valid_value_count": len(sample),
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
    for (left_key, left, _), (right_key, right, _) in combinations(eligible, 2):
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
            "protected_row_count": sum(row_count for _, _, row_count in protected),
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
