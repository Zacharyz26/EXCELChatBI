"""混合检索：向量（bge/hashing）+ 稀疏（中文 BM25）RRF 融合 → reranker 重排。

知识库回答必带引用来源；检索无结果时如实告知，不编造（红线6）。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.rag.embedding import Embedder
from packages.rag.rerank import Reranker
from packages.rag.store import KnowledgeStore, SearchHit
from packages.rag.tokenizer import tokenize

# 每路召回数量与 RRF 常数（检索参数，非模型名）。
_RECALL_K = 20
_RRF_K = 60
# 相关性门槛：重排分数 ≤ 该值视为不相关。低于门槛则如实告知无结果（红线6）。
# 切到真实 bge-reranker 时应改为按其分数分布配置的阈值。
_MIN_RELEVANCE = 0.0


@dataclass
class RetrievalResult:
    """检索结果：命中片段（含 source）+ 是否为空。"""

    hits: list[SearchHit]
    is_empty: bool


class HybridRetriever:
    """向量 + BM25 混合检索，RRF 融合后再过 reranker。"""

    def __init__(
        self, embedder: Embedder, store: KnowledgeStore, reranker: Reranker
    ) -> None:
        self._embedder = embedder
        self._store = store
        self._reranker = reranker

    def retrieve(self, query: str, top_k: int = 5) -> RetrievalResult:
        """混合检索并重排，返回带来源的片段。

        无命中时 `is_empty=True`，上层须如实告知"知识库无相关内容"。
        两路（向量 + 中文 BM25）都参与召回，RRF 融合后交给 reranker。
        """
        if self._store.count() == 0:
            return RetrievalResult(hits=[], is_empty=True)

        query_vec = self._embedder.embed([query])[0]
        vec_hits = self._store.vector_search(query_vec, _RECALL_K)
        bm25_hits = self._store.bm25_search(tokenize(query), _RECALL_K)

        fused = self._rrf_fuse(vec_hits, bm25_hits)
        if not fused:
            return RetrievalResult(hits=[], is_empty=True)

        ranked = self._reranker.rerank(query, [h.text for h in fused], top_k=top_k)
        # 相关性门槛：过滤掉低于门槛的候选，避免"检索无结果却硬凑答案"（红线6）
        hits = [fused[idx] for idx, score in ranked if score > _MIN_RELEVANCE]
        return RetrievalResult(hits=hits, is_empty=len(hits) == 0)

    @staticmethod
    def _rrf_fuse(
        vec_hits: list[SearchHit], bm25_hits: list[SearchHit]
    ) -> list[SearchHit]:
        """Reciprocal Rank Fusion：按各路排名倒数加权融合，去重。"""
        scores: dict[str, float] = {}
        by_id: dict[str, SearchHit] = {}
        for ranking in (vec_hits, bm25_hits):
            for rank, hit in enumerate(ranking):
                scores[hit.chunk_id] = scores.get(hit.chunk_id, 0.0) + 1.0 / (_RRF_K + rank + 1)
                by_id.setdefault(hit.chunk_id, hit)
        ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [by_id[cid] for cid, _ in ordered]
