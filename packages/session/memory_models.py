"""v2.5 记忆控制面的持久化契约。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MemoryScope = Literal["conversation", "project", "subject"]
MemoryKind = Literal[
    "field_alias",
    "user_preference",
    "confirmed_decision",
    "entity_mapping",
    "conversation_summary",
]
MemorySourceType = Literal[
    "message",
    "user_confirmation",
    "artifact",
    "evidence",
    "invocation",
]
MemoryStatus = Literal["active", "conflict", "superseded", "deleted"]
MemoryWriteOutcome = Literal["created", "conflict", "replayed", "reused", "revised"]
MemoryLinkTarget = Literal[
    "conversation",
    "message",
    "task_run",
    "dataset",
    "artifact",
    "claim",
    "evidence",
    "invocation",
]


@dataclass(frozen=True, slots=True)
class MemoryDraft:
    """等待 Memory Policy 校验的候选记忆。"""

    scope: MemoryScope
    kind: MemoryKind
    semantic_key: str
    content_summary: str
    source_type: MemorySourceType
    source_ref: str
    source_hash: str
    confidence: float
    conversation_id: str | None = None
    valid_from: str | None = None
    expires_at: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """带来源、作用域和版本的持久记忆记录。"""

    memory_id: str
    tenant_id: str
    project_id: str
    scope: MemoryScope
    scope_key: str
    conversation_id: str | None
    subject_user_id: str | None
    kind: MemoryKind
    semantic_key: str
    content_summary: str
    source_type: MemorySourceType
    source_ref: str
    source_hash: str
    confidence: float
    valid_from: str
    expires_at: str | None
    version: int
    status: MemoryStatus
    supersedes_id: str | None
    conflicts_with_id: str | None
    created_by_user_id: str
    created_at: str
    updated_at: str
    deleted_at: str | None


@dataclass(frozen=True, slots=True)
class MemoryWriteResult:
    """记忆写操作结果；冲突记录不会进入新快照。"""

    record: MemoryRecord
    outcome: MemoryWriteOutcome


@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    """一次模型/TaskRun 使用的不可变记忆选择快照。"""

    memory_snapshot_id: str
    tenant_id: str
    project_id: str
    subject_user_id: str
    conversation_id: str | None
    run_id: str | None
    compaction_id: str | None
    policy_version: str
    selection_hash: str
    content_hash: str
    record_count: int
    created_at: str


@dataclass(frozen=True, slots=True)
class MemoryLink:
    """记忆与项目内受控资源之间的显式关联。"""

    link_id: str
    memory_id: str
    project_id: str
    target_type: MemoryLinkTarget
    target_ref: str
    created_by_user_id: str
    tenant_id: str
    created_at: str
