"""chart_screenshot 测试：schema 校验（红线3）+ 无头 chromium 渲染出非空 PNG。

渲染依赖 .[chart-screenshot] 与已安装的 chromium；缺失时自动 skip，不拖垮套件。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.chart.server import build_server  # noqa: E402
from packages.governance.schema_validator import SchemaValidationError  # noqa: E402

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _tool():
    return build_server()._tools["chart_screenshot"]


def test_schema_rejects_missing_option() -> None:
    # 红线3：缺 option 必须在执行前被 schema 拦下
    with pytest.raises(SchemaValidationError):
        _tool().invoke({"width": 300})


def test_schema_rejects_unknown_field() -> None:
    with pytest.raises(SchemaValidationError):
        _tool().invoke({"option": {}, "chart_id": "x"})


def test_screenshot_renders_nonempty_png() -> None:
    option = {
        "title": {"text": "销售额 by 地区"},
        "xAxis": {"type": "category", "data": ["华东", "华北", "华南"]},
        "yAxis": {"type": "value"},
        "series": [{"type": "bar", "data": [1500, 400, 900]}],
    }
    try:
        res = _tool().invoke({"option": option, "width": 500, "height": 320})
    except RuntimeError as exc:  # 未装 playwright / chromium 起不来 → skip
        if "chromium" in str(exc).lower() or "playwright" in str(exc).lower():
            pytest.skip(f"渲染环境不可用：{exc}")
        raise

    path = Path(res["image_path"])
    try:
        assert path.exists()
        data = path.read_bytes()
        assert data[:8] == _PNG_MAGIC          # 真 PNG
        assert res["bytes"] > 2000             # 非空图（含柱子/文字）
        assert res["width"] == 500 and res["height"] == 320
    finally:
        path.unlink(missing_ok=True)
