"""知识库文档版本、增量同步、删除和原子落盘测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.common.config import Settings
from packages.rag.embedding import HashingEmbedder
from packages.rag.lifecycle import SourceDocument, load_text_documents, sync_documents
from packages.rag.rerank import LexicalReranker
from packages.rag.retriever import HybridRetriever
from packages.rag.store import LocalKnowledgeStore
from pydantic import ValidationError


def test_incremental_update_delete_and_reconnect(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    store = LocalKnowledgeStore(str(index_dir))
    embedder = HashingEmbedder(dim=32)

    first = sync_documents(
        [SourceDocument("metrics/a.md", "# 活跃用户\n有效登录的去重用户数。")],
        embedder,
        store,
    )
    assert first.created == ["metrics/a.md"]
    assert first.updated == []
    document = store.documents()[0]
    assert document.version == 1
    assert document.content_hash
    assert document.updated_at

    unchanged = sync_documents(
        [SourceDocument("metrics/a.md", "# 活跃用户\n有效登录的去重用户数。")],
        embedder,
        store,
    )
    assert unchanged.chunks == 0
    assert unchanged.skipped == ["metrics/a.md"]
    assert store.count() == first.total_chunks

    changed = sync_documents(
        [SourceDocument("metrics/a.md", "# 活跃用户\n最近 30 天有效登录的去重用户数。")],
        embedder,
        store,
    )
    assert changed.updated == ["metrics/a.md"]
    assert store.documents()[0].version == 2

    reconnected = LocalKnowledgeStore(str(index_dir))
    persisted = reconnected.documents()[0]
    assert persisted.version == 2
    assert persisted.source == "metrics/a.md"
    assert reconnected.delete_document(persisted.document_id) > 0
    assert reconnected.documents() == []


def test_full_rebuild_removes_absent_sources(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(str(tmp_path / "index"))
    embedder = HashingEmbedder(dim=16)
    sync_documents(
        [SourceDocument("a.md", "A 文档"), SourceDocument("b.md", "B 文档")],
        embedder,
        store,
    )

    result = sync_documents(
        [SourceDocument("b.md", "B 文档")], embedder, store, full=True
    )

    assert result.deleted == ["a.md"]
    assert result.skipped == ["b.md"]
    assert [item.source for item in store.documents()] == ["b.md"]


def test_updated_and_deleted_content_is_not_retrievable(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(str(tmp_path / "index"))
    embedder = HashingEmbedder(dim=64)
    retriever = HybridRetriever(embedder, store, LexicalReranker())
    sync_documents(
        [SourceDocument("policy.md", "# 旧口径\n北极星指标按旧规则计算。")],
        embedder,
        store,
    )
    sync_documents(
        [SourceDocument("policy.md", "# 新口径\n北极星指标按新规则计算。")],
        embedder,
        store,
    )

    updated = retriever.retrieve("北极星指标规则", top_k=5)
    assert any("新规则" in hit.text for hit in updated.hits)
    assert all("旧规则" not in hit.text for hit in updated.hits)

    document_id = store.documents()[0].document_id
    store.delete_document(document_id)
    deleted = retriever.retrieve("北极星指标规则", top_k=5)
    assert deleted.is_empty
    assert deleted.diagnostics.rejection_reason == "empty_store"


def test_embedding_failure_keeps_existing_index(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(str(tmp_path / "index"))
    embedder = HashingEmbedder(dim=16)
    sync_documents([SourceDocument("a.md", "原始内容")], embedder, store)
    before = store.count()

    class BrokenEmbedder(HashingEmbedder):
        def embed_with_sparse(
            self, texts: list[str]
        ) -> tuple[list[list[float]], list[dict[str, float]] | None]:
            raise RuntimeError("embedding failed")

    with pytest.raises(RuntimeError, match="embedding failed"):
        sync_documents(
            [SourceDocument("a.md", "新内容")], BrokenEmbedder(dim=16), store, full=True
        )
    assert store.count() == before
    assert store.documents()[0].version == 1


def test_document_loader_preserves_relative_source_and_limits(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "metric.md").write_text("指标口径", encoding="utf-8")
    documents = load_text_documents(
        tmp_path, source_root=tmp_path, max_files=1, max_document_chars=100
    )
    assert documents == [SourceDocument("nested/metric.md", "指标口径")]

    with pytest.raises(ValueError, match="字符上限"):
        load_text_documents(
            tmp_path, source_root=tmp_path, max_files=1, max_document_chars=2
        )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rag_embedder": "bge", "rag_store": "local"}, "必须与"),
        ({"rag_embedder": "hashing", "rag_store": "milvus"}, "必须与"),
        ({"embedding_device": "tpu"}, "embedding_device"),
        ({"rag_min_relevance": 1.5}, "less than or equal"),
        ({"milvus_collection": "bad-name"}, "milvus_collection"),
    ],
)
def test_invalid_rag_configuration_fails_fast(
    kwargs: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **kwargs)
