#!/usr/bin/env python3
"""知识库增量同步/全量原子重建命令。"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.common.config import get_settings  # noqa: E402
from packages.rag.embedding import BGEEmbedder, HashingEmbedder  # noqa: E402
from packages.rag.lifecycle import load_text_documents, sync_documents  # noqa: E402
from packages.rag.source_store import SourceDocumentStore  # noqa: E402
from packages.rag.store import KnowledgeStore, LocalKnowledgeStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="同步或重建 ChatBI 知识库")
    parser.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    parser.add_argument(
        "--path",
        help="受信导入文件或目录；省略时从 KB_SOURCE_DIR 的持久原文重建",
    )
    args = parser.parse_args()

    settings = get_settings()
    source_store = SourceDocumentStore(settings.kb_source_dir)
    if args.path:
        path = Path(args.path).resolve()
        if not path.exists():
            parser.error(f"路径不存在: {path}")
        source_root = path if path.is_dir() else path.parent
        documents = load_text_documents(
            path,
            source_root=source_root,
            max_files=settings.kb_max_files,
            max_document_chars=settings.kb_max_document_chars,
        )
        source_store.publish(documents, full=args.mode == "full")
    else:
        source_status = source_store.status()
        documents = source_store.documents()
        if source_status.generation == "empty":
            path = Path(settings.kb_docs_dir).resolve()
            if not path.exists():
                parser.error(
                    "KB_SOURCE_DIR 为空且初始 KB_DOCS_DIR 不存在: " f"{settings.kb_docs_dir}"
                )
            documents = load_text_documents(
                path,
                source_root=path if path.is_dir() else path.parent,
                max_files=settings.kb_max_files,
                max_document_chars=settings.kb_max_document_chars,
            )
            source_store.publish(documents, full=True)
    embedder = (
        BGEEmbedder(
            settings.embedding_model,
            device=settings.embedding_device,
            cache_dir=settings.model_cache_dir,
        )
        if settings.rag_embedder == "bge"
        else HashingEmbedder(dim=settings.embedding_dim)
    )
    store: KnowledgeStore
    if settings.rag_store == "milvus":
        from packages.rag.milvus_store import MilvusKnowledgeStore

        store = MilvusKnowledgeStore(
            settings.milvus_uri,
            collection=settings.milvus_collection,
            token=settings.milvus_token,
        )
    else:
        store = LocalKnowledgeStore(settings.kb_index_dir)
    try:
        result = sync_documents(documents, embedder, store, full=args.mode == "full")
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
