"""检索稀疏路与阈值配置测试（纯替身，不依赖 .[rag] 重依赖）。

覆盖：稀疏路优先（决策1）、无稀疏表示时回退中文 BM25、RAG_MIN_RELEVANCE
配置化过滤、摄入管线稀疏表示落 StoredChunk。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.rag.embedding import Embedder, HashingEmbedder, SparseVector  # noqa: E402
from packages.rag.pipeline import chunk_and_embed  # noqa: E402
from packages.rag.rerank import Reranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402
from packages.rag.store import KnowledgeStore, SearchHit, StoredChunk  # noqa: E402


class _SparseEmbedder(Embedder):
    """带稀疏表示的假 embedder（模拟 bge-m3 双路）。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    def embed_with_sparse(
        self, texts: list[str]
    ) -> tuple[list[list[float]], list[SparseVector] | None]:
        return self.embed(texts), [{"7": 0.9} for _ in texts]


class _RecordingStore(KnowledgeStore):
    """记录走了哪条稀疏路的假存储。"""

    def __init__(self, supports_sparse: bool) -> None:
        self.supports_sparse = supports_sparse
        self.sparse_called = False
        self.bm25_called = False
        self._hit = SearchHit(chunk_id="c1", text="留存率定义", source="a.md", score=1.0)

    def add(self, chunks: list[StoredChunk]) -> int:
        return len(chunks)

    def count(self) -> int:
        return 1

    def clear(self) -> None: ...

    def sources(self) -> list[str]:
        return ["a.md"]

    def topics(self) -> list[str]:
        return []

    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        return [self._hit]

    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        self.bm25_called = True
        return [self._hit]

    def sparse_search(self, query_sparse: dict[str, float], top_k: int) -> list[SearchHit]:
        self.sparse_called = True
        assert query_sparse == {"7": 0.9}
        return [self._hit]


class _PassthroughReranker(Reranker):
    def rerank(self, query: str, candidates: list[str], top_k: int = 5) -> list[tuple[int, float]]:
        return [(i, 0.8) for i in range(len(candidates))][:top_k]


def test_sparse_path_preferred_when_both_support() -> None:
    """embedder 与 store 都支持稀疏 → 走 lexical weights 检索，不再调 BM25。"""
    store = _RecordingStore(supports_sparse=True)
    retriever = HybridRetriever(_SparseEmbedder(), store, _PassthroughReranker())

    result = retriever.retrieve("留存率怎么算")

    assert store.sparse_called and not store.bm25_called
    assert not result.is_empty


def test_bm25_fallback_when_embedder_has_no_sparse() -> None:
    """替身 embedder 无稀疏表示 → 回退中文 BM25 备路（替身链路不受影响）。"""
    store = _RecordingStore(supports_sparse=True)
    retriever = HybridRetriever(HashingEmbedder(dim=8), store, _PassthroughReranker())

    retriever.retrieve("留存率怎么算")

    assert store.bm25_called and not store.sparse_called


def test_min_relevance_threshold_is_configurable() -> None:
    """RAG_MIN_RELEVANCE 高于重排分 → 全部过滤并如实返回空（红线6）。"""
    store = _RecordingStore(supports_sparse=False)
    retriever = HybridRetriever(
        HashingEmbedder(dim=8), store, _PassthroughReranker(), min_relevance=0.9
    )

    result = retriever.retrieve("留存率怎么算")

    assert result.is_empty and result.hits == []


def test_threshold_gates_on_top1_not_per_hit() -> None:
    """阈值按 top1 判定整体相关性；过线后保留全部重排结果，不逐条截尾。

    次位相关命中绝对分天然偏低，逐条截断会误伤（验收中真实回归过）。
    """

    class _DecayingReranker(Reranker):
        def rerank(
            self, query: str, candidates: list[str], top_k: int = 5
        ) -> list[tuple[int, float]]:
            scores = [0.9, 0.05, 0.01]  # top1 高分，尾部低于门槛
            return [(i, scores[i]) for i in range(min(len(candidates), 3))][:top_k]

    class _MultiHitStore(_RecordingStore):
        def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
            return [
                SearchHit(chunk_id=f"c{i}", text=f"片段{i}", source="a.md", score=1.0)
                for i in range(3)
            ]

    store = _MultiHitStore(supports_sparse=False)
    retriever = HybridRetriever(
        HashingEmbedder(dim=8), store, _DecayingReranker(), min_relevance=0.02
    )

    result = retriever.retrieve("留存率怎么算")

    assert not result.is_empty
    assert [h.score for h in result.hits] == [0.9, 0.05, 0.01]  # 尾部低分保留


def test_retrieve_writes_rerank_score_back_to_hits() -> None:
    """命中项 score 回写为重排分（阈值标定与观测依据）。"""
    store = _RecordingStore(supports_sparse=False)
    retriever = HybridRetriever(HashingEmbedder(dim=8), store, _PassthroughReranker())

    result = retriever.retrieve("留存率怎么算")

    assert result.hits[0].score == 0.8


def test_pipeline_attaches_sparse_vectors() -> None:
    """摄入管线：bge 型 embedder 的稀疏表示随 StoredChunk 入库；替身为空 dict。"""
    text = "# 留存率\n\n留存率指新增用户第 N 日仍活跃的比例。"
    with_sparse = chunk_and_embed(text, "a.md", _SparseEmbedder())
    assert with_sparse and all(c.sparse == {"7": 0.9} for c in with_sparse)

    without = chunk_and_embed(text, "a.md", HashingEmbedder(dim=8))
    assert without and all(c.sparse == {} for c in without)
