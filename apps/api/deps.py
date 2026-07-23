"""FastAPI 依赖注入：配置、模型网关、MCP 工具服务（进程内）。

生产工具仍走进程内 `Tool.invoke`（仍经 schema 校验挂载点，红线3）。标准 MCP
Tool Contract、stdio Server adapter、Client Gateway 和影子比对已落地；双传输
探针和阶段 2 执行切换前，本模块仍不得把协议路径当成生产依赖。
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers.chart.server import build_server as build_chart_server
from mcp_servers.common.base_server import MCPServer
from mcp_servers.dataset_ops.server import build_server as build_dataset_ops_server
from mcp_servers.excel_parser.server import build_server as build_excel_server
from mcp_servers.report.server import build_server as build_report_server
from mcp_servers.stats.server import build_server as build_stats_server
from packages.common.config import Settings, get_settings
from packages.models.gateway import ModelGateway
from packages.models.registry import ModelRegistry
from packages.rag.embedding import BGEEmbedder, Embedder, HashingEmbedder
from packages.rag.rerank import BGEReranker, LexicalReranker, Reranker
from packages.rag.retriever import HybridRetriever
from packages.rag.store import KnowledgeStore, LocalKnowledgeStore
from packages.session.store import SessionStore


def settings_dep() -> Settings:
    """注入全局配置。"""
    return get_settings()


@lru_cache
def model_gateway_dep() -> ModelGateway:
    """注入模型路由网关单例（registry 从配置文件加载）。"""
    registry = ModelRegistry(get_settings().model_registry_path)
    registry.load()
    return ModelGateway(registry)


@lru_cache
def session_store_dep() -> SessionStore:
    """注入 SQLite 会话持久层单例（内部连接按操作创建，可在线程池中调用）。"""
    settings = get_settings()
    return SessionStore(
        settings.chat_db_path,
        cache_size=settings.conversation_cache_size,
    )


@lru_cache
def excel_tools_dep() -> MCPServer:
    """注入 Excel 解析工具服务（进程内）。"""
    return build_excel_server()


@lru_cache
def chart_tools_dep() -> MCPServer:
    """注入图表工具服务（进程内）。"""
    return build_chart_server()


@lru_cache
def stats_tools_dep() -> MCPServer:
    """注入统计分析工具服务（进程内；需 uv sync --extra stats）。"""
    return build_stats_server()


@lru_cache
def report_tools_dep() -> MCPServer:
    """注入报告工具服务（进程内；PDF 需 uv sync --extra report）。"""
    return build_report_server()


@lru_cache
def dataset_ops_tools_dep() -> MCPServer:
    """注入数据集变换/聚合工具服务（进程内，阶段2 新增）。"""
    return build_dataset_ops_server()


# ── 知识库问答（RAG）组件，后端由 config 选择，模型名不硬编码 ──

@lru_cache
def embedder_dep() -> Embedder:
    """注入向量器（默认 hashing；bge-m3 需装 .[rag]，device 走配置）。"""
    s = get_settings()
    if s.rag_embedder == "bge":
        return BGEEmbedder(s.embedding_model, device=s.embedding_device)
    return HashingEmbedder(dim=s.embedding_dim)


@lru_cache
def reranker_dep() -> Reranker:
    """注入重排器（默认 lexical；bge 需装 .[rag]，device 走配置）。"""
    s = get_settings()
    if s.rag_reranker == "bge":
        return BGEReranker(s.rerank_model, device=s.embedding_device)
    return LexicalReranker()


@lru_cache
def kb_store_dep() -> KnowledgeStore:
    """注入知识库存储单例（local JSON 落盘 | Milvus Lite/standalone，决策2）。"""
    s = get_settings()
    if s.rag_store == "milvus":
        from packages.rag.milvus_store import MilvusKnowledgeStore

        return MilvusKnowledgeStore(
            s.milvus_uri,
            collection=s.milvus_collection,
            token=s.milvus_token,
        )
    return LocalKnowledgeStore(s.kb_index_dir)


@lru_cache
def retriever_dep() -> HybridRetriever:
    """注入混合检索器单例（相关性阈值走配置，按分数分布标定）。"""
    return HybridRetriever(
        embedder_dep(),
        kb_store_dep(),
        reranker_dep(),
        min_relevance=get_settings().rag_min_relevance,
    )
