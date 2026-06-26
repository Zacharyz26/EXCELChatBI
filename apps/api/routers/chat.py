"""对话接口：SSE 流式（设计文档 5.1 / 第7节流式协同）。"""

from __future__ import annotations

from fastapi import APIRouter

from apps.api.schemas import ChatRequest

router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("")
async def chat(req: ChatRequest) -> object:
    """接收对话请求，经编排层处理后以 SSE 流式返回（token / 中间步骤 / 图表）。"""
    raise NotImplementedError(
        "TODO: 指代消解 → orchestrator 路由 → 模型/工具调用 → EventSourceResponse 流式推送"
    )
