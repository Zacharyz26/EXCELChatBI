"""集中式配置加载。

配置一律走环境变量 + 配置文件，禁止在业务代码硬编码密钥 / 连接串 / 模型名
（CLAUDE 第6节、红线）。本模块用 pydantic-settings 从 `.env` 读取，并提供
`get_settings()` 单例。具体连接逻辑由各存储客户端实现，这里只暴露配置值。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """全局运行配置。字段与 `.env.example` 一一对应。"""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    # 模型路由
    model_registry_path: str = "config/models.yaml"
    deepseek_api_base: str = ""
    deepseek_api_key: str = ""
    vision_api_base: str = ""
    vision_api_key: str = ""

    # 对话工作区持久层（SQLite 真相源 + 单进程内存热缓存）
    chat_db_path: str = ".data/chatbi.db"
    conversation_cache_size: int = 128
    chat_history_limit: int = 20
    chat_profile_max_chars: int = 12_000

    # Agent 循环护栏（14.5.1 初值 6，2026-07-17 按真实使用调优为 12：
    # 多指标出图的常见计划是 3×统计 + 3×聚合 + 3×图表，6 次会把图表全部挡掉）
    agent_max_tool_calls: int = 12         # 单轮对话工具调用总数上限
    agent_tool_result_max_chars: int = 6_000  # 工具结果回填模型前的截断上限
    agent_registry_max_entries: int = 12   # 分析登记表全量条目上限，更旧的摘要化

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
        """拒绝会静默丢失稀疏召回能力的 RAG 后端组合。"""
        if (self.rag_embedder == "bge") != (self.rag_store == "milvus"):
            raise ValueError("RAG_EMBEDDER=bge 必须与 RAG_STORE=milvus 配对")
        if self.rag_embedder == "bge" and not self.embedding_model.strip():
            raise ValueError("启用 bge 时 EMBEDDING_MODEL 不能为空")
        if self.rag_reranker == "bge" and not self.rerank_model.strip():
            raise ValueError("启用 bge reranker 时 RERANK_MODEL 不能为空")
        return self


@lru_cache
def get_settings() -> Settings:
    """返回全局配置单例（首次调用时读取环境）。"""
    return Settings()
