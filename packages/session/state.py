"""旧运行态结构，保留供迁移期兼容。

阶段 1 起，项目、对话、消息和工件以 SQLite 为真相源；完整持久化模型见
``packages.session.models``。本模块暂不参与新对话工作区，等阶段 3 Agent 上下文
装配时再移除兼容结构。
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
    """会话状态。"""

    session_id: str
    history: list[Turn] = field(default_factory=list)          # 按需压缩
    active_dataset: str | None = None                          # 当前活跃数据集引用
    chart_registry: dict[str, ChartEntry] = field(default_factory=dict)  # chart_id → 项
    entity_map: dict[str, str] = field(default_factory=dict)   # 别名 → 实体ID（指代消解）
    last_analysis: str | None = None                           # 上次分析结果引用
    global_summary: str | None = None                          # 早期轮次滚动摘要
