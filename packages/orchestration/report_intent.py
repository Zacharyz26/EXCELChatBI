"""Bounded existing-material operands, not permission or evidence of delivery."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from packages.common.text_surface import parse_text_surface

_NOUN = r"汇总表|统计表|聚合表|折线图|柱状图|条形图|饼图|散点图|趋势图|图表"
_QUALIFIER = r"月度|年度|季度|每日|当前|已有|已生成"
_ITEM = rf"(?:(?:{_QUALIFIER})的?)?(?:{_NOUN})"
_DELIVERY_ITEM = rf"(?:{_ITEM}|(?:期间|整个期间的)?(?:总计|合计)|单位)"
_ITEM_LIST = rf"{_DELIVERY_ITEM}(?:\s*(?:和|以及|及|、)\s*{_DELIVERY_ITEM})*"
_SOURCE = re.compile(
    rf"(?:^|[，,。；;\n])\s*(?:请\s*)?(?:用|使用|基于)\s*"
    rf"(?P<selector>(?:当前)?(?:已生成|已有|现有)|当前|刚才|上次|最新|上述)(?:的)?\s*"
    rf"(?P<operands>{_ITEM}(?:\s*(?:和|以及|及|、)\s*{_ITEM})*)"
)
_REPORT = re.compile(
    rf"(?:生成|制作|创建|组装|编制|导出)\s*(?:新的?|一份)?\s*"
    rf"(?:(?:包含|含有|含|带有)(?P<materials>{_ITEM_LIST})的?)?\s*"
    r"(?:(?:PDF|Markdown)\s*)?报告",
    re.I,
)


def _request_view(text: str) -> str:
    chars = list(text)
    for node in parse_text_surface(text).nodes:
        if node.kind in {"quote", "inline_code", "fence", "blockquote"}:
            left, right = node.span
            chars[left:right] = " " * (right - left)
    return "".join(chars)


def report_creation_mentions(text: str) -> tuple[tuple[tuple[int, int], str], ...]:
    """返回结构完整的报告动作及局部用途，材料长度不改变交付义务。"""
    # Runtime import avoids creating a second polarity grammar. This function
    # does not call operation_mentions (which consumes the projection below).
    from .operation_intent import operation_prefix_disposition

    view = _request_view(text)
    result = []
    for match in _REPORT.finditer(view):
        left = max(
            (m.end() for m in re.finditer(r"[，,。；;\n]", view[: match.start()])), default=0
        )
        prefix = re.sub(r"(?:重新|再次|再)\s*$", "", view[left : match.start()])
        disposition = operation_prefix_disposition(prefix)
        # The explicit creation verb remains current after a source operand;
        # local prohibition/defer/description is never replaced by this default.
        if disposition is None:
            marker = re.search(
                r"(?:不要|暂不|不需要|无需|不用|禁止|稍后|以后|后续|下一轮|说明|描述|讨论)[^，,。；;\n]*$",
                prefix,
            )
            disposition = operation_prefix_disposition(marker[0]) if marker else "current"
        result.append((match.span(), disposition or "described"))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ReportSourceOperand:
    artifact_type: str
    noun_span: tuple[int, int]
    chart_kind: str | None = None
    temporal_grain: str | None = None
    qualifier_span: tuple[int, int] | None = None
    selection: str = "recent"


@dataclass(frozen=True, slots=True)
class ReportSourceIntent:
    source_hash: str
    # All spans are exact original-source noun spans, never synthetic text.
    operands: tuple[ReportSourceOperand, ...] = ()
    material_operands: tuple[ReportSourceOperand, ...] = ()
    inactive_spans: tuple[tuple[int, int], ...] = ()

    @property
    def required_operands(self) -> tuple[ReportSourceOperand, ...]:
        return self.operands + self.material_operands

    @property
    def required_types(self) -> frozenset[str]:
        return frozenset(item.artifact_type for item in self.required_operands)

    def consumes(self, start: int, end: int) -> bool:
        return any(item.noun_span[0] <= start < end <= item.noun_span[1] for item in self.operands)

    def material_owns(self, start: int, end: int) -> bool:
        return any(left <= start < end <= right for left, right in self.inactive_spans) or any(
            item.noun_span[0] <= start < end <= item.noun_span[1] for item in self.required_operands
        )

    def production_text(self, text: str) -> str:
        if hashlib.sha256(text.encode()).hexdigest() != self.source_hash:
            raise ValueError("report_source_intent_source_mismatch")
        chars = list(text)
        for left, right in self.inactive_spans:
            chars[left:right] = " " * (right - left)
        for item in self.operands:
            left, right = item.noun_span
            chars[left:right] = " " * (right - left)
        return "".join(chars)


def report_source_intent(text: str) -> ReportSourceIntent:
    """Recognize complete source operands only; unmatched syntax stays executable."""
    source_hash = hashlib.sha256(text.encode()).hexdigest()
    view = _request_view(text)
    creations = report_creation_mentions(text)
    current = {span for span, disposition in creations if disposition == "current"}
    inactive = tuple(span for span, disposition in creations if disposition != "current")
    if not current:
        return ReportSourceIntent(source_hash, inactive_spans=inactive)
    operands: list[ReportSourceOperand] = []
    materials: list[ReportSourceOperand] = []
    owned_ranges = [
        (
            match.start("operands"),
            match["operands"],
            operands,
            "unique" if match["selector"] == "上述" else "recent",
        )
        for match in _SOURCE.finditer(view)
        if not view[match.end() :]
        or re.match(r"\s*(?:[，,。；;\n]|生成|制作|创建|组装|编制|导出|$)", view[match.end() :])
    ]
    owned_ranges.extend(
        (match.start("materials"), match["materials"], materials, "material")
        for match in _REPORT.finditer(view)
        if match.span() in current and match["materials"] is not None
    )
    for offset, operand_text, target, selection in owned_ranges:
        for item in re.finditer(
            rf"(?:(?P<qualifier>{_QUALIFIER})的?)?(?P<noun>{_NOUN})", operand_text
        ):
            noun = item["noun"]
            kind = "table" if noun.endswith("表") and noun != "图表" else "chart"
            chart_kind = {
                "折线图": "line",
                "柱状图": "bar",
                "条形图": "bar",
                "饼图": "pie",
                "散点图": "scatter",
                "趋势图": "line",
            }.get(noun)
            grain = {"月度": "month", "年度": "year", "季度": "quarter", "每日": "day"}.get(
                item["qualifier"]
            )
            qualifier_span = (
                (offset + item.start("qualifier"), offset + item.end("qualifier"))
                if item["qualifier"] is not None
                else None
            )
            target.append(
                ReportSourceOperand(
                    kind,
                    (offset + item.start("noun"), offset + item.end("noun")),
                    chart_kind,
                    grain,
                    qualifier_span,
                    selection,
                )
            )
    return ReportSourceIntent(source_hash, tuple(operands), tuple(materials), inactive)


def source_operand_matches(
    operand: ReportSourceOperand,
    *,
    artifact_type: str,
    source_tool: str | None,
    params: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> bool:
    """Match declared material identity using existing tool fields, never titles/values."""
    if artifact_type != operand.artifact_type:
        return False
    if artifact_type == "chart":
        option = payload.get("option")
        if source_tool != "gen_chart" or not isinstance(option, dict):
            return False
        kind = params.get("chart_type")
        series = option.get("series")
        if (
            not isinstance(kind, str)
            or payload.get("chart_type") != kind
            or not isinstance(series, list)
            or not series
            or any(not isinstance(item, dict) or item.get("type") != kind for item in series)
        ):
            return False
        if operand.chart_kind is not None and kind != operand.chart_kind:
            return False
        encoding = params.get("encoding")
        group = encoding.get("x") if isinstance(encoding, dict) else None
    elif artifact_type == "table":
        if source_tool != "aggregate_preview":
            return False
        group = params.get("group_col")
        if not isinstance(group, str) or payload.get("group_col") != group:
            return False
    else:
        return False
    if operand.temporal_grain is not None:
        # This is the existing finite field-identity contract. The aggregate
        # tool does not perform arbitrary calendar bucketing. Unknown names
        # cannot acquire monthly semantics from labels, totals or date-looking rows.
        field = {"month": "月份", "year": "年份", "quarter": "季度", "day": "日期"}.get(
            operand.temporal_grain
        )
        if field is None or group != field:
            return False
    return True
