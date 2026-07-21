"""知识库文档生命周期：增量同步、版本记录与原子全量重建。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from packages.common.logging import get_logger
from packages.rag.embedding import Embedder
from packages.rag.pipeline import chunk_and_embed
from packages.rag.store import (
    DocumentInfo,
    KnowledgeStore,
    StoredChunk,
    document_id_for_source,
)

TEXT_SUFFIXES = {".md", ".txt", ".markdown"}
_log = get_logger("rag.lifecycle")


@dataclass(frozen=True)
class SourceDocument:
    """一次同步中的原始文档。"""

    source: str
    text: str


@dataclass
class SyncResult:
    """文档同步结果，供 API、CLI 与前端展示。"""

    documents: int
    chunks: int
    total_chunks: int
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)


def content_hash(text: str) -> str:
    """计算原始文档内容哈希，用于增量变更检测。"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_text_documents(
    path: Path,
    *,
    source_root: Path,
    max_files: int,
    max_document_chars: int,
) -> list[SourceDocument]:
    """从白名单目录读取文本文件，并保留相对路径作为稳定来源。"""
    files = (
        [item for item in sorted(path.rglob("*")) if item.suffix.lower() in TEXT_SUFFIXES]
        if path.is_dir()
        else [path]
    )
    if len(files) > max_files:
        raise ValueError(f"知识库文档数超过上限 {max_files}")
    documents: list[SourceDocument] = []
    for item in files:
        if item.suffix.lower() not in TEXT_SUFFIXES:
            raise ValueError(f"暂仅支持纯文本 .md/.txt/.markdown: {item.name}")
        text = item.read_text(encoding="utf-8")
        if len(text) > max_document_chars:
            raise ValueError(f"文档 {item.name} 超过字符上限 {max_document_chars}")
        try:
            source = item.relative_to(source_root).as_posix()
        except ValueError:
            source = item.name
        documents.append(SourceDocument(source=source, text=text))
    return documents


def sync_documents(
    documents: list[SourceDocument],
    embedder: Embedder,
    store: KnowledgeStore,
    *,
    full: bool = False,
) -> SyncResult:
    """同步文档。

    incremental 模式仅重算新增/变更文档；full 模式重算全部输入，并由存储后端在
    新索引准备完成后原子切换。embedding 失败时不会触碰现有索引。
    """
    started = time.perf_counter()
    by_source: dict[str, SourceDocument] = {}
    for document in documents:
        source = document.source.strip()
        if not source:
            raise ValueError("知识库文档 source 不能为空")
        if source in by_source:
            raise ValueError(f"知识库文档来源重复: {source}")
        by_source[source] = SourceDocument(source=source, text=document.text)

    existing = {item.document_id: item for item in store.documents()}
    incoming_ids = {document_id_for_source(source) for source in by_source}
    now = datetime.now(UTC).isoformat()
    prepared: list[StoredChunk] = []
    changed_ids: set[str] = set()
    created: list[str] = []
    updated: list[str] = []
    skipped: list[str] = []

    for source, document in by_source.items():
        document_id = document_id_for_source(source)
        digest = content_hash(document.text)
        previous = existing.get(document_id)
        unchanged = previous is not None and previous.content_hash == digest
        if unchanged and not full:
            skipped.append(source)
            continue

        version = _next_version(previous, digest)
        updated_at = (
            previous.updated_at
            if unchanged and previous is not None and previous.updated_at
            else now
        )
        chunks = chunk_and_embed(
            document.text,
            source,
            embedder,
            document_id=document_id,
            content_hash=digest,
            version=version,
            updated_at=updated_at,
        )
        _assign_chunk_ids(chunks)
        prepared.extend(chunks)
        changed_ids.add(document_id)
        if previous is None:
            created.append(source)
        elif unchanged:
            skipped.append(source)
        else:
            updated.append(source)

    deleted = [
        item.source
        for document_id, item in existing.items()
        if full and document_id not in incoming_ids
    ]
    if full:
        store.replace_documents(prepared, set(existing), full=True)
    elif changed_ids:
        store.replace_documents(prepared, changed_ids)

    result = SyncResult(
        documents=len(documents),
        chunks=len(prepared),
        total_chunks=store.count(),
        created=created,
        updated=updated,
        skipped=skipped,
        deleted=deleted,
    )
    _log.info(
        "rag.sync",
        mode="full" if full else "incremental",
        input_documents=len(documents),
        embedded_chunks=len(prepared),
        total_chunks=result.total_chunks,
        created=len(created),
        updated=len(updated),
        skipped=len(skipped),
        deleted=len(deleted),
        elapsed_ms=round((time.perf_counter() - started) * 1_000, 3),
        store=type(store).__name__,
        embedder=type(embedder).__name__,
    )
    return result


def _next_version(previous: DocumentInfo | None, digest: str) -> int:
    if previous is None:
        return 1
    if previous.content_hash == digest:
        return previous.version
    return max(previous.version, 1) + 1


def _assign_chunk_ids(chunks: list[StoredChunk]) -> None:
    for index, chunk in enumerate(chunks):
        seed = f"{chunk.document_id}:{chunk.version}:{index}:{chunk.content_hash}"
        chunk.chunk_id = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
