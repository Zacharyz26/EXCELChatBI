"""报告工具实现（Markdown · WeasyPrint）。

insight_summary 由 LLM 生成中文解读，但所有数值引用自工具结果（红线2）；
报告中图表绑定底层 data_ref，支持追问（设计文档 6.1）。用户可见文案一律中文。
"""

from __future__ import annotations

from typing import Any


def gen_report_md(args: dict[str, Any]) -> dict[str, Any]:
    """组装 Markdown 报告（图表 + 解读），持久化并返回 report_ref。"""
    raise NotImplementedError("TODO: 组装 Markdown，绑定 chart/data_ref，持久化")


def insight_summary(args: dict[str, Any]) -> dict[str, Any]:
    """对分析结果生成中文洞察解读（数值引用自工具结果）。"""
    raise NotImplementedError("TODO: 基于分析结果生成中文解读")


def export_pdf(args: dict[str, Any]) -> dict[str, Any]:
    """将 Markdown 报告导出为 PDF（WeasyPrint），存 MinIO。"""
    raise NotImplementedError("TODO: WeasyPrint 渲染 PDF，上传 MinIO，返回引用")
