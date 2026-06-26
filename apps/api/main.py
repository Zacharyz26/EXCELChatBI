"""FastAPI 应用入口：`uv run uvicorn apps.api.main:app --reload`。"""

from __future__ import annotations

from fastapi import FastAPI

from apps.api.routers import chat, health, upload


def create_app() -> FastAPI:
    """构建 FastAPI 应用并挂载路由。"""
    app = FastAPI(title="ChatBI API", version="0.1.0")
    app.include_router(health.router)
    app.include_router(chat.router)
    app.include_router(upload.router)
    return app


app = create_app()
