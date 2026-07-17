"""规划→出图链路测试（红线1 + 红线2，用假网关离线验证，不依赖真实模型）。

- 红线1：喂给模型的 payload 只含数据画像，不含超出样本行的原始数据。
- 红线2：最终图表数值来自真实数据聚合，而非模型输出。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.chart_planner import build_messages, plan_chart  # noqa: E402
from mcp_servers.chart.tools import gen_chart  # noqa: E402
from mcp_servers.excel_parser.tools import infer_schema, parse_excel  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402


class FakeGateway:
    """假模型网关：返回固定图表规划，不触网。"""

    def __init__(self, content: str) -> None:
        self._content = content
        self.received: list[Message] = []

    async def complete(
        self, scenario: Scenario, messages: list[Message], *, params: dict | None = None
    ) -> ModelResponse:
        self.received = messages
        return ModelResponse(content=self._content, model="fake")


@pytest.fixture
def dataset_ref(tmp_path: Path) -> str:
    # 每个分组 ≥5 行，避开第3层小分组保护（默认阈值5），聚焦验证"数值来自真实数据"。
    # 华东 5 行合计 3000.5；华北 5 行合计 1850.0。
    df = pd.DataFrame(
        {
            "区域": ["华东"] * 5 + ["华北"] * 5,
            "销售额": [1200.5, 800.0, 500.0, 300.0, 200.0, 950.0, 400.0, 300.0, 150.0, 50.0],
        }
    )
    path = tmp_path / "t.xlsx"
    df.to_excel(path, index=False)
    return parse_excel({"file_ref": str(path)})["dataset_ref"]


def test_payload_contains_only_profile(tmp_path: Path) -> None:
    # 构造一张 10 行表，哨兵值只在第 8 行（超出前5样本）
    rows = [{"城市": f"城市{i}", "金额": i * 10} for i in range(10)]
    rows[7]["城市"] = "SENTINEL_ROW_8"
    df = pd.DataFrame(rows)
    path = tmp_path / "big.xlsx"
    df.to_excel(path, index=False)
    ref = parse_excel({"file_ref": str(path)})["dataset_ref"]
    profile = infer_schema({"dataset_ref": ref})

    messages = build_messages(profile)
    user_payload = messages[-1].content
    # 红线1：超出样本行的原始数据不得出现在 payload 中
    assert "SENTINEL_ROW_8" not in user_payload
    assert len(profile.sample_rows) == 5


@pytest.mark.asyncio
async def test_full_link_numbers_from_data(dataset_ref: str) -> None:
    profile = infer_schema({"dataset_ref": dataset_ref})
    gateway = FakeGateway('{"chart_type":"bar","x":"区域","y":"销售额","agg":"sum"}')

    gen_args = await plan_chart(profile, gateway)  # type: ignore[arg-type]
    assert gen_args["chart_type"] == "bar"

    result = gen_chart(gen_args)
    option = result["option"]
    cats = option["xAxis"]["data"]
    values = option["series"][0]["data"]
    by_cat = dict(zip(cats, values, strict=False))
    # 红线2：数值是真实聚合（华东=1200.5+1800=3000.5，华北=950+900=1850）
    assert by_cat["华东"] == 3000.5
    assert by_cat["华北"] == 1850.0
    # 视觉默认值：柱宽上限防止分类少时柱子过宽；grid 收敛留白（纯样式不碰数值）
    assert option["series"][0]["barMaxWidth"] == 48
    assert option["grid"]["containLabel"] is True
