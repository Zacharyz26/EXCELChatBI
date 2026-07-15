"""SQLite 会话持久层的领域模型。

这些对象只描述持久化数据，不承担 API 校验或 Agent 编排职责。JSON 字段保留为
Python 对象，由 :mod:`packages.session.store` 在数据库边界统一序列化。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias

JsonObject: TypeAlias = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Project:
    """一个数据分析项目。"""

    id: str
    name: str
    created_at: str


@dataclass(frozen=True, slots=True)
class Dataset:
    """项目内的数据集登记项；真实数据仍由 dataset_ref 指向 parquet。"""

    ref: str
    project_id: str
    filename: str
    profile: JsonObject
    parent_ref: str | None
    transform: JsonObject | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Conversation:
    """项目内的一段历史对话。"""

    id: str
    project_id: str
    title: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Message:
    """一条持久化消息。"""

    id: str
    conversation_id: str
    role: str
    content: str
    tool_calls: list[JsonObject] | None
    created_at: str


@dataclass(frozen=True, slots=True)
class Artifact:
    """消息产生的画像、图表、表格、统计结果或报告工件。"""

    id: str
    conversation_id: str
    message_id: str
    type: str
    payload: JsonObject | None
    file_ref: str | None
    source_tool: str | None
    params: JsonObject | None
    dataset_ref: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ConversationContext:
    """可由 SQLite 重建并放入内存热缓存的对话上下文快照。"""

    conversation: Conversation
    messages: tuple[Message, ...]
    artifacts: tuple[Artifact, ...]
