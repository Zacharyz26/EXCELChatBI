"""知识库文档版本、增量同步、删除和原子落盘测试。"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.common.config import Settings
from packages.rag.embedding import HashingEmbedder
from packages.rag.lifecycle import SourceDocument, load_text_documents, sync_documents
from packages.rag.rerank import LexicalReranker
from packages.rag.retriever import HybridRetriever
from packages.rag.source_store import SourceDocumentStore
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

    result = sync_documents([SourceDocument("b.md", "B 文档")], embedder, store, full=True)

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
        sync_documents([SourceDocument("a.md", "新内容")], BrokenEmbedder(dim=16), store, full=True)
    assert store.count() == before
    assert store.documents()[0].version == 1


def test_source_store_is_atomic_fact_source_and_detects_tampering(
    tmp_path: Path,
) -> None:
    source_store = SourceDocumentStore(str(tmp_path / "sources"))
    first = source_store.publish(
        [SourceDocument("a.md", "A1"), SourceDocument("nested/b.md", "B1")],
        full=True,
    )
    assert first.document_count == 2

    second = source_store.publish([SourceDocument("a.md", "A2")])
    assert second.generation != first.generation
    assert source_store.documents() == [
        SourceDocument("a.md", "A2"),
        SourceDocument("nested/b.md", "B1"),
    ]

    manifest = (tmp_path / "sources" / "manifest.json").read_text(encoding="utf-8")
    assert "A2" not in manifest
    blob = next((tmp_path / "sources" / "documents").glob("*.txt"))
    blob.write_text("tampered", encoding="utf-8")
    with pytest.raises(RuntimeError, match="校验失败"):
        source_store.documents()


def test_published_empty_source_generation_does_not_reimport_seed(tmp_path: Path) -> None:
    from apps.api.routers.kb import _collect_rebuild_docs
    from apps.api.schemas import RebuildRequest

    seed_dir = tmp_path / "seed"
    seed_dir.mkdir()
    (seed_dir / "seed.md").write_text("不应复活", encoding="utf-8")
    source_store = SourceDocumentStore(str(tmp_path / "sources"))
    cleared = source_store.publish([], full=True)
    settings = Settings(
        _env_file=None,
        kb_docs_dir=str(seed_dir),
        kb_source_dir=str(tmp_path / "sources"),
    )

    assert cleared.generation != "empty"
    assert _collect_rebuild_docs(RebuildRequest(), settings, source_store) == []


def test_local_index_generation_switch_rollback_and_reopen(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    store = LocalKnowledgeStore(str(index_dir))
    embedder = HashingEmbedder(dim=32)
    sync_documents([SourceDocument("metric.md", "第一代口径")], embedder, store)
    first = store.status().active_collection

    sync_documents([SourceDocument("metric.md", "第二代口径")], embedder, store)
    second = store.status()
    assert second.active_collection != first
    assert second.previous_collection == first
    assert len(second.generations) == 2

    rolled_back = store.rollback()
    assert rolled_back.active_collection == first
    assert rolled_back.previous_collection == second.active_collection
    assert store.documents()[0].version == 1

    reopened = LocalKnowledgeStore(str(index_dir))
    assert reopened.status().active_collection == first
    assert reopened.documents()[0].version == 1


def test_local_reader_follows_generation_switched_by_another_process(
    tmp_path: Path,
) -> None:
    index_dir = tmp_path / "index"
    writer = LocalKnowledgeStore(str(index_dir))
    embedder = HashingEmbedder(dim=16)
    sync_documents([SourceDocument("metric.md", "旧代")], embedder, writer)
    reader = LocalKnowledgeStore(str(index_dir))

    sync_documents([SourceDocument("metric.md", "新代")], embedder, writer)

    assert reader.documents()[0].version == 2
    assert reader.status().active_collection == writer.status().active_collection


def test_local_generation_cleanup_keeps_active_and_previous(tmp_path: Path) -> None:
    store = LocalKnowledgeStore(str(tmp_path / "index"))
    embedder = HashingEmbedder(dim=16)
    for version in range(4):
        sync_documents([SourceDocument("metric.md", f"口径版本 {version}")], embedder, store)
    assert len(store.status().generations) == 4
    assert store.cleanup_generations(retain=2) == 2
    status = store.status()
    assert set(status.generations) == {
        status.active_collection,
        status.previous_collection,
    }
    with pytest.raises(ValueError, match="至少为 2"):
        store.cleanup_generations(retain=1)


def test_document_loader_preserves_relative_source_and_limits(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    (nested / "metric.md").write_text("指标口径", encoding="utf-8")
    documents = load_text_documents(
        tmp_path, source_root=tmp_path, max_files=1, max_document_chars=100
    )
    assert documents == [SourceDocument("nested/metric.md", "指标口径")]

    with pytest.raises(ValueError, match="字符上限"):
        load_text_documents(tmp_path, source_root=tmp_path, max_files=1, max_document_chars=2)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rag_embedder": "bge", "rag_store": "local"}, "必须与"),
        ({"rag_embedder": "hashing", "rag_store": "milvus"}, "必须与"),
        ({"embedding_device": "tpu"}, "embedding_device"),
        ({"rag_min_relevance": 1.5}, "less than or equal"),
        ({"milvus_collection": "bad-name"}, "milvus_collection"),
        (
            {"rag_runtime_profile": "cpu"},
            "必须使用 bge/bge/milvus",
        ),
        (
            {
                "rag_runtime_profile": "gpu",
                "rag_embedder": "bge",
                "rag_reranker": "bge",
                "rag_store": "milvus",
                "embedding_device": "cpu",
            },
            "EMBEDDING_DEVICE=cuda",
        ),
    ],
)
def test_invalid_rag_configuration_fails_fast(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(_env_file=None, **kwargs)
