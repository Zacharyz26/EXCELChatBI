"""中文分词（jieba）。BM25 稀疏检索必须分词，否则中文召回严重退化。"""

from __future__ import annotations

import jieba

# 最小中文停用词表（高频虚词/标点）。刻意保持精简、场景无关。
_STOPWORDS: frozenset[str] = frozenset(
    {
        "的", "了", "和", "是", "在", "我", "有", "也", "就", "都", "而", "及",
        "与", "或", "一个", "什么", "怎么", "如何", "为", "对", "把", "被", "让",
        "吗", "呢", "啊", "这", "那", "你", "他", "她", "它", "我们", "请",
        " ", "\t", "\n", "，", "。", "、", "？", "！", "：", "；", "（", "）",
        "《", "》", "“", "”", "‘", "’", "-", "—", "…", ",", ".", "?", "!",
    }
)


def tokenize(text: str) -> list[str]:
    """中文分词，用于 BM25 稀疏检索。

    Args:
        text: 原始中文文本。

    Returns:
        分词后的 token 列表（去停用词、去空白、小写归一）。
    """
    tokens: list[str] = []
    for tok in jieba.lcut(text):
        t = tok.strip().lower()
        if not t or t in _STOPWORDS:
            continue
        tokens.append(t)
    return tokens
