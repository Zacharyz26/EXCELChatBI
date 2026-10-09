"""API 请求 / 响应模型（Pydantic）。"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from packages.session.lineage import (
    LineageNodeStatus,
    LineageNodeType,
    LineageRelation,
)
from packages.session.memory_models import (
    MemoryKind,
    MemoryScope,
    MemorySourceType,
    MemoryStatus,
    MemoryWriteOutcome,
)
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

WorkspaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
ConversationTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
ConversationId = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]
ChatMessageText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20_000)
]


class ProjectCreate(BaseModel):
    """创建项目。"""

    name: WorkspaceName


class ProjectUpdate(BaseModel):
    """重命名项目。"""

    name: WorkspaceName


class ProjectResponse(BaseModel):
    """项目响应。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    created_at: str


class ConversationCreate(BaseModel):
    """在项目内创建对话。"""

    title: ConversationTitle = "新对话"


class ConversationUpdate(BaseModel):
    """修改对话标题。"""

    title: ConversationTitle


class ConversationResponse(BaseModel):
    """对话摘要。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    title: str
    created_at: str
    updated_at: str


class DatasetUpdate(BaseModel):
    """重命名数据集显示名。"""

    filename: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]


class DatasetResponse(BaseModel):
    """项目内数据集登记项。"""

    model_config = ConfigDict(from_attributes=True)

    ref: str
    project_id: str
    filename: str
    profile: dict[str, Any]
    parent_ref: str | None
    transform: dict[str, Any] | None
    created_at: str


class MessageResponse(BaseModel):
    """持久化消息。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    role: str
    content: str
    tool_calls: list[dict[str, Any]] | None
    created_at: str


class ArtifactResponse(BaseModel):
    """消息关联工件。"""

    model_config = ConfigDict(from_attributes=True)

    id: str
    conversation_id: str
    message_id: str
    type: str
    payload: dict[str, Any] | None
    file_ref: str | None
    source_tool: str | None
    params: dict[str, Any] | None
    dataset_ref: str | None
    created_at: str


class MemoryLinkResponse(BaseModel):
    """记忆关联的项目内受控资源。"""

    target_type: str
    target_ref: str


class MemoryResponse(BaseModel):
    """供项目成员治理的安全记忆视图，不暴露租户、subject 或来源摘要。"""

    memory_id: str
    project_id: str
    scope: MemoryScope
    conversation_id: str | None
    kind: MemoryKind
    content_summary: str
    source_type: MemorySourceType
    confidence: float
    valid_from: str
    expires_at: str | None
    version: int
    status: MemoryStatus
    supersedes_id: str | None
    conflicts_with_id: str | None
    created_at: str
    updated_at: str
    deleted_at: str | None
    links: list[MemoryLinkResponse] = Field(default_factory=list)


class MemoryListResponse(BaseModel):
    """分页后的项目记忆治理列表。"""

    items: list[MemoryResponse]
    total: int
    offset: int
    limit: int


class MemoryRevisionRequest(BaseModel):
    """用户可纠正的字段；身份、作用域、语义键和原始来源保持不可变。"""

    expected_version: int = Field(ge=1)
    content_summary: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=4_000),
    ]
    confidence: float = Field(ge=0.0, le=1.0)
    expires_at: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
        ]
        | None
    )


class MemoryMutationResponse(BaseModel):
    """不可变修订及幂等重放结果。"""

    memory: MemoryResponse
    outcome: MemoryWriteOutcome


class LineageNodeResponse(BaseModel):
    """血缘图节点；不包含工具参数、结果正文或文件路径。"""

    node_id: str
    node_type: LineageNodeType
    resource_ref: str
    label: str
    status: LineageNodeStatus
    conversation_id: str | None
    run_id: str | None
    metadata: dict[str, Any]
    created_at: str | None


class LineageEdgeResponse(BaseModel):
    """血缘图中的确定性有向关系。"""

    source: str
    target: str
    relation: LineageRelation
    ordinal: int | None = None
    role: str | None = None


class LineageIssueResponse(BaseModel):
    """不含资源 ID 的血缘完整性问题计数。"""

    code: str
    count: int


class LineageGraphResponse(BaseModel):
    """项目级有界血缘图及其完整性摘要。"""

    project_id: str
    nodes: list[LineageNodeResponse]
    edges: list[LineageEdgeResponse]
    graph_hash: str
    integrity_status: str
    issues: list[LineageIssueResponse]
    total_nodes: int
    total_edges: int
    truncated: bool


class ConversationDetailResponse(BaseModel):
    """历史对话及其消息、工件快照。"""

    conversation: ConversationResponse
    messages: list[MessageResponse]
    artifacts: list[ArtifactResponse]


class ChatStreamRequest(BaseModel):
    """对话式 Agent 的流式对话请求（/chat/stream）。"""

    conversation_id: ConversationId
    message: ChatMessageText
    parent_run_id: (
        Annotated[
            str,
            StringConstraints(pattern=r"^[0-9a-f]{32}$"),
        ]
        | None
    ) = None


class RunFeedbackRequest(BaseModel):
    """对固定终态 TaskRun 的追加式用户反馈。"""

    rating: Literal["helpful", "not_helpful"]
    comment: (
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
        ]
        | None
    ) = None
    evidence_ids: list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]] = Field(
        default_factory=list, max_length=100
    )
    artifact_ids: list[Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]] = Field(
        default_factory=list, max_length=100
    )


class ClarificationAnswerRequest(BaseModel):
    """回答一个阻塞澄清问题并继续原 TaskRun。"""

    answer: Any
    resume_token: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=16, max_length=200),
    ]


class PlanRevisionRequest(BaseModel):
    """用户在 paused 安全边界提交的完整不可变计划修订。"""

    plan: dict[str, Any]
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ]
    skipped_step_ids: list[
        Annotated[
            str,
            StringConstraints(
                strip_whitespace=True,
                min_length=1,
                max_length=100,
                pattern=r"^[a-z][a-z0-9_-]{0,63}$",
            ),
        ]
    ] = Field(default_factory=list, max_length=24)


class UploadResponse(BaseModel):
    """XLSX、legacy XLS 或 CSV 上传响应：数据集引用 + 数据画像。

    注意：返回的是画像，原始整表只在服务端以 dataset_ref 引用（红线1）。
    """

    dataset_ref: str
    profile: dict[str, Any]
    messages: list[MessageResponse] | None = None
    artifact: ArtifactResponse | None = None


class IngestRequest(BaseModel):
    """知识库摄入请求：路径（文件/目录）或内联文本，二选一。"""

    path: str | None = Field(default=None, max_length=4096)
    text: str | None = None
    source: str | None = Field(default=None, max_length=512)  # 内联文本时的来源标注

    @model_validator(mode="after")
    def exactly_one_input(self) -> IngestRequest:
        if bool(self.path) == bool(self.text):
            raise ValueError("path 与 text 必须且只能提供一个")
        return self


class IngestResponse(BaseModel):
    """摄入统计。"""

    ingested_docs: int
    chunks: int
    total_chunks: int  # 库内片段总数
    created: list[str] = Field(default_factory=list)
    updated: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    deleted: list[str] = Field(default_factory=list)


class RebuildRequest(BaseModel):
    """全量重建请求；未传路径时使用持久原文事实源。"""

    path: str | None = Field(default=None, max_length=4096)


class KBDocumentResponse(BaseModel):
    """知识库文档清单项。"""

    document_id: str
    source: str
    content_hash: str
    version: int
    updated_at: str
    chunk_count: int


class KBOverviewResponse(BaseModel):
    """知识库概览：供前端展示"能问什么"与派生示例问题。"""

    chunk_count: int
    sources: list[str]
    topics: list[str]
    documents: list[KBDocumentResponse] = Field(default_factory=list)


class DeleteDocumentResponse(BaseModel):
    """删除文档结果。"""

    document_id: str
    removed_chunks: int
