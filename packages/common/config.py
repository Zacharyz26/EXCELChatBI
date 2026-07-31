"""集中式配置加载。

配置一律走环境变量 + 配置文件，禁止在业务代码硬编码密钥 / 连接串 / 模型名
（CLAUDE 第6节、红线）。本模块用 pydantic-settings 从 `.env` 读取，并提供
`get_settings()` 单例。具体连接逻辑由各存储客户端实现，这里只暴露配置值。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

from mcp_servers.common.service_catalog import validate_service_keys
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局运行配置。字段与 `.env.example` 一一对应。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    process_role: Literal["api", "mcp_server"] = "api"
    log_level: str = "INFO"

    # API 身份边界。开发环境可显式关闭；staging/production 必须使用 Bearer。
    auth_mode: Literal["disabled", "bearer"] = "disabled"
    auth_tokens_json: str = ""
    auth_default_user_id: str = "local-user"
    auth_default_tenant_id: str = "local"

    # 模型路由
    model_registry_path: str = "config/models.yaml"
    deepseek_api_base: str = ""
    deepseek_api_key: str = ""
    vision_api_base: str = ""
    vision_api_key: str = ""

    # 对话工作区持久层（SQLite 真相源 + 单进程内存热缓存）
    chat_db_path: str = ".data/chatbi.db"
    workspace_backup_dir: str = ".data/workspace_backups"
    conversation_cache_size: int = 128
    chat_history_limit: int = 20
    chat_profile_max_chars: int = 12_000
    chat_compaction_trigger_chars: int = Field(default=24_000, ge=100, le=2_000_000)
    chat_compaction_keep_recent: int = Field(default=8, ge=1, le=100)
    chat_compaction_summary_max_chars: int = Field(default=4_000, ge=256, le=12_000)
    chat_compaction_message_max_chars: int = Field(default=320, ge=40, le=2_000)

    # Agent 循环护栏（14.5.1 初值 6，2026-07-17 按真实使用调优为 12：
    # 多指标出图的常见计划是 3×统计 + 3×聚合 + 3×图表，6 次会把图表全部挡掉）
    agent_max_tool_calls: int = 12         # 单轮对话工具调用总数上限
    agent_tool_result_max_chars: int = 6_000  # 工具结果回填模型前的截断上限
    agent_registry_max_entries: int = 12   # 分析登记表全量条目上限，更旧的摘要化
    agent_run_timeout_seconds: int = Field(default=300, ge=10, le=3600)
    agent_model_timeout_seconds: int = Field(default=90, ge=5, le=600)
    agent_tool_timeout_seconds: int = Field(default=120, ge=5, le=1800)
    agent_approval_ttl_seconds: int = Field(default=900, ge=60, le=86400)
    agent_recovery_stale_seconds: int = Field(default=0, ge=0, le=86400)

    # v2.4 阶段 2D：Executor 的规范 MCP Client Gateway。
    # in_process 仅供兼容/测试；staging/production 强制 Streamable HTTP。
    agent_mcp_transport: Literal[
        "in_process", "stdio", "streamable_http"
    ] = "in_process"
    agent_mcp_stdio_command_json: str = ""
    agent_mcp_stdio_cwd: str = ""
    agent_mcp_http_url: str = ""
    agent_mcp_service_token: str = ""
    agent_mcp_service_token_file: str = ""
    agent_mcp_context_signing_key: str = ""
    agent_mcp_context_signing_key_file: str = ""
    agent_mcp_server_urls_json: str = ""
    agent_mcp_service_tokens_json: str = ""
    agent_mcp_service_token_files_json: str = ""
    agent_mcp_connect_timeout_seconds: float = Field(default=10, ge=1, le=120)
    agent_mcp_max_reconnects: int = Field(default=1, ge=0, le=3)
    agent_mcp_allow_in_process_fallback: bool = False

    # 生产存储预留（达到多 worker / 多实例等触发条件后再接入）
    redis_host: str = "127.0.0.1"
    redis_port: int = 6379
    session_ttl_seconds: int = 3600
    postgres_host: str = "127.0.0.1"
    postgres_port: int = 5432
    milvus_host: str = "127.0.0.1"
    milvus_port: int = 19530
    minio_endpoint: str = "127.0.0.1:9000"

    # 沙箱（红线5）
    sandbox_timeout_seconds: int = 30
    sandbox_max_memory_mb: int = 512

    # 本地数据集存储（切片用本地落盘代替 MinIO；生产切 MinIO，留 TODO）
    dataset_dir: str = ".data/datasets"
    upload_dir: str = ".data/uploads"
    max_upload_mb: int = 50              # 上传文件大小上限（超限 413，防内存 DoS）
    report_dir: str = ".data/reports"   # 报告与图表截图落盘目录
    report_temp_grace_seconds: int = Field(default=3600, ge=0)

    # 图表服务端截图（Playwright 无头 chromium）；留空则自动探测已安装的 chromium
    chromium_executable_path: str = ""
    # 表行数处理上限：parse_excel 读表前按元数据检查，超过直接拒绝（防解压后 OOM）；
    # 后续支持超大表时改 DuckDB 分块而非拒绝（留 TODO）
    large_table_row_threshold: int = 500_000

    # 数据画像安全策略配置（缺失时用内置宽松默认，见 packages/governance/data_boundary）
    data_policy_path: str = "config/data_policy.yaml"

    # 中文 RAG（知识库问答）
    rag_embedder: Literal["hashing", "bge"] = "hashing"
    rag_reranker: Literal["lexical", "bge"] = "lexical"
    rag_store: Literal["local", "milvus"] = "local"
    milvus_uri: str = ".data/milvus_lite.db"  # 本地文件=Milvus Lite；http(s)://…=standalone（决策2）
    milvus_token: str = ""  # Standalone 开启鉴权后使用 user:password；不得写入日志
    milvus_collection: str = Field(
        default="kb_chunks", min_length=1, max_length=200, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    embedding_device: Literal["auto", "cpu", "cuda"] = "auto"
    rag_min_relevance: float = Field(default=0.0, ge=0.0, le=1.0)
    embedding_dim: int = Field(default=256, gt=0)
    kb_index_dir: str = ".data/kb_index"  # 本地知识库索引落盘目录
    kb_docs_dir: str = "docs/kb_samples"  # 默认摄入的样例文档目录
    kb_backup_dir: str = ".data/kb_backups"
    kb_max_files: int = Field(default=1_000, ge=1)
    kb_max_document_chars: int = Field(default=2_000_000, ge=1)

    # 中文模型（决策1：bge-m3；可配本地权重目录路径实现离线侧载）
    embedding_model: str = "BAAI/bge-m3"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"

    @model_validator(mode="after")
    def validate_rag_profile(self) -> Settings:
        """拒绝不安全身份配置及会静默丢能力的 RAG 后端组合。"""
        self.agent_mcp_service_token = _resolve_secret(
            value=self.agent_mcp_service_token,
            file_path=self.agent_mcp_service_token_file,
            label="MCP 服务令牌",
        )
        self.agent_mcp_context_signing_key = _resolve_secret(
            value=self.agent_mcp_context_signing_key,
            file_path=self.agent_mcp_context_signing_key_file,
            label="MCP 请求上下文签名密钥",
        )
        service_urls = _parse_string_mapping(
            self.agent_mcp_server_urls_json,
            label="MCP 服务 URL",
        )
        direct_service_tokens = _parse_string_mapping(
            self.agent_mcp_service_tokens_json,
            label="MCP 服务令牌",
        )
        service_token_files = _parse_string_mapping(
            self.agent_mcp_service_token_files_json,
            label="MCP 服务令牌文件",
        )
        if direct_service_tokens and service_token_files:
            raise ValueError("MCP 分服务令牌不能同时通过值和文件配置")
        if service_token_files:
            direct_service_tokens = {
                service: _read_secret_file(file_path, label=f"{service} MCP 服务令牌")
                for service, file_path in service_token_files.items()
            }
            self.agent_mcp_service_tokens_json = json.dumps(
                direct_service_tokens,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        deployed = self.app_env.lower() in {"staging", "production"}
        deployed_api = deployed and self.process_role == "api"
        if deployed_api and self.auth_mode != "bearer":
            raise ValueError("staging/production 必须启用 AUTH_MODE=bearer")
        if self.auth_mode == "bearer" and not self.auth_tokens_json.strip():
            raise ValueError("AUTH_MODE=bearer 时 AUTH_TOKENS_JSON 不能为空")
        if not self.auth_default_user_id.strip() or not self.auth_default_tenant_id.strip():
            raise ValueError("默认认证主体和租户不能为空")
        if (self.rag_embedder == "bge") != (self.rag_store == "milvus"):
            raise ValueError("RAG_EMBEDDER=bge 必须与 RAG_STORE=milvus 配对")
        if self.rag_embedder == "bge" and not self.embedding_model.strip():
            raise ValueError("启用 bge 时 EMBEDDING_MODEL 不能为空")
        if self.rag_reranker == "bge" and not self.rerank_model.strip():
            raise ValueError("启用 bge reranker 时 RERANK_MODEL 不能为空")
        if deployed_api and self.agent_mcp_transport != "streamable_http":
            raise ValueError(
                "staging/production 的 Agent Executor 必须使用 MCP Streamable HTTP"
            )
        if deployed_api and self.agent_mcp_allow_in_process_fallback:
            raise ValueError("staging/production 禁止降级到进程内工具执行")
        if (
            self.agent_mcp_transport == "stdio"
            and not self.agent_mcp_stdio_command_json.strip()
        ):
            raise ValueError("AGENT_MCP_TRANSPORT=stdio 时必须配置 stdio command")
        if self.agent_mcp_stdio_command_json.strip():
            try:
                stdio_command = json.loads(self.agent_mcp_stdio_command_json)
            except json.JSONDecodeError as exc:
                raise ValueError("MCP stdio command 必须是 JSON 字符串数组") from exc
            if (
                not isinstance(stdio_command, list)
                or not stdio_command
                or any(
                    not isinstance(item, str) or not item.strip()
                    for item in stdio_command
                )
            ):
                raise ValueError("MCP stdio command 必须是非空 JSON 字符串数组")
        if self.agent_mcp_transport == "streamable_http":
            if service_urls:
                validate_service_keys(service_urls, label="MCP URL")
                validate_service_keys(direct_service_tokens, label="MCP 令牌")
                if any(
                    not url.startswith(("http://", "https://"))
                    for url in service_urls.values()
                ):
                    raise ValueError("MCP Streamable HTTP URL 必须使用 http(s)")
            else:
                if not self.agent_mcp_http_url.startswith(("http://", "https://")):
                    raise ValueError("MCP Streamable HTTP URL 必须使用 http(s)")
                if not self.agent_mcp_service_token.strip():
                    raise ValueError("MCP Streamable HTTP 必须配置服务令牌")
        if deployed_api and not self.agent_mcp_context_signing_key.strip():
            raise ValueError("staging/production 必须配置 MCP 请求上下文签名密钥")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回全局配置单例（首次调用时读取环境）。"""
    return Settings()


def _parse_string_mapping(raw: str, *, label: str) -> dict[str, str]:
    if not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}必须是 JSON 对象") from exc
    if not isinstance(value, dict) or any(
        not isinstance(key, str)
        or not key.strip()
        or not isinstance(item, str)
        or not item.strip()
        for key, item in value.items()
    ):
        raise ValueError(f"{label}必须是字符串到非空字符串的 JSON 对象")
    return {key.strip(): item.strip() for key, item in value.items()}


def _resolve_secret(*, value: str, file_path: str, label: str) -> str:
    if value.strip() and file_path.strip():
        raise ValueError(f"{label}不能同时通过值和文件配置")
    if file_path.strip():
        return _read_secret_file(file_path, label=label)
    return value.strip()


def _read_secret_file(file_path: str, *, label: str) -> str:
    try:
        value = Path(file_path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{label}文件不可读") from exc
    if not value:
        raise ValueError(f"{label}文件不能为空")
    return value
