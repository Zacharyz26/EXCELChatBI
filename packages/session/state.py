"""旧运行态结构，保留供历史兼容，不是 v2.4 AgentState。

当前项目、对话、消息和工件以 SQLite 为真相源；v2.4 将另行设计持久化的
TaskContract/AgentState/TaskEvent/Checkpoint/Claim/Evidence。v2.5 的 compaction
与 coref 不应继续扩展本兼容结构后直接充当新控制面。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Turn:
    """单轮对话。"""

    role: str
    content: str


@dataclass
class ChartEntry:
    """已生成图表的注册项，用于图表追问（设计文档 6.2）。"""

    data_ref: str  # 底层数据引用（PostgreSQL / MinIO）
    gen_params: dict[str, object]  # 生成参数，便于复算


@dataclass
class SessionState:
    """历史会话状态结构；不得与 v2.4 目标驱动 AgentState 混用。"""

    session_id: str
    history: list[Turn] = field(default_factory=list)          # 按需压缩
    active_dataset: str | None = None                          # 当前活跃数据集引用
    chart_registry: dict[str, ChartEntry] = field(default_factory=dict)  # chart_id → 项
    entity_map: dict[str, str] = field(default_factory=dict)   # 别名 → 实体ID（指代消解）
    last_analysis: str | None = None                           # 上次分析结果引用
    global_summary: str | None = None                          # 早期轮次滚动摘要
