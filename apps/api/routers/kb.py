"""知识库接口：文档同步、生命周期管理与带引用问答。"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from packages.common.config import Settings
from packages.models.gateway import ModelGateway
from packages.rag.embedding import Embedder
from packages.rag.lifecycle import (
    SourceDocument,
    SyncResult,
    load_text_documents,
    sync_documents,
)
from packages.rag.retriever import HybridRetriever
from packages.rag.store import KnowledgeStore

from apps.api.deps import (
    embedder_dep,
    kb_store_dep,
    model_gateway_dep,
    retriever_dep,
    settings_dep,
)
from apps.api.schemas import (
    Citation,
    DeleteDocumentResponse,
    IngestRequest,
    IngestResponse,
    KBDocumentResponse,
    KBOverviewResponse,
    KBQueryRequest,
    KBQueryResponse,
    RebuildRequest,
)
from apps.orchestrator.kb_qa import answer_question

router = APIRouter(prefix="/kb", tags=["kb"])


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    embedder: Embedder = Depends(embedder_dep),
    store: KnowledgeStore = Depends(kb_store_dep),
    settings: Settings = Depends(settings_dep),
) -> IngestResponse:
    """增量同步文档：内容未变时跳过，内容变化时按 source 替换并递增版本。"""
    documents = await run_in_threadpool(_collect_docs, req, settings)
    if not documents:
        raise HTTPException(status_code=400, detail="路径内没有可摄入的文本文件")
    result = await run_in_threadpool(sync_documents, documents, embedder, store)
    return _sync_response(result)


@router.post("/rebuild", response_model=IngestResponse)
async def rebuild(
    req: RebuildRequest,
    embedder: Embedder = Depends(embedder_dep),
    store: KnowledgeStore = Depends(kb_store_dep),
    settings: Settings = Depends(settings_dep),
) -> IngestResponse:
    """从目录完整构建新索引，准备成功后原子替换活动索引。"""
    ingest_req = IngestRequest(path=req.path or settings.kb_docs_dir)
    documents = await run_in_threadpool(_collect_docs, ingest_req, settings)
    result = await run_in_threadpool(
        sync_documents, documents, embedder, store, full=True
    )
    return _sync_response(result)


@router.delete("/documents/{document_id}", response_model=DeleteDocumentResponse)
async def delete_document(
    document_id: str,
    store: KnowledgeStore = Depends(kb_store_dep),
) -> DeleteDocumentResponse:
    """按稳定文档 ID 删除来源及其所有片段。"""
    removed = await run_in_threadpool(store.delete_document, document_id)
    if removed == 0:
        raise HTTPException(status_code=404, detail="知识库文档不存在")
    return DeleteDocumentResponse(document_id=document_id, removed_chunks=removed)


@router.get("/overview", response_model=KBOverviewResponse)
async def overview(
    store: KnowledgeStore = Depends(kb_store_dep),
) -> KBOverviewResponse:
    """知识库概览与可管理文档清单。"""
    count, sources, topics, documents = await run_in_threadpool(
        lambda: (store.count(), store.sources(), store.topics(), store.documents())
    )
    return KBOverviewResponse(
        chunk_count=count,
        sources=sources,
        topics=topics,
        documents=[KBDocumentResponse(**asdict(item)) for item in documents],
    )


@router.post("/query", response_model=KBQueryResponse)
async def query(
    req: KBQueryRequest,
    retriever: HybridRetriever = Depends(retriever_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
) -> KBQueryResponse:
    """中文提问 → 检索 → 带引用生成 / 诚实无答。"""
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="问题不能为空")
    try:
        result = await answer_question(req.question, retriever, gateway, top_k=req.top_k)
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"生成失败（检查 DEEPSEEK_API_KEY 与网络）：{exc}"
        ) from exc
    return KBQueryResponse(
        answer=result["answer"],
        citations=[Citation(**citation) for citation in result["citations"]],
        is_empty=result["is_empty"],
    )


def _collect_docs(req: IngestRequest, settings: Settings) -> list[SourceDocument]:
    """把请求归一为文档列表，并限制服务端路径、文件数与单文档大小。"""
    if req.text is not None:
        if len(req.text) > settings.kb_max_document_chars:
            raise HTTPException(
                status_code=413,
                detail=f"文档超过字符上限 {settings.kb_max_document_chars}",
            )
        return [SourceDocument(source=req.source or "inline", text=req.text)]

    base = Path(settings.kb_docs_dir).resolve()
    path = Path(req.path or settings.kb_docs_dir).resolve()
    if path != base and base not in path.parents:
        raise HTTPException(
            status_code=403, detail=f"path 超出允许的知识库目录: {settings.kb_docs_dir}"
        )
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {req.path}")
    try:
        return load_text_documents(
            path,
            source_root=base,
            max_files=settings.kb_max_files,
            max_document_chars=settings.kb_max_document_chars,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sync_response(result: SyncResult) -> IngestResponse:
    """把生命周期结果映射为兼容原字段的 API 响应。"""
    return IngestResponse(
        ingested_docs=result.documents,
        chunks=result.chunks,
        total_chunks=result.total_chunks,
        created=result.created,
        updated=result.updated,
        skipped=result.skipped,
        deleted=result.deleted,
    )
