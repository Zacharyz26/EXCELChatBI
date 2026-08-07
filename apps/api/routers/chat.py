"""对话接口：对话式 Agent 的 SSE 流式入口（阶段3，设计文档 14.5）。

/chat/stream 即 Agent 循环：模型自动规划并调用注册表工具（画像/统计/图表/
变换/聚合/检索/报告），SSE 透明度事件见 14.5.3。红线1 按 13.5 助手通道例外
执行（免白名单门控，数据物料留审计日志）；红线2/3 由循环与注册表强制。
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from mcp_servers.common.base_server import MCPServer
from packages.common.config import Settings
from packages.governance.permissions import Principal
from packages.models.gateway import ModelGateway
from packages.rag.retriever import HybridRetriever
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from sse_starlette.sse import EventSourceResponse

from apps.api.auth import current_principal_dep
from apps.api.authz import require_conversation_access, require_run_access
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
from apps.api.run_host import agent_run_manager, conversation_locks
from apps.api.schemas import ChatStreamRequest
from apps.orchestrator.agent_loop import (
    AgentLoopConfig,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import (
    AgentContext,
    build_registry,
    enabled_capability_profiles_from_settings,
    mcp_client_config_from_settings,
)

router = APIRouter(prefix="/chat", tags=["chat"])


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
    principal: Principal = Depends(current_principal_dep),
) -> EventSourceResponse:
    """对话式 Agent 一轮对话：规划 → 工具调用 → 流式回答（SSE）。"""
    conversation = await run_in_threadpool(
        require_conversation_access,
        store,
        req.conversation_id,
        principal,
        write=True,
    )
    if req.parent_run_id is not None:
        parent = await run_in_threadpool(
            TaskStore(store.db_path).get_run,
            req.parent_run_id,
        )
        if parent is None:
            raise HTTPException(status_code=404, detail="父任务不存在")
        require_run_access(store, parent, principal)
        if (
            parent.project_id != conversation.project_id
            or parent.conversation_id != conversation.id
        ):
            raise HTTPException(status_code=404, detail="父任务不存在")
        if parent.status not in {"completed", "blocked", "failed", "cancelled"}:
            raise HTTPException(status_code=409, detail="只能从终态任务创建分析分支")

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
            subject_id=principal.user_id,
        ),
        mcp_config=mcp_client_config_from_settings(settings),
        enabled_capability_profiles=enabled_capability_profiles_from_settings(settings),
    )
    config = AgentLoopConfig(
        history_limit=settings.chat_history_limit,
        profile_max_chars=settings.chat_profile_max_chars,
        compaction_trigger_chars=settings.chat_compaction_trigger_chars,
        compaction_keep_recent=settings.chat_compaction_keep_recent,
        compaction_summary_max_chars=settings.chat_compaction_summary_max_chars,
        compaction_message_max_chars=settings.chat_compaction_message_max_chars,
        max_tool_calls=settings.agent_max_tool_calls,
        tool_result_max_chars=settings.agent_tool_result_max_chars,
        registry_max_entries=settings.agent_registry_max_entries,
        run_timeout_seconds=settings.agent_run_timeout_seconds,
        model_timeout_seconds=settings.agent_model_timeout_seconds,
        tool_timeout_seconds=settings.agent_tool_timeout_seconds,
        approval_ttl_seconds=settings.agent_approval_ttl_seconds,
        max_parallel_tools=settings.agent_max_parallel_tools,
    )
    run_id = uuid.uuid4().hex
    subscription = agent_run_manager.start(
        run_id,
        lambda control: stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text=req.message,
            store=store,
            gateway=gateway,
            registry=registry,
            locks=conversation_locks,
            config=config,
            planner_gateway=gateway,
            principal=principal,
            run_id=run_id,
            control=control,
            autonomy_mode=req.autonomy_mode,
            parent_run_id=req.parent_run_id,
        ),
    )
    return EventSourceResponse(
        subscription,
        ping=15,
        headers={"X-ChatBI-Run-ID": run_id},
    )
