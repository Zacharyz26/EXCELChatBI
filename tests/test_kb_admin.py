"""知识库运维命令的 Local/Lite 安全边界测试。"""

from __future__ import annotations

from argparse import Namespace
from pathlib import Path

import pytest
from packages.common.config import Settings
from packages.rag.embedding import HashingEmbedder
from packages.rag.lifecycle import SourceDocument, sync_documents
from packages.rag.store import LocalKnowledgeStore
from scripts.kb_admin import _backup, _restore


def test_local_offline_backup_and_restore(tmp_path: Path) -> None:
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
        [SourceDocument("metric.md", "# 指标\n备份内容")],
        HashingEmbedder(dim=16),
        store,
    )

    result = _backup(
        settings,
        Namespace(output=None, service_stopped=True),
    )
    backup_path = Path(str(result["path"]))
    assert (backup_path / "manifest.json").is_file()

    store.clear()
    restored = _restore(
        settings,
        Namespace(input=str(backup_path), service_stopped=True, yes=True),
    )
    assert restored["status"] == "restored"
    reopened = LocalKnowledgeStore(str(index_dir))
    assert reopened.documents()[0].source == "metric.md"


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


def test_standalone_backup_redirects_to_official_tool() -> None:
    settings = Settings(
        _env_file=None,
        rag_embedder="bge",
        rag_store="milvus",
        milvus_uri="http://127.0.0.1:19530",
    )
    with pytest.raises(RuntimeError, match="milvus-backup"):
        _backup(settings, Namespace(output=None, service_stopped=True))
