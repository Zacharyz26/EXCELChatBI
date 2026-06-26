"""Excel 解析工具实现（pandas / openpyxl，大表走 DuckDB 分块）。

要点：仅产出"数据画像"，原始整表不进 LLM（红线1）；大表超阈值切 DuckDB（第7节）。
"""

from __future__ import annotations

from typing import Any

from mcp_servers.excel_parser.profile import DataProfile


def parse_excel(args: dict[str, Any]) -> dict[str, Any]:
    """解析 Excel 文件，落地为数据集引用（dataset_ref），不返回整表。"""
    raise NotImplementedError("TODO: pandas/openpyxl 读取 → 持久化 → 返回 dataset_ref")


def infer_schema(args: dict[str, Any]) -> DataProfile:
    """推断 schema 与统计摘要，生成数据画像（DataProfile）。"""
    raise NotImplementedError("TODO: 推断列类型/空值率/统计摘要/样本行，组装 DataProfile")


def data_preview(args: dict[str, Any]) -> dict[str, Any]:
    """返回少量样本行供用户确认（前端先展示画像再分析）。"""
    raise NotImplementedError("TODO: 返回脱敏样本行")
