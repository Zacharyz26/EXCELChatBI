"""Milvus 知识库存储：Lite 本地运行，Standalone 仅需切换 URI。

已有集合会在连接时主动加载。文档更新在新集合中完成，确认加载成功后再通过
Lite 指针文件或 Standalone alias 发布，避免重建过程中暴露半成品索引。
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from packages.common.logging import get_logger
from packages.rag.store import (
    DocumentInfo,
    KnowledgeStore,
    SearchHit,
    StoredChunk,
    StoreStatus,
    _norm,
    chunk_document_id,
    summarize_documents,
)

_log = get_logger("rag.milvus_store")
_EMPTY_SPARSE_PLACEHOLDER = {0: 1e-9}
_METADATA_FIELDS = {"document_id", "content_hash", "version", "updated_at"}


def _configure_local_no_proxy(uri: str) -> None:
    """确保本地 Milvus/gRPC 不被系统 HTTP 代理劫持。"""
    parsed = urlsplit(uri)
    is_lite = not parsed.scheme or parsed.scheme == "file"
    is_loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if not (is_lite or is_loopback):
        return
    required = ["127.0.0.1", "localhost", "::1"]
    for key in ("NO_PROXY", "no_proxy"):
        values = [item.strip() for item in os.environ.get(key, "").split(",") if item.strip()]
        for item in required:
            if item not in values:
                values.append(item)
        os.environ[key] = ",".join(values)


class MilvusKnowledgeStore(KnowledgeStore):
    """基于 pymilvus MilvusClient 的知识库存储（需装 .[rag]）。"""

    supports_sparse = True

    def __init__(
        self, uri: str, collection: str = "kb_chunks", *, token: str = ""
    ) -> None:
        try:
            from pymilvus import MilvusClient
        except ImportError as exc:
            raise RuntimeError(
                "缺少 pymilvus：请先 `uv sync --extra rag`，"
                "或将配置 rag_store 改回 local"
            ) from exc
        _configure_local_no_proxy(uri)
        self._uri = uri
        self._base_collection = collection
        self._is_lite = not urlsplit(uri).scheme or urlsplit(uri).scheme == "file"
        self._alias = f"{collection}_active"
        self._pointer_path = self._make_pointer_path(uri, collection)
        self._write_lock = threading.RLock()
        self._state_lock = threading.Lock()
        self._read_state = threading.local()
        self._active_reads: dict[str, int] = {}
        client_args = {"uri": uri}
        if token and not self._is_lite:
            client_args["token"] = token
        self._client = MilvusClient(**client_args)
        self._collection, self._previous_collection = self._resolve_active_state()
        if self._client.has_collection(self._collection):
            # create_collection 后通常已加载，但进程重连后的已有集合是 released。
            self._client.load_collection(collection_name=self._collection)

    # ── 写入与生命周期 ──

    def add(self, chunks: list[StoredChunk]) -> int:
        if not chunks:
            return 0
        with self._write_lock:
            self._ensure_collection(dim=len(chunks[0].vector))
            existing = {
                (str(row["source"]), _norm(str(row["text"])))
                for row in self._scan(["source", "text"])
            }
            fresh: list[StoredChunk] = []
            for chunk in chunks:
                key = (chunk.source, _norm(chunk.text))
                if key in existing:
                    continue
                existing.add(key)
                if not chunk.chunk_id:
                    chunk.chunk_id = uuid.uuid4().hex
                fresh.append(chunk)
            self._insert_chunks(self._collection, fresh)
            return len(fresh)

    def count(self) -> int:
        collection = self._collection_view()
        if not self._client.has_collection(collection):
            return 0
        result = self._client.query(
            collection_name=collection,
            filter="",
            output_fields=["count(*)"],
        )
        return int(result[0]["count(*)"]) if result else 0

    def clear(self) -> None:
        """清除当前知识库及其重建代际，避免重启后回退到旧集合。"""
        with self._write_lock:
            if not self._is_lite:
                try:
                    self._client.drop_alias(alias=self._alias)
                except Exception:
                    pass
            for name in self._client.list_collections():
                if name == self._base_collection or self._is_generation(name):
                    self._client.drop_collection(collection_name=name)
            if self._pointer_path is not None:
                self._pointer_path.unlink(missing_ok=True)
            with self._state_lock:
                self._collection = self._base_collection
                self._previous_collection = None

    def sources(self) -> list[str]:
        return list(dict.fromkeys(str(row["source"]) for row in self._scan(["source"])))

    def topics(self) -> list[str]:
        return list(
            dict.fromkeys(
                str(row["section"])
                for row in self._scan(["section"])
                if row.get("section")
            )
        )

    def documents(self) -> list[DocumentInfo]:
        return summarize_documents(self._all_chunks(include_vectors=False))

    @contextmanager
    def retrieval_snapshot(self) -> Iterator[None]:
        """固定物理集合，并阻止清理任务删除仍被检索使用的代际。"""
        previous = getattr(self._read_state, "collection", None)
        with self._state_lock:
            collection = previous or self._collection
            self._active_reads[collection] = self._active_reads.get(collection, 0) + 1
            self._read_state.collection = collection
        try:
            yield
        finally:
            with self._state_lock:
                remaining = self._active_reads[collection] - 1
                if remaining:
                    self._active_reads[collection] = remaining
                else:
                    del self._active_reads[collection]
                if previous is None:
                    del self._read_state.collection
                else:
                    self._read_state.collection = previous

    def status(self) -> StoreStatus:
        generations = tuple(sorted(self._managed_collections(), reverse=True))
        return StoreStatus(
            backend="milvus_lite" if self._is_lite else "milvus_standalone",
            ready=True,
            chunk_count=self.count(),
            document_count=len(self.documents()),
            active_collection=self._collection,
            previous_collection=self._previous_collection,
            generations=generations,
        )

    def rollback(self) -> StoreStatus:
        """原子切回上一代；再次调用可切回回滚前的代际。"""
        with self._write_lock:
            previous = self._previous_collection
            if previous is None or not self._client.has_collection(previous):
                raise RuntimeError("没有可回滚的上一代知识库索引")
            current = self._collection
            self._client.load_collection(collection_name=previous)
            self._publish(previous, previous=current)
            with self._state_lock:
                self._collection = previous
                self._previous_collection = current
            _log.warning(
                "milvus.collection_rolled_back",
                active=previous,
                previous=current,
            )
            return self.status()

    def cleanup_generations(self, retain: int = 2) -> int:
        """至少保留活动与上一代；返回实际删除的集合数。"""
        if retain < 2:
            raise ValueError("retain 必须至少为 2，以保留活动和回滚代际")
        with self._write_lock:
            managed = sorted(self._managed_collections(), reverse=True)
            keep = {self._collection}
            if self._previous_collection:
                keep.add(self._previous_collection)
            for name in managed:
                if len(keep) >= retain:
                    break
                keep.add(name)
            return self._cleanup_generations(keep)

    def replace_documents(
        self,
        chunks: list[StoredChunk],
        document_ids: set[str],
        *,
        full: bool = False,
    ) -> tuple[int, int]:
        """在新集合构建完整快照，加载成功后原子发布。"""
        with self._write_lock:
            previous_collection = self._collection
            previous = self._all_chunks(include_vectors=True)
            if full:
                kept: list[StoredChunk] = []
                removed = len(previous)
            else:
                kept = [item for item in previous if chunk_document_id(item) not in document_ids]
                removed = len(previous) - len(kept)
            merged = [*kept, *chunks]
            dim = len(merged[0].vector) if merged else self._collection_dim()
            if dim <= 0:
                # 空库删除是天然幂等，无需创建无法确定维度的集合。
                return removed, len(chunks)

            stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
            candidate = f"{self._base_collection}_v{stamp}_{uuid.uuid4().hex[:8]}"
            try:
                self._create_collection(candidate, dim)
                self._insert_chunks(candidate, merged)
                self._client.load_collection(collection_name=candidate)
                self._publish(candidate, previous=previous_collection)
            except Exception:
                if self._client.has_collection(candidate):
                    self._client.drop_collection(collection_name=candidate)
                raise
            with self._state_lock:
                self._collection = candidate
                self._previous_collection = previous_collection
            self._cleanup_generations({candidate, previous_collection})
            _log.info(
                "milvus.collection_published",
                collection=candidate,
                chunks=len(merged),
                mode="full" if full else "incremental",
            )
            return removed, len(chunks)

    def close(self) -> None:
        self._client.close()

    # ── 检索 ──

    def vector_search(self, query_vec: list[float], top_k: int) -> list[SearchHit]:
        return self._search(data=query_vec, anns_field="dense", top_k=top_k)

    def sparse_search(
        self, query_sparse: dict[str, float], top_k: int
    ) -> list[SearchHit]:
        if not query_sparse:
            return []
        return self._search(
            data=self._to_milvus_sparse(query_sparse), anns_field="sparse", top_k=top_k
        )

    def bm25_search(self, query_tokens: list[str], top_k: int) -> list[SearchHit]:
        del query_tokens, top_k
        return []

    # ── 内部 ──

    def _search(self, *, data: Any, anns_field: str, top_k: int) -> list[SearchHit]:
        collection = self._collection_view()
        if top_k <= 0 or not self._client.has_collection(collection):
            return []
        results = self._client.search(
            collection_name=collection,
            data=[data],
            anns_field=anns_field,
            limit=top_k,
            output_fields=["text", "source", "section"],
        )
        hits: list[SearchHit] = []
        for item in results[0] if results else []:
            entity = item.get("entity", {})
            chunk_id = (
                item.get("id")
                or item.get("chunk_id")
                or entity.get("chunk_id")
                or ""
            )
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
        collection = self._collection_view()
        if not self._client.has_collection(collection):
            return []
        available = self._field_names(collection)
        requested = [field for field in fields if field in available]
        iterator = self._client.query_iterator(
            collection_name=collection,
            batch_size=1_000,
            limit=-1,
            filter="",
            output_fields=requested,
        )
        rows: list[dict[str, Any]] = []
        try:
            while batch := iterator.next():
                rows.extend(batch)
        finally:
            iterator.close()
        return rows

    def _all_chunks(self, *, include_vectors: bool) -> list[StoredChunk]:
        fields = [
            "chunk_id",
            "text",
            "source",
            "section",
            "document_id",
            "content_hash",
            "version",
            "updated_at",
        ]
        if include_vectors:
            fields.extend(["dense", "sparse"])
        rows = self._scan(fields)
        return [
            StoredChunk(
                chunk_id=str(row.get("chunk_id", "")),
                text=str(row.get("text", "")),
                source=str(row.get("source", "")),
                section=str(row.get("section", "")) or None,
                vector=[float(value) for value in row.get("dense", [])],
                sparse={str(key): float(value) for key, value in row.get("sparse", {}).items()},
                document_id=str(row.get("document_id", "")),
                content_hash=str(row.get("content_hash", "")),
                version=int(row.get("version", 1)),
                updated_at=str(row.get("updated_at", "")),
            )
            for row in rows
        ]

    def _ensure_collection(self, dim: int) -> None:
        if self._client.has_collection(self._collection):
            return
        self._create_collection(self._collection, dim)
        self._client.load_collection(collection_name=self._collection)

    def _create_collection(self, name: str, dim: int) -> None:
        from pymilvus import DataType

        schema = self._client.create_schema(auto_id=False)
        schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("text", DataType.VARCHAR, max_length=8192)
        schema.add_field("source", DataType.VARCHAR, max_length=512)
        schema.add_field("section", DataType.VARCHAR, max_length=512)
        schema.add_field("document_id", DataType.VARCHAR, max_length=64)
        schema.add_field("content_hash", DataType.VARCHAR, max_length=64)
        schema.add_field("version", DataType.INT64)
        schema.add_field("updated_at", DataType.VARCHAR, max_length=64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)

        index_params = self._client.prepare_index_params()
        index_params.add_index(field_name="dense", index_type="AUTOINDEX", metric_type="IP")
        index_params.add_index(
            field_name="sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="IP"
        )
        self._client.create_collection(
            collection_name=name, schema=schema, index_params=index_params
        )
        _log.info("milvus.collection_created", collection=name, dim=dim)

    def _insert_chunks(self, collection: str, chunks: list[StoredChunk]) -> None:
        if not chunks:
            return
        has_metadata = _METADATA_FIELDS <= self._field_names(collection)
        rows: list[dict[str, Any]] = []
        for chunk in chunks:
            row: dict[str, Any] = {
                "chunk_id": chunk.chunk_id,
                "text": chunk.text,
                "source": chunk.source,
                "section": chunk.section or "",
                "dense": chunk.vector,
                "sparse": self._to_milvus_sparse(chunk.sparse),
            }
            if has_metadata:
                row.update(
                    document_id=chunk_document_id(chunk),
                    content_hash=chunk.content_hash,
                    version=chunk.version,
                    updated_at=chunk.updated_at,
                )
            rows.append(row)
        self._client.insert(collection_name=collection, data=rows)

    def _field_names(self, collection: str) -> set[str]:
        description = self._client.describe_collection(collection_name=collection)
        return {str(field["name"]) for field in description.get("fields", [])}

    def _collection_dim(self) -> int:
        if not self._client.has_collection(self._collection):
            return 0
        description = self._client.describe_collection(collection_name=self._collection)
        for field in description.get("fields", []):
            if field.get("name") == "dense":
                params = field.get("params", {})
                return int(params.get("dim", field.get("dim", 0)))
        return 0

    def _resolve_active_state(self) -> tuple[str, str | None]:
        if self._is_lite:
            if self._pointer_path is not None and self._pointer_path.exists():
                try:
                    state = json.loads(self._pointer_path.read_text(encoding="utf-8"))
                    target = str(state["collection"])
                    previous = state.get("previous")
                    if self._client.has_collection(target):
                        valid_previous = (
                            str(previous)
                            if previous and self._client.has_collection(str(previous))
                            else self._infer_previous(target)
                        )
                        return target, valid_previous
                except (OSError, ValueError, KeyError, TypeError):
                    _log.warning("milvus.pointer_invalid", path=str(self._pointer_path))
            return self._base_collection, self._infer_previous(self._base_collection)
        try:
            alias = self._client.describe_alias(alias=self._alias)
        except Exception:
            return self._base_collection, self._infer_previous(self._base_collection)
        active = str(
            alias.get("collection") or alias.get("collection_name") or self._base_collection
        )
        return active, self._infer_previous(active)

    def _publish(self, candidate: str, *, previous: str | None) -> None:
        if self._is_lite:
            if self._pointer_path is None:
                raise RuntimeError("Milvus Lite 活动集合指针路径不可用")
            self._pointer_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._pointer_path.with_suffix(f"{self._pointer_path.suffix}.tmp")
            tmp.write_text(
                json.dumps({"collection": candidate, "previous": previous}),
                encoding="utf-8",
            )
            os.replace(tmp, self._pointer_path)
            return
        try:
            self._client.describe_alias(alias=self._alias)
        except Exception:
            self._client.create_alias(collection_name=candidate, alias=self._alias)
        else:
            self._client.alter_alias(collection_name=candidate, alias=self._alias)

    def _cleanup_generations(self, keep: set[str]) -> int:
        """保留活动与上一代索引用于快速回滚，清理更老的内部代际。"""
        removed = 0
        with self._state_lock:
            keep = keep | set(self._active_reads)
        try:
            for name in self._managed_collections():
                if name not in keep:
                    self._client.drop_collection(collection_name=name)
                    removed += 1
        except Exception as exc:
            # 发布已成功，清理失败不能把成功同步伪装成失败；后续更新会重试清理。
            _log.warning("milvus.generation_cleanup_failed", error=str(exc))
        return removed

    def _managed_collections(self) -> list[str]:
        return [
            name
            for name in self._client.list_collections()
            if name == self._base_collection or self._is_generation(name)
        ]

    def _collection_view(self) -> str:
        return getattr(self._read_state, "collection", self._collection)

    def _infer_previous(self, active: str) -> str | None:
        candidates = [name for name in self._managed_collections() if name != active]
        return max(candidates, key=self._generation_sort_key, default=None)

    def _generation_sort_key(self, name: str) -> tuple[int, str]:
        if name == self._base_collection:
            return (0, name)
        suffix = name.removeprefix(f"{self._base_collection}_v")
        if re.fullmatch(r"\d{20}_[0-9a-f]{8}", suffix):
            return (2, suffix)
        return (1, suffix)

    def _is_generation(self, name: str) -> bool:
        prefix = f"{self._base_collection}_v"
        suffix = name.removeprefix(prefix)
        return name.startswith(prefix) and bool(
            re.fullmatch(r"(?:[0-9a-f]{12}|\d{20}_[0-9a-f]{8})", suffix)
        )

    @staticmethod
    def _make_pointer_path(uri: str, collection: str) -> Path | None:
        parsed = urlsplit(uri)
        if parsed.scheme and parsed.scheme != "file":
            return None
        path = Path(parsed.path if parsed.scheme == "file" else uri)
        return path.with_name(f"{path.name}.{collection}.active.json")

    @staticmethod
    def _to_milvus_sparse(sparse: dict[str, float]) -> dict[int, float]:
        if not sparse:
            return dict(_EMPTY_SPARSE_PLACEHOLDER)
        return {int(token_id): float(weight) for token_id, weight in sparse.items()}
