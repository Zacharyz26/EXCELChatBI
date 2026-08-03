"""Entrypoint for one of the five independently routed Agent MCP services."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from apps.orchestrator.agent_tools import (
    AgentContext,
    AgentToolRegistry,
    build_registry,
)
from packages.common.config import Settings, get_settings
from packages.common.logging import get_logger
from packages.knowledge.domain_store import DomainDefinitionStore
from packages.rag.embedding import BGEEmbedder, Embedder, HashingEmbedder
from packages.rag.rerank import BGEReranker, LexicalReranker, Reranker
from packages.rag.retriever import HybridRetriever
from packages.rag.store import LocalKnowledgeStore
from packages.session.store import SessionStore

from mcp_servers.chart.server import build_server as build_chart_server
from mcp_servers.common.adapter import MCPServerAdapter, MCPToolBinding
from mcp_servers.common.client_gateway import MCPClientConfig
from mcp_servers.common.contracts import (
    MCPProtocolError,
    MCPRequestContext,
)
from mcp_servers.common.sdk_adapter import run_adapter
from mcp_servers.common.service_catalog import (
    AGENT_MCP_SERVICE_TOOLS,
    AGENT_MCP_SERVICES,
)
from mcp_servers.dataset_ops.server import build_server as build_dataset_ops_server
from mcp_servers.excel_parser.server import build_server as build_excel_server
from mcp_servers.report.server import build_server as build_report_server
from mcp_servers.stats.server import build_server as build_stats_server

_log = get_logger("mcp.agent_service")


class AgentServiceRuntime:
    """Own deterministic dependencies and enforce server-side resource scope."""

    def __init__(self, service_name: str, settings: Settings) -> None:
        if service_name not in AGENT_MCP_SERVICE_TOOLS:
            raise RuntimeError(
                f"MCP_AGENT_SERVICE 必须是: {', '.join(AGENT_MCP_SERVICES)}"
            )
        self.service_name = service_name
        self.settings = settings
        self.store = SessionStore(
            settings.chat_db_path,
            cache_size=settings.conversation_cache_size,
            read_only=True,
        )
        self.definitions = DomainDefinitionStore(self.store, read_only=True)
        self.excel = build_excel_server()
        self.stats = build_stats_server()
        self.chart = build_chart_server()
        self.dataset_ops = build_dataset_ops_server()
        self.report = build_report_server()
        self.retriever = self._build_retriever()
        canonical = self._registry(context=None)
        descriptors = {
            descriptor.name: descriptor
            for descriptor in canonical.mcp_descriptors()
        }
        bindings = [
            MCPToolBinding(
                descriptor=descriptors[tool_name],
                context_handler=self._handler(tool_name),
            )
            for tool_name in AGENT_MCP_SERVICE_TOOLS[service_name]
        ]
        self.adapter = MCPServerAdapter(service_name, bindings)

    def _handler(
        self,
        tool_name: str,
    ) -> Callable[[dict[str, Any], MCPRequestContext], Any]:
        def execute(
            arguments: dict[str, Any],
            context: MCPRequestContext,
        ) -> Any:
            return self.execute(tool_name, arguments, context)

        return execute

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        request_context: MCPRequestContext,
    ) -> Any:
        """Authorize signed Host context and invoke the canonical deterministic runner."""
        self._authorize(request_context, arguments)
        _log.info(
            "mcp.service_call",
            service=self.service_name,
            tool=tool_name,
            run_id=request_context.run_id,
            invocation_id=request_context.invocation_id,
            trace_id=request_context.trace_id,
            project_id=request_context.project_id,
        )
        # Lineage remains a Host-owned database commit. Only report assembly needs
        # conversation artifacts inside the service.
        agent_context = (
            AgentContext(
                store=self.store,
                project_id=request_context.project_id,
                conversation_id=request_context.conversation_id,
                subject_id=request_context.subject_id,
            )
            if self.service_name in {"report-tools", "knowledge-tools"}
            else None
        )
        registry = self._registry(context=agent_context)
        return registry.execute(tool_name, json.dumps(arguments, ensure_ascii=False))

    def readiness(self) -> tuple[bool, dict[str, Any]]:
        try:
            schema_version = self.store.schema_version
            if schema_version <= 0:
                raise RuntimeError("SQLite schema 未初始化")
            if self.service_name == "knowledge-tools":
                if not self.retriever.store.status().ready:
                    raise RuntimeError("知识存储未就绪")
            return True, {
                "tool_count": len(self.adapter.names),
                "storage": "ready",
            }
        except Exception as exc:
            return False, {
                "tool_count": len(self.adapter.names),
                "storage": "not_ready",
                "reason": type(exc).__name__,
            }

    def _authorize(
        self,
        context: MCPRequestContext,
        arguments: dict[str, Any],
    ) -> None:
        project = self.store.get_project(context.project_id)
        conversation = self.store.get_conversation(context.conversation_id)
        if project is None or conversation is None:
            raise MCPProtocolError("resource_not_found", "MCP 上下文资源不存在")
        if conversation.project_id != context.project_id:
            raise MCPProtocolError(
                "project_scope_violation",
                "MCP 对话不属于请求项目",
            )
        dataset_ref = arguments.get("dataset_ref")
        if isinstance(dataset_ref, str):
            dataset = self.store.get_dataset(dataset_ref)
            if dataset is None:
                raise MCPProtocolError("resource_not_found", "数据集未登记")
            if dataset.project_id != context.project_id:
                raise MCPProtocolError(
                    "project_scope_violation",
                    "数据集不属于请求项目",
                )

    def _registry(self, *, context: AgentContext | None) -> AgentToolRegistry:
        return build_registry(
            excel=self.excel,
            stats=self.stats,
            chart=self.chart,
            dataset_ops=self.dataset_ops,
            report=self.report,
            retriever=self.retriever,
            context=context,
            mcp_config=MCPClientConfig(),
            definition_store=self.definitions,
        )

    def _build_retriever(self) -> HybridRetriever:
        settings = self.settings
        if self.service_name != "knowledge-tools":
            return HybridRetriever(
                HashingEmbedder(dim=settings.embedding_dim),
                LocalKnowledgeStore("/tmp/chatbi-unused-kb"),
                LexicalReranker(),
            )
        if settings.rag_embedder == "bge":
            embedder: Embedder = BGEEmbedder(
                settings.embedding_model,
                device=settings.embedding_device,
            )
        else:
            embedder = HashingEmbedder(dim=settings.embedding_dim)
        if settings.rag_reranker == "bge":
            reranker: Reranker = BGEReranker(
                settings.rerank_model,
                device=settings.embedding_device,
            )
        else:
            reranker = LexicalReranker()
        if settings.rag_store != "local":
            raise RuntimeError(
                "阶段 2E 默认服务入口仅启用 local RAG；Milvus profile 由后续阶段接入"
            )
        return HybridRetriever(
            embedder,
            LocalKnowledgeStore(settings.kb_index_dir),
            reranker,
            min_relevance=settings.rag_min_relevance,
        )


def main() -> None:
    if os.getenv("PROCESS_ROLE", "mcp_server") != "mcp_server":
        raise RuntimeError("独立 MCP 服务必须设置 PROCESS_ROLE=mcp_server")
    settings = get_settings()
    service_name = os.getenv("MCP_AGENT_SERVICE", "").strip()
    runtime = AgentServiceRuntime(service_name, settings)
    _ensure_runtime_paths(runtime)
    run_adapter(
        runtime.adapter,
        default_port=8000,
        readiness_check=runtime.readiness,
    )


def _ensure_runtime_paths(runtime: AgentServiceRuntime) -> None:
    settings = runtime.settings
    paths = [Path(settings.dataset_dir)]
    if runtime.service_name in {"chart-tools", "report-tools"}:
        paths.append(Path(settings.report_dir))
    if runtime.service_name == "knowledge-tools":
        paths.append(Path(settings.kb_index_dir))
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    main()
