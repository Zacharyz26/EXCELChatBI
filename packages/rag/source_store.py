"""Persistent source-of-truth storage for knowledge-base documents."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.rag.lifecycle import SourceDocument, content_hash
from packages.rag.store import document_id_for_source

_MANIFEST_FORMAT = 1


@dataclass(frozen=True)
class SourceStoreStatus:
    """Non-sensitive source repository state for operations and readiness."""

    document_count: int
    generation: str


class SourceDocumentStore:
    """Atomic manifest over immutable UTF-8 source blobs.

    The manifest is the only mutable pointer. A failed index build can therefore
    leave a newer source generation waiting to be rebuilt, but it cannot destroy
    the currently serving index or partially publish a source generation.
    """

    def __init__(self, source_dir: str) -> None:
        self._dir = Path(source_dir)
        self._documents_dir = self._dir / "documents"
        self._manifest_path = self._dir / "manifest.json"
        self._lock = threading.RLock()
        self._documents_dir.mkdir(parents=True, exist_ok=True)

    def documents(self) -> list[SourceDocument]:
        """Load and verify the complete current source generation."""
        with self._lock:
            manifest = self._load_manifest()
            documents: list[SourceDocument] = []
            for record in manifest["documents"]:
                source = str(record["source"])
                blob = self._resolve_blob(str(record["blob"]))
                try:
                    text = blob.read_text(encoding="utf-8")
                except (OSError, UnicodeError) as exc:
                    raise RuntimeError(f"知识库原文不可读: {source}") from exc
                if content_hash(text) != record["content_hash"]:
                    raise RuntimeError(f"知识库原文校验失败: {source}")
                documents.append(SourceDocument(source=source, text=text))
            return documents

    def publish(
        self,
        documents: list[SourceDocument],
        *,
        full: bool = False,
    ) -> SourceStoreStatus:
        """Upsert documents or atomically replace the complete source set."""
        normalized = self._normalize(documents)
        with self._lock:
            current = self._load_manifest()
            records = (
                {} if full else {str(item["source"]): dict(item) for item in current["documents"]}
            )
            for document in normalized:
                digest = content_hash(document.text)
                blob_name = self._blob_name(document.source, digest)
                blob_path = self._documents_dir / blob_name
                if not blob_path.exists():
                    self._write_text_atomic(blob_path, document.text)
                records[document.source] = {
                    "source": document.source,
                    "document_id": document_id_for_source(document.source),
                    "content_hash": digest,
                    "blob": blob_name,
                }
            generation = self._new_generation(records)
            next_manifest: dict[str, object] = {
                "format": _MANIFEST_FORMAT,
                "generation": generation,
                "updated_at": datetime.now(UTC).isoformat(),
                "documents": [records[source] for source in sorted(records)],
            }
            self._write_json_atomic(self._manifest_path, next_manifest)
            self._remove_unreferenced_blobs(records)
            return SourceStoreStatus(
                document_count=len(records),
                generation=generation,
            )

    def delete(self, document_id: str) -> bool:
        """Delete one original by stable document ID and publish a new generation."""
        with self._lock:
            current = self._load_manifest()
            records = {
                str(item["source"]): dict(item)
                for item in current["documents"]
                if item["document_id"] != document_id
            }
            if len(records) == len(current["documents"]):
                return False
            generation = self._new_generation(records)
            next_manifest: dict[str, object] = {
                "format": _MANIFEST_FORMAT,
                "generation": generation,
                "updated_at": datetime.now(UTC).isoformat(),
                "documents": [records[source] for source in sorted(records)],
            }
            self._write_json_atomic(self._manifest_path, next_manifest)
            self._remove_unreferenced_blobs(records)
            return True

    def status(self) -> SourceStoreStatus:
        with self._lock:
            manifest = self._load_manifest()
            return SourceStoreStatus(
                document_count=len(manifest["documents"]),
                generation=str(manifest["generation"]),
            )

    def managed_files(self) -> list[Path]:
        """Return only current fact-source files; orphan blobs are rebuild garbage."""
        with self._lock:
            if not self._manifest_path.exists():
                return []
            manifest = self._load_manifest()
            paths = [self._resolve_blob(str(record["blob"])) for record in manifest["documents"]]
            missing = [path.name for path in paths if not path.is_file()]
            if missing:
                raise RuntimeError("知识库原文 blob 缺失")
            return [self._manifest_path, *paths]

    def _load_manifest(self) -> dict[str, Any]:
        if not self._manifest_path.exists():
            return {"format": _MANIFEST_FORMAT, "generation": "empty", "documents": []}
        try:
            payload = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("知识库原文 manifest 不可读") from exc
        if not isinstance(payload, dict) or payload.get("format") != _MANIFEST_FORMAT:
            raise RuntimeError("知识库原文 manifest 格式不受支持")
        generation = payload.get("generation")
        raw_records = payload.get("documents")
        if not isinstance(generation, str) or not generation:
            raise RuntimeError("知识库原文 manifest 缺少 generation")
        if not isinstance(raw_records, list):
            raise RuntimeError("知识库原文 manifest.documents 格式错误")
        records: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in raw_records:
            if not isinstance(raw, dict):
                raise RuntimeError("知识库原文 manifest 记录格式错误")
            source_value = raw.get("source")
            document_id_value = raw.get("document_id")
            content_hash_value = raw.get("content_hash")
            blob_value = raw.get("blob")
            if not all(
                isinstance(value, str) and bool(value)
                for value in (
                    source_value,
                    document_id_value,
                    content_hash_value,
                    blob_value,
                )
            ):
                raise RuntimeError("知识库原文 manifest 记录字段错误")
            assert isinstance(source_value, str)
            assert isinstance(document_id_value, str)
            assert isinstance(content_hash_value, str)
            assert isinstance(blob_value, str)
            record = {
                "source": source_value,
                "document_id": document_id_value,
                "content_hash": content_hash_value,
                "blob": blob_value,
            }
            source = record["source"]
            if source in seen or record["document_id"] != document_id_for_source(source):
                raise RuntimeError("知识库原文 manifest 来源重复或 ID 不匹配")
            self._resolve_blob(record["blob"])
            seen.add(source)
            records.append(record)
        return {**payload, "documents": records}

    def _resolve_blob(self, name: str) -> Path:
        if not name or Path(name).name != name or not name.endswith(".txt"):
            raise RuntimeError("知识库原文 blob 路径非法")
        path = (self._documents_dir / name).resolve()
        if path.parent != self._documents_dir.resolve():
            raise RuntimeError("知识库原文 blob 超出受控目录")
        return path

    @staticmethod
    def _normalize(documents: list[SourceDocument]) -> list[SourceDocument]:
        normalized: list[SourceDocument] = []
        seen: set[str] = set()
        for document in documents:
            source = document.source.strip()
            if not source:
                raise ValueError("知识库文档 source 不能为空")
            if source in seen:
                raise ValueError(f"知识库文档来源重复: {source}")
            seen.add(source)
            normalized.append(SourceDocument(source=source, text=document.text))
        return normalized

    @staticmethod
    def _blob_name(source: str, digest: str) -> str:
        source_digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        return f"{source_digest}-{digest}.txt"

    @staticmethod
    def _new_generation(records: dict[str, dict[str, object]]) -> str:
        fingerprint = json.dumps(
            [(source, records[source]["content_hash"]) for source in sorted(records)],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()[:20]

    def _remove_unreferenced_blobs(self, records: dict[str, dict[str, object]]) -> None:
        referenced = {str(record["blob"]) for record in records.values()}
        for path in self._documents_dir.glob("*.txt"):
            if path.name not in referenced:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # Garbage collection is not part of the atomic publish result.
                    # A later publish or backup can safely ignore the orphan blob.
                    continue

    @staticmethod
    def _write_text_atomic(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
        temporary = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
