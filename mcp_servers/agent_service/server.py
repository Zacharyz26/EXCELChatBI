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
from packages.knowledge.domain_models import DomainDefinition
from packages.knowledge.domain_store import DomainAccessDenied, DomainDefinitionStore
from packages.rag.embedding import BGEEmbedder, Embedder, HashingEmbedder
from packages.rag.rerank import BGEReranker, LexicalReranker, Reranker
from packages.rag.retriever import HybridRetriever
from packages.rag.store import KnowledgeStore, LocalKnowledgeStore
from packages.session.store import SessionStore

from mcp_servers.chart.server import build_server as build_chart_server
from mcp_servers.common.adapter import MCPServerAdapter, MCPToolBinding
from mcp_servers.common.client_gateway import MCPClientConfig
from mcp_servers.common.contracts import (
    MCPProtocolError,
    MCPRequestContext,
    MCPResourceContents,
    MCPResourceDescriptor,
    stable_hash,
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


class DomainDefinitionResourceProvider:
    """Expose versioned definitions without making the service token an identity."""

    catalog_uri_prefix = "chatbi://domain-definitions/catalog/"

    @classmethod
    def catalog_uri(cls, project_id: str) -> str:
        return f"{cls.catalog_uri_prefix}{project_id}"

    def __init__(
        self,
        sessions: SessionStore,
        definitions: DomainDefinitionStore,
    ) -> None:
        self._sessions = sessions
        self._definitions = definitions

    def list_resources(
        self,
        context: MCPRequestContext,
    ) -> tuple[MCPResourceDescriptor, ...]:
        self._require_conversation_scope(context)
        definitions = self._definitions.list_definitions_for_subject(
            project_id=context.project_id,
            subject_id=context.subject_id,
        )
        return (
            _definition_catalog_descriptor(context.project_id, definitions),
            *(_definition_descriptor(item) for item in definitions),
        )

    def read_resource(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceContents:
        self._require_conversation_scope(context)
        if uri == self.catalog_uri(context.project_id):
            definitions = self._definitions.list_definitions_for_subject(
                project_id=context.project_id,
                subject_id=context.subject_id,
            )
            catalog_version = _definition_catalog_version(definitions)
            return MCPResourceContents(
                uri=self.catalog_uri(context.project_id),
                text=_serialize_definition_catalog(
                    context.project_id,
                    definitions,
                    catalog_version=catalog_version,
                ),
                metadata={
                    "com.chatbi/resource-kind": "domain-definition-catalog",
                    "com.chatbi/catalog-version": catalog_version,
                },
            )
        definition = self._definitions.get_resource_for_subject(
            project_id=context.project_id,
            resource_uri=uri,
            subject_id=context.subject_id,
        )
        if definition is None:
            raise FileNotFoundError("领域定义 Resource 不存在")
        return MCPResourceContents(
            uri=definition.resource_uri,
            text=_serialize_definition(definition),
            metadata=_definition_resource_metadata(definition),
        )

    def _require_conversation_scope(self, context: MCPRequestContext) -> None:
        project = self._sessions.get_project(context.project_id)
        conversation = self._sessions.get_conversation(context.conversation_id)
        if project is None or conversation is None or conversation.project_id != context.project_id:
            raise DomainAccessDenied("领域定义项目不存在")


class AgentServiceRuntime:
    """Own deterministic dependencies and enforce server-side resource scope."""

    def __init__(self, service_name: str, settings: Settings) -> None:
        if service_name not in AGENT_MCP_SERVICE_TOOLS:
            raise RuntimeError(f"MCP_AGENT_SERVICE 必须是: {', '.join(AGENT_MCP_SERVICES)}")
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
        descriptors = {descriptor.name: descriptor for descriptor in canonical.mcp_descriptors()}
        bindings = [
            MCPToolBinding(
                descriptor=descriptors[tool_name],
                context_handler=self._handler(tool_name),
            )
            for tool_name in AGENT_MCP_SERVICE_TOOLS[service_name]
        ]
        resource_provider = (
            DomainDefinitionResourceProvider(self.store, self.definitions)
            if service_name == "knowledge-tools"
            else None
        )
        self.adapter = MCPServerAdapter(
            service_name,
            bindings,
            resource_provider=resource_provider,
        )

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
                cache_dir=settings.model_cache_dir,
            )
        else:
            embedder = HashingEmbedder(dim=settings.embedding_dim)
        if settings.rag_reranker == "bge":
            reranker: Reranker = BGEReranker(
                settings.rerank_model,
                device=settings.embedding_device,
                cache_dir=settings.model_cache_dir,
            )
        else:
            reranker = LexicalReranker()
        if settings.rag_store == "milvus":
            from packages.rag.milvus_store import MilvusKnowledgeStore

            store: KnowledgeStore = MilvusKnowledgeStore(
                settings.milvus_uri,
                collection=settings.milvus_collection,
                token=settings.milvus_token,
            )
        else:
            store = LocalKnowledgeStore(settings.kb_index_dir)
        return HybridRetriever(
            embedder,
            store,
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


def _definition_descriptor(definition: DomainDefinition) -> MCPResourceDescriptor:
    encoded = _serialize_definition(definition).encode("utf-8")
    return MCPResourceDescriptor(
        uri=definition.resource_uri,
        name=f"{definition.semantic_key}.v{definition.version}",
        title=definition.title,
        description=definition.description,
        size=len(encoded),
        metadata=_definition_resource_metadata(definition),
    )


def _definition_catalog_descriptor(
    project_id: str,
    definitions: tuple[DomainDefinition, ...],
) -> MCPResourceDescriptor:
    catalog_version = _definition_catalog_version(definitions)
    encoded = _serialize_definition_catalog(
        project_id,
        definitions,
        catalog_version=catalog_version,
    ).encode("utf-8")
    return MCPResourceDescriptor(
        uri=DomainDefinitionResourceProvider.catalog_uri(project_id),
        name="domain-definitions",
        title="领域定义目录",
        description="当前项目可见的不可变领域定义版本目录。",
        size=len(encoded),
        metadata={
            "com.chatbi/resource-kind": "domain-definition-catalog",
            "com.chatbi/catalog-version": catalog_version,
        },
    )


def _definition_catalog_version(
    definitions: tuple[DomainDefinition, ...],
) -> str:
    return stable_hash(
        [
            {
                "definition_id": item.definition_id,
                "semantic_key": item.semantic_key,
                "version": item.version,
                "formula_hash": item.formula_hash,
                "resource_uri": item.resource_uri,
            }
            for item in definitions
        ]
    )


def _serialize_definition_catalog(
    project_id: str,
    definitions: tuple[DomainDefinition, ...],
    *,
    catalog_version: str,
) -> str:
    return json.dumps(
        {
            "resource_uri": DomainDefinitionResourceProvider.catalog_uri(project_id),
            "project_id": project_id,
            "catalog_version": catalog_version,
            "definitions": [
                {
                    "definition_id": item.definition_id,
                    "semantic_key": item.semantic_key,
                    "version": item.version,
                    "formula_hash": item.formula_hash,
                    "resource_uri": item.resource_uri,
                }
                for item in definitions
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _definition_resource_metadata(definition: DomainDefinition) -> dict[str, Any]:
    return {
        "com.chatbi/definition-id": definition.definition_id,
        "com.chatbi/semantic-key": definition.semantic_key,
        "com.chatbi/definition-version": definition.version,
        "com.chatbi/formula-hash": definition.formula_hash,
    }


def _serialize_definition(definition: DomainDefinition) -> str:
    payload = {
        "resource_uri": definition.resource_uri,
        "definition_id": definition.definition_id,
        "project_id": definition.project_id,
        "semantic_key": definition.semantic_key,
        "definition_kind": definition.definition_kind,
        "version": definition.version,
        "title": definition.title,
        "description": definition.description,
        "formula": definition.formula,
        "formula_hash": definition.formula_hash,
        "grain": list(definition.grain),
        "scope": definition.scope,
        "owner": definition.owner,
        "source_ref": definition.source_ref,
        "effective_from": definition.effective_from,
        "effective_to": definition.effective_to,
    }
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


if __name__ == "__main__":
    main()
