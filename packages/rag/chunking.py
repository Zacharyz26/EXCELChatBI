"""文档分块：按标题层级 + 滑窗语义分块（设计文档 6.3 离线流程）。

中文优先：按字符长度滑窗（中文无空格，不能按词数）。Markdown 标题作为段边界与
section 标注，保留 source 供引用（红线6）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# 分块参数（字符数）。中文按字符切；场景无关，可后续调参。
_CHUNK_SIZE = 300
_CHUNK_OVERLAP = 60


@dataclass
class Chunk:
    """文档切片。"""

    text: str
    source: str           # 引用来源（红线6：问答必带引用）
    section: str | None = None


def split_document(text: str, source: str) -> list[Chunk]:
    """将文档切分为带来源标注的 chunk 列表。

    先按 Markdown 标题分段（段落归属最近的标题），段内再按字符滑窗切分。

    Args:
        text: 文档全文。
        source: 文档来源标识，写入每个 chunk 供引用。
    """
    chunks: list[Chunk] = []
    for section, body in _split_sections(text):
        for piece in _sliding_window(body):
            chunks.append(Chunk(text=piece, source=source, section=section))
    return chunks


def _split_sections(text: str) -> list[tuple[str | None, str]]:
    """按标题把全文分为 (section标题, 段落正文) 列表。"""
    sections: list[tuple[str | None, list[str]]] = [(None, [])]
    for line in text.splitlines():
        m = _HEADING.match(line.strip())
        if m:
            sections.append((m.group(2).strip(), []))
        else:
            sections[-1][1].append(line)
    result: list[tuple[str | None, str]] = []
    for section, lines in sections:
        body = "\n".join(lines).strip()
        if body:
            result.append((section, body))
    return result


def _sliding_window(body: str) -> list[str]:
    """段内按字符长度滑窗切分（带重叠）。"""
    body = body.strip()
    if len(body) <= _CHUNK_SIZE:
        return [body] if body else []
    pieces: list[str] = []
    start = 0
    step = _CHUNK_SIZE - _CHUNK_OVERLAP
    while start < len(body):
        piece = body[start : start + _CHUNK_SIZE].strip()
        if piece:
            pieces.append(piece)
        start += step
    return pieces
