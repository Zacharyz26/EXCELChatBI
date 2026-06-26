"""中文分词（jieba）。BM25 稀疏检索必须分词，否则中文召回严重退化。"""

from __future__ import annotations


def tokenize(text: str) -> list[str]:
    """中文分词，用于 BM25 稀疏检索。

    Args:
        text: 原始中文文本。

    Returns:
        分词后的 token 列表。
    """
    raise NotImplementedError("TODO: jieba.lcut(text)，可叠加停用词过滤")
