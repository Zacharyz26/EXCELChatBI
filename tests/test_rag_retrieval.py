"""检索测试：召回正确、中文双路（向量 + BM25）都参与、无结果诚实。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.rag.embedding import HashingEmbedder  # noqa: E402
from packages.rag.pipeline import chunk_and_embed  # noqa: E402
from packages.rag.rerank import LexicalReranker  # noqa: E402
from packages.rag.retriever import HybridRetriever  # noqa: E402
from packages.rag.store import LocalKnowledgeStore  # noqa: E402
from packages.rag.tokenizer import tokenize  # noqa: E402

_DOCS = {
    "指标.md": "# 活跃用户\n活跃用户指统计周期内有过有效登录行为的去重用户数。",
    "留存.md": "# 留存率\n留存率指某日新增用户在其后第N日仍活跃的比例。",
    "收入.md": "# 收入口径\n收入指扣除退款后的净收入，按订单完成时间归属。",
}


@pytest.fixture
def store(tmp_path: Path) -> LocalKnowledgeStore:
    s = LocalKnowledgeStore(index_dir=str(tmp_path / "kb"))
    emb = HashingEmbedder(dim=256)
    for src, text in _DOCS.items():
        s.add(chunk_and_embed(text, src, emb))
    return s


def test_recall_top_hit(store: LocalKnowledgeStore) -> None:
    retriever = HybridRetriever(HashingEmbedder(256), store, LexicalReranker())
    res = retriever.retrieve("活跃用户是怎么定义的", top_k=3)
    assert not res.is_empty
    assert res.hits[0].source == "指标.md"
    assert res.diagnostics.backend.endswith("LocalKnowledgeStore")
    assert res.diagnostics.returned_hits == len(res.hits)
    assert res.diagnostics.dense_candidates > 0
    assert res.diagnostics.sparse_candidates > 0
    assert res.diagnostics.total_ms >= 0


def test_both_paths_participate(store: LocalKnowledgeStore) -> None:
    # 中文双路：向量召回与 BM25 召回都应返回结果（验收④）
    emb = HashingEmbedder(256)
    q = "留存率"
    vec_hits = store.vector_search(emb.embed([q])[0], 10)
    bm25_hits = store.bm25_search(tokenize(q), 10)
    assert len(vec_hits) > 0
    assert len(bm25_hits) > 0
    # BM25 对精确中文词命中的最相关文档应是留存
    assert bm25_hits[0].source == "留存.md"


def test_no_result_is_honest(store: LocalKnowledgeStore) -> None:
    retriever = HybridRetriever(HashingEmbedder(256), store, LexicalReranker())
    res = retriever.retrieve("量子纠缠与航天器轨道力学", top_k=3)
    assert res.is_empty
    assert res.hits == []
    assert res.diagnostics.rejection_reason == "below_threshold"
