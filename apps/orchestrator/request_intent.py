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
_ENGLISH_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:\b(?:generate|create|produce|export|download|build|compile|write|make|"
    r"assemble|prepare)\b.{0,40}\breport\b|"
    r"\bgive\s+me\b.{0,40}\breport\b|"
    r"\breport\b.{0,20}\b(?:generate|create|export|download)\b)",
    re.IGNORECASE,
)
_REPORT_NEGATION_PATTERN = re.compile(
    r"(?:(?:不要|无需|不需要|不用|别).{0,10}报告|"
    r"\b(?:do\s+not|don't|dont|no|without|skip)\b.{0,30}\breport\b)",
    re.IGNORECASE,
)
_EXISTING_REPORT_REFERENCE_PATTERN = re.compile(
    r"(?:(?:引用|查看|打开|展示|使用|读取|基于|下载|导出).{0,12}"
    r"(?:已有|现有|之前|上次|刚才|已生成).{0,8}(?:报告|pdf|markdown)|"
    r"(?:已有|现有|之前|上次|刚才|已生成).{0,4}报告|"
    r"\b(?:use|reference|cite|open|show|view|read|download|export)\b.{0,30}"
    r"\b(?:existing|previous|prior|last|generated)\b.{0,12}"
    r"\b(?:report|pdf|markdown)\b)",
    re.IGNORECASE,
)
_NEW_REPORT_FROM_REFERENCE_PATTERN = re.compile(
    r"(?:(?:生成|创建|制作|编制|组装).{0,12}(?:一份|新的?|新|更新).{0,8}报告|"
    r"(?:已有|现有|之前|上次|刚才|已生成).{0,6}报告.{0,16}"
    r"(?:重新|再次|更新|重做)?(?:生成|创建|制作|编制|组装).{0,8}报告|"
    r"(?:重新|再次|更新|重做).{0,6}(?:生成|创建|制作|编制|组装).{0,8}报告|"
    r"\b(?:generate|create|produce|build|compile|write|make|assemble|prepare)\b"
    r".{0,16}\b(?:a\s+|an\s+)?(?:new|updated|revised|refreshed)\b.{0,20}"
    r"\breport\b|"
    r"\b(?:existing|previous|prior|last|generated)\b.{0,12}\breport\b.{0,24}"
    r"\b(?:generate|create|produce|build|compile|write|make|assemble|prepare)\b"
    r".{0,20}\breport\b)",
    re.IGNORECASE,
)
_PDF_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:(?:生成|导出|制作|创建|输出|给我|请给).{0,10}pdf|"
    r"pdf.{0,10}(?:生成|导出|制作|创建|下载)|"
    r"\b(?:generate|create|produce|export|download|make|prepare)\b.{0,30}\bpdf\b|"
    r"\bpdf\b.{0,20}\b(?:generate|create|export|download)\b)",
    re.IGNORECASE,
)
_MARKDOWN_REPORT_REQUEST_PATTERN = re.compile(
    r"(?:(?:生成|导出|制作|创建|输出|给我|请给).{0,10}markdown|"
    r"markdown.{0,10}(?:生成|导出|制作|创建|下载)|"
    r"\b(?:generate|create|produce|export|download|make|prepare)\b.{0,30}"
    r"\bmarkdown\b|"
    r"\bmarkdown\b.{0,20}\b(?:generate|create|export|download)\b)",
    re.IGNORECASE,
)
_MARKDOWN_NEGATION_PATTERN = re.compile(
    r"(?:(?:不要|无需|不需要|不用|别).{0,6}markdown|"
    r"\b(?:do\s+not|don't|dont|no|not|without|skip)\b.{0,20}\bmarkdown\b)",
    re.IGNORECASE,
)
_PDF_REQUEST_PATTERN = re.compile(r"pdf", re.IGNORECASE)
_PDF_NEGATION_PATTERN = re.compile(
    r"(?:(?:不要|无需|不需要|不用|别).{0,6}pdf|"
    r"\b(?:do\s+not|don't|dont|no|not|without|skip)\b.{0,20}\bpdf\b)",
    re.IGNORECASE,
)
_CLAUSE_SPLIT_PATTERN = re.compile(r"[,，;；。.!?！？\n]+")


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
    for clause in _request_clauses(user_text):
        references_existing = _EXISTING_REPORT_REFERENCE_PATTERN.search(clause) is not None
        creates_updated_report = _NEW_REPORT_FROM_REFERENCE_PATTERN.search(clause) is not None
        report_requested = (
            _REPORT_NEGATION_PATTERN.search(clause) is None
            and (
                _REPORT_REQUEST_PATTERN.search(clause) is not None
                or _ENGLISH_REPORT_REQUEST_PATTERN.search(clause) is not None
            )
        )
        if report_requested:
            if not references_existing or creates_updated_report:
                return True
        if (
            _PDF_REPORT_REQUEST_PATTERN.search(clause) is not None
            and _PDF_NEGATION_PATTERN.search(clause) is None
            and (not references_existing or creates_updated_report)
        ):
            return True
        if (
            _MARKDOWN_REPORT_REQUEST_PATTERN.search(clause) is not None
            and _MARKDOWN_NEGATION_PATTERN.search(clause) is None
            and (not references_existing or creates_updated_report)
        ):
            return True
    return False


def requests_pdf(user_text: str) -> bool:
    """Return whether a report request explicitly asks for PDF export."""
    return any(
        _PDF_REQUEST_PATTERN.search(clause) is not None
        and _PDF_NEGATION_PATTERN.search(clause) is None
        for clause in _request_clauses(user_text)
    )


def _request_clauses(user_text: str) -> tuple[str, ...]:
    """Split independent request clauses so a local negation does not mask a later action."""
    return tuple(
        clause.strip()
        for clause in _CLAUSE_SPLIT_PATTERN.split(user_text)
        if clause.strip()
    )
