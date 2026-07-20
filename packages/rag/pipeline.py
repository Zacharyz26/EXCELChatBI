"""RAG 摄入管线：文档文本 → 分块 → 分词 + 向量 → 可入库的 StoredChunk。"""

from __future__ import annotations

from packages.rag.chunking import split_document
from packages.rag.embedding import Embedder
from packages.rag.store import StoredChunk
from packages.rag.tokenizer import tokenize


def chunk_and_embed(text: str, source: str, embedder: Embedder) -> list[StoredChunk]:
    """把一篇文档切块，并为每块生成分词与稠密/稀疏表示，返回可入库的 StoredChunk。

    bge-m3 后端单次编码同时产出稀疏 lexical weights（决策1）；
    替身后端 sparse 为空，检索时走中文 BM25 备路。

    Args:
        text: 文档全文。
        source: 来源标识（供引用，红线6）。
        embedder: 向量器。
    """
    chunks = split_document(text, source)
    if not chunks:
        return []
    vectors, sparse_vectors = embedder.embed_with_sparse([c.text for c in chunks])
    if sparse_vectors is None:
        sparse_vectors = [{} for _ in chunks]
    return [
        StoredChunk(
            text=c.text,
            source=c.source,
            section=c.section,
            tokens=tokenize(c.text),
            vector=vec,
            sparse=sparse,
        )
        for c, vec, sparse in zip(chunks, vectors, sparse_vectors, strict=True)
    ]
