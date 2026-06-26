"""可观测性：全链路 trace。

记录每次模型调用、工具调用的耗时、token、成本，供治理层观测与成本记账
（设计文档 3.2 / 第9节）。
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator


@contextmanager
def trace_span(name: str, **attrs: object) -> Iterator[None]:
    """为一次模型 / 工具调用开 trace span，记录耗时与属性。

    Args:
        name: span 名称，如 "model.complete" / "tool.parse_excel"。
        attrs: 附加属性（model、tool、tokens、cost 等）。
    """
    raise NotImplementedError("TODO: 计时 + 结构化输出 trace；yield 包裹被测代码")
    yield
