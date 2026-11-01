"""保留 Markdown 表格的行列关系，不把单元格拆成无语义数字。"""

from __future__ import annotations

import re
from dataclasses import dataclass

from packages.common.text_surface import SourceProjection, parse_text_surface, table_cell_parts

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
    cell_sources: tuple[tuple[str, str, tuple[int, int], tuple[int, int]], ...] = ()
    header_span: tuple[int, int] | None = None
    source_context: str = ""
    literal_label: bool = False
    constraint_context: str = ""


@dataclass(frozen=True)
class ProfileTableContext:
    """同一行字段格、目标值列标题与值格的显式关系，不依赖数值相等。"""

    field: str
    role_label: str
    field_span: tuple[int, int]
    role_span: tuple[int, int]
    value_source_span: tuple[int, int]


@dataclass(frozen=True)
class StatementProjection:
    """唯一分句投影，保存原文坐标和有界标题/表格上下文。"""

    text: str
    source_map: tuple[int | None, ...]
    cells: tuple[tuple[str, str], ...] | None = None
    value_span: tuple[int, int] | None = None
    heading_context: str = ""
    table_context: ProfileTableContext | None = None
    source_context: str = ""
    literal_label: bool = False
    constraint_context: str = ""


def delimited_spans(text: str) -> list[tuple[int, int]]:
    """旧接口消费同一源容器；未闭合尾段仍保留，不能用来豁免数量。"""
    spans = {
        node.span
        for node in parse_text_surface(text).nodes
        if node.kind in {"quote", "inline_code", "bracket"}
    }
    return sorted(
        span
        for span in spans
        if not any(other != span and other[0] <= span[0] and span[1] <= other[1] for other in spans)
    )


def source_wrapper_view(text: str) -> str:
    """仅投影包住整个陈述组的闭合包装；内文与数量仍逐一核验。"""
    surface = parse_text_surface(text)
    view = list(text)
    left, right = 0, len(text)
    while True:
        while left < right and text[left].isspace():
            left += 1
        while right > left and (text[right - 1].isspace() or text[right - 1] in "。!?！？;"):
            right -= 1
        node = next(
            (
                n
                for n in surface.nodes
                if n.kind in {"quote", "bracket", "inline_code"}
                and n.span == (left, right)
                and n.closed
            ),
            None,
        )
        if node is None:
            break
        for a, b in node.marker_spans:
            view[a:b] = " " * (b - a)
        left, right = node.content_span
    return "".join(view)


def statement_projections(
    text: str,
    measurement_labels: set[str],
    *,
    source_labels: set[str],
    profile_fields: set[str],
    dimensions: set[str],
) -> list[StatementProjection]:
    """标题、内联、表格与分句使用唯一组合和源位置映射，不按同值回查。"""
    from apps.orchestrator.control.profile_claims import (
        profile_field_header,
        profile_heading_sections,
        profile_statistic_role,
    )
    from apps.orchestrator.control.profile_population import stated_population

    output = []
    section_offset = 0
    for section, heading_context in profile_heading_sections(text, profile_fields):
        normalized_section, inline_map = inline_numeric_projection(source_wrapper_view(section))
        original_projection = SourceProjection(
            normalized_section, tuple(section_offset + position for position in inline_map)
        )
        for fragment in table_claim_fragments(
            normalized_section,
            dimensions,
            measurement_labels=measurement_labels,
            source_labels=source_labels - measurement_labels,
        ):
            scope = stated_population(fragment.text, profile_fields)
            parts = (
                [fragment.text]
                if fragment.cells is not None
                else split_claim_statements(
                    fragment.text,
                    measurement_labels,
                    source_labels=source_labels,
                    protected_spans=(scope.span,) if scope else (),
                )
            )
            table_context = None
            if (
                fragment.cells is not None
                and fragment.value_span is not None
                and fragment.header_span
            ):
                fields = [
                    (value, span)
                    for cell_header, value, _header_span, span in fragment.cell_sources
                    if profile_field_header(cell_header)
                ]
                header = normalized_section[slice(*fragment.header_span)]
                if (
                    len(fields) == 1
                    and fields[0][0] in profile_fields
                    and profile_statistic_role(_plain(header)) is not None
                ):
                    field, field_span = fields[0]

                    positions = fragment.source_map[slice(*fragment.value_span)]
                    assert positions and positions[0] is not None and positions[-1] is not None
                    table_context = ProfileTableContext(
                        field,
                        _plain(header),
                        original_projection.source_span(*field_span),
                        original_projection.source_span(*fragment.header_span),
                        original_projection.source_span(positions[0], positions[-1] + 1),
                    )
            cursor = 0
            for statement in parts:
                start = cursor
                if fragment.text[start : start + len(statement)] != statement:
                    raise ValueError("statement_source_projection_lost")
                cursor += len(statement)
                source_map = tuple(
                    section_offset + inline_map[position] if position is not None else None
                    for position in fragment.source_map[start:cursor]
                )
                output.append(
                    StatementProjection(
                        statement,
                        source_map,
                        fragment.cells,
                        fragment.value_span,
                        heading_context,
                        table_context,
                        fragment.source_context,
                        fragment.literal_label,
                        fragment.constraint_context,
                    )
                )
        section_offset += len(section)
    return output


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
    for match in re.finditer(r"[。！？!?；;\n]+|—+", "".join(boundary_view)):
        if match[0].startswith("—") and not any(
            re.match(r"\s*" + re.escape(field), text[match.end() :])
            for field in (source_labels or set()) - measurement_labels
        ):
            # A dash introduces an independent field statement only when its
            # right side actually names a source field. A following 该列 still
            # needs the preceding explicit field/unit description, not a new
            # owner invented from the nearest number.
            continue
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
    text: str,
    dimensions: set[str] | None = None,
    *,
    measurement_labels: set[str] | None = None,
    source_labels: set[str] | None = None,
) -> list[ClaimFragment]:
    """保留单元格结构供身份校验；展开文本仅用于数字提取及可读审计。"""
    surface = parse_text_surface(text)
    output: list[ClaimFragment] = []
    tables = sorted({node.table for node in surface.nodes if node.table is not None})
    cursor = 0
    for table in tables:
        assert table is not None
        headers = sorted(
            (node for node in surface.nodes if node.table == table and node.kind == "table_header"),
            key=lambda node: node.column or 0,
        )
        rows = sorted(
            {
                node.row
                for node in surface.nodes
                if node.table == table and node.kind == "table_cell" and node.row is not None
            }
        )
        if not rows:
            continue
        if cursor < table:
            output.append(ClaimFragment(text[cursor:table], source_map=tuple(range(cursor, table))))
        header_values = [_plain(text[slice(*node.content_span)]) for node in headers]
        for header in headers:
            output.extend(_header_fragments(text, header.content_span, source_labels or set()))
        last_end = table
        for row in rows:
            cells = sorted(
                (
                    node
                    for node in surface.nodes
                    if node.table == table and node.row == row and node.kind == "table_cell"
                ),
                key=lambda node: node.column or 0,
            )
            row_start = text.rfind("\n", 0, cells[0].span[0]) + 1
            newline = text.find("\n", cells[-1].span[1])
            row_end = newline + 1 if newline >= 0 else len(text)
            last_end = row_end
            if len(cells) != len(headers) or not all(node.closed for node in cells):
                output.append(
                    ClaimFragment(
                        text[row_start:row_end], (), source_map=tuple(range(row_start, row_end))
                    )
                )
                continue
            values = [text[slice(*node.content_span)] for node in cells]
            dimension_columns = {
                i for i, title in enumerate(header_values) if title in (dimensions or set())
            }
            for column, cell in enumerate(values):
                statistic_cell = measurement_labels is not None and bool(
                    statistic_value_slots(header_values[column] + "=", measurement_labels, set())
                )
                if column in dimension_columns or not (has_numeric_symbol(cell) or statistic_cell):
                    continue
                labels = [
                    f"{header_values[i]}={_plain(value)}"
                    for i, value in enumerate(values)
                    if i != column and (i in dimension_columns or not _numeric_cell(value))
                ]
                prefix = " ".join([*labels, f"{header_values[column]}="])
                output.append(
                    ClaimFragment(
                        prefix + cell,
                        tuple(
                            (header_values[i], _plain(value))
                            for i, value in enumerate(values)
                            if i != column
                        ),
                        (len(prefix), len(prefix) + len(cell)),
                        tuple([None] * len(prefix) + list(range(*cells[column].content_span))),
                        tuple(
                            (
                                header_values[i],
                                _plain(value),
                                headers[i].content_span,
                                cells[i].content_span,
                            )
                            for i, value in enumerate(values)
                            if i != column
                        ),
                        headers[column].content_span,
                    )
                )
        cursor = last_end
    if cursor < len(text):
        output.append(ClaimFragment(text[cursor:], source_map=tuple(range(cursor, len(text)))))
    return output


def _header_fragments(
    text: str, span: tuple[int, int], source_labels: set[str]
) -> list[ClaimFragment]:
    """表头原文逐片核验；独立括号事实保留完整父头的身份/范围语境。"""
    from apps.orchestrator.control.dataset_identity import identity_declarations

    start, end = span
    raw = text[start:end]
    protected = [
        match.span() for label in source_labels for match in re.finditer(re.escape(label), raw)
    ]
    groups: list[tuple[int, int, int, int]] = []
    for node in parse_text_surface(raw).nodes:
        if node.kind != "bracket" or not node.closed:
            continue
        if any(a <= node.span[0] < node.span[1] <= b for a, b in protected):
            continue
        body = raw[slice(*node.content_span)]
        # A wrapper containing an independently named source/identity starts
        # its own statement. A bare value wrapper remains with its predicate.
        if not (
            any(re.match(r"\s*" + re.escape(label), body) for label in source_labels)
            or any(identity_declarations(body, headers_only=True))
        ):
            continue
        if any(a <= node.span[0] < node.span[1] <= b for a, b, _, _ in groups):
            continue
        groups.append((*node.span, *node.content_span))
    pieces: list[tuple[int, int]] = []
    constraints = list(raw)
    cursor = 0
    for left, right, body_left, body_right in sorted(groups):
        if cursor < left:
            pieces.append((cursor, left))
        pieces.append((body_left, body_right))
        # Children retain their own source audit. The parent constraint view
        # must not reinterpret a child's statistic predicate as a filter.
        constraints[left:right] = " " * (right - left)
        cursor = right
    if cursor < len(raw):
        pieces.append((cursor, len(raw)))
    return [
        ClaimFragment(
            raw[left:right],
            source_map=tuple(range(start + left, start + right)),
            source_context=raw,
            literal_label=source_wrapper_view(raw[left:right]).strip() in source_labels,
            constraint_context="".join(constraints),
        )
        for left, right in pieces
        if raw[left:right].strip()
    ]


def _cells(line: str) -> list[str]:
    return [cell for cell, _ in _cell_parts(line)]


def _cell_parts(line: str) -> list[tuple[str, int]]:
    """兼容入口，仅转发共同源结构的单元格扫描。"""
    return list(table_cell_parts(line))


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
    removed = {
        position
        for node in parse_text_surface(text).nodes
        if node.kind == "bold"
        for left, right in node.marker_spans
        for position in range(left, right)
    }
    offsets = tuple(position for position in range(len(text)) if position not in removed)
    return "".join(text[position] for position in offsets), offsets


def mask_fenced_code(text: str) -> str:
    """按配对围栏屏蔽代码内容，保留位置；代码里的引号不能影响外层作用域。"""
    view = list(text)
    for node in parse_text_surface(text).nodes:
        if node.kind == "fence":
            left, right = node.span
            view[left:right] = [char if char in "\r\n" else " " for char in text[left:right]]
    return "".join(view)
