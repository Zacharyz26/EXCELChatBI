"""向量库接口（Milvus）。中文 RAG 向量存储与检索。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SearchHit:
    """一次向量检索命中。"""

    text: str
    source: str
    score: float


class VectorStore:
    """Milvus 向量库封装。"""

    def __init__(self, host: str, port: int, collection: str) -> None:
        self._host = host
        self._port = port
        self._collection = collection

    def upsert(self, vectors: list[list[float]], payloads: list[dict]) -> None:
        """写入向量及其 payload（含 source）。"""
        raise NotImplementedError("TODO: pymilvus 插入")

    def search(self, vector: list[float], top_k: int = 10) -> list[SearchHit]:
        """按向量近邻检索。"""
        raise NotImplementedError("TODO: pymilvus 检索")
