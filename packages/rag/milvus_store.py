"""Milvus 知识库存储（决策2：Milvus Lite 起步，换 standalone 只改 URI）。

- uri 为本地文件路径（如 `.data/milvus_lite.db`）时即 Milvus Lite（pymilvus 内嵌，
  零部署）；换 standalone 时把 uri 改成 `http://host:19530`，代码不动。
- 集合含稠密（bge-m3 dense，IP 度量）与稀疏（lexical weights，SPARSE_FLOAT_VECTOR）
  双字段（决策1）；稀疏字段仅在配对 bge embedder 时有真实内容。
- 集合按首次写入的向量维度懒创建，维度不进配置（避免与 embedder 配置漂移）。
- 幂等去重语义与 LocalKnowledgeStore 一致：同 (source, 归一化文本) 不重复入库。
"""

from __future__ import annotations

from typing import Any

from packages.common.logging import get_logger
from packages.rag.store import KnowledgeStore, SearchHit, StoredChunk, _norm

_log = get_logger("rag.milvus_store")

# 全量拉取 source/section 等标量时的上限（知识库为文档级语料，远低于此）
_SCAN_LIMIT = 16_384
# Milvus 稀疏向量不允许空：替身 embedder 无稀疏表示时写入的占位维度
_EMPTY_SPARSE_PLACEHOLDER = {0: 1e-9}


class MilvusKnowledgeStore(KnowledgeStore):
    """基于 pymilvus MilvusClient 的知识库存储（需装 .[rag]）。"""

    supports_sparse = True

    def __init__(self, uri: str, collection: str = "kb_chunks") -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pymilvus：请先 `uv sync --extra rag`，"
                "或将配置 rag_store 改回 local"
            ) from exc
        self._client = MilvusClient(uri=uri)
        self._collection = collection

    # ── 写入 ──

    def add(self, chunks: list[StoredChunk]) -> int:
        if not chunks:
            return 0
        self._ensure_collection(dim=len(chunks[0].vector))
        existing = {
            (row["source"], _norm(row["text"]))
            for row in self._scan(["source", "text"])
        }
        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            key = (chunk.source, _norm(chunk.text))
            if key in existing:
                continue
            existing.add(key)
            if not chunk.chunk_id:
                import uuid

                chunk.chunk_id = uuid.uuid4().hex
            rows.append(
                {
                    "chunk_id": chunk.chunk_id,
                    "text": chunk.text,
                    "source": chunk.source,
                    "section": chunk.section or "",
                    "dense": chunk.vector,
                    "sparse": self._to_milvus_sparse(chunk.sparse),
                }
            )
        if rows:
            self._client.insert(collection_name=self._collection, data=rows)
        return len(rows)

    def count(self) -> int:
        if not self._client.has_collection(self._collection):
            return 0
        result = self._client.query(
            collection_name=self._collection,
            filter="",
            output_fields=["count(*)"],
        )
        return int(result[0]["count(*)"]) if result else 0

    def clear(self) -> None:
        if self._client.has_collection(self._collection):
            self._client.drop_collection(self._collection)

    def sources(self) -> list[str]:
        out: list[str] = []
        for row in self._scan(["source"]):
            if row["source"] not in out:
                out.append(row["source"])
        return out

    def topics(self) -> list[str]:
        out: list[str] = []
        for row in self._scan(["section"]):
            if row["section"] and row["section"] not in out:
                out.append(row["section"])
        return out

    # ── 检索 ──

    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        return self._search(data=query_vec, anns_field="dense", top_k=top_k)

    def sparse_search(
        self, query_sparse: dict[str, float], top_k: int
    ) -> list[SearchHit]:
        """bge-m3 lexical weights 稀疏检索（决策1：取代自实现 BM25）。"""
        if not query_sparse:
            return []
        return self._search(
            data=self._to_milvus_sparse(query_sparse), anns_field="sparse", top_k=top_k
        )

    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        """Milvus 后端的稀疏路是 bge-m3 lexical weights，无 BM25 备路。

        配对替身 embedder（无稀疏表示）时混合检索退化为纯稠密路——
        Milvus 后端应与 bge embedder 配对使用（见验收基线文档）。
        """
        del query_tokens, top_k
        return []

    # ── 内部 ──

    def _search(self, *, data: Any, anns_field: str, top_k: int) -> list[SearchHit]:
        if top_k <= 0 or not self._client.has_collection(self._collection):
            return []
        results = self._client.search(
            collection_name=self._collection,
            data=[data],
            anns_field=anns_field,
            limit=top_k,
            output_fields=["text", "source", "section"],
        )
        hits: list[SearchHit] = []
        for item in results[0] if results else []:
            entity = item.get("entity", {})
            # 主键的返回键名随 pymilvus 版本变化：旧版 "id"，2.6+ 用主键字段名。
            # chunk_id 为空会让 RRF 融合按空串折叠成单候选，必须兜底取全。
            chunk_id = item.get("id") or item.get("chunk_id") or entity.get("chunk_id") or ""
            hits.append(
                SearchHit(
                    chunk_id=str(chunk_id),
                    text=str(entity.get("text", "")),
                    source=str(entity.get("source", "")),
                    score=float(item.get("distance", 0.0)),
                    section=str(entity.get("section")) or None,
                )
            )
        return hits

    def _scan(self, fields: list[str]) -> list[dict[str, Any]]:
        if not self._client.has_collection(self._collection):
            return []
        return list(
            self._client.query(
                collection_name=self._collection,
                filter="",
                output_fields=fields,
                limit=_SCAN_LIMIT,
            )
        )

    def _ensure_collection(self, dim: int) -> None:
        if self._client.has_collection(self._collection):
            return
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("section", DataType.VARCHAR, max_length=512)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)

        index_params = self._client.prepare_index_params()
        index_params.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="IP")
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
        )
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params
        )
        _log.info("milvus.collection_created", collection=self._collection, dim=dim)

    @staticmethod
    def _to_milvus_sparse(sparse: dict[str, float]) -> dict[int, float]:
        """lexical weights（str token_id）→ Milvus 稀疏格式（int 下标，禁空）。"""
        if not sparse:
            return dict(_EMPTY_SPARSE_PLACEHOLDER)
        return {int(token_id): float(weight) for token_id, weight in sparse.items()}
