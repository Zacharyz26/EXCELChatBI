"""stats LLM 解读切片测试：摘要剔明细（红线1）+ 解读文本 + 降级。

重点：证明喂给模型的 payload 只含摘要，不含 trend 逐行分量、不含异常逐点原值。
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

from apps.api.deps import model_gateway_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from apps.orchestrator.stats_interpreter import (  # noqa: E402
    build_messages,
    extract_summary,
    interpret_stats,
)
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.stats.tools import anomaly_detect, regression, trend_analysis  # noqa: E402
from packages.common.dataset_store import save_dataframe  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402


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


# ── 真实统计结果（含明细）作为输入 ──

@pytest.fixture
def trend_result() -> dict:
    n = 48
    x = np.arange(n)
    val = 10 + 0.5 * x + 3 * np.sin(2 * np.pi * x / 12)
    ref = save_dataframe(
        pd.DataFrame({"日期": pd.date_range("2024-01-01", periods=n, freq="D"), "销量": val})
    )
    return trend_analysis(
        {"dataset_ref": ref, "value_col": "销量", "time_col": "日期",
         "method": "stl", "period": 12, "forecast_horizon": 3}
    )


@pytest.fixture
def anomaly_result() -> dict:
    rng = np.random.default_rng(0)
    vals = 100 + rng.normal(0, 1, 30)
    vals[5], vals[15], vals[25] = 500.0, 600.0, 700.0   # 三个不同的异常原值
    ref = save_dataframe(pd.DataFrame({"v": vals}))
    return anomaly_detect({"dataset_ref": ref, "value_col": "v", "method": "iqr"})


@pytest.fixture
def regression_result() -> dict:
    rng = np.random.default_rng(1)
    x1 = np.arange(20, dtype=float)
    x2 = rng.normal(0, 1, 20)
    y = 5 + 2 * x1 + 3 * x2
    ref = save_dataframe(pd.DataFrame({"y": y, "x1": x1, "x2": x2}))
    return regression({"dataset_ref": ref, "target": "y", "features": ["x1", "x2"], "kind": "ols"})


# ── 摘要提取：白名单剔除明细（红线1）──

def test_summary_trend_drops_rowwise_series(trend_result: dict) -> None:
    assert "points" in trend_result and "time" in trend_result  # 完整结果确有明细
    s = extract_summary("trend", trend_result)
    assert "points" not in s and "time" not in s               # 逐行分量/时间数组被剔除
    assert set(s) >= {"direction", "slope", "seasonality_strength", "forecast",
                      "time_start", "time_end"}
    # forecast 是少量预测点（口径允许）；首末时间点是标量而非数组
    assert isinstance(s["time_start"], str) and isinstance(s["time_end"], str)


def test_summary_anomaly_collapses_points_to_aggregate(anomaly_result: dict) -> None:
    assert len(anomaly_result["anomalies"]) >= 3               # 完整结果含逐点原值
    s = extract_summary("anomaly", anomaly_result)
    assert "anomalies" not in s                                # 逐点列表被剔除
    assert s["n_anomalies"] == anomaly_result["n_anomalies"]
    agg = s["anomaly_value_summary"]
    assert set(agg) == {"min", "max", "mean"}                  # 只剩聚合范围/均值
    assert agg["min"] == pytest.approx(500.0, abs=1) and agg["max"] == pytest.approx(700.0, abs=1)


def test_summary_regression_passthrough(regression_result: dict) -> None:
    s = extract_summary("regression", regression_result)
    assert set(s) == {"kind", "r_squared", "adj_r_squared", "n_obs",
                      "model_pvalue", "coefficients"}


def test_summary_unknown_kind_raises() -> None:
    with pytest.raises(ValueError, match="未知统计类型"):
        extract_summary("clustering", {})


# ── 发往模型的 payload 不含明细 ──

def test_payload_excludes_trend_series(trend_result: dict) -> None:
    summary = extract_summary("trend", trend_result)
    user = build_messages("trend", summary)[1].content
    for marker in ('"points"', '"trend"', '"seasonal"', '"resid"', '"time"'):
        assert marker not in user


def test_payload_excludes_anomaly_points(anomaly_result: dict) -> None:
    summary = extract_summary("anomaly", anomaly_result)
    user = build_messages("anomaly", summary)[1].content
    assert '"anomalies"' not in user and '"index"' not in user and '"score"' not in user


# ── 解读调用与降级 ──

@pytest.mark.asyncio
async def test_interpret_returns_text_and_sends_only_summary(trend_result: dict) -> None:
    gw = FakeGateway()
    text = await interpret_stats("trend", trend_result, gw)  # type: ignore[arg-type]
    assert text == "销售额呈上升趋势，预计继续增长。"
    sent = "\n".join(m.content for m in gw.received)
    assert '"points"' not in sent and '"resid"' not in sent   # 模型收到的确无明细


@pytest.mark.asyncio
async def test_interpret_degrades_to_none_when_model_down(trend_result: dict) -> None:
    text = await interpret_stats("trend", trend_result, DownGateway())  # type: ignore[arg-type]
    assert text is None


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
