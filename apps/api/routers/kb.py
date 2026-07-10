"""知识库接口（F1）：摄入 /kb/ingest 与问答 /kb/query。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from packages.common.config import Settings
from packages.models.gateway import ModelGateway
from packages.rag.embedding import Embedder
from packages.rag.pipeline import chunk_and_embed
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
    IngestRequest,
    IngestResponse,
    KBOverviewResponse,
    KBQueryRequest,
    KBQueryResponse,
)
from apps.orchestrator.kb_qa import answer_question

router = APIRouter(prefix="/kb", tags=["kb"])

_TEXT_SUFFIXES = {".md", ".txt", ".markdown"}


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    embedder: Embedder = Depends(embedder_dep),
    store: KnowledgeStore = Depends(kb_store_dep),
    settings: Settings = Depends(settings_dep),
) -> IngestResponse:
    """摄入文档：内联文本或路径（文件/目录，先支持 .md/.txt）。"""
    # 读盘 + 分块 + embedding 是阻塞重活 → 线程池，不卡事件循环
    docs = await run_in_threadpool(_collect_docs, req, settings.kb_docs_dir)
    if not docs:
        raise HTTPException(status_code=400, detail="未提供可摄入内容（path 或 text）")

    def _ingest_all() -> int:
        added = 0
        for source, text in docs:
            added += store.add(chunk_and_embed(text, source, embedder))
        return added

    added = await run_in_threadpool(_ingest_all)
    return IngestResponse(ingested_docs=len(docs), chunks=added, total_chunks=store.count())


@router.get("/overview", response_model=KBOverviewResponse)
async def overview(
    store: KnowledgeStore = Depends(kb_store_dep),
) -> KBOverviewResponse:
    """知识库概览：片段数、来源文件、主题（小节标题）——供前端展示与派生示例问题。"""
    return KBOverviewResponse(
        chunk_count=store.count(),
        sources=store.sources(),
        topics=store.topics(),
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
    except Exception as exc:  # 生成失败友好降级（红线：不静默吞异常）
        raise HTTPException(
            status_code=502, detail=f"生成失败（检查 DEEPSEEK_API_KEY 与网络）：{exc}"
        ) from exc
    return KBQueryResponse(
        answer=result["answer"],
        citations=[Citation(**c) for c in result["citations"]],
        is_empty=result["is_empty"],
    )


def _collect_docs(req: IngestRequest, kb_docs_dir: str) -> list[tuple[str, str]]:
    """把请求归一为 [(source, text)] 列表。

    path 摄入限定在配置的知识库目录 `kb_docs_dir` 白名单内，禁止任意服务端路径。
    """
    if req.text:
        return [(req.source or "inline", req.text)]
    if not req.path:
        return []
    base = Path(kb_docs_dir).resolve()
    p = Path(req.path).resolve()
    # 先做白名单校验（越界一律 403，不泄露越界路径是否存在）
    if p != base and base not in p.parents:
        raise HTTPException(
            status_code=403, detail=f"path 超出允许的知识库目录: {kb_docs_dir}"
        )
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {req.path}")
    files = (
        [f for f in sorted(p.rglob("*")) if f.suffix.lower() in _TEXT_SUFFIXES]
        if p.is_dir()
        else [p]
    )
    docs: list[tuple[str, str]] = []
    for f in files:
        if f.suffix.lower() not in _TEXT_SUFFIXES:
            raise HTTPException(status_code=400, detail=f"暂仅支持纯文本 .md/.txt: {f.name}")
        docs.append((f.name, f.read_text(encoding="utf-8")))
    return docs
