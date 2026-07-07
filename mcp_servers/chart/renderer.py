"""ECharts option → PNG 服务端渲染（Playwright 无头 chromium）。

设计文档 5.3：图表由前端 ECharts 渲染，后端拿不到图片；报告 PDF 需要真实图片，
故用无头 chromium 加载**本地内置的** echarts 资源，setOption 后导出 PNG。

要点：
- 截图必须**关闭动画**（animation=false），否则截到入场动画未完成的空图。
- playwright 惰性导入：未装 .[chart-screenshot] 时，本模块可被 import，仅调用渲染时才需要。
- chromium 路径可由配置指定；缺省自动探测 playwright 已安装的完整 chromium。
- 池化复用留后续：当前每次调用独立启动/关闭浏览器（sync API 线程亲和，先求正确）。
"""

from __future__ import annotations

import glob
import os
from pathlib import Path
from typing import Any

from packages.common.config import get_settings

_ASSETS = Path(__file__).parent / "assets"
_ECHARTS_JS = _ASSETS / "echarts.min.js"

# 每个 chart 独立 HTML 容器；宽高由调用方给定
_HTML_TEMPLATE = "<div id='c' style='width:{w}px;height:{h}px;'></div>"

# 在容器里初始化 echarts、setOption、导出 dataURL（关动画，白底）
_RENDER_JS = """(opt) => {
    const chart = echarts.init(document.getElementById('c'), null, {renderer: 'canvas'});
    chart.setOption(opt);
    return chart.getDataURL({type: 'png', pixelRatio: 2, backgroundColor: '#fff'});
}"""


def _resolve_chromium() -> str | None:
    """定位可用的 chromium 可执行文件。

    优先用配置 `chromium_executable_path`；否则探测 playwright 缓存里的完整 chromium
    （本环境 chrome-headless-shell 下载失败，故不依赖默认 headless-shell 路径）。
    返回 None 时交由 playwright 用其默认解析。
    """
    configured = get_settings().chromium_executable_path
    if configured:
        return configured
    home = os.path.expanduser("~")
    patterns = [
        f"{home}/.cache/ms-playwright/chromium-*/chrome-linux64/chrome",
        f"{home}/.cache/ms-playwright/chromium-*/chrome-linux/chrome",
    ]
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]  # 取版本号最大的
    return None


def render_option_to_png(option: dict[str, Any], width: int = 700, height: int = 420) -> bytes:
    """把一个 ECharts option 渲染为 PNG 字节。

    Args:
        option: ECharts 配置（数值来自 gen_chart 的真实数据，红线2）。
        width: 画布宽（px）。
        height: 画布高（px）。

    Returns:
        PNG 图像字节。

    Raises:
        RuntimeError: 渲染环境不可用（未装 playwright / chromium 起不来）。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # 未装 .[chart-screenshot]
        raise RuntimeError(
            "图表截图需要 playwright：uv sync --extra chart-screenshot && "
            "uv run playwright install chromium"
        ) from exc

    # 关动画：导出最终态而非动画中间帧
    render_opt = {**option, "animation": False}
    echarts_js = _ECHARTS_JS.read_text(encoding="utf-8")
    chromium = _resolve_chromium()

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                executable_path=chromium,
                args=["--no-sandbox", "--disable-gpu"],
            )
            try:
                page = browser.new_page(viewport={"width": width + 40, "height": height + 40})
                page.set_content(_HTML_TEMPLATE.format(w=width, h=height))
                page.add_script_tag(content=echarts_js)
                data_url = page.evaluate(_RENDER_JS, render_opt)
            finally:
                browser.close()
    except Exception as exc:  # 浏览器起不来/渲染失败
        raise RuntimeError(f"图表渲染失败（chromium 无法启动或渲染出错）：{exc}") from exc

    import base64

    header, _, b64 = data_url.partition(",")
    if "png" not in header or not b64:
        raise RuntimeError(f"渲染未返回 PNG dataURL：{data_url[:60]!r}")
    return base64.b64decode(b64)


def render_option_to_file(
    option: dict[str, Any], out_path: Path, width: int = 700, height: int = 420
) -> Path:
    """渲染并写入 PNG 文件，返回文件路径。"""
    png = render_option_to_png(option, width, height)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(png)
    return out_path
