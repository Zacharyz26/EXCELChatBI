"""使用核心依赖验证 API 导入、lifespan 与 readiness。"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from httpx import ASGITransport, AsyncClient


async def _check_app() -> None:
    """显式运行 ASGI lifespan，并通过进程内 HTTP 客户端检查 readiness。"""
    from apps.api.main import app

    async with app.router.lifespan_context(app):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health/ready")
            response.raise_for_status()
            payload = response.json()
            if payload.get("status") != "ready":
                raise RuntimeError(f"unexpected readiness payload: {payload}")


def main() -> None:
    """在隔离临时目录启动应用，并校验默认轻量 RAG 的就绪探针。"""
    with tempfile.TemporaryDirectory(prefix="chatbi-api-smoke-") as temp_dir:
        root = Path(temp_dir)
        settings = {
            "APP_ENV": "development",
            "PROCESS_ROLE": "api",
            "AUTH_MODE": "disabled",
            "AGENT_MCP_TRANSPORT": "in_process",
            "MODEL_REGISTRY_PATH": "config/models.example.yaml",
            "RAG_EMBEDDER": "hashing",
            "RAG_RERANKER": "lexical",
            "RAG_STORE": "local",
            "RAG_RUNTIME_PROFILE": "baseline",
            "CHAT_DB_PATH": str(root / "chatbi.db"),
            "UPLOAD_DIR": str(root / "uploads"),
            "DATASET_DIR": str(root / "datasets"),
            "REPORT_DIR": str(root / "reports"),
            "KB_INDEX_DIR": str(root / "kb-index"),
            "KB_SOURCE_DIR": str(root / "kb-sources"),
            "KB_BACKUP_DIR": str(root / "kb-backups"),
            "WORKSPACE_BACKUP_DIR": str(root / "workspace-backups"),
        }
        os.environ.update(settings)
        asyncio.run(_check_app())

    print("core API startup smoke passed")


if __name__ == "__main__":
    main()
