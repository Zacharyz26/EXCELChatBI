"""混合检索：向量（bge）+ 稀疏（中文 BM25）融合 → bge-reranker 重排。

知识库回答必带引用来源；检索无结果时如实告知，不编造（红线6）。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.rag.store import SearchHit


@dataclass
class RetrievalResult:
    """检索结果：命中片段（含 source）+ 是否为空。"""

    hits: list[SearchHit]
    is_empty: bool


class HybridRetriever:
    """向量 + BM25 混合检索，再过 reranker。"""

    def retrieve(self, query: str, top_k: int = 5) -> RetrievalResult:
        """混合检索并重排，返回带来源的片段。

        无命中时 `is_empty=True`，上层须如实告知"知识库无相关内容"。
        """
        raise NotImplementedError(
            "TODO: 向量召回 + BM25 召回 → 融合 → reranker → 组装 RetrievalResult"
        )
