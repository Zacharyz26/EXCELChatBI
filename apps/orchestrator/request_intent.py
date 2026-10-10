"""Deterministic detection of explicit chart and report requests."""

from __future__ import annotations

import re
from typing import cast

from mcp_servers.excel_parser.advisor import infer_data_roles_from_mapping
from packages.session.models import Dataset, JsonObject

_CHART_REQUEST_PATTERN = re.compile(
    r"(?:图表|图像|可视化|画图|绘图|出图|折线图|柱状图|条形图|饼图|散点图|趋势图|"
    r"(?:生成|绘制|画|做|出|展示|显示|查看).{0,6}图|chart|plot|graph|visuali[sz])",
    re.IGNORECASE,
)
_CHART_NEGATION_PATTERN = re.compile(
    r"(?:不要|无需|不需要|不用|别).{0,6}(?:图|图表|图像|可视化|chart|plot|graph)",
    re.IGNORECASE,
)
_ALL_DIMENSION_CHART_PATTERN = re.compile(
    r"(?:各|每个|所有|全部)\s*[【\[]?\s*维度(?:列)?\s*[】\]]?",
    re.IGNORECASE,
)
_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:(?:生成|导出|制作|创建|组装|整理|汇总|编制|输出|给我|请给).{0,10}"
    r"报告|报告.{0,10}(?:生成|导出|制作|创建|下载))",
    re.IGNORECASE,
)
_REPORT_NEGATION_PATTERN = re.compile(r"(?:不要|无需|不需要|不用|别).{0,6}报告", re.IGNORECASE)
_PDF_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:(?:生成|导出|制作|创建|输出|给我|请给).{0,10}pdf|"
    r"pdf.{0,10}(?:生成|导出|制作|创建|下载))",
    re.IGNORECASE,
)
_MARKDOWN_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:(?:生成|导出|制作|创建|输出|给我|请给).{0,10}markdown|"
    r"markdown.{0,10}(?:生成|导出|制作|创建|下载))",
    re.IGNORECASE,
)
_MARKDOWN_NEGATION_PATTERN = re.compile(
    r"(?:不要|无需|不需要|不用|别).{0,6}markdown", re.IGNORECASE
)
_PDF_REQUEST_PATTERN = re.compile(r"pdf", re.IGNORECASE)
_PDF_NEGATION_PATTERN = re.compile(r"(?:不要|无需|不需要|不用|别).{0,6}pdf", re.IGNORECASE)


def requests_chart(user_text: str) -> bool:
    """Return whether the user explicitly requests a chart."""
    return (
        _CHART_NEGATION_PATTERN.search(user_text) is None
        and _CHART_REQUEST_PATTERN.search(user_text) is not None
    )


def required_chart_dimensions(
    user_text: str,
    datasets: list[Dataset],
) -> tuple[str, ...]:
    """Compile explicit/all-dimension chart coverage from governed profiles."""
    if not requests_chart(user_text) or not datasets:
        return ()
    mentioned = [
        dataset
        for dataset in datasets
        if dataset.ref in user_text or dataset.filename in user_text
    ]
    if len(mentioned) == 1:
        dataset = mentioned[0]
    elif len(datasets) == 1:
        dataset = datasets[0]
    else:
        return ()
    try:
        inferred = infer_data_roles_from_mapping(dataset.profile, dataset_ref=dataset.ref)
    except ValueError:
        return ()
    raw_columns = inferred.get("columns")
    columns = cast(list[JsonObject], raw_columns) if isinstance(raw_columns, list) else []
    dimensions = tuple(
        str(item["column"])
        for item in columns
        if isinstance(item.get("column"), str)
        and item.get("primary_role") == "dimension"
        and not bool(item.get("ambiguous"))
    )
    explicitly_named = tuple(column for column in dimensions if column in user_text)
    if _ALL_DIMENSION_CHART_PATTERN.search(user_text) is not None:
        return dimensions
    return explicitly_named


def requests_report(user_text: str) -> bool:
    """Return whether the user explicitly requests a generated report."""
    report_requested = (
        _REPORT_REQUEST_PATTERN.search(user_text) is not None
        and _REPORT_NEGATION_PATTERN.search(user_text) is None
    )
    pdf_requested = (
        _PDF_REPORT_REQUEST_PATTERN.search(user_text) is not None
        and _PDF_NEGATION_PATTERN.search(user_text) is None
    )
    markdown_requested = (
        _MARKDOWN_REPORT_REQUEST_PATTERN.search(user_text) is not None
        and _MARKDOWN_NEGATION_PATTERN.search(user_text) is None
    )
    return report_requested or pdf_requested or markdown_requested


def requests_pdf(user_text: str) -> bool:
    """Return whether a report request explicitly asks for PDF export."""
    return (
        _PDF_NEGATION_PATTERN.search(user_text) is None
        and _PDF_REQUEST_PATTERN.search(user_text) is not None
    )
