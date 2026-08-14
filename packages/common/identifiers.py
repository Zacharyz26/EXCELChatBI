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
DATASET_REF_ARGUMENT_KEYS = (
    "dataset_ref",
    "left_dataset_ref",
    "right_dataset_ref",
)


class InvalidDatasetRefError(ValueError):
    """数据集引用不是服务端生成的 32 位小写十六进制标识符。"""


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
