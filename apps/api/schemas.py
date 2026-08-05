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
DatasetRef = Annotated[
    str,
    StringConstraints(pattern=r"^[0-9a-f]{32}$"),
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
    expires_at: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
    ] | None


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
    autonomy_mode: Literal["assisted", "read_only", "autonomous"] = "read_only"
    parent_run_id: Annotated[
        str,
        StringConstraints(pattern=r"^[0-9a-f]{32}$"),
    ] | None = None


class RunFeedbackRequest(BaseModel):
    """对固定终态 TaskRun 的追加式用户反馈。"""

    rating: Literal["helpful", "not_helpful"]
    comment: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=1000),
    ] | None = None
    evidence_ids: list[
        Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    ] = Field(default_factory=list, max_length=100)
    artifact_ids: list[
        Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{32}$")]
    ] = Field(default_factory=list, max_length=100)


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


class ApprovalDecisionRequest(BaseModel):
    """对一个固定版本 ApprovalRecord 做批准或拒绝决定。"""

    expected_version: int = Field(ge=1)
    decision: Literal["approved", "denied"]
    reason: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
    ]


class ApprovalResponse(BaseModel):
    """浏览器可见的安全授权摘要，不包含主体内部字段或幂等哈希。"""

    approval_id: str
    run_id: str
    plan_id: str
    plan_version: int
    step_id: str
    tool_name: str
    tool_schema_hash: str
    parameter_summary_hash: str
    risk_level: Literal["high", "critical"]
    status: Literal["pending", "approved", "denied", "consumed", "revoked"]
    version: int
    expires_at: str
    decision_reason: str | None
    requested_at: str
    updated_at: str
    decided_at: str | None
    consumed_at: str | None


class UploadResponse(BaseModel):
    """Excel 上传响应：数据集引用 + 数据画像（供前端展示并确认）。

    注意：返回的是画像，原始整表只在服务端以 dataset_ref 引用（红线1）。
    """

    dataset_ref: str
    profile: dict[str, Any]
    messages: list[MessageResponse] | None = None
    artifact: ArtifactResponse | None = None


class AnalyzeRequest(BaseModel):
    """分析请求：基于已上传数据集出图。"""

    dataset_ref: DatasetRef


class ChartResponse(BaseModel):
    """出图响应：ECharts 配置（数值来自真实数据，红线2）。"""

    chart_id: str
    chart_type: str
    option: dict[str, Any]


class StatsRequest(BaseModel):
    """统计分析请求：基于已上传数据集跑趋势/异常/回归。

    params 为工具专属入参（如 value_col/time_col/target/features），
    与 dataset_ref 合并后经 Tool.invoke 做 JSON Schema 校验（红线3）。
    """

    dataset_ref: DatasetRef
    kind: str                      # trend | anomaly | regression
    params: dict[str, Any] = {}
    interpret: bool = False        # 是否附带 LLM 中文解读（默认关，不平白付模型成本）


class StatsResponse(BaseModel):
    """统计分析响应：结构化结果（数值来自工具，红线2）。

    result 内可能含明细级数组（STL 逐行分量、异常点原值），仅供前端渲染；
    interpretation 为可选的 LLM 中文解读——喂模型的只有摘要（红线1），
    模型不可用时为 None（降级，统计结果照常返回）。
    """

    kind: str
    result: dict[str, Any]
    interpretation: str | None = None


class ReportChartSpec(BaseModel):
    """报告中要包含的一张图（编排层重跑 gen_chart→chart_screenshot 出图片）。"""

    chart_type: str
    encoding: dict[str, Any]
    caption: str | None = None


class ReportStatSpec(BaseModel):
    """报告中要包含的一项统计（编排层重跑 stats 工具拿真实结果）。"""

    kind: str                      # trend | anomaly | regression
    params: dict[str, Any] = {}
    caption: str | None = None


class ReportRequest(BaseModel):
    """报告生成请求：基于 dataset_ref 重跑分析并组装成可下载报告。

    interpret=true 时，各统计段的中文解读由编排层调 stats_interpreter（已门控的
    唯一 LLM 出口）生成后传给 report 工具；report 工具本身不调 LLM（红线1/铁律）。
    """

    dataset_ref: DatasetRef
    title: str = "分析报告"
    charts: list[ReportChartSpec] = []
    stats: list[ReportStatSpec] = []
    interpret: bool = False


class ReportResponse(BaseModel):
    """报告生成响应：报告 id 与下载链接。"""

    report_id: str
    md_url: str
    pdf_url: str


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


class KBQueryRequest(BaseModel):
    """知识库问答请求（单轮中文提问）。"""

    question: str
    top_k: int = Field(default=5, ge=1, le=20)


class Citation(BaseModel):
    """引用来源（红线6）。"""

    source: str
    snippet: str
    section: str | None = None


class KBQueryResponse(BaseModel):
    """问答响应：答案 + 引用；无结果时如实告知。"""

    answer: str
    citations: list[Citation]
    is_empty: bool
