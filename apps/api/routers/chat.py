"""对话接口：SSE 流式（设计文档 5.1 / 第7节流式协同）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from packages.common.config import Settings
from packages.models.gateway import ModelGateway
from packages.session.store import SessionStore
from sse_starlette.sse import EventSourceResponse

from apps.api.deps import model_gateway_dep, session_store_dep, settings_dep
from apps.api.schemas import ChatRequest, ChatStreamRequest
from apps.orchestrator.chat_stream import ConversationLockPool, stream_plain_chat

router = APIRouter(prefix="/chat", tags=["chat"])
_conversation_locks = ConversationLockPool()


@router.post("/stream", response_class=EventSourceResponse)
async def chat_stream(
    req: ChatStreamRequest,
    store: SessionStore = Depends(session_store_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
    settings: Settings = Depends(settings_dep),
) -> EventSourceResponse:
    """基于持久化历史进行纯 LLM 文本对话，以 SSE 返回增量。"""
    conversation = await run_in_threadpool(store.get_conversation, req.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return EventSourceResponse(
        stream_plain_chat(
            conversation_id=req.conversation_id,
            user_text=req.message,
            store=store,
            gateway=gateway,
            locks=_conversation_locks,
            history_limit=settings.chat_history_limit,
            profile_max_chars=settings.chat_profile_max_chars,
        ),
        ping=15,
    )


@router.post("")
async def chat(req: ChatRequest) -> object:
    """接收对话请求，经编排层处理后以 SSE 流式返回（token / 中间步骤 / 图表）。"""
    raise NotImplementedError(
        "TODO: 指代消解 → orchestrator 路由 → 模型/工具调用 → EventSourceResponse 流式推送"
    )
