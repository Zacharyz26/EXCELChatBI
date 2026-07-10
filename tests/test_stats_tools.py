"""统计分析工具 + /analyze/stats 路由测试。

红线2：断言数值由工具从真实数据算出（回归系数/R²、异常索引、趋势方向）。
红线3：路由对非法入参/列返回 422。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.stats.tools import (  # noqa: E402
    anomaly_detect,
    correlation,
    regression,
    trend_analysis,
)
from packages.common.dataset_store import save_dataframe  # noqa: E402


@pytest.fixture
def trend_ref() -> str:
    """上升趋势 + 周期 12 的季节序列。"""
    n = 48
    x = np.arange(n)
    val = 10 + 0.5 * x + 3 * np.sin(2 * np.pi * x / 12)
    df = pd.DataFrame({"日期": pd.date_range("2024-01-01", periods=n, freq="D"), "销量": val})
    return save_dataframe(df)


@pytest.fixture
def anomaly_ref() -> str:
    """平稳序列，index=10 处植入一个明显离群点。"""
    rng = np.random.default_rng(0)
    vals = 100 + rng.normal(0, 1, 30)
    vals[10] = 500.0
    return save_dataframe(pd.DataFrame({"v": vals}))


@pytest.fixture
def regression_ref() -> str:
    """无噪声线性关系 y = 5 + 2*x1 + 3*x2。"""
    rng = np.random.default_rng(1)
    x1 = np.arange(20, dtype=float)
    x2 = rng.normal(0, 1, 20)
    y = 5 + 2 * x1 + 3 * x2
    return save_dataframe(pd.DataFrame({"y": y, "x1": x1, "x2": x2}))


# ── 工具层 ──

def test_trend_stl_detects_upward_and_seasonality(trend_ref: str) -> None:
    res = trend_analysis(
        {
            "dataset_ref": trend_ref,
            "value_col": "销量",
            "time_col": "日期",
            "method": "stl",
            "period": 12,
            "forecast_horizon": 3,
        }
    )
    assert res["method"] == "stl"
    assert res["direction"] == "上升"
    assert res["slope"] > 0
    assert res["seasonality_strength"] > 0.5      # 明显季节性
    assert len(res["points"]["trend"]) == 48       # 逐行分量全量返回（供前端）
    assert len(res["forecast"]) == 3               # 线性外推 3 步
    assert res["forecast"][-1] > res["forecast"][0]


def test_trend_prophet_forecasts(trend_ref: str) -> None:
    try:
        res = trend_analysis(
            {"dataset_ref": trend_ref, "value_col": "销量", "time_col": "日期",
             "method": "prophet", "period": 12, "forecast_horizon": 3}
        )
    except (ImportError, RuntimeError) as exc:  # prophet/cmdstan 不可用 → skip
        pytest.skip(f"prophet 不可用：{exc}")
    assert res["method"] == "prophet"
    assert res["direction"] == "上升"                 # 数据本就上升
    assert len(res["points"]["trend"]) == 48          # 逐行趋势分量
    assert len(res["forecast"]) == 3                  # Prophet 预测 3 期
    assert all(v is not None for v in res["forecast"])


def test_trend_prophet_handles_duplicate_dates() -> None:
    # 复现 test.xlsx 结构：多行共享同一日期（Prophet 会折叠历史，需按原 ds 对齐）
    n = 60
    days = np.repeat(np.arange(n // 3), 3)          # 每个日期 3 行
    val = 100 + 5 * days + np.random.default_rng(0).normal(0, 1, n)
    ref = save_dataframe(pd.DataFrame({
        "日期": pd.to_datetime("2024-01-01") + pd.to_timedelta(days, unit="D"),
        "销售额": val,
    }))
    try:
        res = trend_analysis({"dataset_ref": ref, "value_col": "销售额", "time_col": "日期",
                              "method": "prophet", "forecast_horizon": 3})
    except (ImportError, RuntimeError) as exc:
        pytest.skip(f"prophet 不可用：{exc}")
    assert len(res["points"]["trend"]) == n          # 逐行对齐，无 shape 错
    assert len(res["forecast"]) == 3


def test_trend_ma_fallback_without_period(trend_ref: str) -> None:
    res = trend_analysis({"dataset_ref": trend_ref, "value_col": "销量", "time_col": "日期"})
    assert res["method"] == "ma"                   # 无 period 退化为移动平均
    assert res["seasonality_strength"] is None
    assert res["direction"] == "上升"


def test_anomaly_iqr_flags_outlier(anomaly_ref: str) -> None:
    res = anomaly_detect({"dataset_ref": anomaly_ref, "value_col": "v", "method": "iqr"})
    assert res["n_total"] == 30
    assert res["n_anomalies"] >= 1
    assert res["anomalies"][0]["index"] == 10       # 最高分即植入的离群点
    assert res["anomalies"][0]["value"] == pytest.approx(500.0, abs=1e-6)


def test_anomaly_isolation_forest_flags_outlier(anomaly_ref: str) -> None:
    res = anomaly_detect(
        {"dataset_ref": anomaly_ref, "value_col": "v", "method": "isolation_forest"}
    )
    assert 10 in [a["index"] for a in res["anomalies"]]


def test_regression_ols_recovers_coefficients(regression_ref: str) -> None:
    res = regression(
        {"dataset_ref": regression_ref, "target": "y", "features": ["x1", "x2"], "kind": "ols"}
    )
    coef = {c["name"]: c for c in res["coefficients"]}
    assert coef["x1"]["coef"] == pytest.approx(2.0, abs=1e-6)
    assert coef["x2"]["coef"] == pytest.approx(3.0, abs=1e-6)
    assert coef["const"]["coef"] == pytest.approx(5.0, abs=1e-6)
    assert res["r_squared"] == pytest.approx(1.0, abs=1e-6)
    assert coef["x1"]["significant"] is True


def test_non_numeric_column_raises(anomaly_ref: str) -> None:
    ref = save_dataframe(pd.DataFrame({"名称": ["甲", "乙", "丙", "丁", "戊"]}))
    with pytest.raises(ValueError, match="不是数值型"):
        anomaly_detect({"dataset_ref": ref, "value_col": "名称", "method": "iqr"})


@pytest.fixture
def correlation_ref() -> str:
    """b≈2a（强正相关）、c 独立。"""
    rng = np.random.default_rng(7)
    a = np.arange(40, dtype=float)
    b = 2 * a + rng.normal(0, 1, 40)
    c = rng.normal(0, 1, 40)
    return save_dataframe(pd.DataFrame({"a": a, "b": b, "c": c}))


def test_correlation_matrix_and_pairs(correlation_ref: str) -> None:
    res = correlation({"dataset_ref": correlation_ref, "columns": ["a", "b", "c"]})
    assert res["method"] == "pearson" and res["n_obs"] == 40
    # 对角为 1、矩阵对称
    assert res["matrix"][0][0] == pytest.approx(1.0)
    assert res["matrix"][0][1] == pytest.approx(res["matrix"][1][0])
    # a-b 强正相关排在最前
    top = res["top_pairs"][0]
    assert {top["a"], top["b"]} == {"a", "b"} and top["corr"] > 0.95 and top["significant"] is True


def test_correlation_spearman_and_non_numeric(correlation_ref: str) -> None:
    res = correlation({"dataset_ref": correlation_ref, "columns": ["a", "b"], "method": "spearman"})
    assert res["method"] == "spearman" and res["matrix"][0][1] > 0.9
    ref = save_dataframe(pd.DataFrame({"名称": list("甲乙丙丁戊"), "x": [1, 2, 3, 4, 5]}))
    with pytest.raises(ValueError, match="不是数值型"):
        correlation({"dataset_ref": ref, "columns": ["名称", "x"]})


# ── 路由层（端到端，同 Excel 链路）──

def test_route_trend_ok(trend_ref: str) -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={
            "dataset_ref": trend_ref,
            "kind": "trend",
            "params": {"value_col": "销量", "time_col": "日期", "period": 12},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "trend"
    assert body["result"]["direction"] == "上升"


def test_route_bad_column_returns_422(trend_ref: str) -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={"dataset_ref": trend_ref, "kind": "trend",
              "params": {"value_col": "不存在", "time_col": "日期"}},
    )
    assert resp.status_code == 422


def test_route_missing_dataset_returns_404() -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={"dataset_ref": "nope", "kind": "anomaly", "params": {"value_col": "v"}},
    )
    assert resp.status_code == 404


def test_route_unknown_kind_returns_422(trend_ref: str) -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={"dataset_ref": trend_ref, "kind": "clustering", "params": {}},
    )
    assert resp.status_code == 422


def test_route_correlation_ok(correlation_ref: str) -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={"dataset_ref": correlation_ref, "kind": "correlation",
              "params": {"columns": ["a", "b", "c"], "method": "pearson"}},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "correlation"
    assert len(body["result"]["matrix"]) == 3
    assert body["result"]["top_pairs"][0]["significant"] is True
