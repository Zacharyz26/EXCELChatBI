"""跨边界使用的不透明资源标识符校验。

数据集引用只能由服务端生成，不能被当作文件名或路径使用。这里保持实现轻量，
供 JSON Schema、存储层和策略网关共同复用。
"""

from __future__ import annotations

import re
from collections.abc import Mapping

DATASET_REF_PATTERN = r"^[0-9a-f]{32}$"
REPORT_ID_PATTERN = r"^[0-9a-f]{32}$"
_DATASET_REF_RE = re.compile(DATASET_REF_PATTERN)
_REPORT_ID_RE = re.compile(REPORT_ID_PATTERN)
# The session resolvers and request compiler consume the same opaque-reference
# language.  The optional prefix and capture group retain the resolver's
# historical bare-reference semantics; only the helper below requires an
# explicit, complete assignment for comparison ownership.
ANALYSIS_REFERENCE_PATTERN = re.compile(
    r"(?:(?:analysis[_ ]?id|dataset[_ ]?ref)\s*[:=]\s*)?"
    r"([0-9a-f]{32}|[0-9a-f]{12})(?![0-9a-f])",
    re.I,
)
MEMORY_REFERENCE_PATTERN = re.compile(
    r"memory[_ ]?id\s*[:=]\s*([0-9a-f]{32})(?![0-9a-f])",
    re.I,
)
DATASET_REF_ARGUMENT_KEYS = (
    "dataset_ref",
    "left_dataset_ref",
    "right_dataset_ref",
)


class InvalidDatasetRefError(ValueError):
    """数据集引用不是服务端生成的 32 位小写十六进制标识符。"""


def explicit_resource_reference_spans(text: str) -> tuple[tuple[int, int], ...]:
    """Return complete explicit resource lexemes, not access authorization.

    A bare hexadecimal ID has no assignment to own.  Prefix substrings such
    as ``fake_analysis_id`` and suffixes such as ``<ID>_extra`` are not complete
    lexemes.  The actual session resolver still checks existence and access.
    """
    spans: set[tuple[int, int]] = set()
    for pattern in (ANALYSIS_REFERENCE_PATTERN, MEMORY_REFERENCE_PATTERN):
        for match in pattern.finditer(text):
            start, end = match.span()
            if match.start(1) == start:
                continue
            if start and re.fullmatch(r"[A-Za-z0-9_]", text[start - 1]):
                continue
            if end < len(text) and re.fullmatch(r"[A-Za-z0-9_]", text[end]):
                continue
            spans.add((start, end))
    return tuple(sorted(spans))


def validate_dataset_ref(value: object) -> str:
    """验证并返回数据集引用；拒绝路径、空白、别名和任意外部字符串。"""
    if not isinstance(value, str) or _DATASET_REF_RE.fullmatch(value) is None:
        raise InvalidDatasetRefError("数据集引用格式非法")
    return value


def dataset_reference_arguments(
    arguments: Mapping[str, object],
) -> tuple[tuple[str, object], ...]:
    """按稳定顺序提取所有受治理数据集参数，供跨边界完整授权。"""
    return tuple(
        (key, arguments[key])
        for key in DATASET_REF_ARGUMENT_KEYS
        if key in arguments
    )


def validate_report_id(value: object) -> str:
    """验证可下载报告的不透明标识符。"""
    if not isinstance(value, str) or _REPORT_ID_RE.fullmatch(value) is None:
        raise ValueError("报告标识符格式非法")
    return value
