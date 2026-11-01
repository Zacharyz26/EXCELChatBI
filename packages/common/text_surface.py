"""有限源文本结构和可核验位置投影，不判断业务事实或 Evidence 真值。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache

from packages.common.text_delimiters import quote_token_at

SURFACE_PARSER_VERSION = "text-surface-v2"
SourceSpan = tuple[int, int]


@dataclass(frozen=True, slots=True)
class SourceProjection:
    """每个可见字符的位置来自原源；生成连接符必须显式标为 None。"""

    text: str
    source_map: tuple[int | None, ...]

    def __post_init__(self) -> None:
        if len(self.text) != len(self.source_map):
            raise ValueError("invalid_source_projection_length")

    def source_span(self, left: int, right: int) -> SourceSpan:
        """只把连续真实原源字符归为一个事实跨度，不按值反向搜索。"""
        if not 0 <= left < right <= len(self.text):
            raise ValueError("invalid_projection_span")
        positions = self.source_map[left:right]
        if any(value is None for value in positions):
            raise ValueError("synthetic_quantity_source")
        first = positions[0]
        assert first is not None
        if tuple(range(first, first + len(positions))) != positions:
            raise ValueError("noncontiguous_quantity_source")
        return first, first + len(positions)


@dataclass(frozen=True, slots=True)
class SurfaceNode:
    """原源语法节点；数量用途和字段语义由上层绑定，节点不提供业务真值。"""

    kind: str
    span: SourceSpan
    content_span: SourceSpan
    parent: int | None = None
    closed: bool = True
    marker_spans: tuple[SourceSpan, ...] = ()
    level: int | None = None
    row: int | None = None
    column: int | None = None
    table: int | None = None


@dataclass(frozen=True, slots=True)
class TextSurface:
    """原文、纯语法和 parser 版本；保存原字符，不做全文 Unicode 归一化。"""

    source_text: str
    source_hash: str
    parser_version: str
    nodes: tuple[SurfaceNode, ...]
    issues: tuple[str, ...]

    @property
    def quote_spans(self) -> tuple[tuple[int, int, int, int], ...]:
        return tuple(
            (node.span[0], node.content_span[0], node.content_span[1], node.span[1])
            for node in self.nodes
            if node.kind in {"quote", "inline_code"} and node.closed
        )

    def marked_view(self, kinds: set[str]) -> str:
        """仅遮结构标记，正文及所有数值字符保留原位置。"""
        view = list(self.source_text)
        for node in self.nodes:
            if node.kind in kinds:
                for left, right in node.marker_spans:
                    view[left:right] = " " * (right - left)
        return "".join(view)


@lru_cache(maxsize=256)
def quote_nodes(text: str) -> tuple[SurfaceNode, ...]:
    """按实际 Markdown 的等宽 code grammar 先界定内联源，再解析普通引号。

    不匹配的反引号宽度是 code 正文，不能伪造闭合/新标签。表达式对应
    已安装 Markdown BacktickInlineProcessor，独立对照测试验证源跨度。
    这里不引入可选渲染依赖，且 code 内容仍须事实审计。
    """
    code_pattern = re.compile(
        r"(?:(?<!\\)((?:\\{2})+)(?=`+)|(?<!\\)(`+)(.+?)(?<!`)\2(?!`))", re.DOTALL
    )
    result = [
        SurfaceNode(
            "inline_code",
            match.span(),
            match.span(3),
            marker_spans=((match.start(2), match.start(3)), (match.end(3), match.end())),
        )
        for match in code_pattern.finditer(text)
        if match.group(3) is not None
    ]
    code_spans = [node.span for node in result]
    pairs = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"', "'": "'"}
    stack: list[tuple[str, int]] = []
    for position in range(len(text)):
        if any(left <= position < right for left, right in code_spans):
            continue
        char = quote_token_at(text, position)
        if char is None or char == "`":
            continue
        if stack and char == stack[-1][0]:
            _, start = stack.pop()
            result.append(
                SurfaceNode(
                    "quote",
                    (start, position + 1),
                    (start + 1, position),
                    marker_spans=((start, start + 1), (position, position + 1)),
                )
            )
        elif char in pairs:
            stack.append((pairs[char], position))
    for _closing, start in stack:
        result.append(
            SurfaceNode(
                "quote",
                (start, len(text)),
                (start + 1, len(text)),
                closed=False,
                marker_spans=((start, start + 1),),
            )
        )
    return tuple(sorted(result, key=lambda node: (node.span[1], -node.span[0])))


def table_cell_parts(line: str) -> tuple[tuple[str, int], ...]:
    """表格单元格与精确起点共用 delimiter 扫描，转义管道不分格。"""
    if "|" not in line:
        return ()
    start, end = len(line) - len(line.lstrip()), len(line.rstrip())
    if line[start:end].startswith("|"):
        start += 1
    if line[start:end].endswith("|"):
        end -= 1
    cursor = start
    parts = []
    for boundary in [*re.finditer(r"(?<!\\)\|", line[start:end]), None]:
        stop = start + boundary.start() if boundary else end
        raw = line[cursor:stop]
        left = cursor + len(raw) - len(raw.lstrip())
        parts.append((raw.strip(), left))
        cursor = stop + 1
    return tuple(parts)


@lru_cache(maxsize=128)
def parse_text_surface(text: str) -> TextSurface:
    """一次解析当前支持的块/包装边界；不闭合围栏保留明确结构义务。"""
    nodes: list[SurfaceNode] = list(quote_nodes(text))
    brackets = dict(zip("(（[［{｛【〈《", ")）]］}｝】〉》", strict=True))
    stack: list[tuple[str, int]] = []
    for position, char in enumerate(text):
        if position and text[position - 1] == "\\":
            continue
        if stack and char == stack[-1][0]:
            _, start = stack.pop()
            nodes.append(
                SurfaceNode(
                    "bracket",
                    (start, position + 1),
                    (start + 1, position),
                    marker_spans=((start, start + 1), (position, position + 1)),
                )
            )
        elif char in brackets:
            stack.append((brackets[char], position))
    for _closing, start in stack:
        nodes.append(
            SurfaceNode(
                "bracket",
                (start, len(text)),
                (start + 1, len(text)),
                closed=False,
                marker_spans=((start, start + 1),),
            )
        )
    lines = text.splitlines(keepends=True)
    offsets = []
    offset = 0
    fence: tuple[str, int, int, int] | None = None
    hidden: list[SourceSpan] = []
    for line in lines:
        offsets.append(offset)
        raw = line.rstrip("\r\n")
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", raw)
        if fence is not None:
            if (
                marker
                and marker[1][0] == fence[0]
                and len(marker[1]) >= fence[1]
                and not marker[2].strip()
            ):
                nodes.append(
                    SurfaceNode(
                        "fence",
                        (fence[2], offset + len(line)),
                        (fence[3], offset),
                        marker_spans=((fence[2], fence[3]), (offset, offset + len(line))),
                    )
                )
                hidden.append((fence[2], offset + len(line)))
                fence = None
        elif marker:
            fence = (marker[1][0], len(marker[1]), offset, offset + len(line))
        elif quote := re.match(r"^\s*>(.*)$", raw):
            nodes.append(
                SurfaceNode(
                    "blockquote",
                    (offset, offset + len(line)),
                    (offset + quote.start(1), offset + quote.end(1)),
                    marker_spans=((offset, offset + quote.start(1)),),
                )
            )
            hidden.append((offset, offset + len(line)))
        elif item := re.match(r"^( *)([-+*]|\d+[.)、])\s+(.+?)\s*$", raw):
            nodes.append(
                SurfaceNode(
                    "list_item",
                    (offset, offset + len(line)),
                    (offset + item.start(3), offset + item.end(3)),
                    marker_spans=((offset + item.start(2), offset + item.end(2)),),
                    level=len(item[1]),
                )
            )
        elif heading := re.fullmatch(r" {0,3}(#{1,6})\s+(.+?)(?:\s+#+)?\s*", raw):
            nodes.append(
                SurfaceNode(
                    "heading",
                    (offset, offset + len(line)),
                    (offset + heading.start(2), offset + heading.end(2)),
                    marker_spans=((offset + heading.start(1), offset + heading.end(1)),),
                    level=len(heading[1]),
                )
            )
        elif bold := re.fullmatch(r" {0,3}\*\*([^\n]+)\*\*\s*", raw):
            nodes.append(
                SurfaceNode(
                    "bold_heading",
                    (offset, offset + len(line)),
                    (offset + bold.start(1), offset + bold.end(1)),
                    marker_spans=(
                        (offset + bold.start(1) - 2, offset + bold.start(1)),
                        (offset + bold.end(1), offset + bold.end(1) + 2),
                    ),
                )
            )
        offset += len(line)
    issues = []
    if fence is not None:
        nodes.append(
            SurfaceNode(
                "fence",
                (fence[2], len(text)),
                (fence[3], len(text)),
                closed=False,
                marker_spans=((fence[2], fence[3]),),
            )
        )
        hidden.append((fence[2], len(text)))
        issues.append("structure_incomplete")
    for pattern in (
        r"(?<![\\*])\*\*(?=\S)(.+?)(?<=\S)\*\*(?!\*)",
        r"(?<![\\\w])__(?=\S)(.+?)(?<=\S)__(?!\w)",
    ):
        for match in re.finditer(pattern, text):
            nodes.append(
                SurfaceNode(
                    "bold",
                    match.span(),
                    match.span(1),
                    marker_spans=((match.start(), match.start(1)), (match.end(1), match.end())),
                )
            )
    # Tables retain header and value-cell provenance, including repeated values.
    index = 0
    while index + 1 < len(lines):
        header = table_cell_parts(lines[index].rstrip("\r\n"))
        divider = table_cell_parts(lines[index + 1].rstrip("\r\n"))
        if (
            not header
            or len(header) != len(divider)
            or not all(re.fullmatch(r":?-+:?", value.replace(" ", "")) for value, _ in divider)
            or any(left <= offsets[index] < right for left, right in hidden)
        ):
            index += 1
            continue
        table = offsets[index]
        for column, (value, left) in enumerate(header):
            span = (offsets[index] + left, offsets[index] + left + len(value))
            nodes.append(SurfaceNode("table_header", span, span, row=0, column=column, table=table))
        index += 2
        row = 1
        while index < len(lines) and (cells := table_cell_parts(lines[index].rstrip("\r\n"))):
            if len(cells) != len(header):
                issues.append("malformed_table_row")
            for column, (value, left) in enumerate(cells):
                span = (offsets[index] + left, offsets[index] + left + len(value))
                nodes.append(
                    SurfaceNode(
                        "table_cell",
                        span,
                        span,
                        row=row,
                        column=column,
                        table=table,
                        closed=len(cells) == len(header),
                    )
                )
            row += 1
            index += 1
    return TextSurface(
        text,
        hashlib.sha256(text.encode()).hexdigest(),
        SURFACE_PARSER_VERSION,
        tuple(sorted(nodes, key=lambda node: (node.span[0], -node.span[1], node.kind))),
        tuple(dict.fromkeys(issues)),
    )
