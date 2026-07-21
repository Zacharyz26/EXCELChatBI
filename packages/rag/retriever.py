"""混合检索：向量（bge/hashing）+ 稀疏（中文 BM25）RRF 融合 → reranker 重排。

知识库回答必带引用来源；检索无结果时如实告知，不编造（红线6）。
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field

from packages.common.logging import get_logger
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
_log = get_logger("rag.retriever")


@dataclass(frozen=True)
class RetrievalDiagnostics:
    """一次检索的非敏感诊断信息，供日志、评测和健康排障。"""

    backend: str = ""
    total_ms: float = 0.0
    embedding_ms: float = 0.0
    dense_ms: float = 0.0
    sparse_ms: float = 0.0
    rerank_ms: float = 0.0
    dense_candidates: int = 0
    sparse_candidates: int = 0
    fused_candidates: int = 0
    returned_hits: int = 0
    top_score: float | None = None
    rejection_reason: str | None = None


@dataclass
class RetrievalResult:
    """检索结果：命中片段（含 source）+ 是否为空。"""

    hits: list[SearchHit]
    is_empty: bool
    diagnostics: RetrievalDiagnostics = field(default_factory=RetrievalDiagnostics)


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
        with self.store.retrieval_snapshot():
            return self._retrieve_snapshot(query, top_k)

    def _retrieve_snapshot(self, query: str, top_k: int) -> RetrievalResult:
        """在同一个存储代际中完成计数、双路召回与重排。"""
        started = time.perf_counter()
        embedding_ms = 0.0
        dense_ms = 0.0
        sparse_ms = 0.0
        rerank_ms = 0.0
        if self.store.count() == 0:
            return self._finish(
                query,
                started,
                [],
                dense_candidates=0,
                sparse_candidates=0,
                fused_candidates=0,
                rejection_reason="empty_store",
            )

        sparse_hits: list[SearchHit] = []
        embedding_started = time.perf_counter()
        if self.store.supports_sparse:
            query_vecs, query_sparse = self.embedder.embed_with_sparse([query])
            query_vec = query_vecs[0]
            embedding_ms = _elapsed_ms(embedding_started)
            if query_sparse is not None:
                sparse_started = time.perf_counter()
                sparse_hits = self.store.sparse_search(query_sparse[0], _RECALL_K)
                sparse_ms += _elapsed_ms(sparse_started)
        else:
            query_vec = self.embedder.embed([query])[0]
            embedding_ms = _elapsed_ms(embedding_started)
        if not sparse_hits:
            sparse_started = time.perf_counter()
            sparse_hits = self.store.bm25_search(tokenize(query), _RECALL_K)
            sparse_ms += _elapsed_ms(sparse_started)
        dense_started = time.perf_counter()
        vec_hits = self.store.vector_search(query_vec, _RECALL_K)
        dense_ms = _elapsed_ms(dense_started)

        fused = self._rrf_fuse(vec_hits, sparse_hits)
        if not fused:
            return self._finish(
                query,
                started,
                [],
                embedding_ms=embedding_ms,
                dense_ms=dense_ms,
                sparse_ms=sparse_ms,
                dense_candidates=len(vec_hits),
                sparse_candidates=len(sparse_hits),
                fused_candidates=0,
                rejection_reason="no_candidates",
            )

        rerank_started = time.perf_counter()
        ranked = self._reranker.rerank(query, [h.text for h in fused], top_k=top_k)
        rerank_ms = _elapsed_ms(rerank_started)
        # 相关性门槛按 top1 判定"知识库是否有相关内容"（红线6：无结果如实告知）。
        # 不逐条过滤：一旦确认相关，重排序列即排序依据——次位命中分数天然偏低，
        # 逐条绝对分截断会误伤真实相关的次位结果（对任何语料成立，非个例调参）。
        if not ranked or ranked[0][1] <= self._min_relevance:
            return self._finish(
                query,
                started,
                [],
                embedding_ms=embedding_ms,
                dense_ms=dense_ms,
                sparse_ms=sparse_ms,
                rerank_ms=rerank_ms,
                dense_candidates=len(vec_hits),
                sparse_candidates=len(sparse_hits),
                fused_candidates=len(fused),
                top_score=ranked[0][1] if ranked else None,
                rejection_reason="below_threshold",
            )
        hits = []
        for idx, score in ranked:
            hit = fused[idx]
            hit.score = score  # 重排分数是最终排序依据，回写供观测/标定
            hits.append(hit)
        return self._finish(
            query,
            started,
            hits,
            embedding_ms=embedding_ms,
            dense_ms=dense_ms,
            sparse_ms=sparse_ms,
            rerank_ms=rerank_ms,
            dense_candidates=len(vec_hits),
            sparse_candidates=len(sparse_hits),
            fused_candidates=len(fused),
            top_score=hits[0].score if hits else None,
        )

    def _finish(
        self,
        query: str,
        started: float,
        hits: list[SearchHit],
        *,
        embedding_ms: float = 0.0,
        dense_ms: float = 0.0,
        sparse_ms: float = 0.0,
        rerank_ms: float = 0.0,
        dense_candidates: int,
        sparse_candidates: int,
        fused_candidates: int,
        top_score: float | None = None,
        rejection_reason: str | None = None,
    ) -> RetrievalResult:
        diagnostics = RetrievalDiagnostics(
            backend=(
                f"{type(self.embedder).__name__}/"
                f"{type(self._reranker).__name__}/"
                f"{type(self.store).__name__}"
            ),
            total_ms=_elapsed_ms(started),
            embedding_ms=embedding_ms,
            dense_ms=dense_ms,
            sparse_ms=sparse_ms,
            rerank_ms=rerank_ms,
            dense_candidates=dense_candidates,
            sparse_candidates=sparse_candidates,
            fused_candidates=fused_candidates,
            returned_hits=len(hits),
            top_score=top_score,
            rejection_reason=rejection_reason,
        )
        _log.info(
            "rag.retrieve",
            query_chars=len(query),
            is_empty=not hits,
            **asdict(diagnostics),
        )
        return RetrievalResult(hits=hits, is_empty=not hits, diagnostics=diagnostics)

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


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1_000, 3)
