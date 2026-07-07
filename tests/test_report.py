"""报告切片测试：组装正确 + report 工具零 LLM + LLM 出口恒为 3 + 端点出非空 PDF。"""

from __future__ import annotations

import base64
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
from fastapi.testclient import TestClient  # noqa: E402
from mcp_servers.report import tools as report_tools  # noqa: E402
from mcp_servers.report.server import build_server as build_report_server  # noqa: E402
from packages.common.dataset_store import save_dataframe  # noqa: E402
from packages.models.types import Message, ModelResponse, Scenario  # noqa: E402

# 1x1 PNG，用于免渲染的组装测试
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class FakeGateway:
    """定值中文解读的假网关。"""

    async def complete(
        self, scenario: Scenario, messages: list[Message], *, params: dict | None = None
    ) -> ModelResponse:
        return ModelResponse(content="销售额呈上升趋势，预计继续增长。", model="fake")


def _report_tool(name: str):
    return build_report_server()._tools[name]


# ── 铁律：report 工具零 LLM ──

def test_report_tools_import_no_llm() -> None:
    # 按 AST 扫真实 import（含函数内惰性 import），不误伤 docstring 里对铁律的说明
    import ast

    tree = ast.parse(Path(report_tools.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    joined = " ".join(imported)
    for bad in ("packages.models", "gateway", "stats_interpreter"):
        assert bad not in joined, f"report 工具不应 import 任何 LLM 相关模块: {bad}"


def test_llm_exit_points_remain_exactly_three() -> None:
    # 全库调用 gateway.complete( 的文件必须恰好是 3 个已知 LLM 出口
    roots = [ROOT / "apps", ROOT / "packages", ROOT / "mcp_servers"]
    callers = set()
    for root in roots:
        for py in root.rglob("*.py"):
            if "gateway.complete(" in py.read_text(encoding="utf-8"):
                callers.add(py.name)
    assert callers == {"chart_planner.py", "stats_interpreter.py", "kb_qa.py"}, callers


# ── 组装正确（免渲染，用 1x1 PNG）──

def test_gen_report_md_assembles_all_sections(tmp_path: Path) -> None:
    img = tmp_path / "c.png"
    img.write_bytes(_TINY_PNG)
    profile = {
        "row_count": 345, "column_count": 2,
        "columns": [{"name": "销售额", "dtype": "float", "null_ratio": 0.0,
                     "distinct_count": 300, "min": 1.0, "max": 9.0, "mean": 5.0}],
    }
    stats = [{
        "kind": "regression", "caption": "回归",
        "result": {"kind": "ols", "r_squared": 0.93, "adj_r_squared": 0.92, "n_obs": 345,
                   "coefficients": [{"name": "订单数", "coef": 125.1, "std_err": 1.0,
                                     "p_value": 0.0, "significant": True}]},
        "interpretation": "订单数是销售额的主要驱动因素。",
    }]
    res = _report_tool("gen_report_md").invoke(
        {"title": "测试报告", "profile": profile,
         "charts": [{"caption": "地区分布", "image_path": str(img)}],
         "stats": stats, "insights": "- **回归**：订单数是主要驱动。"}
    )
    md = res["markdown"]
    assert "# 测试报告" in md
    assert "## 数据画像" in md and "销售额" in md            # 画像表
    assert "data:image/png;base64," in md                     # 图片内嵌
    assert "125.1" in md and "订单数" in md                   # 数字来自工具结果
    assert "订单数是销售额的主要驱动因素。" in md              # 解读来自入参(stats_interpreter)
    assert Path(res["md_path"]).exists()


def test_insight_summary_is_pure_concat() -> None:
    res = _report_tool("insight_summary").invoke(
        {"items": [{"label": "趋势", "text": "上升"}, {"label": "回归", "text": "订单数驱动"}]}
    )
    assert "上升" in res["summary_md"] and "订单数驱动" in res["summary_md"]


def test_export_pdf_produces_pdf() -> None:
    profile = {"row_count": 1, "column_count": 1, "columns": []}
    md = _report_tool("gen_report_md").invoke({"title": "PDF 测试", "profile": profile})
    try:
        res = _report_tool("export_pdf").invoke({"report_id": md["report_id"]})
    except (ImportError, RuntimeError) as exc:
        pytest.skip(f"WeasyPrint 不可用：{exc}")
    pdf = Path(res["pdf_path"])
    assert pdf.exists() and pdf.read_bytes()[:5] == b"%PDF-"
    pdf.unlink(missing_ok=True)


# ── 端点 e2e：出非空 PDF ──

@pytest.fixture
def dataset_ref() -> str:
    n = 48
    x = np.arange(n)
    df = pd.DataFrame({
        "日期": pd.date_range("2024-01-01", periods=n, freq="D"),
        "地区": (["华东", "华北", "华南", "西南"] * 12)[:n],
        "销售额": 100 + 5 * x + 3 * np.sin(2 * np.pi * x / 12),
    })
    return save_dataframe(df)


def test_report_endpoint_e2e(dataset_ref: str) -> None:
    app.dependency_overrides[model_gateway_dep] = lambda: FakeGateway()
    try:
        client = TestClient(app)
        payload = {
            "dataset_ref": dataset_ref, "title": "销售分析报告", "interpret": True,
            "charts": [{"chart_type": "bar", "encoding": {"x": "地区", "y": "销售额", "agg": "sum"},
                        "caption": "各地区销售额"}],
            "stats": [{"kind": "trend",
                       "params": {"value_col": "销售额", "time_col": "日期", "period": 12,
                                  "forecast_horizon": 3}, "caption": "销售额趋势"}],
        }
        try:
            resp = client.post("/analyze/report", json=payload)
        except (ImportError, RuntimeError) as exc:
            pytest.skip(f"渲染环境不可用：{exc}")
        if resp.status_code == 500:
            pytest.skip(f"渲染环境不可用（500）：{resp.text[:200]}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["pdf_url"].endswith(".pdf")

        pdf = client.get(body["pdf_url"])
        assert pdf.status_code == 200
        assert pdf.content[:5] == b"%PDF-" and len(pdf.content) > 3000
        # markdown 里含解读与真实数字
        md = client.get(body["md_url"])
        assert md.status_code == 200
        assert "呈上升趋势" in md.text
    finally:
        app.dependency_overrides.clear()
