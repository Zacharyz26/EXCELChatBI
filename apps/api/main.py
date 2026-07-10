"""FastAPI 应用入口：`uv run uvicorn apps.api.main:app --reload`。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from packages.common.config import get_settings
from packages.common.logging import configure_logging

from apps.api.deps import embedder_dep, reranker_dep
from apps.api.routers import analyze, chat, health, kb, report, stats, upload


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """启动时初始化结构化日志，并对 RAG 后端配置做 fail-fast 自检。"""
    configure_logging(get_settings().log_level)
    # fail-fast：配置指向未实现的后端（如 rag_embedder=bge）时启动即报错，
    # 而不是服务看似正常、首次检索请求才 500。
    embedder_dep()
    reranker_dep()
    yield


def create_app() -> FastAPI:
    """构建 FastAPI 应用并挂载路由。"""
    app = FastAPI(title="ChatBI API", version="0.1.0", lifespan=lifespan)
    # 本地前端（Vite :5173）跨域，便于联调
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(health.router)
    app.include_router(upload.router)
    app.include_router(analyze.router)
    app.include_router(stats.router)
    app.include_router(report.router)
    app.include_router(kb.router)
    app.include_router(chat.router)
    return app


app = create_app()
