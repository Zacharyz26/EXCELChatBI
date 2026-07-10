"""stats LLM 解读切片测试：摘要剔明细（红线1）+ 策略门控 + 解读文本 + 降级。

重点：证明喂给模型的 payload 只含摘要，不含 trend 逐行分量、不含异常逐点原值；
且单异常点（小分组）与 EXCLUDE 列场景下摘要进一步降级（无原值 / 无系数）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import model_gateway_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from apps.orchestrator.stats_interpreter import (  # noqa: E402
    build_messages,
    extract_summary,
    interpret_stats,
)
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.stats.tools import (  # noqa: E402
    anomaly_detect,
    correlation,
    regression,
    trend_analysis,
)
from packages.common.dataset_store import save_dataframe, save_metadata  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402


class Case(NamedTuple):
    """一次统计的完整结果 + 数据集引用 + 入参（供 extract_summary 门控）。"""

    result: dict
    ref: str
    params: dict


class FakeGateway:
    """记录收到的消息并回定值文本。"""

    def __init__(self, text: str = "销售额呈上升趋势，预计继续增长。") -> None:
        self._text = text
        self.received: list[Message] = []

    async def complete(
        self, scenario: Scenario, messages: list[Message], *, params: dict | None = None
    ) -> ModelResponse:
        self.received = messages
        return ModelResponse(content=self._text, model="fake")


class DownGateway:
    """模拟模型不可用（所有候选失败）。"""

    async def complete(
        self, scenario: Scenario, messages: list[Message], *, params: dict | None = None
    ) -> ModelResponse:
        raise RuntimeError("所有候选模型均失败")


class ErrorGateway:
    """模拟网关抛任意异常（覆盖 registry 未配场景 / key 缺失等配置错误）。"""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        raise self._exc


# ── 真实统计结果（含明细）作为输入 ──

@pytest.fixture
def trend_case() -> Case:
    n = 48
    x = np.arange(n)
    val = 10 + 0.5 * x + 3 * np.sin(2 * np.pi * x / 12)
    ref = save_dataframe(
        pd.DataFrame({"日期": pd.date_range("2024-01-01", periods=n, freq="D"), "销量": val})
    )
    params = {"value_col": "销量", "time_col": "日期", "method": "stl",
              "period": 12, "forecast_horizon": 3}
    return Case(trend_analysis({"dataset_ref": ref, **params}), ref, params)


@pytest.fixture
def anomaly_case() -> Case:
    # 6 个异常点（≥ small_group_min_size=5），聚合描述应正常输出
    rng = np.random.default_rng(0)
    vals = 100 + rng.normal(0, 1, 30)
    for i, v in zip([3, 8, 13, 18, 23, 28], [500.0, 540.0, 580.0, 620.0, 660.0, 700.0],
                    strict=True):
        vals[i] = v
    ref = save_dataframe(pd.DataFrame({"v": vals}))
    params = {"value_col": "v", "method": "iqr"}
    return Case(anomaly_detect({"dataset_ref": ref, **params}), ref, params)


@pytest.fixture
def regression_case() -> Case:
    rng = np.random.default_rng(1)
    x1 = np.arange(20, dtype=float)
    x2 = rng.normal(0, 1, 20)
    y = 5 + 2 * x1 + 3 * x2
    ref = save_dataframe(pd.DataFrame({"y": y, "x1": x1, "x2": x2}))
    params = {"target": "y", "features": ["x1", "x2"], "kind": "ols"}
    return Case(regression({"dataset_ref": ref, **params}), ref, params)


# ── 摘要提取：白名单剔除明细（红线1）──

def test_summary_trend_drops_rowwise_series(trend_case: Case) -> None:
    assert "points" in trend_case.result and "time" in trend_case.result  # 完整结果确有明细
    s = extract_summary("trend", trend_case.result, trend_case.ref, trend_case.params)
    assert "points" not in s and "time" not in s               # 逐行分量/时间数组被剔除
    assert set(s) >= {"direction", "slope", "seasonality_strength", "forecast",
                      "time_start", "time_end"}
    assert "policy_redacted" not in s                          # 普通数据集不降级
    assert isinstance(s["time_start"], str) and isinstance(s["time_end"], str)


def test_summary_anomaly_collapses_points_to_aggregate(anomaly_case: Case) -> None:
    assert len(anomaly_case.result["anomalies"]) >= 5          # 完整结果含逐点原值
    s = extract_summary("anomaly", anomaly_case.result, anomaly_case.ref, anomaly_case.params)
    assert "anomalies" not in s                                # 逐点列表被剔除
    assert s["n_anomalies"] == anomaly_case.result["n_anomalies"]
    agg = s["anomaly_value_summary"]
    assert set(agg) == {"min", "max", "mean"}                  # 只剩聚合范围/均值
    assert agg["min"] == pytest.approx(500.0, abs=1) and agg["max"] == pytest.approx(700.0, abs=1)


def test_summary_regression_passthrough(regression_case: Case) -> None:
    s = extract_summary("regression", regression_case.result, regression_case.ref,
                        regression_case.params)
    assert set(s) == {"kind", "r_squared", "adj_r_squared", "n_obs",
                      "model_pvalue", "coefficients"}


def test_summary_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="未知统计类型"):
        extract_summary("clustering", {}, "someref", {})


# ── 策略门控：单异常点（小分组）与 EXCLUDE 列降级 ──

def test_summary_single_anomaly_omits_raw_values() -> None:
    # 仅 1 个异常点 < small_group_min_size(5)：min/max/mean≈原值，必须只给计数
    rng = np.random.default_rng(2)
    vals = 100 + rng.normal(0, 1, 30)
    vals[10] = 999.0
    ref = save_dataframe(pd.DataFrame({"v": vals}))
    params = {"value_col": "v", "method": "iqr"}
    result = anomaly_detect({"dataset_ref": ref, **params})
    assert result["n_anomalies"] < 5

    s = extract_summary("anomaly", result, ref, params)
    assert "anomaly_value_summary" not in s and "max_score" not in s  # 原值/分数被门控
    assert s["n_anomalies"] == result["n_anomalies"]                  # 计数仍给
    # 发给 LLM 的 payload 里不出现异常原值 999
    user = build_messages("anomaly", s)[1].content
    assert "999" not in user


def test_summary_correlation_sends_pairs_not_matrix() -> None:
    rng = np.random.default_rng(4)
    a = np.arange(40, dtype=float)
    df = pd.DataFrame({"a": a, "b": 2 * a + rng.normal(0, 1, 40), "c": rng.normal(0, 1, 40)})
    ref = save_dataframe(df)
    params = {"columns": ["a", "b", "c"], "method": "pearson"}
    result = correlation({"dataset_ref": ref, **params})

    s = extract_summary("correlation", result, ref, params)
    assert "matrix" not in s                       # n×n 矩阵不进 LLM（仅前端热力图）
    assert set(s) == {"method", "columns", "n_obs", "top_pairs"}
    assert s["top_pairs"][0]["significant"] is True


def test_summary_correlation_excluded_column_drops_pairs() -> None:
    rng = np.random.default_rng(5)
    a = np.arange(40, dtype=float)
    df = pd.DataFrame({"a": a, "薪资": 2 * a + rng.normal(0, 1, 40), "c": rng.normal(0, 1, 40)})
    ref = save_dataframe(df)
    save_metadata(ref, {"policy": {"columns": {"薪资": "exclude"}}})   # 标敏感列
    params = {"columns": ["a", "薪资", "c"], "method": "pearson"}
    result = correlation({"dataset_ref": ref, **params})

    s = extract_summary("correlation", result, ref, params)
    assert "top_pairs" not in s and "columns" not in s   # 相关对会暴露敏感列关系 → 去掉
    assert s.get("policy_redacted") is True
    assert set(s) == {"method", "n_columns", "policy_redacted"}


def test_summary_regression_excluded_column_drops_coefficients() -> None:
    rng = np.random.default_rng(3)
    x1 = np.arange(20, dtype=float)
    x2 = rng.normal(0, 1, 20)
    y = 5 + 2 * x1 + 3 * x2
    ref = save_dataframe(pd.DataFrame({"y": y, "x1": x1, "x2": x2}))
    save_metadata(ref, {"policy": {"columns": {"x1": "exclude"}}})   # 标 x1 敏感
    params = {"target": "y", "features": ["x1", "x2"], "kind": "ols"}
    result = regression({"dataset_ref": ref, **params})

    s = extract_summary("regression", result, ref, params)
    assert "coefficients" not in s                             # 系数被降级剔除
    assert s.get("policy_redacted") is True
    assert s["r_squared"] is not None                          # 拟合结论仍保留
    # payload 里不出现系数字段
    assert '"coefficients"' not in build_messages("regression", s)[1].content


# ── 发往模型的 payload 不含明细 ──

def test_payload_excludes_trend_series(trend_case: Case) -> None:
    summary = extract_summary("trend", trend_case.result, trend_case.ref, trend_case.params)
    user = build_messages("trend", summary)[1].content
    for marker in ('"points"', '"trend"', '"seasonal"', '"resid"', '"time"'):
        assert marker not in user


def test_payload_excludes_anomaly_points(anomaly_case: Case) -> None:
    summary = extract_summary("anomaly", anomaly_case.result, anomaly_case.ref, anomaly_case.params)
    user = build_messages("anomaly", summary)[1].content
    assert '"anomalies"' not in user and '"index"' not in user and '"score"' not in user


# ── 解读调用与降级 ──

@pytest.mark.asyncio
async def test_interpret_returns_text_and_sends_only_summary(trend_case: Case) -> None:
    gw = FakeGateway()
    text = await interpret_stats(
        "trend", trend_case.result, gw, trend_case.ref, trend_case.params  # type: ignore[arg-type]
    )
    assert text == "销售额呈上升趋势，预计继续增长。"
    sent = "\n".join(m.content for m in gw.received)
    assert '"points"' not in sent and '"resid"' not in sent   # 模型收到的确无明细


@pytest.mark.asyncio
async def test_interpret_degrades_to_none_when_model_down(trend_case: Case) -> None:
    text = await interpret_stats(
        "trend", trend_case.result, DownGateway(), trend_case.ref, trend_case.params  # type: ignore[arg-type]
    )
    assert text is None


@pytest.mark.asyncio
async def test_interpret_degrades_on_config_errors(trend_case: Case) -> None:
    """registry 未配场景(KeyError) / key 缺失(ValueError) 同样降级，不拖垮统计接口。"""
    for exc in (KeyError("registry 未配置场景路由"), ValueError("缺少 API key")):
        text = await interpret_stats(
            "trend", trend_case.result, ErrorGateway(exc), trend_case.ref, trend_case.params
        )
        assert text is None, f"{type(exc).__name__} 应降级为 None 而非抛出"


# ── 路由端到端 ──

@pytest.fixture
def dataset_ref() -> str:
    n = 48
    x = np.arange(n)
    val = 10 + 0.5 * x + 3 * np.sin(2 * np.pi * x / 12)
    return save_dataframe(
        pd.DataFrame({"日期": pd.date_range("2024-01-01", periods=n, freq="D"), "销量": val})
    )


def test_route_interpret_true_returns_interpretation(dataset_ref: str) -> None:
    app.dependency_overrides[model_gateway_dep] = lambda: FakeGateway("这是中文解读。")
    try:
        client = TestClient(app)
        resp = client.post(
            "/analyze/stats",
            json={"dataset_ref": dataset_ref, "kind": "trend", "interpret": True,
                  "params": {"value_col": "销量", "time_col": "日期", "period": 12}},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["interpretation"] == "这是中文解读。"
        assert body["result"]["points"]["trend"]              # 完整明细仍返回前端
    finally:
        app.dependency_overrides.clear()


def test_route_interpret_degrades_but_returns_stats(dataset_ref: str) -> None:
    app.dependency_overrides[model_gateway_dep] = lambda: DownGateway()
    try:
        client = TestClient(app)
        resp = client.post(
            "/analyze/stats",
            json={"dataset_ref": dataset_ref, "kind": "trend", "interpret": True,
                  "params": {"value_col": "销量", "time_col": "日期", "period": 12}},
        )
        assert resp.status_code == 200, resp.text            # 解读失败不拖垮接口
        body = resp.json()
        assert body["interpretation"] is None
        assert body["result"]["direction"] == "上升"
    finally:
        app.dependency_overrides.clear()


def test_route_interpret_false_skips_llm(dataset_ref: str) -> None:
    # 不注入 gateway 替身；interpret 缺省 false，不应触碰模型
    client = TestClient(app)
    resp = client.post(
        "/analyze/stats",
        json={"dataset_ref": dataset_ref, "kind": "trend",
              "params": {"value_col": "销量", "time_col": "日期", "period": 12}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["interpretation"] is None
