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
# 相关性门槛默认值：重排分数 ≤ 该值视为不相关，如实告知无结果（红线6）。
# 真实阈值走配置 RAG_MIN_RELEVANCE，按 reranker 分数分布标定（见验收基线文档）。
_MIN_RELEVANCE = 0.0


@dataclass
class RetrievalResult:
    """检索结果：命中片段（含 source）+ 是否为空。"""

    hits: list[SearchHit]
    is_empty: bool


class HybridRetriever:
    """稠密 + 稀疏双路混合检索，RRF 融合后再过 reranker。

    稀疏路（决策1）：embedder 与 store 都支持 bge-m3 稀疏时走 lexical weights
    检索；否则回退自实现中文 BM25 备路（替身链路不受影响）。
    """

    def __init__(
        self,
        embedder: Embedder,
        store: KnowledgeStore,
        reranker: Reranker,
        *,
        min_relevance: float = _MIN_RELEVANCE,
    ) -> None:
        self.embedder = embedder
        self.store = store
        self._reranker = reranker
        self._min_relevance = min_relevance

    def retrieve(self, query: str, top_k: int = 5) -> RetrievalResult:
        """混合检索并重排，返回带来源的片段。

        无命中时 `is_empty=True`，上层须如实告知"知识库无相关内容"。
        重排分数写回命中项 score，供阈值标定与观测。
        """
        if self.store.count() == 0:
            return RetrievalResult(hits=[], is_empty=True)

        sparse_hits: list[SearchHit] = []
        if self.store.supports_sparse:
            query_vecs, query_sparse = self.embedder.embed_with_sparse([query])
            query_vec = query_vecs[0]
            if query_sparse is not None:
                sparse_hits = self.store.sparse_search(query_sparse[0], _RECALL_K)
        else:
            query_vec = self.embedder.embed([query])[0]
        if not sparse_hits:
            sparse_hits = self.store.bm25_search(tokenize(query), _RECALL_K)
        vec_hits = self.store.vector_search(query_vec, _RECALL_K)

        fused = self._rrf_fuse(vec_hits, sparse_hits)
        if not fused:
            return RetrievalResult(hits=[], is_empty=True)

        ranked = self._reranker.rerank(query, [h.text for h in fused], top_k=top_k)
        # 相关性门槛按 top1 判定"知识库是否有相关内容"（红线6：无结果如实告知）。
        # 不逐条过滤：一旦确认相关，重排序列即排序依据——次位命中分数天然偏低，
        # 逐条绝对分截断会误伤真实相关的次位结果（对任何语料成立，非个例调参）。
        if not ranked or ranked[0][1] <= self._min_relevance:
            return RetrievalResult(hits=[], is_empty=True)
        hits = []
        for idx, score in ranked:
            hit = fused[idx]
            hit.score = score  # 重排分数是最终排序依据，回写供观测/标定
            hits.append(hit)
        return RetrievalResult(hits=hits, is_empty=False)

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
