"""知识库运维命令的 Local/Lite 安全边界测试。"""

from __future__ import annotations

import json
from argparse import Namespace
from pathlib import Path

import pytest
from packages.common.config import Settings
from packages.rag.embedding import HashingEmbedder
from packages.rag.lifecycle import SourceDocument, sync_documents
from packages.rag.source_store import SourceDocumentStore
from packages.rag.store import LocalKnowledgeStore
from scripts.kb_admin import _backup, _offline_files, _restore, _sha256


def test_local_offline_backup_and_restore(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    settings = Settings(
        _env_file=None,
        rag_embedder="hashing",
        rag_store="local",
        kb_index_dir=str(index_dir),
        kb_source_dir=str(tmp_path / "sources"),
        kb_backup_dir=str(tmp_path / "backups"),
    )
    store = LocalKnowledgeStore(str(index_dir))
    sync_documents(
        [SourceDocument("metric.md", "# 指标\n备份内容")],
        HashingEmbedder(dim=16),
        store,
    )
    sources = SourceDocumentStore(settings.kb_source_dir)
    sources.publish([SourceDocument("metric.md", "# 指标\n备份内容")], full=True)
    (Path(settings.kb_source_dir) / "documents" / "orphan.txt").write_text(
        "不应进入业务备份",
        encoding="utf-8",
    )

    result = _backup(
        settings,
        Namespace(output=None, service_stopped=True),
    )
    backup_path = Path(str(result["path"]))
    assert (backup_path / "manifest.json").is_file()

    store.clear()
    sources.publish([SourceDocument("metric.md", "已被覆盖")], full=True)
    restored = _restore(
        settings,
        Namespace(input=str(backup_path), service_stopped=True, yes=True),
    )
    assert restored["status"] == "restored"
    reopened = LocalKnowledgeStore(str(index_dir))
    assert reopened.documents()[0].source == "metric.md"
    assert SourceDocumentStore(settings.kb_source_dir).documents() == [
        SourceDocument("metric.md", "# 指标\n备份内容")
    ]
    assert len(list((Path(settings.kb_source_dir) / "documents").glob("*.txt"))) == 1
    manifest = (backup_path / "manifest.json").read_text(encoding="utf-8")
    assert '"model_cache_included": false' in manifest
    assert not (backup_path / "models").exists()
    assert not (backup_path / "sources" / "documents" / "orphan.txt").exists()


def test_backup_requires_explicit_stopped_ack(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        rag_embedder="hashing",
        rag_store="local",
        kb_index_dir=str(tmp_path / "index"),
    )
    with pytest.raises(RuntimeError, match="--service-stopped"):
        _backup(settings, Namespace(output=None, service_stopped=False))


def test_restore_validates_every_file_before_replacing_index(tmp_path: Path) -> None:
    index_dir = tmp_path / "index"
    settings = Settings(
        _env_file=None,
        rag_embedder="hashing",
        rag_store="local",
        kb_index_dir=str(index_dir),
        kb_backup_dir=str(tmp_path / "backups"),
    )
    store = LocalKnowledgeStore(str(index_dir))
    sync_documents(
        [SourceDocument("metric.md", "# 指标\n备份前内容")],
        HashingEmbedder(dim=16),
        store,
    )
    result = _backup(settings, Namespace(output=None, service_stopped=True))
    backup_path = Path(str(result["path"]))
    (backup_path / "index.json").write_text("tampered", encoding="utf-8")

    sync_documents(
        [SourceDocument("metric.md", "# 指标\n当前内容")],
        HashingEmbedder(dim=16),
        store,
    )
    before = (index_dir / "index.json").read_bytes()

    with pytest.raises(RuntimeError, match="校验失败"):
        _restore(
            settings,
            Namespace(input=str(backup_path), service_stopped=True, yes=True),
        )

    assert (index_dir / "index.json").read_bytes() == before


def test_format_one_local_backup_removes_new_generation_pointer(
    tmp_path: Path,
) -> None:
    index_dir = tmp_path / "index"
    settings = Settings(
        _env_file=None,
        rag_embedder="hashing",
        rag_store="local",
        kb_index_dir=str(index_dir),
    )
    store = LocalKnowledgeStore(str(index_dir))
    embedder = HashingEmbedder(dim=16)
    sync_documents([SourceDocument("metric.md", "旧备份")], embedder, store)

    backup = tmp_path / "format-one"
    backup.mkdir()
    backup_index = backup / "index.json"
    backup_index.write_bytes((index_dir / "index.json").read_bytes())
    (backup / "manifest.json").write_text(
        json.dumps(
            {
                "format": 1,
                "backend": "local",
                "files": [
                    {
                        "name": "index.json",
                        "size": backup_index.stat().st_size,
                        "sha256": _sha256(backup_index),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    sync_documents([SourceDocument("metric.md", "当前新代")], embedder, store)
    assert (index_dir / "active.json").exists()

    _restore(
        settings,
        Namespace(input=str(backup), service_stopped=True, yes=True),
    )

    assert not (index_dir / "active.json").exists()
    assert LocalKnowledgeStore(str(index_dir)).documents()[0].version == 1


def test_standalone_backup_redirects_to_official_tool() -> None:
    settings = Settings(
        _env_file=None,
        rag_embedder="bge",
        rag_store="milvus",
        milvus_uri="http://127.0.0.1:19530",
    )
    with pytest.raises(RuntimeError, match="milvus-backup"):
        _backup(settings, Namespace(output=None, service_stopped=True))


def test_milvus_lite_backup_scope_includes_persistent_sources(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        rag_embedder="bge",
        rag_reranker="bge",
        rag_store="milvus",
        milvus_uri=str(tmp_path / "lite.db"),
        kb_source_dir=str(tmp_path / "sources"),
    )
    SourceDocumentStore(settings.kb_source_dir).publish(
        [SourceDocument("metric.md", "受管原文")],
        full=True,
    )

    backend, files = _offline_files(settings)

    assert backend == "milvus_lite"
    assert "sources/manifest.json" in {name for name, _ in files}
    assert any(name.startswith("sources/documents/") for name, _ in files)
