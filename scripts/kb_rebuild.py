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
from packages.rag.store import KnowledgeStore, LocalKnowledgeStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="同步或重建 ChatBI 知识库")
    parser.add_argument("--mode", choices=("incremental", "full"), default="incremental")
    parser.add_argument("--path", help="文档文件或目录；默认读取 KB_DOCS_DIR")
    args = parser.parse_args()

    settings = get_settings()
    path = Path(args.path or settings.kb_docs_dir).resolve()
    if not path.exists():
        parser.error(f"路径不存在: {path}")
    source_root = path if path.is_dir() else path.parent
    documents = load_text_documents(
        path,
        source_root=source_root,
        max_files=settings.kb_max_files,
        max_document_chars=settings.kb_max_document_chars,
    )
    embedder = (
        BGEEmbedder(settings.embedding_model, device=settings.embedding_device)
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
        result = sync_documents(
            documents, embedder, store, full=args.mode == "full"
        )
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
