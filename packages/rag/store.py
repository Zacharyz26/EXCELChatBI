"""知识库存储：向量 + BM25 混合检索的后端。

默认 `LocalKnowledgeStore`：落盘 JSON、numpy 余弦、自实现中文 BM25，无外部依赖。
Milvus 后端后续可插拔（同 `KnowledgeStore` 接口）。
"""

from __future__ import annotations

import abc
import json
import math
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np

from packages.common.config import get_settings


@dataclass
class StoredChunk:
    """入库的片段：文本 + 来源 + 分词 + 向量。"""

    text: str
    source: str
    section: str | None = None
    tokens: list[str] = field(default_factory=list)
    vector: list[float] = field(default_factory=list)
    chunk_id: str = ""


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


class KnowledgeStore(abc.ABC):
    """知识库存储抽象：向量检索 + BM25 检索。"""

    @abc.abstractmethod
    def add(self, chunks: list[StoredChunk]) -> int:
        """写入片段，返回新增数量。"""

    @abc.abstractmethod
    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        """稠密向量近邻检索。"""

    @abc.abstractmethod
    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        """中文 BM25 稀疏检索。"""

    @abc.abstractmethod
    def count(self) -> int:
        """片段总数。"""

    @abc.abstractmethod
    def clear(self) -> None:
        """清空索引。"""


class LocalKnowledgeStore(KnowledgeStore):
    """本地落盘知识库（JSON 持久化 + numpy 余弦 + 自实现 BM25）。"""

    def __init__(self, index_dir: str | None = None) -> None:
        self._dir = Path(index_dir or get_settings().kb_index_dir)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._path = self._dir / "index.json"
        self._chunks: list[StoredChunk] = []
        self._load()

    # ── 写入 ──

    def add(self, chunks: list[StoredChunk]) -> int:
        for c in chunks:
            if not c.chunk_id:
                c.chunk_id = uuid.uuid4().hex
            self._chunks.append(c)
        self._persist()
        return len(chunks)

    def count(self) -> int:
        return len(self._chunks)

    def clear(self) -> None:
        self._chunks = []
        self._persist()

    # ── 检索 ──

    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        if not self._chunks:
            return []
        mat = np.array([c.vector for c in self._chunks], dtype=float)
        q = np.array(query_vec, dtype=float)
        scores = mat @ q  # 向量已 L2 归一，点积即余弦
        return self._top_hits(scores, top_k)

    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        if not self._chunks or not query_tokens:
            return []
        n = len(self._chunks)
        doc_tokens = [c.tokens for c in self._chunks]
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
        return self._top_hits(scores, top_k, positive_only=True)

    # ── 内部 ──

    def _top_hits(
        self, scores: np.ndarray, top_k: int, positive_only: bool = False
    ) -> list[SearchHit]:
        order = np.argsort(-scores)[: max(top_k, 0)]
        hits: list[SearchHit] = []
        for i in order:
            score = float(scores[i])
            if positive_only and score <= 0.0:
                continue
            c = self._chunks[int(i)]
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

    def _persist(self) -> None:
        data = [asdict(c) for c in self._chunks]
        self._path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _load(self) -> None:
        if self._path.exists():
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._chunks = [StoredChunk(**rec) for rec in data]
