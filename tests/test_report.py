"""报告工具测试：确定性组装、零 LLM 与非空 PDF。"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.report import tools as report_tools  # noqa: E402
from mcp_servers.report.server import build_server as build_report_server  # noqa: E402

# 1x1 PNG，用于免渲染的组装测试
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


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


def test_llm_exit_points_remain_explicitly_allowlisted() -> None:
    # 全库非 Agent 流式调用的 gateway.complete 出口必须显式受控。
    # semantic_verifier 仅供隔离评测，生产接入仍受 go/no-go 门禁约束。
    roots = [ROOT / "apps", ROOT / "packages", ROOT / "mcp_servers"]
    callers = set()
    for root in roots:
        for py in root.rglob("*.py"):
            if "gateway.complete(" in py.read_text(encoding="utf-8"):
                callers.add(py.name)
    assert callers == {"semantic_verifier.py"}, callers


# ── 组装正确（免渲染，用 1x1 PNG）──


def test_gen_report_md_assembles_all_sections(tmp_path: Path) -> None:
    img = tmp_path / "c.png"
    img.write_bytes(_TINY_PNG)
    profile = {
        "row_count": 345,
        "column_count": 2,
        "columns": [
            {
                "name": "销售额",
                "dtype": "float",
                "null_ratio": 0.0,
                "distinct_count": 300,
                "min": 1.0,
                "max": 9.0,
                "mean": 5.0,
            }
        ],
    }
    stats = [
        {
            "kind": "regression",
            "caption": "回归",
            "result": {
                "kind": "ols",
                "r_squared": 0.93,
                "adj_r_squared": 0.92,
                "n_obs": 345,
                "coefficients": [
                    {
                        "name": "订单数",
                        "coef": 125.1,
                        "std_err": 1.0,
                        "p_value": 0.0,
                        "significant": True,
                    }
                ],
            },
            "interpretation": "订单数是销售额的主要驱动因素。",
        }
    ]
    res = _report_tool("gen_report_md").invoke(
        {
            "title": "测试报告",
            "profile": profile,
            "charts": [{"caption": "地区分布", "image_path": str(img)}],
            "stats": stats,
            "insights": "- **回归**：订单数是主要驱动。",
        }
    )
    md = res["markdown"]
    assert "# 测试报告" in md
    assert "## 数据画像" in md and "销售额" in md  # 画像表
    assert "data:image/png;base64," in md  # 图片内嵌
    assert "125.1" in md and "订单数" in md  # 数字来自工具结果
    assert "订单数是销售额的主要驱动因素。" in md  # 解读来自已验证的入参
    assert Path(res["md_path"]).exists()


def test_report_file_is_atomically_published_without_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(report_tools, "_reports_dir", lambda: tmp_path)

    result = _report_tool("gen_report_md").invoke(
        {"title": "原子报告", "profile": {"row_count": 1, "columns": []}}
    )

    assert Path(result["md_path"]).read_text(encoding="utf-8").startswith("# 原子报告")
    assert list(tmp_path.glob("*.tmp")) == []


def test_insight_summary_is_pure_concat() -> None:
    res = _report_tool("insight_summary").invoke(
        {"items": [{"label": "趋势", "text": "上升"}, {"label": "回归", "text": "订单数驱动"}]}
    )
    assert "上升" in res["summary_md"] and "订单数驱动" in res["summary_md"]


def test_report_renders_governed_forecast_evidence() -> None:
    stats = [
        {
            "kind": "forecast",
            "caption": "销售额预测",
            "result": {
                "selected_method": "drift",
                "reliability": "moderate",
                "horizon": 2,
                "validation_metrics": {"mae": 1.25, "rmse": 1.5},
                "baseline": {"beats_baseline": True},
                "predictions": [
                    {
                        "time": "2026-01-01T00:00:00",
                        "point": 120.0,
                        "lower": 117.0,
                        "upper": 123.0,
                    }
                ],
            },
        }
    ]

    result = _report_tool("gen_report_md").invoke(
        {
            "title": "预测报告",
            "profile": {"row_count": 30, "columns": []},
            "stats": stats,
        }
    )

    markdown = result["markdown"]
    assert "drift" in markdown and "moderate" in markdown
    assert "1.25" in markdown and "117" in markdown and "123" in markdown


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
