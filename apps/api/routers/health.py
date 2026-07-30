"""健康检查（真正可运行，用于验证服务起得来）。"""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from packages.rag.store import KnowledgeStore
from packages.session.store import SessionStore

from apps.api.deps import kb_store_dep, session_store_dep

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """存活探针。"""
    return {"status": "ok"}


@router.get("/health/ready")
async def readiness(
    store: KnowledgeStore = Depends(kb_store_dep),
    session: SessionStore = Depends(session_store_dep),
) -> dict[str, object]:
    """验证知识库与 SQLite/Memory 控制面，不泄露 URI、路径或密钥。"""
    try:
        workspace_status = await run_in_threadpool(session.readiness_status)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "component": "workspace_store",
                "error": type(exc).__name__,
            },
        ) from exc
    try:
        status = await run_in_threadpool(store.status)
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "status": "not_ready",
                "component": "knowledge_store",
                "error": type(exc).__name__,
            },
        ) from exc
    return {
        "status": "ready",
        "workspace_store": workspace_status,
        "knowledge_store": asdict(status),
    }
