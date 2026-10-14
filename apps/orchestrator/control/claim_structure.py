"""保留 Markdown 表格的行列关系，不把单元格拆成无语义数字。"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ClaimFragment:
    """一个数值陈述及其原始表格标签单元格，标签顺序和重复值均保留。"""

    text: str
    cells: tuple[tuple[str, str], ...] | None = None
    value_span: tuple[int, int] | None = None


def table_claim_text(text: str, dimensions: set[str] | None = None) -> str:
    """把每个表格值绑定到自身列标题及同一行的标签，畸形表格保持原样以拒绝。"""
    return "\n".join(fragment.text for fragment in table_claim_fragments(text, dimensions))


def table_claim_fragments(text: str, dimensions: set[str] | None = None) -> list[ClaimFragment]:
    """保留单元格结构供身份校验；展开文本仅用于数字提取及可读审计。"""
    lines = text.splitlines()
    output: list[ClaimFragment] = []
    index = 0
    while index < len(lines):
        header = [_plain(cell) for cell in _cells(lines[index])]
        divider = _cells(lines[index + 1]) if index + 1 < len(lines) else []
        if (
            not header
            or len(header) != len(divider)
            or not all(re.fullmatch(r":?-+:?", cell.replace(" ", "")) for cell in divider)
        ):
            output.append(ClaimFragment(lines[index]))
            index += 1
            continue
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
                    if column in dimension_columns or not re.search(r"\d", cell):
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
    return output


def _cells(line: str) -> list[str]:
    if "|" not in line:
        return []
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]


def _plain(cell: str) -> str:
    return cell.strip().strip("*`_").strip()


def _numeric_cell(cell: str) -> bool:
    return (
        re.fullmatch(r"[-+]?(?:\d[\d,]*(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?%?", _plain(cell))
        is not None
    )


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
