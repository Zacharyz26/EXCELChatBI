"""Bounded existing-material operands, not permission or evidence of delivery."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from packages.common.text_surface import parse_text_surface

_NOUN = r"汇总表|统计表|聚合表|折线图|柱状图|条形图|饼图|散点图|趋势图|图表"
_QUALIFIER = r"月度|年度|季度|每日|当前|已有|已生成"
_ITEM = rf"(?:(?:{_QUALIFIER})的?)?(?:{_NOUN})"
_SOURCE = re.compile(
    rf"(?:^|[，,。；;\n])\s*(?:请\s*)?(?:用|使用|基于)\s*"
    rf"(?:(?:当前)?(?:已生成|已有|现有)|当前)(?:的)?\s*"
    rf"(?P<operands>{_ITEM}(?:\s*(?:和|以及|及|、)\s*{_ITEM})*)"
)
_REPORT = re.compile(r"(?:生成|制作|创建|组装|编制|导出)\s*(?:新的?|一份)?\s*(?:PDF\s*)?报告", re.I)


@dataclass(frozen=True, slots=True)
class ReportSourceOperand:
    artifact_type: str
    noun_span: tuple[int, int]
    chart_kind: str | None = None
    temporal_grain: str | None = None
    qualifier_span: tuple[int, int] | None = None


@dataclass(frozen=True, slots=True)
class ReportSourceIntent:
    source_hash: str
    # All spans are exact original-source noun spans, never synthetic text.
    operands: tuple[ReportSourceOperand, ...] = ()

    @property
    def required_types(self) -> frozenset[str]:
        return frozenset(item.artifact_type for item in self.operands)

    def consumes(self, start: int, end: int) -> bool:
        return any(item.noun_span[0] <= start < end <= item.noun_span[1] for item in self.operands)

    def production_text(self, text: str) -> str:
        if hashlib.sha256(text.encode()).hexdigest() != self.source_hash:
            raise ValueError("report_source_intent_source_mismatch")
        chars = list(text)
        for item in self.operands:
            left, right = item.noun_span
            chars[left:right] = " " * (right - left)
        return "".join(chars)


def report_source_intent(text: str) -> ReportSourceIntent:
    """Recognize complete source operands only; unmatched syntax stays executable."""
    source_hash = hashlib.sha256(text.encode()).hexdigest()
    chars = list(text)
    for node in parse_text_surface(text).nodes:
        if node.kind in {"quote", "inline_code", "fence"}:
            left, right = node.span
            chars[left:right] = " " * (right - left)
    view = "".join(chars)
    if _REPORT.search(view) is None:
        return ReportSourceIntent(source_hash)
    operands: list[ReportSourceOperand] = []
    for match in _SOURCE.finditer(view):
        # A bounded operand ends before another command or clause; partial
        # noun matches cannot turn an unknown longer word into a trusted input.
        tail = view[match.end() :]
        if tail and not re.match(r"\s*(?:[，,。；;\n]|生成|制作|创建|组装|编制|导出|$)", tail):
            continue
        for item in re.finditer(
            rf"(?:(?P<qualifier>{_QUALIFIER})的?)?(?P<noun>{_NOUN})", match["operands"]
        ):
            offset = match.start("operands")
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
            operands.append(
                ReportSourceOperand(
                    kind,
                    (offset + item.start("noun"), offset + item.end("noun")),
                    chart_kind,
                    grain,
                    qualifier_span,
                )
            )
    return ReportSourceIntent(source_hash, tuple(operands))
