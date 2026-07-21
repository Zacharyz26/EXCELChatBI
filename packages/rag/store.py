"""知识库存储：向量 + BM25 混合检索的后端。

默认 `LocalKnowledgeStore`：落盘 JSON、numpy 余弦、自实现中文 BM25，无外部依赖。
Milvus 后端后续可插拔（同 `KnowledgeStore` 接口）。
"""

from __future__ import annotations

import abc
import json
import math
import os
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from packages.common.config import get_settings


@dataclass
class StoredChunk:
    """入库的片段：文本 + 来源 + 分词 + 稠密向量 +（可选）稀疏表示。"""

    text: str
    source: str
    section: str | None = None
    tokens: list[str] = field(default_factory=list)
    vector: list[float] = field(default_factory=list)
    # bge-m3 lexical weights（token_id → 权重）；替身 embedder 下为空 dict
    sparse: dict[str, float] = field(default_factory=dict)
    chunk_id: str = ""
    document_id: str = ""
    content_hash: str = ""
    version: int = 1
    updated_at: str = ""


@dataclass(frozen=True)
class DocumentInfo:
    """知识库文档清单项，由同 document_id 的片段聚合得到。"""

    document_id: str
    source: str
    content_hash: str
    version: int
    updated_at: str
    chunk_count: int


@dataclass(frozen=True)
class StoreStatus:
    """知识库存储运行状态；不包含连接密钥或文档正文。"""

    backend: str
    ready: bool
    chunk_count: int
    document_count: int
    active_collection: str | None = None
    previous_collection: str | None = None
    generations: tuple[str, ...] = ()


@dataclass
class SearchHit:
    """一次检索命中（含 source，供引用）。"""

    chunk_id: str
    text: str
    source: str
    score: float
    section: str | None = None


# BM25 参数（检索算法参数，非模型名）。
_BM25_K1 = 1.5
_BM25_B = 0.75


def _norm(text: str) -> str:
    """归一化文本（折叠空白），用于按内容去重（红线6：去重不改引用真实性）。"""
    return " ".join(text.split())


def document_id_for_source(source: str) -> str:
    """来源稳定映射为文档 ID；路径变化视为新文档。"""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"chatbi-kb:{source}").hex


def chunk_document_id(chunk: StoredChunk) -> str:
    """兼容升级前没有 document_id 的历史片段。"""
    return chunk.document_id or document_id_for_source(chunk.source)


def summarize_documents(chunks: list[StoredChunk]) -> list[DocumentInfo]:
    """把片段聚合为稳定、有版本信息的文档清单。"""
    grouped: dict[str, list[StoredChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk_document_id(chunk), []).append(chunk)
    documents: list[DocumentInfo] = []
    for document_id, items in grouped.items():
        versions = [item.version for item in items]
        hashes = [item.content_hash for item in items if item.content_hash]
        updated = [item.updated_at for item in items if item.updated_at]
        documents.append(
            DocumentInfo(
                document_id=document_id,
                source=items[0].source,
                content_hash=hashes[0] if hashes else "",
                version=max(versions, default=1),
                updated_at=max(updated, default=""),
                chunk_count=len(items),
            )
        )
    return sorted(documents, key=lambda item: item.source)


class KnowledgeStore(abc.ABC):
    """知识库存储抽象：稠密向量 + 稀疏（BM25 或 bge-m3 lexical）双路检索。"""

    #: 是否支持 bge-m3 稀疏向量检索（决策1：稀疏路取代自实现 BM25）。
    #: True 时检索层优先走 sparse_search；否则回退中文 BM25 备路。
    supports_sparse: bool = False

    @abc.abstractmethod
    def add(self, chunks: list[StoredChunk]) -> int:
        """写入片段，返回新增数量。"""

    @abc.abstractmethod
    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        """稠密向量近邻检索。"""

    @abc.abstractmethod
    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        """中文 BM25 稀疏检索（替身/本地后端的稀疏路）。"""

    def sparse_search(
        self, query_sparse: dict[str, float], top_k: int
    ) -> list[SearchHit]:
        """bge-m3 稀疏向量检索；仅 supports_sparse=True 的后端实现。"""
        del query_sparse, top_k
        return []

    @abc.abstractmethod
    def count(self) -> int:
        """片段总数。"""

    @abc.abstractmethod
    def clear(self) -> None:
        """清空索引。"""

    @abc.abstractmethod
    def sources(self) -> list[str]:
        """去重后的来源文件列表（顺序稳定）。"""

    @abc.abstractmethod
    def topics(self) -> list[str]:
        """去重后的小节标题列表（供问答引导，顺序稳定）。"""

    def documents(self) -> list[DocumentInfo]:
        """返回文档清单；具体存储后端应实现。"""
        raise NotImplementedError

    def replace_documents(
        self,
        chunks: list[StoredChunk],
        document_ids: set[str],
        *,
        full: bool = False,
    ) -> tuple[int, int]:
        """原子替换指定文档或整个索引，返回 (移除片段数, 写入片段数)。"""
        raise NotImplementedError

    def delete_document(self, document_id: str) -> int:
        """删除一个文档并返回移除片段数。"""
        removed, _ = self.replace_documents([], {document_id})
        return removed

    def status(self) -> StoreStatus:
        """返回可用于 readiness/运维的非敏感状态。"""
        return StoreStatus(
            backend=type(self).__name__,
            ready=True,
            chunk_count=self.count(),
            document_count=len(self.documents()),
        )

    def rollback(self) -> StoreStatus:
        """切回上一代索引；不支持代际的后端应明确报错。"""
        raise RuntimeError(f"{type(self).__name__} 不支持索引代际回滚")

    def cleanup_generations(self, retain: int = 2) -> int:
        """清理历史索引代际，返回清理数量。"""
        del retain
        return 0

    @contextmanager
    def retrieval_snapshot(self) -> Iterator[None]:
        """固定一次混合检索所读取的存储快照；默认后端无需额外处理。"""
        yield

    def close(self) -> None:
        """释放存储连接；无外部资源的后端无需处理。"""
        return None


class LocalKnowledgeStore(KnowledgeStore):
    """本地落盘知识库（JSON 持久化 + numpy 余弦 + 自实现 BM25）。"""

    def __init__(self, index_dir: str | None = None) -> None:
        self._dir = Path(index_dir or get_settings().kb_index_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "index.json"
        self._chunks: list[StoredChunk] = []
        # 写锁：摄入进线程池后并发 add/clear 会真并发，需串行化改列表与写盘
        self._write_lock = threading.Lock()
        self._read_state = threading.local()
        self._load()

    # ── 写入 ──

    def add(self, chunks: list[StoredChunk]) -> int:
        # 幂等去重（红线6 根因层）：同 (source, 归一化文本) 已存在则跳过，
        with self._write_lock:
            next_chunks = list(self._chunks)
            existing = {(c.source, _norm(c.text)) for c in next_chunks}
            added = 0
            for c in chunks:
                key = (c.source, _norm(c.text))
                if key in existing:
                    continue
                if not c.chunk_id:
                    c.chunk_id = uuid.uuid4().hex
                next_chunks.append(c)
                existing.add(key)
                added += 1
            self._persist(next_chunks)
            self._chunks = next_chunks
            return added

    def count(self) -> int:
        return len(self._chunks_view())

    def clear(self) -> None:
        with self._write_lock:
            self._persist([])
            self._chunks = []

    def sources(self) -> list[str]:
        out: list[str] = []
        for c in self._chunks:
            if c.source not in out:
                out.append(c.source)
        return out

    def topics(self) -> list[str]:
        out: list[str] = []
        for c in self._chunks:
            if c.section and c.section not in out:
                out.append(c.section)
        return out

    def documents(self) -> list[DocumentInfo]:
        return summarize_documents(self._chunks)

    @contextmanager
    def retrieval_snapshot(self) -> Iterator[None]:
        """用线程本地引用固定一次检索，发布新列表时无需阻塞读者。"""
        previous = getattr(self._read_state, "chunks", None)
        self._read_state.chunks = self._chunks
        try:
            yield
        finally:
            if previous is None:
                del self._read_state.chunks
            else:
                self._read_state.chunks = previous

    def status(self) -> StoreStatus:
        return StoreStatus(
            backend="local",
            ready=True,
            chunk_count=self.count(),
            document_count=len(self.documents()),
            active_collection=self._path.name,
            generations=(self._path.name,),
        )

    def replace_documents(
        self,
        chunks: list[StoredChunk],
        document_ids: set[str],
        *,
        full: bool = False,
    ) -> tuple[int, int]:
        """用临时文件 + os.replace 原子发布本地索引。"""
        with self._write_lock:
            previous = self._chunks
            if full:
                kept: list[StoredChunk] = []
                removed = len(previous)
            else:
                kept = [c for c in previous if chunk_document_id(c) not in document_ids]
                removed = len(previous) - len(kept)
            next_chunks = [*kept, *chunks]
            # 先原子发布文件，再切内存引用；写盘失败时读者始终看到旧快照。
            self._persist(next_chunks)
            self._chunks = next_chunks
            return removed, len(chunks)

    # ── 检索 ──

    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        chunks = self._chunks_view()
        if not chunks:
            return []
        mat = np.array([c.vector for c in chunks], dtype=float)
        q = np.array(query_vec, dtype=float)
        scores = mat @ q  # 向量已 L2 归一，点积即余弦
        return self._top_hits(scores, top_k, chunks=chunks)

    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        chunks = self._chunks_view()
        if not chunks or not query_tokens:
            return []
        n = len(chunks)
        doc_tokens = [c.tokens for c in chunks]
        doc_len = np.array([len(t) for t in doc_tokens], dtype=float)
        avgdl = float(doc_len.mean()) or 1.0
        # 文档频率
        df: dict[str, int] = {}
        for toks in doc_tokens:
            for term in set(toks):
                df[term] = df.get(term, 0) + 1
        scores = np.zeros(n, dtype=float)
        for term in set(query_tokens):
            if term not in df:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            for i, toks in enumerate(doc_tokens):
                f = toks.count(term)
                if not f:
                    continue
                denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc_len[i] / avgdl)
                scores[i] += idf * (f * (_BM25_K1 + 1)) / denom
        return self._top_hits(scores, top_k, positive_only=True, chunks=chunks)

    # ── 内部 ──

    def _chunks_view(self) -> list[StoredChunk]:
        return getattr(self._read_state, "chunks", self._chunks)

    def _top_hits(
        self,
        scores: np.ndarray,
        top_k: int,
        positive_only: bool = False,
        *,
        chunks: list[StoredChunk] | None = None,
    ) -> list[SearchHit]:
        snapshot = self._chunks if chunks is None else chunks
        order = np.argsort(-scores)[: max(top_k, 0)]
        hits: list[SearchHit] = []
        for i in order:
            score = float(scores[i])
            if positive_only and score <= 0.0:
                continue
            c = snapshot[int(i)]
            hits.append(
                SearchHit(
                    chunk_id=c.chunk_id,
                    text=c.text,
                    source=c.source,
                    score=score,
                    section=c.section,
                )
            )
        return hits

    def _persist(self, chunks: list[StoredChunk] | None = None) -> None:
        target = self._chunks if chunks is None else chunks
        data = [asdict(c) for c in target]
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, self._path)

    def _load(self) -> None:
        if not self._path.exists():
            return
        data = json.loads(self._path.read_text(encoding="utf-8"))
        chunks = [StoredChunk(**rec) for rec in data]
        # 自愈：清除历史重复摄入产生的完全相同副本，并写回净化后的索引
        seen: set[tuple[str, str]] = set()
        deduped: list[StoredChunk] = []
        for c in chunks:
            key = (c.source, _norm(c.text))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(c)
        self._chunks = deduped
        if len(deduped) != len(chunks):
            self._persist()
