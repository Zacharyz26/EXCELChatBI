"""对话接口：对话式 Agent 的 SSE 流式入口（阶段3，设计文档 14.5）。

/chat/stream 即 Agent 循环：模型自动规划并调用注册表工具（画像/统计/图表/
变换/聚合/检索/报告），SSE 透明度事件见 14.5.3。红线1 按 13.5 助手通道例外
执行（免白名单门控，数据物料留审计日志）；红线2/3 由循环与注册表强制。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from mcp_servers.common.base_server import MCPServer
from packages.common.config import Settings
from packages.models.gateway import ModelGateway
from packages.rag.retriever import HybridRetriever
from packages.session.store import SessionStore
from sse_starlette.sse import EventSourceResponse

from apps.api.deps import (
    chart_tools_dep,
    dataset_ops_tools_dep,
    excel_tools_dep,
    model_gateway_dep,
    report_tools_dep,
    retriever_dep,
    session_store_dep,
    settings_dep,
    stats_tools_dep,
)
from apps.api.schemas import ChatStreamRequest
from apps.orchestrator.agent_loop import (
    AgentLoopConfig,
    ConversationLockPool,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import AgentContext, build_registry

router = APIRouter(prefix="/chat", tags=["chat"])
_conversation_locks = ConversationLockPool()


@router.post("/stream", response_class=EventSourceResponse)
async def chat_stream(
    req: ChatStreamRequest,
    store: SessionStore = Depends(session_store_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
    settings: Settings = Depends(settings_dep),
    excel: MCPServer = Depends(excel_tools_dep),
    stats: MCPServer = Depends(stats_tools_dep),
    chart: MCPServer = Depends(chart_tools_dep),
    dataset_ops: MCPServer = Depends(dataset_ops_tools_dep),
    report: MCPServer = Depends(report_tools_dep),
    retriever: HybridRetriever = Depends(retriever_dep),
) -> EventSourceResponse:
    """对话式 Agent 一轮对话：规划 → 工具调用 → 流式回答（SSE）。"""
    conversation = await run_in_threadpool(store.get_conversation, req.conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    registry = build_registry(
        excel=excel,
        stats=stats,
        chart=chart,
        dataset_ops=dataset_ops,
        report=report,
        retriever=retriever,
        context=AgentContext(
            store=store,
            project_id=conversation.project_id,
            conversation_id=conversation.id,
        ),
    )
    config = AgentLoopConfig(
        history_limit=settings.chat_history_limit,
        profile_max_chars=settings.chat_profile_max_chars,
        max_tool_calls=settings.agent_max_tool_calls,
        tool_result_max_chars=settings.agent_tool_result_max_chars,
        registry_max_entries=settings.agent_registry_max_entries,
    )
    return EventSourceResponse(
        stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text=req.message,
            store=store,
            gateway=gateway,
            registry=registry,
            locks=_conversation_locks,
            config=config,
        ),
        ping=15,
    )
