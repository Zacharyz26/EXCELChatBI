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
from mcp_servers.common.catalog import tool_output_schema  # noqa: E402
from mcp_servers.common.contracts import validate_json  # noqa: E402
from mcp_servers.stats.tools import (  # noqa: E402
    anomaly_detect,
    correlation,
    dimension_contribution,
    forecast,
    group_compare,
    regression,
    trend_analysis,
)
from packages.common.dataset_store import save_dataframe, save_metadata  # noqa: E402


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


@pytest.fixture
def order_regression_ref() -> str:
    """订单数可作为 OLS 因变量，销售额为有效自变量。"""
    rng = np.random.default_rng(11)
    sales = np.linspace(1_000, 20_000, 24)
    orders = np.rint(5 + sales / 180 + rng.normal(0, 2, len(sales))).astype(int)
    return save_dataframe(pd.DataFrame({"销售额": sales, "订单数": orders}))


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
    evidence = res["statistical_evidence"]
    assert evidence["schema"] == "chatbi-statistical-evidence-v1"
    assert evidence["analysis_kind"] == "trend"
    assert evidence["sample"] == {
        "total_rows": 48,
        "valid_rows": 48,
        "excluded_rows": 0,
        "missing_policy": "complete_case_drop",
        "minimum_required": 5,
        "meets_minimum": True,
    }
    assert evidence["inference"]["causal_claim_allowed"] is False
    assert any("时间泄漏" in item for item in evidence["limitations"])


def test_forecast_uses_chronological_holdout_and_beats_naive_baseline() -> None:
    values = np.arange(1, 31, dtype=float)
    ref = save_dataframe(
        pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=len(values), freq="D"),
                "销量": values,
            }
        )
    )

    result = forecast(
        {
            "dataset_ref": ref,
            "time_col": "日期",
            "value_col": "销量",
            "horizon": 3,
            "method": "auto",
            "validation_size": 6,
        }
    )

    assert result["selected_method"] == "drift"
    assert result["reliability"] == "moderate"
    assert result["validation_metrics"]["mae"] == pytest.approx(0.0)
    assert result["baseline"]["beats_baseline"] is True
    assert result["split"]["training_observations"] == 24
    assert result["split"]["validation_observations"] == 6
    assert result["split"]["training_end"] < result["split"]["validation_start"]
    assert result["leakage_checks"] == {
        "passed": True,
        "chronological_split": True,
        "duplicate_timestamps": False,
        "regular_frequency": True,
        "future_target_rows_used": False,
        "preprocessing_fit_on_training_only": True,
    }
    assert [item["point"] for item in result["predictions"]] == [31.0, 32.0, 33.0]
    evidence = result["statistical_evidence"]
    assert evidence["analysis_kind"] == "forecast"
    assert any("留出窗口" in item for item in evidence["limitations"])
    validate_json(
        result,
        tool_output_schema("forecast"),
        code="invalid_tool_output",
        label="预测输出",
    )


def test_forecast_seasonal_naive_uses_complete_cycles() -> None:
    pattern = np.array([10.0, 20.0, 30.0, 40.0])
    values = np.tile(pattern, 8)
    ref = save_dataframe(
        pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=len(values), freq="W"),
                "销量": values,
            }
        )
    )

    result = forecast(
        {
            "dataset_ref": ref,
            "time_col": "日期",
            "value_col": "销量",
            "horizon": 5,
            "method": "seasonal_naive",
            "seasonal_period": 4,
            "validation_size": 8,
        }
    )

    assert result["selected_method"] == "seasonal_naive"
    assert [item["point"] for item in result["predictions"]] == [10.0, 20.0, 30.0, 40.0, 10.0]
    assert result["prediction_interval"]["level"] == 0.95
    assert result["validation_metrics"]["mae"] == pytest.approx(0.0)


def test_forecast_validation_window_must_cover_horizon() -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=24, freq="D"),
                "销售额": np.arange(24, dtype=float),
            }
        )
    )

    with pytest.raises(ValueError, match="validation_size 至少覆盖 horizon"):
        forecast(
            {
                "dataset_ref": ref,
                "time_col": "日期",
                "value_col": "销售额",
                "horizon": 6,
                "validation_size": 4,
            }
        )


def test_forecast_that_does_not_beat_naive_is_explicitly_limited() -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=20, freq="D"),
                "销售额": np.repeat(100.0, 20),
            }
        )
    )

    result = forecast(
        {
            "dataset_ref": ref,
            "time_col": "日期",
            "value_col": "销售额",
            "horizon": 2,
        }
    )

    assert result["selected_method"] == "naive"
    assert result["baseline"]["beats_baseline"] is False
    assert result["reliability"] == "limited"
    assert any(
        "低置信参考" in item
        for item in result["statistical_evidence"]["limitations"]
    )


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (
            pd.DataFrame(
                {
                    "日期": pd.to_datetime(
                        ["2025-01-01", "2025-01-02", "2025-01-04"]
                    ),
                    "值": [1.0, 2.0, 3.0],
                }
            ),
            "时间间隔不规则",
        ),
        (
            pd.DataFrame(
                {
                    "日期": pd.to_datetime(["2025-01-01"] * 12),
                    "值": np.arange(12, dtype=float),
                }
            ),
            "重复时间点",
        ),
    ],
)
def test_forecast_rejects_time_leakage_risks(frame: pd.DataFrame, message: str) -> None:
    # Irregular case needs enough records to pass the sample-size gate first.
    if len(frame) < 12:
        frame = pd.concat([frame] * 4, ignore_index=True)
        frame["日期"] = pd.to_datetime(
            [
                "2025-01-01",
                "2025-01-02",
                "2025-01-04",
                "2025-01-05",
                "2025-01-06",
                "2025-01-08",
                "2025-01-09",
                "2025-01-10",
                "2025-01-12",
                "2025-01-13",
                "2025-01-14",
                "2025-01-16",
            ]
        )
    ref = save_dataframe(frame)

    with pytest.raises(ValueError, match=message):
        forecast(
            {
                "dataset_ref": ref,
                "time_col": "日期",
                "value_col": "值",
                "horizon": 2,
            }
        )


def test_forecast_rejects_protected_values() -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=20, freq="D"),
                "值": np.arange(20, dtype=float),
            }
        )
    )
    save_metadata(ref, {"policy": {"columns": {"值": "exclude"}}})

    with pytest.raises(ValueError, match="受数据策略保护"):
        forecast(
            {
                "dataset_ref": ref,
                "time_col": "日期",
                "value_col": "值",
                "horizon": 2,
            }
        )


def test_anomaly_iqr_flags_outlier(anomaly_ref: str) -> None:
    res = anomaly_detect({"dataset_ref": anomaly_ref, "value_col": "v", "method": "iqr"})
    assert res["n_total"] == 30
    assert res["n_anomalies"] >= 1
    assert res["anomalies"][0]["index"] == 10       # 最高分即植入的离群点
    assert res["anomalies"][0]["value"] == pytest.approx(500.0, abs=1e-6)
    evidence = res["statistical_evidence"]
    assert evidence["analysis_kind"] == "anomaly"
    assert any("候选解释因素" in item for item in evidence["limitations"])


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
    assert coef["x1"]["adjusted_p_value"] is not None
    evidence = res["statistical_evidence"]
    assert evidence["inference"]["tests_count"] == 2
    assert evidence["inference"]["multiple_testing_method"] == "holm"
    assert evidence["inference"]["causal_claim_allowed"] is False


def test_regression_order_count_as_target(order_regression_ref: str) -> None:
    res = regression(
        {
            "dataset_ref": order_regression_ref,
            "target": "订单数",
            "features": ["销售额"],
            "kind": "ols",
        }
    )
    assert res["n_obs"] == 24
    assert res["r_squared"] is not None and res["r_squared"] > 0.9
    assert {coef["name"] for coef in res["coefficients"]} == {"const", "销售额"}


def test_regression_returns_diagnostics_and_detects_collinearity() -> None:
    x1 = np.linspace(1, 40, 40)
    x2 = 2 * x1
    y = 3 + x1 + np.sin(x1)
    ref = save_dataframe(pd.DataFrame({"y": y, "x1": x1, "x2": x2}))

    result = regression(
        {"dataset_ref": ref, "target": "y", "features": ["x1", "x2"]}
    )

    diagnostics = result["diagnostics"]
    assert diagnostics["residual_normality"]["test"] == "jarque_bera"
    assert diagnostics["heteroskedasticity"]["test"] == "breusch_pagan"
    assert diagnostics["autocorrelation"]["test"] == "durbin_watson"
    assert diagnostics["multicollinearity"]["rank_deficient"] is True
    assert "multicollinearity_risk" in diagnostics["warnings"]
    validate_json(
        result,
        tool_output_schema("regression"),
        code="invalid_tool_output",
        label="回归输出",
    )


def test_regression_enforces_feature_scaled_minimum_sample() -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "y": np.arange(10, dtype=float),
                "x1": np.arange(10, dtype=float),
                "x2": np.linspace(2, 20, 10),
            }
        )
    )

    with pytest.raises(ValueError, match=r"10 < 15"):
        regression({"dataset_ref": ref, "target": "y", "features": ["x1", "x2"]})


def test_regression_rejects_target_as_feature(order_regression_ref: str) -> None:
    with pytest.raises(ValueError, match="因变量不能同时作为自变量"):
        regression(
            {
                "dataset_ref": order_regression_ref,
                "target": "订单数",
                "features": ["订单数"],
                "kind": "ols",
            }
        )


def test_regression_rejects_duplicate_features(order_regression_ref: str) -> None:
    with pytest.raises(ValueError, match="自变量不能重复"):
        regression(
            {
                "dataset_ref": order_regression_ref,
                "target": "订单数",
                "features": ["销售额", "销售额"],
                "kind": "ols",
            }
        )


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
    assert top["adjusted_p_value"] >= top["p_value"]
    evidence = res["statistical_evidence"]
    assert evidence["inference"] == {
        "alpha": 0.05,
        "tests_count": 3,
        "multiple_testing_method": "holm",
        "causal_claim_allowed": False,
    }
    assert any("相关不等于因果" in item for item in evidence["limitations"])


def test_statistical_evidence_counts_complete_case_exclusions() -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "a": [1.0, 2.0, None, 4.0, 5.0, 6.0],
                "b": [2.0, 4.0, 6.0, 8.0, 10.0, 12.0],
            }
        )
    )
    res = correlation({"dataset_ref": ref, "columns": ["a", "b"]})
    sample = res["statistical_evidence"]["sample"]
    assert sample["total_rows"] == 6
    assert sample["valid_rows"] == 5
    assert sample["excluded_rows"] == 1
    validate_json(
        res,
        tool_output_schema("correlation"),
        code="invalid_tool_output",
        label="统计工具输出",
    )


def test_correlation_spearman_and_non_numeric(correlation_ref: str) -> None:
    res = correlation({"dataset_ref": correlation_ref, "columns": ["a", "b"], "method": "spearman"})
    assert res["method"] == "spearman" and res["matrix"][0][1] > 0.9
    ref = save_dataframe(pd.DataFrame({"名称": list("甲乙丙丁戊"), "x": [1, 2, 3, 4, 5]}))
    with pytest.raises(ValueError, match="不是数值型"):
        correlation({"dataset_ref": ref, "columns": ["名称", "x"]})


def test_dimension_contribution_merges_small_groups_and_reports_coverage() -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "地区": ["甲"] * 6 + ["乙"] * 5 + ["小一"] * 2 + ["小二"] * 3,
                "销售额": [10.0] * 6 + [5.0] * 5 + [2.0] * 2 + [3.0] * 3,
            }
        )
    )

    result = dimension_contribution(
        {
            "dataset_ref": ref,
            "dimension_col": "地区",
            "value_col": "销售额",
            "method": "sum",
        }
    )

    assert result["group_count"] == 4
    assert result["returned_share"] == pytest.approx(1.0)
    protected = [item for item in result["groups"] if item["protected"]]
    assert len(protected) == 1
    assert protected[0]["dimension"] == "其他"
    assert protected[0]["count"] == 5
    assert result["small_group_protection"]["protected_group_count"] == 2
    validate_json(
        result,
        tool_output_schema("dimension_contribution"),
        code="invalid_tool_output",
        label="贡献输出",
    )


def test_dimension_contribution_rejects_negative_or_protected_columns() -> None:
    negative_ref = save_dataframe(pd.DataFrame({"地区": ["甲"] * 5, "值": [1, 2, -1, 3, 4]}))
    with pytest.raises(ValueError, match="非负"):
        dimension_contribution(
            {"dataset_ref": negative_ref, "dimension_col": "地区", "value_col": "值"}
        )

    protected_ref = save_dataframe(pd.DataFrame({"地区": ["甲"] * 5, "值": range(5)}))
    save_metadata(protected_ref, {"policy": {"columns": {"地区": "mask"}}})
    with pytest.raises(ValueError, match="受数据策略保护"):
        dimension_contribution(
            {"dataset_ref": protected_ref, "dimension_col": "地区", "value_col": "值"}
        )


def test_group_compare_uses_welch_holm_and_suppresses_small_groups() -> None:
    rng = np.random.default_rng(23)
    frame = pd.DataFrame(
        {
            "群体": ["甲"] * 12 + ["乙"] * 12 + ["丙"] * 12 + ["不可见小群"] * 2,
            "指标": np.concatenate(
                [
                    rng.normal(0, 1, 12),
                    rng.normal(4, 1, 12),
                    rng.normal(8, 1, 12),
                    np.array([1000.0, 1100.0]),
                ]
            ),
        }
    )
    ref = save_dataframe(frame)

    result = group_compare(
        {"dataset_ref": ref, "group_col": "群体", "value_col": "指标"}
    )

    assert result["method"] == "welch_anova"
    assert result["overall"]["significant"] is True
    assert len(result["groups"]) == 3
    assert len(result["pairwise"]) == 3
    assert all(item["adjusted_p_value"] >= item["p_value"] for item in result["pairwise"])
    assert "不可见小群" not in str(result)
    assert result["small_group_protection"] == {
        "minimum_group_size": 5,
        "mode": "drop",
        "protected_group_count": 1,
        "protected_row_count": 2,
    }
    evidence = result["statistical_evidence"]
    assert evidence["analysis_kind"] == "group_comparison"
    assert evidence["inference"]["multiple_testing_method"] == "holm"
    validate_json(
        result,
        tool_output_schema("group_compare"),
        code="invalid_tool_output",
        label="分群输出",
    )


def test_group_compare_fails_closed_for_zero_variance_groups() -> None:
    ref = save_dataframe(
        pd.DataFrame({"群体": ["甲"] * 5 + ["乙"] * 5, "指标": [1.0] * 5 + [2.0] * 5})
    )

    with pytest.raises(ValueError, match="非零组内方差"):
        group_compare({"dataset_ref": ref, "group_col": "群体", "value_col": "指标"})


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
        json={"dataset_ref": "f" * 32, "kind": "anomaly", "params": {"value_col": "v"}},
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


def test_route_dimension_contribution_ok() -> None:
    ref = save_dataframe(
        pd.DataFrame({"地区": ["甲"] * 5 + ["乙"] * 5, "销售额": [10.0] * 5 + [5.0] * 5})
    )
    client = TestClient(app)
    response = client.post(
        "/analyze/stats",
        json={
            "dataset_ref": ref,
            "kind": "contribution",
            "params": {"dimension_col": "地区", "value_col": "销售额"},
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"]["groups"][0]["share"] == pytest.approx(2 / 3)


def test_route_regression_order_count_as_target(order_regression_ref: str) -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={
            "dataset_ref": order_regression_ref,
            "kind": "regression",
            "params": {"target": "订单数", "features": ["销售额"], "kind": "ols"},
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "regression"
    assert body["result"]["n_obs"] == 24


@pytest.mark.parametrize(
    ("features", "detail"),
    [
        (["订单数"], "因变量不能同时作为自变量"),
        (["销售额", "销售额"], "自变量不能重复"),
    ],
)
def test_route_regression_rejects_invalid_feature_roles(
    order_regression_ref: str,
    features: list[str],
    detail: str,
) -> None:
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={
            "dataset_ref": order_regression_ref,
            "kind": "regression",
            "params": {"target": "订单数", "features": features, "kind": "ols"},
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == detail
