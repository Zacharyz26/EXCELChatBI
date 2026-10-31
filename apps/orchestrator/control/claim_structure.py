"""保留 Markdown 表格的行列关系，不把单元格拆成无语义数字。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.common.text_delimiters import delimited_spans as delimited_spans

from apps.orchestrator.control.numeric_display import (
    NUMBER_LITERAL,
    has_numeric_symbol,
    measurement_value_context,
    numeric_quote_view,
)
from apps.orchestrator.control.statistic_statements import (
    paired_quote_spans,
    quantity_expression_spans,
    statistic_value_slots,
)


@dataclass(frozen=True)
class ClaimFragment:
    """一个数值陈述及其原始表格标签单元格，标签顺序和重复值均保留。"""

    text: str
    cells: tuple[tuple[str, str], ...] | None = None
    value_span: tuple[int, int] | None = None
    source_map: tuple[int | None, ...] = ()


def split_claim_statements(
    text: str,
    measurement_labels: set[str],
    *,
    source_labels: set[str] | None = None,
    protected_spans: tuple[tuple[int, int], ...] = (),
) -> list[str]:
    """Split complete assertions, retaining soft breaks inside a value slot.

    A grouping bracket does not merge its complete statistical sentences.
    Preserve a line break only while it continues a statistic's value slot;
    retain actual characters rather than concatenating numeric tokens.
    """
    spans = delimited_spans(text)
    numeric_view, _ = numeric_quote_view(text, measurement_labels)
    numeric_wrappers = [
        (left, right)
        for _, left, right, _ in paired_quote_spans(text)
        if NUMBER_LITERAL.fullmatch(numeric_view[left:right].strip())
    ]
    # A known field/group label can itself contain sentence punctuation.
    # Mask only the boundary-search view, preserving every source character
    # for numeric extraction and the existing exact semantic identity checks.
    boundary_view = list(text)
    for left, right in protected_spans:
        boundary_view[left:right] = " " * (right - left)
    for label in source_labels or set():
        if label:
            for label_match in re.finditer(re.escape(label), text):
                boundary_view[label_match.start() : label_match.end()] = " " * len(label)
    # Reuse complete quantity ownership once, retaining original offsets.
    # A complete expression must not lend its owner to the next statement.
    quantities = quantity_expression_spans(text, measurement_labels) if "\n" in text else []
    statements: list[str] = []
    start = 0
    for match in re.finditer(r"[。！？!?；;\n]+", "".join(boundary_view)):
        inside_delimiter = any(a <= match.start() < b for a, b in spans)
        if not match[0].strip("\n") and any(
            a <= match.start() < match.end() <= b for a, b in numeric_wrappers
        ):
            continue
        if (
            (match.group() == "\n" or (inside_delimiter and not match.group().strip("\n")))
            and (inside_delimiter or not re.match(r"[ \t\r]*\n", text[match.end() :]))
            and not re.match(r"\s*(?:[-+*]\s+|\d+[.)]\s+|#{1,6}\s+|>\s*)", text[match.end() :])
            # A postfix count is already complete; its label is not a new
            # empty value slot belonging to the following statistical sentence.
            and not re.search(
                r"个\s*(?:缺失值|空值|不同取值|重复值)\s*$", text[start : match.start()]
            )
            and not any(
                start <= a < b <= match.start() and not text[b : match.start()].strip()
                for a, b in quantities
            )
            and (
                any(start <= a < match.start() < match.end() <= b for a, b in quantities)
                or measurement_value_context(
                    text[start:],
                    match.start() - start,
                    match.end() - start,
                    measurement_labels | {"有", "存在"},
                )
            )
        ):
            continue
        statements.append(text[start : match.end()])
        start = match.end()
    if start < len(text):
        statements.append(text[start:])
    return statements


def table_claim_text(text: str, dimensions: set[str] | None = None) -> str:
    """把每个表格值绑定到自身列标题及同一行的标签，畸形表格保持原样以拒绝。"""
    return "\n".join(fragment.text for fragment in table_claim_fragments(text, dimensions))


def table_claim_fragments(
    text: str, dimensions: set[str] | None = None, *, measurement_labels: set[str] | None = None
) -> list[ClaimFragment]:
    """保留单元格结构供身份校验；展开文本仅用于数字提取及可读审计。"""
    raw_lines = text.splitlines(keepends=True)
    lines = [line.rstrip("\r\n") for line in raw_lines]
    output: list[ClaimFragment] = []
    prose: list[str] = []
    prose_map: list[int | None] = []
    spans = delimited_spans(text)
    offsets: list[int] = []
    offset = 0
    for line in raw_lines:
        offsets.append(offset)
        offset += len(line)
    index = 0
    while index < len(lines):
        header = [_plain(cell) for cell in _cells(lines[index])]
        divider = _cells(lines[index + 1]) if index + 1 < len(lines) else []
        if (
            any(a < offsets[index] < b for a, b in spans)
            or not header
            or len(header) != len(divider)
            or not all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in divider)
        ):
            if prose:
                prose_map.append(offsets[index] - 1)
            prose.append(lines[index])
            prose_map.extend(range(offsets[index], offsets[index] + len(lines[index])))
            index += 1
            continue
        if prose:
            output.append(ClaimFragment("\n".join(prose), source_map=tuple(prose_map)))
            prose.clear()
            prose_map.clear()
        index += 2
        while index < len(lines) and (row := _cells(lines[index])):
            if len(row) != len(header):
                output.append(
                    ClaimFragment(
                        lines[index],
                        (),
                        source_map=tuple(range(offsets[index], offsets[index] + len(lines[index]))),
                    )
                )
            else:
                dimension_columns = {
                    i for i, title in enumerate(header) if title in (dimensions or set())
                }
                # Cells are located by ordered delimiters, never by numeric value.
                cell_offsets = [offsets[index] + start for _, start in _cell_parts(lines[index])]
                for column, cell in enumerate(row):
                    # Do not consume labels as measurements. Dates/identifiers remain labels.
                    statistic_cell = measurement_labels is not None and bool(
                        statistic_value_slots(header[column] + "=", measurement_labels, set())
                    )
                    if column in dimension_columns or not (
                        has_numeric_symbol(cell) or statistic_cell
                    ):
                        continue
                    labels = [
                        f"{header[i]}={_plain(value)}"
                        for i, value in enumerate(row)
                        if i != column and (i in dimension_columns or not _numeric_cell(value))
                    ]
                    prefix = " ".join([*labels, f"{header[column]}="])
                    output.append(
                        ClaimFragment(
                            prefix + cell,
                            tuple(
                                (header[i], _plain(value))
                                for i, value in enumerate(row)
                                if i != column
                            ),
                            (len(prefix), len(prefix) + len(cell)),
                            tuple(
                                [None] * len(prefix)
                                + list(
                                    range(cell_offsets[column], cell_offsets[column] + len(cell))
                                )
                            ),
                        )
                    )
            index += 1
    if prose:
        output.append(ClaimFragment("\n".join(prose), source_map=tuple(prose_map)))
    return output


def _cells(line: str) -> list[str]:
    return [cell for cell, _ in _cell_parts(line)]


def _cell_parts(line: str) -> list[tuple[str, int]]:
    """表格单元格内容与同一 delimiter 解析的原文起点。"""
    if "|" not in line:
        return []
    start = len(line) - len(line.lstrip())
    end = len(line.rstrip())
    if line[start:end].startswith("|"):
        start += 1
    if line[start:end].endswith("|"):
        end -= 1
    parts = []
    cursor = start
    for boundary in [*re.finditer(r"(?<!\\)\|", line[start:end]), None]:
        stop = start + boundary.start() if boundary else end
        raw = line[cursor:stop]
        left = cursor + len(raw) - len(raw.lstrip())
        parts.append((raw.strip(), left))
        cursor = stop + 1
    return parts


def _plain(cell: str) -> str:
    return cell.strip().strip("*`_").strip()


def _numeric_cell(cell: str) -> bool:
    return NUMBER_LITERAL.fullmatch(_plain(cell)) is not None


def dimension_mentions(
    text: str, dimensions: dict[str, set[str]]
) -> list[tuple[int, int, str, str]]:
    """只把已知维度位置的数字识别为标签，不豁免同值的测量数字。"""
    mentions = []
    for dimension, groups in dimensions.items():
        for group in groups:
            if not group:
                continue
            pattern = r"(?<![A-Za-z0-9_.-])" + re.escape(group) + r"(?![A-Za-z0-9_.-])"
            for match in re.finditer(pattern, text):
                if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", group):
                    prefix = text[: match.start()]
                    suffix = text[match.end() :]
                    labelled = re.search(re.escape(dimension) + r"\s*[:：=]?\s*$", prefix)
                    year = (
                        "年" in dimension or dimension.casefold() in {"year", "years"}
                    ) and re.match(r"\s*年", suffix)
                    if not labelled and not year:
                        continue
                mentions.append((match.start(), match.end(), dimension, group))
    return [
        item
        for item in mentions
        if not any(
            other[2] == item[2]
            and other[0] <= item[0]
            and other[1] >= item[1]
            and other[1] - other[0] > item[1] - item[0]
            for other in mentions
        )
    ]


def inline_numeric_text(text: str) -> str:
    """Read paired bold spans as visible text without creating word boundaries.

    Keep every enclosed character/number; unmatched or escaped markup stays
    literal. Removing delimiters before numeric extraction keeps all offsets
    consistent and cannot turn a field-name suffix into a separate subject.
    """
    return inline_numeric_projection(text)[0]


def inline_numeric_projection(text: str) -> tuple[str, tuple[int, ...]]:
    """配对内联包装的文本与原文位置同源产生，表格/统计不靠同值定位。"""
    offsets = list(range(len(text)))
    for pattern in (
        r"(?<![\\*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\*)",
        r"(?<![\\\w])__(?=\S)(.+?)(?<=\S)__(?!\w)",
    ):
        chars = list(text)
        removed: set[int] = set()
        for match in re.finditer(pattern, text):
            removed.update(range(match.start(), match.start(1)))
            removed.update(range(match.end(1), match.end()))
        text = "".join(char for index, char in enumerate(chars) if index not in removed)
        offsets = [offset for index, offset in enumerate(offsets) if index not in removed]
    return text, tuple(offsets)


def mask_fenced_code(text: str) -> str:
    """按配对围栏屏蔽代码内容，保留位置；代码里的引号不能影响外层作用域。"""
    fence: tuple[str, int] | None = None
    result: list[str] = []
    for line in text.splitlines(keepends=True):
        hidden = fence is not None
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            hidden = True
            if fence is None:
                fence = (marker[1][0], len(marker[1]))
            elif (
                marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and re.fullmatch(r" {0,3}" + re.escape(marker[1]) + r"\s*", line)
            ):
                fence = None
        result.append("".join(c if c in "\r\n" else " " for c in line) if hidden else line)
    return "".join(result)
