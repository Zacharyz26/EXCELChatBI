"""MilvusKnowledgeStore 实测（Milvus Lite）。

未安装 .[rag]（pymilvus[milvus_lite]）时整体跳过；安装后在临时目录起内嵌
Lite 实例，验证决策 1/2 的关键点：稀疏向量检索可用、幂等去重、URI 即后端。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

pytest.importorskip("pymilvus")
pytest.importorskip("milvus_lite", reason="需要 pymilvus[milvus_lite]（uv sync --extra rag）")

from packages.rag.store import StoredChunk  # noqa: E402

# 代理环境（如 WSL）会劫持本机 gRPC：Milvus Lite 连接前必须豁免 127.0.0.1
os.environ.setdefault("NO_PROXY", "127.0.0.1,localhost")
os.environ.setdefault("no_proxy", "127.0.0.1,localhost")


def _chunks() -> list[StoredChunk]:
    return [
        StoredChunk(text="活跃用户指有效登录的去重用户数", source="a.md", section="活跃用户",
                    vector=[1.0, 0.0, 0.0, 0.0], sparse={"101": 0.8, "202": 0.5}),
        StoredChunk(text="留存率指新增用户第N日仍活跃比例", source="b.md", section="留存率",
                    vector=[0.0, 1.0, 0.0, 0.0], sparse={"303": 0.9}),
        StoredChunk(text="替身链路无稀疏表示的片段", source="c.md", section=None,
                    vector=[0.0, 0.0, 1.0, 0.0], sparse={}),
    ]


@pytest.fixture
def store(tmp_path: Path) -> Any:
    from packages.rag.milvus_store import MilvusKnowledgeStore

    return MilvusKnowledgeStore(str(tmp_path / "lite.db"))


def test_roundtrip_dedupe_and_scalar_fields(store: Any) -> None:
    assert store.count() == 0
    assert store.add(_chunks()) == 3
    assert store.add(_chunks()) == 0            # 幂等：同 (source, 文本) 不重复入库
    assert store.count() == 3
    assert store.sources() == ["a.md", "b.md", "c.md"]
    assert store.topics() == ["活跃用户", "留存率"]

    store.clear()
    assert store.count() == 0
    assert store.vector_search([1.0, 0.0, 0.0, 0.0], 3) == []


def test_dense_and_sparse_search(store: Any) -> None:
    store.add(_chunks())

    dense = store.vector_search([0.0, 1.0, 0.0, 0.0], 2)
    assert dense and dense[0].section == "留存率"
    assert dense[0].source == "b.md"
    # chunk_id 必须非空且互异：为空会让 RRF 融合按空串折叠成单候选（真实回归过）
    ids = [h.chunk_id for h in dense]
    assert all(ids) and len(set(ids)) == len(ids)

    # 决策1 核心：bge-m3 lexical weights 稀疏检索（Milvus Lite 支持已实测）
    sparse = store.sparse_search({"303": 1.0}, 2)
    assert sparse and sparse[0].section == "留存率"
    both = store.sparse_search({"101": 1.0, "202": 1.0}, 2)
    assert both and both[0].section == "活跃用户"

    # Milvus 后端无 BM25 备路（与 bge 配对使用；替身配对时退化纯稠密）
    assert store.bm25_search(["留存率"], 3) == []


def test_retriever_with_milvus_and_sparse_embedder(tmp_path: Path) -> None:
    """检索层集成：稀疏 embedder + Milvus 存储 → 双路召回、重排、带来源。"""
    from packages.rag.milvus_store import MilvusKnowledgeStore
    from packages.rag.retriever import HybridRetriever

    from tests.test_rag_sparse import _PassthroughReranker, _SparseEmbedder

    store = MilvusKnowledgeStore(str(tmp_path / "lite2.db"))
    embedder = _SparseEmbedder()
    chunk = StoredChunk(
        text="转化率指完成目标行为的比例", source="d.md", section="转化率",
        vector=embedder.embed(["x"])[0], sparse={"7": 0.9},
    )
    store.add([chunk])

    result = HybridRetriever(embedder, store, _PassthroughReranker()).retrieve("转化率")

    assert not result.is_empty
    assert result.hits[0].source == "d.md"
    assert result.hits[0].section == "转化率"
