"""Excel 分析可靠性加固：选错列时带错重规划一次后成功。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import model_gateway_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.excel_parser.tools import parse_excel  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402


class ScriptedGateway:
    """按脚本依次返回内容，并记录是否收到 json_object 参数。"""

    def __init__(self, contents: list[str]) -> None:
        self._contents = contents
        self.calls = 0
        self.saw_json_mode = False

    async def complete(
        self, scenario: Scenario, messages: list[Message], *, params: dict | None = None
    ) -> ModelResponse:
        if params and params.get("response_format", {}).get("type") == "json_object":
            self.saw_json_mode = True
        content = self._contents[min(self.calls, len(self._contents) - 1)]
        self.calls += 1
        return ModelResponse(content=content, model="fake")


@pytest.fixture
def dataset_ref(tmp_path: Path) -> str:
    df = pd.DataFrame(
        {
            "区域": ["华东"] * 5 + ["华北"] * 5,
            "销售额": [100, 200, 300, 400, 500, 60, 70, 80, 90, 100],
        }
    )
    p = tmp_path / "t.xlsx"
    df.to_excel(p, index=False)
    return parse_excel({"file_ref": str(p)})["dataset_ref"]


def test_replan_recovers_from_bad_column(dataset_ref: str) -> None:
    # 第一次选了不存在的列 → gen_chart 报错 → 带错重规划 → 第二次选对
    gw = ScriptedGateway(
        [
            '{"chart_type":"bar","x":"不存在的列","y":"销售额","agg":"sum"}',
            '{"chart_type":"bar","x":"区域","y":"销售额","agg":"sum"}',
        ]
    )
    app.dependency_overrides[model_gateway_dep] = lambda: gw
    try:
        client = TestClient(app)
        resp = client.post("/analyze", json={"dataset_ref": dataset_ref})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["chart_type"] == "bar"
        assert dict(zip(
            data["option"]["xAxis"]["data"], data["option"]["series"][0]["data"], strict=False
        )) == {"华东": 1500.0, "华北": 400.0}
        assert gw.calls == 2          # 触发了一次重规划
        assert gw.saw_json_mode is True  # 启用了 json_object 模式
    finally:
        app.dependency_overrides.clear()


def test_replan_gives_up_after_two_failures(dataset_ref: str) -> None:
    gw = ScriptedGateway(['{"chart_type":"bar","x":"永不存在","y":"销售额","agg":"sum"}'])
    app.dependency_overrides[model_gateway_dep] = lambda: gw
    try:
        client = TestClient(app)
        resp = client.post("/analyze", json={"dataset_ref": dataset_ref})
        assert resp.status_code == 422
        assert gw.calls == 2          # 初次 + 重规划一次后放弃
    finally:
        app.dependency_overrides.clear()
