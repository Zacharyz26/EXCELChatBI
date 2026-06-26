"""健康检查（真正可运行，用于验证服务起得来）。"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    """存活探针。"""
    return {"status": "ok"}
