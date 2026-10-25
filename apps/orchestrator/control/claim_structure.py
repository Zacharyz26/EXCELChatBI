"""保留 Markdown 表格的行列关系，不把单元格拆成无语义数字。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from apps.orchestrator.control.numeric_display import (
    NUMBER_LITERAL,
    has_numeric_symbol,
    measurement_value_context,
    numeric_quote_view,
)
from apps.orchestrator.control.statistic_statements import (
    paired_quote_spans,
    quantity_expression_spans,
    quote_token_at,
    statistic_value_slots,
)


@dataclass(frozen=True)
class ClaimFragment:
    """一个数值陈述及其原始表格标签单元格，标签顺序和重复值均保留。"""

    text: str
    cells: tuple[tuple[str, str], ...] | None = None
    value_span: tuple[int, int] | None = None


# Delimiters describe assertion scope, never regions to skip numeric checks.
_PAIRS = dict(zip("(（[［{｛【「『〈《“‘", ")）]］}｝】」』〉》”’", strict=True))


def delimited_spans(text: str) -> list[tuple[int, int]]:
    """Keep nested bracket/quote content together, including an unclosed tail."""
    stack: list[tuple[str, int]] = []
    spans: list[tuple[int, int]] = []
    escaped = False
    for index, char in enumerate(text):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char in "\"'`“”‘’「」『』" and quote_token_at(text, index) is None:
            continue
        if stack and char == stack[-1][0]:
            _, start = stack.pop()
            if not stack:
                spans.append((start, index + 1))
            continue
        if char in _PAIRS:
            stack.append((_PAIRS[char], index))
        elif char in "\"'`":
            stack.append((char, index))
    if stack:
        spans.append((stack[0][1], len(text)))
    return spans


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
    lines = text.splitlines()
    output: list[ClaimFragment] = []
    prose: list[str] = []
    spans = delimited_spans("\n".join(lines))
    offsets: list[int] = []
    offset = 0
    for line in lines:
        offsets.append(offset)
        offset += len(line) + 1
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
            prose.append(lines[index])
            index += 1
            continue
        if prose:
            output.append(ClaimFragment("\n".join(prose)))
            prose.clear()
        index += 2
        while index < len(lines) and (row := _cells(lines[index])):
            if len(row) != len(header):
                output.append(ClaimFragment(lines[index], ()))
            else:
                dimension_columns = {
                    i for i, title in enumerate(header) if title in (dimensions or set())
                }
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
                        )
                    )
            index += 1
    if prose:
        output.append(ClaimFragment("\n".join(prose)))
    return output


def _cells(line: str) -> list[str]:
    if "|" not in line:
        return []
    return [
        cell.strip()
        for cell in re.split(r"(?<!\\)\|", line.strip().removeprefix("|").removesuffix("|"))
    ]


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
    text = re.sub(r"(?<![\\*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\*)", r"\1", text)
    return re.sub(r"(?<![\\\w])__(?=\S)(.+?)(?<=\S)__(?!\w)", r"\1", text)


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
