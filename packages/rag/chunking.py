"""文档分块：按标题层级 + 滑窗语义分块（设计文档 6.3 离线流程）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Chunk:
    """文档切片。"""

    text: str
    source: str           # 引用来源（红线6：问答必带引用）
    section: str | None = None


def split_document(text: str, source: str) -> list[Chunk]:
    """将文档切分为带来源标注的 chunk 列表。

    Args:
        text: 文档全文。
        source: 文档来源标识，写入每个 chunk 供引用。
    """
    raise NotImplementedError("TODO: 标题层级切分 + 滑窗，保留 source")
