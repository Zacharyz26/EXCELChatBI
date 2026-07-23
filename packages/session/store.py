"""SQLite 会话持久层。

数据库连接按操作创建并在同一线程内关闭，适合由 FastAPI 线程池调用。SQLite 是
持久化真相源，ConversationCache 只保存可随时重建的热快照。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import Any, cast

from packages.session.cache import ConversationCache
from packages.session.migrations import CURRENT_SCHEMA_VERSION, migrate_database
from packages.session.models import (
    Artifact,
    Conversation,
    ConversationContext,
    Dataset,
    JsonObject,
    Message,
    Project,
)

_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
_SCHEMA_LOCK = Lock()
_MESSAGE_ROLES = {"system", "user", "assistant", "tool"}

_SCHEMA_V1 = """
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS datasets (
    ref TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
    profile_json TEXT NOT NULL,
    parent_ref TEXT,
    transform_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_ref) REFERENCES datasets(ref) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    tool_calls_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    type TEXT NOT NULL CHECK (length(trim(type)) > 0),
    payload_json TEXT,
    file_ref TEXT,
    source_tool TEXT,
    params_json TEXT,
    dataset_ref TEXT,
    created_at TEXT NOT NULL,
    CHECK (payload_json IS NOT NULL OR file_ref IS NOT NULL),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (dataset_ref) REFERENCES datasets(ref) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_datasets_project_created
    ON datasets(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversations_project_updated
    ON conversations(project_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_conversation_created
    ON artifacts(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_artifacts_message
    ON artifacts(message_id);

PRAGMA user_version = 1;
COMMIT;
"""


class SessionStore:
    """项目、对话、消息和工件的 SQLite repository。"""

    def __init__(self, db_path: str, *, cache_size: int = 128) -> None:
        self._path = Path(db_path)
        self._cache = ConversationCache(cache_size)
        self._initialize()

    @property
    def db_path(self) -> Path:
        """当前 SQLite 文件路径。"""
        return self._path

    @property
    def schema_version(self) -> int:
        """读取数据库 schema 版本。"""
        with self._connection() as connection:
            row = connection.execute("PRAGMA user_version").fetchone()
        return int(row[0]) if row is not None else 0

    # ── Project ──

    def create_project(self, name: str) -> Project:
        """创建项目。"""
        clean_name = _required_text(name, "项目名称")
        project = Project(id=_new_id(), name=clean_name, created_at=_utc_now())
        with self._connection() as connection, connection:
            connection.execute(
                "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)",
                (project.id, project.name, project.created_at),
            )
        return project

    def get_project(self, project_id: str) -> Project | None:
        """按 ID 读取项目。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id, name, created_at FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return _project_from_row(row) if row is not None else None

    def list_projects(self) -> list[Project]:
        """按创建顺序列出项目。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT id, name, created_at FROM projects ORDER BY created_at, rowid"
            ).fetchall()
        return [_project_from_row(row) for row in rows]

    def update_project(self, project_id: str, name: str) -> Project | None:
        """重命名项目；不存在返回 None。"""
        clean_name = _required_text(name, "项目名称")
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE projects SET name = ? WHERE id = ?", (clean_name, project_id)
            )
        return self.get_project(project_id) if cursor.rowcount else None

    def delete_project(self, project_id: str) -> bool:
        """删除项目，并由外键级联清理其数据库记录。"""
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            # Evidence 对单个 Artifact 使用 RESTRICT；删除整个项目时先删除 TaskRun，
            # 让 Evidence/Claim 完整级联后再删除项目及其 Artifact。
            connection.execute("DELETE FROM task_runs WHERE project_id = ?", (project_id,))
            cursor = connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        if cursor.rowcount:
            self._cache.clear()
            return True
        return False

    # ── Dataset ──

    def register_dataset(
        self,
        *,
        ref: str,
        project_id: str,
        filename: str,
        profile: JsonObject,
        parent_ref: str | None = None,
        transform: JsonObject | None = None,
    ) -> Dataset:
        """登记一个已由 dataset_store 落盘的数据集。"""
        clean_ref = _required_text(ref, "数据集引用")
        clean_filename = _required_text(filename, "文件名")
        dataset = Dataset(
            ref=clean_ref,
            project_id=project_id,
            filename=clean_filename,
            profile=profile,
            parent_ref=parent_ref,
            transform=transform,
            created_at=_utc_now(),
        )
        with self._connection() as connection, connection:
            if parent_ref is not None:
                parent = connection.execute(
                    "SELECT project_id FROM datasets WHERE ref = ?", (parent_ref,)
                ).fetchone()
                if parent is None:
                    raise ValueError(f"父数据集不存在: {parent_ref}")
                if _row_text(parent, "project_id") != project_id:
                    raise ValueError("父数据集与衍生数据集必须属于同一项目")
            connection.execute(
                """
                INSERT INTO datasets(
                    ref, project_id, filename, profile_json, parent_ref,
                    transform_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset.ref,
                    dataset.project_id,
                    dataset.filename,
                    _dump_json(dataset.profile),
                    dataset.parent_ref,
                    _dump_json(dataset.transform) if dataset.transform is not None else None,
                    dataset.created_at,
                ),
            )
        return dataset

    def get_dataset(self, dataset_ref: str) -> Dataset | None:
        """按 dataset_ref 读取登记项。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT ref, project_id, filename, profile_json, parent_ref,
                       transform_json, created_at
                FROM datasets WHERE ref = ?
                """,
                (dataset_ref,),
            ).fetchone()
        return _dataset_from_row(row) if row is not None else None

    def list_datasets(self, project_id: str) -> list[Dataset]:
        """按登记顺序列出项目数据集。"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT ref, project_id, filename, profile_json, parent_ref,
                       transform_json, created_at
                FROM datasets
                WHERE project_id = ?
                ORDER BY created_at, rowid
                """,
                (project_id,),
            ).fetchall()
        return [_dataset_from_row(row) for row in rows]

    def update_dataset_filename(self, dataset_ref: str, filename: str) -> Dataset | None:
        """重命名数据集显示名；不存在返回 None。"""
        clean = _required_text(filename, "文件名")
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE datasets SET filename = ? WHERE ref = ?", (clean, dataset_ref)
            )
        if not cursor.rowcount:
            return None
        self._cache.clear()  # 画像卡等缓存快照里可能带旧名
        return self.get_dataset(dataset_ref)

    def dataset_usage(self, dataset_ref: str) -> tuple[int, int]:
        """数据集的使用面：(引用它的对话数, 衍生子数据集数)。误删保护用。"""
        with self._connection() as connection:
            conversations = connection.execute(
                "SELECT COUNT(DISTINCT conversation_id) FROM artifacts WHERE dataset_ref = ?",
                (dataset_ref,),
            ).fetchone()
            derived = connection.execute(
                "SELECT COUNT(*) FROM datasets WHERE parent_ref = ?", (dataset_ref,)
            ).fetchone()
        return int(conversations[0]), int(derived[0])

    def delete_dataset(self, dataset_ref: str) -> bool:
        """删除数据集登记项。

        外键行为：衍生数据集的 parent_ref 与工件的 dataset_ref 均 ON DELETE SET NULL，
        衍生数据与历史工件本身保留。parquet 文件的清理由调用方（路由层）负责。
        """
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "DELETE FROM datasets WHERE ref = ?", (dataset_ref,)
            )
        if cursor.rowcount:
            # 受影响对话不可枚举（工件 dataset_ref 被置空），整体失效热缓存
            self._cache.clear()
            return True
        return False

    def record_profile_upload(
        self,
        *,
        ref: str,
        project_id: str,
        conversation_id: str,
        filename: str,
        profile: JsonObject,
        user_content: str,
        assistant_content: str,
    ) -> tuple[Dataset, tuple[Message, Message], Artifact]:
        """原子登记上传数据集、对话消息和画像工件。

        该事务是上传接口与对话持久层的边界：任一数据库写入失败时，数据集登记、
        两条消息和画像工件全部回滚，不留下半套对话记录。
        """
        dataset = Dataset(
            ref=_required_text(ref, "数据集引用"),
            project_id=project_id,
            filename=_required_text(filename, "文件名"),
            profile=profile,
            parent_ref=None,
            transform=None,
            created_at=_utc_now(),
        )
        user_message = Message(
            id=_new_id(),
            conversation_id=conversation_id,
            role="user",
            content=user_content,
            tool_calls=None,
            created_at=_utc_now(),
        )
        assistant_message = Message(
            id=_new_id(),
            conversation_id=conversation_id,
            role="assistant",
            content=assistant_content,
            tool_calls=None,
            created_at=_utc_now(),
        )
        artifact = Artifact(
            id=_new_id(),
            conversation_id=conversation_id,
            message_id=assistant_message.id,
            type="profile",
            payload=profile,
            file_ref=None,
            source_tool="infer_schema",
            params={"dataset_ref": dataset.ref},
            dataset_ref=dataset.ref,
            created_at=_utc_now(),
        )
        profile_json = _dump_json(profile)
        params_json = _dump_json(artifact.params)

        with self._connection() as connection, connection:
            conversation_row = connection.execute(
                "SELECT project_id, title FROM conversations WHERE id = ?", (conversation_id,)
            ).fetchone()
            if conversation_row is None:
                raise ValueError(f"对话不存在: {conversation_id}")
            if _row_text(conversation_row, "project_id") != project_id:
                raise ValueError("对话不属于指定项目")

            connection.execute(
                """
                INSERT INTO datasets(
                    ref, project_id, filename, profile_json, parent_ref,
                    transform_json, created_at
                ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
                """,
                (
                    dataset.ref,
                    dataset.project_id,
                    dataset.filename,
                    profile_json,
                    dataset.created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, tool_calls_json, created_at
                ) VALUES (?, ?, ?, ?, NULL, ?)
                """,
                [
                    (
                        user_message.id,
                        user_message.conversation_id,
                        user_message.role,
                        user_message.content,
                        user_message.created_at,
                    ),
                    (
                        assistant_message.id,
                        assistant_message.conversation_id,
                        assistant_message.role,
                        assistant_message.content,
                        assistant_message.created_at,
                    ),
                ],
            )
            connection.execute(
                """
                INSERT INTO artifacts(
                    id, conversation_id, message_id, type, payload_json, file_ref,
                    source_tool, params_json, dataset_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.conversation_id,
                    artifact.message_id,
                    artifact.type,
                    profile_json,
                    artifact.source_tool,
                    params_json,
                    artifact.dataset_ref,
                    artifact.created_at,
                ),
            )
            current_title = _row_text(conversation_row, "title")
            next_title = dataset.filename[:200] if current_title == "新对话" else current_title
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (next_title, artifact.created_at, conversation_id),
            )

        self._cache.invalidate(conversation_id)
        return dataset, (user_message, assistant_message), artifact

    # ── Conversation ──

    def create_conversation(self, project_id: str, title: str = "新对话") -> Conversation:
        """在项目中创建对话。"""
        clean_title = _required_text(title, "对话标题")
        now = _utc_now()
        conversation = Conversation(
            id=_new_id(),
            project_id=project_id,
            title=clean_title,
            created_at=now,
            updated_at=now,
        )
        with self._connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO conversations(id, project_id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    conversation.id,
                    conversation.project_id,
                    conversation.title,
                    conversation.created_at,
                    conversation.updated_at,
                ),
            )
        return conversation

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        """按 ID 读取对话。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT id, project_id, title, created_at, updated_at
                FROM conversations WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
        return _conversation_from_row(row) if row is not None else None

    def list_conversations(self, project_id: str) -> list[Conversation]:
        """按最近更新时间倒序列出项目对话。"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, project_id, title, created_at, updated_at
                FROM conversations
                WHERE project_id = ?
                ORDER BY updated_at DESC, rowid DESC
                """,
                (project_id,),
            ).fetchall()
        return [_conversation_from_row(row) for row in rows]

    def update_conversation(self, conversation_id: str, title: str) -> Conversation | None:
        """修改对话标题并更新时间；不存在返回 None。"""
        clean_title = _required_text(title, "对话标题")
        with self._connection() as connection, connection:
            cursor = connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (clean_title, _utc_now(), conversation_id),
            )
        if not cursor.rowcount:
            return None
        self._cache.invalidate(conversation_id)
        return self.get_conversation(conversation_id)

    def delete_conversation(self, conversation_id: str) -> bool:
        """删除对话，并级联清理消息和工件。"""
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM task_runs WHERE conversation_id = ?", (conversation_id,)
            )
            cursor = connection.execute(
                "DELETE FROM conversations WHERE id = ?", (conversation_id,)
            )
        self._cache.invalidate(conversation_id)
        return bool(cursor.rowcount)

    # ── Message ──

    def start_user_turn(
        self,
        *,
        conversation_id: str,
        content: str,
        suggested_title: str,
    ) -> tuple[Conversation, Message]:
        """原子写入用户消息，并在首条用户消息时自动设置对话标题。"""
        clean_content = _required_text(content, "消息内容")
        clean_title = _required_text(suggested_title, "对话标题")[:200]
        now = _utc_now()
        message = Message(
            id=_new_id(),
            conversation_id=conversation_id,
            role="user",
            content=clean_content,
            tool_calls=None,
            created_at=now,
        )
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT id, project_id, title, created_at, updated_at
                FROM conversations WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"对话不存在: {conversation_id}")
            has_user_message = connection.execute(
                """
                SELECT 1 FROM messages
                WHERE conversation_id = ? AND role = 'user'
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            current_title = _row_text(row, "title")
            title = (
                clean_title
                if current_title == "新对话" and has_user_message is None
                else current_title
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, tool_calls_json, created_at
                ) VALUES (?, ?, 'user', ?, NULL, ?)
                """,
                (message.id, message.conversation_id, message.content, message.created_at),
            )
            connection.execute(
                "UPDATE conversations SET title = ?, updated_at = ? WHERE id = ?",
                (title, now, conversation_id),
            )

        conversation = Conversation(
            id=_row_text(row, "id"),
            project_id=_row_text(row, "project_id"),
            title=title,
            created_at=_row_text(row, "created_at"),
            updated_at=now,
        )
        self._cache.invalidate(conversation_id)
        return conversation, message

    def append_message(
        self,
        *,
        conversation_id: str,
        role: str,
        content: str,
        tool_calls: list[JsonObject] | None = None,
        message_id: str | None = None,
    ) -> Message:
        """向对话追加消息，并失效对应上下文缓存。"""
        if role not in _MESSAGE_ROLES:
            raise ValueError(f"不支持的消息角色: {role}")
        now = _utc_now()
        message = Message(
            id=_required_text(message_id, "消息 ID") if message_id is not None else _new_id(),
            conversation_id=conversation_id,
            role=role,
            content=content,
            tool_calls=tool_calls,
            created_at=now,
        )
        with self._connection() as connection, connection:
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, tool_calls_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    message.id,
                    message.conversation_id,
                    message.role,
                    message.content,
                    _dump_json(message.tool_calls) if message.tool_calls is not None else None,
                    message.created_at,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        self._cache.invalidate(conversation_id)
        return message

    def list_messages(self, conversation_id: str) -> list[Message]:
        """按写入顺序读取对话消息。"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, role, content, tool_calls_json, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        return [_message_from_row(row) for row in rows]

    # ── Artifact ──

    def create_artifact(
        self,
        *,
        conversation_id: str,
        message_id: str,
        type: str,
        payload: JsonObject | None = None,
        file_ref: str | None = None,
        source_tool: str | None = None,
        params: JsonObject | None = None,
        dataset_ref: str | None = None,
    ) -> Artifact:
        """创建消息工件，并校验消息、数据集与对话的项目归属。"""
        clean_type = _required_text(type, "工件类型")
        if payload is None and file_ref is None:
            raise ValueError("工件必须包含 payload 或 file_ref")
        now = _utc_now()
        artifact = Artifact(
            id=_new_id(),
            conversation_id=conversation_id,
            message_id=message_id,
            type=clean_type,
            payload=payload,
            file_ref=file_ref,
            source_tool=source_tool,
            params=params,
            dataset_ref=dataset_ref,
            created_at=now,
        )
        with self._connection() as connection, connection:
            message_row = connection.execute(
                "SELECT conversation_id FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            if message_row is None:
                raise ValueError(f"消息不存在: {message_id}")
            if _row_text(message_row, "conversation_id") != conversation_id:
                raise ValueError("工件消息不属于指定对话")

            if dataset_ref is not None:
                ownership = connection.execute(
                    """
                    SELECT c.project_id AS conversation_project_id,
                           d.project_id AS dataset_project_id
                    FROM conversations c
                    JOIN datasets d ON d.ref = ?
                    WHERE c.id = ?
                    """,
                    (dataset_ref, conversation_id),
                ).fetchone()
                if ownership is None:
                    raise ValueError(f"数据集不存在: {dataset_ref}")
                if _row_text(ownership, "conversation_project_id") != _row_text(
                    ownership, "dataset_project_id"
                ):
                    raise ValueError("工件数据集与对话必须属于同一项目")

            connection.execute(
                """
                INSERT INTO artifacts(
                    id, conversation_id, message_id, type, payload_json, file_ref,
                    source_tool, params_json, dataset_ref, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.id,
                    artifact.conversation_id,
                    artifact.message_id,
                    artifact.type,
                    _dump_json(artifact.payload) if artifact.payload is not None else None,
                    artifact.file_ref,
                    artifact.source_tool,
                    _dump_json(artifact.params) if artifact.params is not None else None,
                    artifact.dataset_ref,
                    artifact.created_at,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, conversation_id),
            )
        self._cache.invalidate(conversation_id)
        return artifact

    def list_artifacts(self, conversation_id: str) -> list[Artifact]:
        """按写入顺序读取对话工件。"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, message_id, type, payload_json, file_ref,
                       source_tool, params_json, dataset_ref, created_at
                FROM artifacts
                WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def list_report_artifacts(self) -> list[Artifact]:
        """List every persisted report Artifact for filesystem reconciliation."""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, conversation_id, message_id, type, payload_json, file_ref,
                       source_tool, params_json, dataset_ref, created_at
                FROM artifacts
                WHERE type = 'report'
                ORDER BY created_at, rowid
                """
            ).fetchall()
        return [_artifact_from_row(row) for row in rows]

    def delete_artifact(self, artifact_id: str) -> bool:
        """Delete one unreferenced Artifact; Evidence-linked records are immutable."""
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT conversation_id FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                return False
            referenced = connection.execute(
                """
                SELECT 1 FROM evidence WHERE artifact_id = ?
                UNION ALL
                SELECT 1 FROM tool_invocations WHERE artifact_id = ?
                LIMIT 1
                """,
                (artifact_id, artifact_id),
            ).fetchone()
            if referenced is not None:
                raise ValueError("Artifact 已被 Evidence 引用，不能单独删除")
            connection.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
            conversation_id = _row_text(row, "conversation_id")
        self._cache.invalidate(conversation_id)
        return True

    # ── 热上下文 ──

    def load_conversation_context(self, conversation_id: str) -> ConversationContext | None:
        """从 LRU 读取对话快照；未命中时从 SQLite 一致性重建。"""
        cached = self._cache.get(conversation_id)
        if cached is not None:
            return cached

        with self._connection() as connection, connection:
            connection.execute("BEGIN")
            conversation_row = connection.execute(
                """
                SELECT id, project_id, title, created_at, updated_at
                FROM conversations WHERE id = ?
                """,
                (conversation_id,),
            ).fetchone()
            if conversation_row is None:
                return None
            message_rows = connection.execute(
                """
                SELECT id, conversation_id, role, content, tool_calls_json, created_at
                FROM messages WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                """
                SELECT id, conversation_id, message_id, type, payload_json, file_ref,
                       source_tool, params_json, dataset_ref, created_at
                FROM artifacts WHERE conversation_id = ?
                ORDER BY created_at, rowid
                """,
                (conversation_id,),
            ).fetchall()

        context = ConversationContext(
            conversation=_conversation_from_row(conversation_row),
            messages=tuple(_message_from_row(row) for row in message_rows),
            artifacts=tuple(_artifact_from_row(row) for row in artifact_rows),
        )
        self._cache.put(context)
        return context

    def invalidate_conversation(self, conversation_id: str) -> None:
        """Invalidate a snapshot after a coordinated transaction outside this repository."""
        self._cache.invalidate(conversation_id)

    # ── SQLite 生命周期 ──

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with _SCHEMA_LOCK, self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            migrate_database(connection, self._path, create_v1=_SCHEMA_V1)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()


def _new_id() -> str:
    return uuid.uuid4().hex


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _required_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label}不能为空")
    return clean


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_object(value: str) -> JsonObject:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("数据库中的 JSON 字段不是对象")
    return cast(JsonObject, parsed)


def _load_tool_calls(value: str) -> list[JsonObject]:
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, dict) for item in parsed):
        raise ValueError("数据库中的 tool_calls_json 格式非法")
    return cast(list[JsonObject], parsed)


def _row_text(row: sqlite3.Row, key: str) -> str:
    return str(row[key])


def _row_optional_text(row: sqlite3.Row, key: str) -> str | None:
    value: Any = row[key]
    return None if value is None else str(value)


def _project_from_row(row: sqlite3.Row) -> Project:
    return Project(
        id=_row_text(row, "id"),
        name=_row_text(row, "name"),
        created_at=_row_text(row, "created_at"),
    )


def _dataset_from_row(row: sqlite3.Row) -> Dataset:
    transform_json = _row_optional_text(row, "transform_json")
    return Dataset(
        ref=_row_text(row, "ref"),
        project_id=_row_text(row, "project_id"),
        filename=_row_text(row, "filename"),
        profile=_load_object(_row_text(row, "profile_json")),
        parent_ref=_row_optional_text(row, "parent_ref"),
        transform=_load_object(transform_json) if transform_json is not None else None,
        created_at=_row_text(row, "created_at"),
    )


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=_row_text(row, "id"),
        project_id=_row_text(row, "project_id"),
        title=_row_text(row, "title"),
        created_at=_row_text(row, "created_at"),
        updated_at=_row_text(row, "updated_at"),
    )


def _message_from_row(row: sqlite3.Row) -> Message:
    tool_calls_json = _row_optional_text(row, "tool_calls_json")
    return Message(
        id=_row_text(row, "id"),
        conversation_id=_row_text(row, "conversation_id"),
        role=_row_text(row, "role"),
        content=_row_text(row, "content"),
        tool_calls=_load_tool_calls(tool_calls_json) if tool_calls_json is not None else None,
        created_at=_row_text(row, "created_at"),
    )


def _artifact_from_row(row: sqlite3.Row) -> Artifact:
    payload_json = _row_optional_text(row, "payload_json")
    params_json = _row_optional_text(row, "params_json")
    return Artifact(
        id=_row_text(row, "id"),
        conversation_id=_row_text(row, "conversation_id"),
        message_id=_row_text(row, "message_id"),
        type=_row_text(row, "type"),
        payload=_load_object(payload_json) if payload_json is not None else None,
        file_ref=_row_optional_text(row, "file_ref"),
        source_tool=_row_optional_text(row, "source_tool"),
        params=_load_object(params_json) if params_json is not None else None,
        dataset_ref=_row_optional_text(row, "dataset_ref"),
        created_at=_row_text(row, "created_at"),
    )
