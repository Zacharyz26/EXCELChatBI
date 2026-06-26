"""图表工具实现。

gen_chart 输出 ECharts JSON（前端渲染）；chart_screenshot 用 Playwright 无头浏览器
服务端渲染截图（实例池化复用，设计文档 5.3）。图表生成时持久化底层 data_ref +
生成参数 + chart_id，写入会话 chart_registry，供追问（设计文档 6.2）。
"""

from __future__ import annotations

from typing import Any


def gen_chart(args: dict[str, Any]) -> dict[str, Any]:
    """生成 ECharts JSON 配置，并登记 chart_id → (data_ref, gen_params)。"""
    raise NotImplementedError("TODO: 组装 ECharts JSON，持久化底层数据与参数，返回 chart_id")


def chart_screenshot(args: dict[str, Any]) -> dict[str, Any]:
    """用 Playwright 无头浏览器渲染 ECharts 并截图（实例池化）。"""
    raise NotImplementedError("TODO: Playwright 加载 ECharts 配置渲染截图，返回图片引用")


def multi_layout(args: dict[str, Any]) -> dict[str, Any]:
    """多图组合布局。"""
    raise NotImplementedError("TODO: 组合多个图表为面板布局")
