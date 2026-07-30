"""受 Memory Policy 和项目成员关系约束的 SQLite 记忆 Repository。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from packages.governance.audit import (
    AuditEvent,
    AuditOutcome,
)
from packages.governance.audit import (
    record as record_audit,
)
from packages.governance.permissions import Principal
from packages.session.memory_models import (
    MemoryDraft,
    MemoryKind,
    MemoryLink,
    MemoryLinkTarget,
    MemoryRecord,
    MemoryScope,
    MemorySnapshot,
    MemorySourceType,
    MemoryStatus,
    MemoryWriteResult,
)
from packages.session.memory_policy import MemoryPolicy, normalize_as_of
from packages.session.store import SessionStore


class MemoryAccessDenied(PermissionError):
    """认证主体不能访问该项目或 subject-scoped 记忆。"""


class MemoryIdempotencyConflict(RuntimeError):
    """同一项目内的幂等键绑定了不同记忆操作。"""


class MemoryVersionConflict(RuntimeError):
    """记忆版本或生命周期状态已变化。"""


class MemoryStore:
    """共享 SessionStore SQLite 文件的长期记忆 Repository。"""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        policy: MemoryPolicy | None = None,
        audit_recorder: Callable[[AuditEvent], None] = record_audit,
    ) -> None:
        self._path = Path(session_store.db_path)
        self._policy = policy or MemoryPolicy()
        self._audit_recorder = audit_recorder

    def remember(
        self,
        *,
        project_id: str,
        principal: Principal,
        draft: MemoryDraft,
        idempotency_key: str,
    ) -> MemoryWriteResult:
        """写入候选记忆并记录不含正文的治理审计。"""
        try:
            result = self._remember(
                project_id=project_id,
                principal=principal,
                draft=draft,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            self._audit_failure(
                action="memory.remember",
                project_id=project_id,
                principal=principal,
                exc=exc,
            )
            raise
        self._audit_allowed(
            action=(
                "memory.conflict"
                if result.outcome == "conflict"
                else "memory.remember"
            ),
            project_id=project_id,
            principal=principal,
            detail={
                "memory_id": result.record.memory_id,
                "version": result.record.version,
                "status": result.record.status,
                "write_outcome": result.outcome,
            },
        )
        return result

    def _remember(
        self,
        *,
        project_id: str,
        principal: Principal,
        draft: MemoryDraft,
        idempotency_key: str,
    ) -> MemoryWriteResult:
        """写入候选记忆；语义冲突形成不可选择的 conflict 记录。"""
        clean_key = _idempotency_key(idempotency_key)
        request_hash = _request_hash(
            "remember",
            project_id,
            principal,
            {"draft": asdict(draft)},
        )
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection,
                project_id=project_id,
                principal=principal,
                write=True,
            )
            replayed = self._operation_replay(
                connection,
                project_id=project_id,
                idempotency_key=clean_key,
                operation_type="remember",
                request_hash=request_hash,
            )
            if replayed is not None:
                return MemoryWriteResult(replayed, "replayed")

            prepared = self._policy.normalize_draft(draft, now=now)
            scope_key, subject_user_id = self._validate_scope(
                connection,
                project_id=project_id,
                principal=principal,
                draft=prepared,
            )
            self._validate_source(
                connection,
                project_id=project_id,
                source_type=prepared.source_type,
                source_ref=prepared.source_ref,
                source_hash=prepared.source_hash,
            )
            active_row = connection.execute(
                """
                SELECT * FROM memory_records
                WHERE tenant_id = ? AND project_id = ? AND scope = ?
                  AND scope_key = ? AND semantic_key = ? AND status = 'active'
                """,
                (
                    principal.tenant_scope,
                    project_id,
                    prepared.scope,
                    scope_key,
                    prepared.semantic_key,
                ),
            ).fetchone()
            active = _memory_from_row(active_row) if active_row is not None else None
            if active is not None and _same_memory(
                active,
                prepared,
                ignore_valid_from=draft.valid_from is None,
            ):
                self._insert_operation(
                    connection,
                    project_id=project_id,
                    principal=principal,
                    idempotency_key=clean_key,
                    operation_type="remember",
                    request_hash=request_hash,
                    result=active,
                    now=now,
                )
                return MemoryWriteResult(active, "reused")

            version = self._next_version(
                connection,
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                scope=prepared.scope,
                scope_key=scope_key,
                semantic_key=prepared.semantic_key,
            )
            record = MemoryRecord(
                memory_id=uuid.uuid4().hex,
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                scope=prepared.scope,
                scope_key=scope_key,
                conversation_id=prepared.conversation_id,
                subject_user_id=subject_user_id,
                kind=prepared.kind,
                semantic_key=prepared.semantic_key,
                content_summary=prepared.content_summary,
                source_type=prepared.source_type,
                source_ref=prepared.source_ref,
                source_hash=prepared.source_hash,
                confidence=prepared.confidence,
                valid_from=cast(str, prepared.valid_from),
                expires_at=prepared.expires_at,
                version=version,
                status="conflict" if active is not None else "active",
                supersedes_id=None,
                conflicts_with_id=active.memory_id if active is not None else None,
                created_by_user_id=principal.user_id,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            self._insert_record(connection, record)
            self._insert_operation(
                connection,
                project_id=project_id,
                principal=principal,
                idempotency_key=clean_key,
                operation_type="remember",
                request_hash=request_hash,
                result=record,
                now=now,
            )
        return MemoryWriteResult(
            record,
            "conflict" if record.status == "conflict" else "created",
        )

    def revise(
        self,
        memory_id: str,
        *,
        project_id: str,
        principal: Principal,
        expected_version: int,
        draft: MemoryDraft,
        idempotency_key: str,
    ) -> MemoryWriteResult:
        """修订记忆并记录不含正文的治理审计。"""
        try:
            result = self._revise(
                memory_id,
                project_id=project_id,
                principal=principal,
                expected_version=expected_version,
                draft=draft,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            self._audit_failure(
                action="memory.revise",
                project_id=project_id,
                principal=principal,
                exc=exc,
            )
            raise
        self._audit_allowed(
            action="memory.revise",
            project_id=project_id,
            principal=principal,
            detail={
                "memory_id": result.record.memory_id,
                "version": result.record.version,
                "status": result.record.status,
                "write_outcome": result.outcome,
            },
        )
        return result

    def _revise(
        self,
        memory_id: str,
        *,
        project_id: str,
        principal: Principal,
        expected_version: int,
        draft: MemoryDraft,
        idempotency_key: str,
    ) -> MemoryWriteResult:
        """以新不可变版本修订 active 记忆，并关闭同语义键的待处理冲突。"""
        clean_key = _idempotency_key(idempotency_key)
        request_hash = _request_hash(
            "revise",
            project_id,
            principal,
            {
                "memory_id": memory_id,
                "expected_version": expected_version,
                "draft": asdict(draft),
            },
        )
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection,
                project_id=project_id,
                principal=principal,
                write=True,
            )
            replayed = self._operation_replay(
                connection,
                project_id=project_id,
                idempotency_key=clean_key,
                operation_type="revise",
                request_hash=request_hash,
            )
            if replayed is not None:
                return MemoryWriteResult(replayed, "replayed")

            row = connection.execute(
                """
                SELECT * FROM memory_records
                WHERE memory_id = ? AND tenant_id = ? AND project_id = ?
                """,
                (memory_id, principal.tenant_scope, project_id),
            ).fetchone()
            if row is None:
                raise MemoryAccessDenied("记忆不存在")
            current = _memory_from_row(row)
            self._assert_subject_visible(current, principal)
            if current.version != expected_version or current.status != "active":
                raise MemoryVersionConflict("记忆版本或状态已经变化")

            prepared = self._policy.normalize_draft(draft, now=now)
            scope_key, subject_user_id = self._validate_scope(
                connection,
                project_id=project_id,
                principal=principal,
                draft=prepared,
            )
            if (
                prepared.scope != current.scope
                or scope_key != current.scope_key
                or prepared.kind != current.kind
                or prepared.semantic_key != current.semantic_key
            ):
                raise ValueError("修订不能改变记忆作用域、kind 或 semantic_key")
            self._validate_source(
                connection,
                project_id=project_id,
                source_type=prepared.source_type,
                source_ref=prepared.source_ref,
                source_hash=prepared.source_hash,
            )
            if _same_memory(current, prepared):
                self._insert_operation(
                    connection,
                    project_id=project_id,
                    principal=principal,
                    idempotency_key=clean_key,
                    operation_type="revise",
                    request_hash=request_hash,
                    result=current,
                    now=now,
                )
                return MemoryWriteResult(current, "reused")

            version = self._next_version(
                connection,
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                scope=current.scope,
                scope_key=current.scope_key,
                semantic_key=current.semantic_key,
            )
            connection.execute(
                """
                UPDATE memory_records
                SET status = 'superseded', updated_at = ?
                WHERE memory_id = ? AND status = 'active' AND version = ?
                """,
                (now, current.memory_id, expected_version),
            )
            connection.execute(
                """
                UPDATE memory_records
                SET status = 'superseded', updated_at = ?
                WHERE tenant_id = ? AND project_id = ? AND scope = ?
                  AND scope_key = ? AND semantic_key = ? AND status = 'conflict'
                """,
                (
                    now,
                    current.tenant_id,
                    current.project_id,
                    current.scope,
                    current.scope_key,
                    current.semantic_key,
                ),
            )
            revised = MemoryRecord(
                memory_id=uuid.uuid4().hex,
                tenant_id=current.tenant_id,
                project_id=current.project_id,
                scope=current.scope,
                scope_key=current.scope_key,
                conversation_id=prepared.conversation_id,
                subject_user_id=subject_user_id,
                kind=current.kind,
                semantic_key=current.semantic_key,
                content_summary=prepared.content_summary,
                source_type=prepared.source_type,
                source_ref=prepared.source_ref,
                source_hash=prepared.source_hash,
                confidence=prepared.confidence,
                valid_from=cast(str, prepared.valid_from),
                expires_at=prepared.expires_at,
                version=version,
                status="active",
                supersedes_id=current.memory_id,
                conflicts_with_id=None,
                created_by_user_id=principal.user_id,
                created_at=now,
                updated_at=now,
                deleted_at=None,
            )
            self._insert_record(connection, revised)
            self._insert_operation(
                connection,
                project_id=project_id,
                principal=principal,
                idempotency_key=clean_key,
                operation_type="revise",
                request_hash=request_hash,
                result=revised,
                now=now,
            )
        return MemoryWriteResult(revised, "revised")

    def soft_delete(
        self,
        memory_id: str,
        *,
        project_id: str,
        principal: Principal,
        expected_version: int,
        idempotency_key: str,
    ) -> MemoryRecord:
        """软删除记忆并记录不含正文的治理审计。"""
        try:
            deleted = self._soft_delete(
                memory_id,
                project_id=project_id,
                principal=principal,
                expected_version=expected_version,
                idempotency_key=idempotency_key,
            )
        except Exception as exc:
            self._audit_failure(
                action="memory.delete",
                project_id=project_id,
                principal=principal,
                exc=exc,
            )
            raise
        self._audit_allowed(
            action="memory.delete",
            project_id=project_id,
            principal=principal,
            detail={
                "memory_id": deleted.memory_id,
                "version": deleted.version,
                "status": deleted.status,
            },
        )
        return deleted

    def _soft_delete(
        self,
        memory_id: str,
        *,
        project_id: str,
        principal: Principal,
        expected_version: int,
        idempotency_key: str,
    ) -> MemoryRecord:
        """软删除 active/conflict 记忆；重放同一命令返回同一结果。"""
        clean_key = _idempotency_key(idempotency_key)
        request_hash = _request_hash(
            "delete",
            project_id,
            principal,
            {"memory_id": memory_id, "expected_version": expected_version},
        )
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection,
                project_id=project_id,
                principal=principal,
                write=True,
            )
            replayed = self._operation_replay(
                connection,
                project_id=project_id,
                idempotency_key=clean_key,
                operation_type="delete",
                request_hash=request_hash,
            )
            if replayed is not None:
                return replayed

            row = connection.execute(
                """
                SELECT * FROM memory_records
                WHERE memory_id = ? AND tenant_id = ? AND project_id = ?
                """,
                (memory_id, principal.tenant_scope, project_id),
            ).fetchone()
            if row is None:
                raise MemoryAccessDenied("记忆不存在")
            current = _memory_from_row(row)
            self._assert_subject_visible(current, principal)
            if current.version != expected_version or current.status not in {
                "active",
                "conflict",
            }:
                raise MemoryVersionConflict("记忆版本或状态已经变化")
            connection.execute(
                """
                UPDATE memory_records
                SET status = 'deleted', updated_at = ?, deleted_at = ?
                WHERE memory_id = ? AND version = ?
                """,
                (now, now, current.memory_id, expected_version),
            )
            deleted_row = connection.execute(
                "SELECT * FROM memory_records WHERE memory_id = ?",
                (current.memory_id,),
            ).fetchone()
            assert deleted_row is not None
            deleted = _memory_from_row(deleted_row)
            self._insert_operation(
                connection,
                project_id=project_id,
                principal=principal,
                idempotency_key=clean_key,
                operation_type="delete",
                request_hash=request_hash,
                result=deleted,
                now=now,
            )
        return deleted

    def get_record(
        self,
        memory_id: str,
        *,
        principal: Principal,
    ) -> MemoryRecord | None:
        """读取主体可见的单条记忆；无权资源按不存在处理。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_records
                WHERE memory_id = ? AND tenant_id = ?
                """,
                (memory_id, principal.tenant_scope),
            ).fetchone()
            if row is None:
                return None
            record = _memory_from_row(row)
            try:
                self._require_project_role(
                    connection,
                    project_id=record.project_id,
                    principal=principal,
                    write=False,
                )
                self._assert_subject_visible(record, principal)
            except MemoryAccessDenied:
                return None
        return record

    def list_records(
        self,
        *,
        project_id: str,
        principal: Principal,
        conversation_id: str | None = None,
        as_of: str | None = None,
        include_conflicts: bool = False,
    ) -> list[MemoryRecord]:
        """按作用域列出当前主体可选择的 active 记忆。"""
        now = _utc_now()
        selected_at = normalize_as_of(as_of, now=now)
        with self._connection() as connection:
            self._require_project_role(
                connection,
                project_id=project_id,
                principal=principal,
                write=False,
            )
            if conversation_id is not None:
                self._require_conversation_project(
                    connection,
                    conversation_id=conversation_id,
                    project_id=project_id,
                )
            return self._list_records(
                connection,
                project_id=project_id,
                principal=principal,
                conversation_id=conversation_id,
                as_of=selected_at,
                include_conflicts=include_conflicts,
            )

    def create_snapshot(
        self,
        *,
        project_id: str,
        principal: Principal,
        conversation_id: str | None = None,
        run_id: str | None = None,
        as_of: str | None = None,
    ) -> tuple[MemorySnapshot, tuple[MemoryRecord, ...]]:
        """冻结记忆选择并记录 snapshot 审计。"""
        try:
            result = self._create_snapshot(
                project_id=project_id,
                principal=principal,
                conversation_id=conversation_id,
                run_id=run_id,
                as_of=as_of,
            )
        except Exception as exc:
            self._audit_failure(
                action="memory.snapshot",
                project_id=project_id,
                principal=principal,
                exc=exc,
            )
            raise
        snapshot, _ = result
        self._audit_allowed(
            action="memory.snapshot",
            project_id=project_id,
            principal=principal,
            run_id=snapshot.run_id,
            detail={
                "memory_snapshot_id": snapshot.memory_snapshot_id,
                "content_hash": snapshot.content_hash,
                "record_count": snapshot.record_count,
                "policy_version": snapshot.policy_version,
            },
        )
        return result

    def _create_snapshot(
        self,
        *,
        project_id: str,
        principal: Principal,
        conversation_id: str | None = None,
        run_id: str | None = None,
        as_of: str | None = None,
    ) -> tuple[MemorySnapshot, tuple[MemoryRecord, ...]]:
        """冻结一次记忆选择；同一 run 始终重放首次快照。"""
        now = _utc_now()
        selected_at = normalize_as_of(as_of, now=now)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection,
                project_id=project_id,
                principal=principal,
                write=False,
            )
            if conversation_id is not None:
                self._require_conversation_project(
                    connection,
                    conversation_id=conversation_id,
                    project_id=project_id,
                )
            if run_id is not None:
                existing = connection.execute(
                    "SELECT * FROM memory_snapshots WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                if existing is not None:
                    snapshot = _snapshot_from_row(existing)
                    if (
                        snapshot.project_id != project_id
                        or snapshot.tenant_id != principal.tenant_scope
                        or snapshot.subject_user_id != principal.user_id
                    ):
                        raise MemoryAccessDenied("TaskRun 记忆快照不存在")
                    return snapshot, tuple(
                        self._snapshot_records(connection, snapshot.memory_snapshot_id)
                    )
                run_row = connection.execute(
                    """
                    SELECT project_id, conversation_id FROM task_runs
                    WHERE run_id = ?
                    """,
                    (run_id,),
                ).fetchone()
                if run_row is None:
                    raise ValueError(f"TaskRun 不存在: {run_id}")
                if str(run_row["project_id"]) != project_id:
                    raise MemoryAccessDenied("TaskRun 不属于指定项目")
                run_conversation_id = str(run_row["conversation_id"])
                if (
                    conversation_id is not None
                    and run_conversation_id != conversation_id
                ):
                    raise ValueError("TaskRun 与记忆快照对话不一致")
                conversation_id = run_conversation_id

            records = self._list_records(
                connection,
                project_id=project_id,
                principal=principal,
                conversation_id=conversation_id,
                as_of=selected_at,
                include_conflicts=False,
            )
            selection_hash = self._policy.selection_hash(
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                subject_user_id=principal.user_id,
                conversation_id=conversation_id,
                run_id=run_id,
                as_of=selected_at,
            )
            content_hash = _content_hash(records)
            snapshot = MemorySnapshot(
                memory_snapshot_id=uuid.uuid4().hex,
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                subject_user_id=principal.user_id,
                conversation_id=conversation_id,
                run_id=run_id,
                policy_version=self._policy.version,
                selection_hash=selection_hash,
                content_hash=content_hash,
                record_count=len(records),
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO memory_snapshots(
                    memory_snapshot_id, tenant_id, project_id, subject_user_id,
                    conversation_id, run_id, policy_version, selection_hash,
                    content_hash, record_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.memory_snapshot_id,
                    snapshot.tenant_id,
                    snapshot.project_id,
                    snapshot.subject_user_id,
                    snapshot.conversation_id,
                    snapshot.run_id,
                    snapshot.policy_version,
                    snapshot.selection_hash,
                    snapshot.content_hash,
                    snapshot.record_count,
                    snapshot.created_at,
                ),
            )
            connection.executemany(
                """
                INSERT INTO memory_snapshot_items(
                    memory_snapshot_id, memory_id, memory_version, position,
                    record_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        snapshot.memory_snapshot_id,
                        record.memory_id,
                        record.version,
                        position,
                        _stable_json(asdict(record)),
                    )
                    for position, record in enumerate(records)
                ],
            )
        return snapshot, tuple(records)

    def get_snapshot(
        self,
        memory_snapshot_id: str,
        *,
        principal: Principal,
    ) -> tuple[MemorySnapshot, tuple[MemoryRecord, ...]] | None:
        """读取主体自己的不可变快照和当时选中的版本。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM memory_snapshots
                WHERE memory_snapshot_id = ? AND tenant_id = ?
                  AND subject_user_id = ?
                """,
                (
                    memory_snapshot_id,
                    principal.tenant_scope,
                    principal.user_id,
                ),
            ).fetchone()
            if row is None:
                return None
            snapshot = _snapshot_from_row(row)
            try:
                self._require_project_role(
                    connection,
                    project_id=snapshot.project_id,
                    principal=principal,
                    write=False,
                )
            except MemoryAccessDenied:
                return None
            records = tuple(
                self._snapshot_records(connection, snapshot.memory_snapshot_id)
            )
        return snapshot, records

    def add_link(
        self,
        memory_id: str,
        *,
        project_id: str,
        principal: Principal,
        target_type: MemoryLinkTarget,
        target_ref: str,
    ) -> MemoryLink:
        """关联受控资源并记录不含 target_ref 的治理审计。"""
        try:
            link = self._add_link(
                memory_id,
                project_id=project_id,
                principal=principal,
                target_type=target_type,
                target_ref=target_ref,
            )
        except Exception as exc:
            self._audit_failure(
                action="memory.link",
                project_id=project_id,
                principal=principal,
                exc=exc,
            )
            raise
        self._audit_allowed(
            action="memory.link",
            project_id=project_id,
            principal=principal,
            detail={
                "memory_id": link.memory_id,
                "link_id": link.link_id,
                "target_type": link.target_type,
            },
        )
        return link

    def _add_link(
        self,
        memory_id: str,
        *,
        project_id: str,
        principal: Principal,
        target_type: MemoryLinkTarget,
        target_ref: str,
    ) -> MemoryLink:
        """把记忆绑定到同项目受控资源；重复绑定返回已有记录。"""
        clean_target_ref = _required_text(target_ref, "target_ref", maximum=200)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection,
                project_id=project_id,
                principal=principal,
                write=True,
            )
            row = connection.execute(
                """
                SELECT * FROM memory_records
                WHERE memory_id = ? AND tenant_id = ? AND project_id = ?
                """,
                (memory_id, principal.tenant_scope, project_id),
            ).fetchone()
            if row is None:
                raise MemoryAccessDenied("记忆不存在")
            record = _memory_from_row(row)
            self._assert_subject_visible(record, principal)
            self._validate_target(
                connection,
                project_id=project_id,
                target_type=target_type,
                target_ref=clean_target_ref,
            )
            existing = connection.execute(
                """
                SELECT * FROM memory_links
                WHERE memory_id = ? AND target_type = ? AND target_ref = ?
                """,
                (memory_id, target_type, clean_target_ref),
            ).fetchone()
            if existing is not None:
                return _link_from_row(existing)
            link = MemoryLink(
                link_id=uuid.uuid4().hex,
                memory_id=memory_id,
                project_id=project_id,
                target_type=target_type,
                target_ref=clean_target_ref,
                created_by_user_id=principal.user_id,
                tenant_id=principal.tenant_scope,
                created_at=_utc_now(),
            )
            connection.execute(
                """
                INSERT INTO memory_links(
                    link_id, memory_id, project_id, target_type, target_ref,
                    created_by_user_id, tenant_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link.link_id,
                    link.memory_id,
                    link.project_id,
                    link.target_type,
                    link.target_ref,
                    link.created_by_user_id,
                    link.tenant_id,
                    link.created_at,
                ),
            )
        return link

    def list_links(
        self,
        memory_id: str,
        *,
        principal: Principal,
    ) -> list[MemoryLink]:
        """列出主体可见记忆的资源关联。"""
        record = self.get_record(memory_id, principal=principal)
        if record is None:
            return []
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM memory_links
                WHERE memory_id = ? AND tenant_id = ?
                ORDER BY created_at, rowid
                """,
                (memory_id, principal.tenant_scope),
            ).fetchall()
        return [_link_from_row(row) for row in rows]

    def _audit_allowed(
        self,
        *,
        action: str,
        project_id: str,
        principal: Principal,
        detail: dict[str, Any],
        run_id: str | None = None,
    ) -> None:
        self._audit_recorder(
            AuditEvent(
                actor=principal.user_id,
                tenant_id=principal.tenant_scope,
                action=action,
                resource="memory_control_plane",
                outcome="allowed",
                project_id=project_id,
                run_id=run_id,
                detail=detail,
            )
        )

    def _audit_failure(
        self,
        *,
        action: str,
        project_id: str,
        principal: Principal,
        exc: Exception,
    ) -> None:
        outcome: AuditOutcome = (
            "denied"
            if isinstance(
                exc,
                MemoryAccessDenied
                | MemoryIdempotencyConflict
                | MemoryVersionConflict
                | ValueError,
            )
            else "error"
        )
        self._audit_recorder(
            AuditEvent(
                actor=principal.user_id,
                tenant_id=principal.tenant_scope,
                action=action,
                resource="memory_control_plane",
                outcome=outcome,
                project_id=project_id,
                detail={"reason_code": type(exc).__name__},
            )
        )

    def _list_records(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        principal: Principal,
        conversation_id: str | None,
        as_of: str,
        include_conflicts: bool,
    ) -> list[MemoryRecord]:
        statuses = ("active", "conflict") if include_conflicts else ("active",)
        placeholders = ",".join("?" for _ in statuses)
        rows = connection.execute(
            f"""
            SELECT * FROM memory_records
            WHERE tenant_id = ? AND project_id = ?
              AND status IN ({placeholders})
              AND confidence >= ?
              AND valid_from <= ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND (
                    scope = 'project'
                    OR (scope = 'subject' AND subject_user_id = ?)
                    OR (scope = 'conversation' AND conversation_id = ?)
              )
            ORDER BY
              CASE scope
                WHEN 'project' THEN 0
                WHEN 'subject' THEN 1
                ELSE 2
              END,
              semantic_key,
              version
            """,
            (
                principal.tenant_scope,
                project_id,
                *statuses,
                self._policy.minimum_selection_confidence,
                as_of,
                as_of,
                principal.user_id,
                conversation_id,
            ),
        ).fetchall()
        return [_memory_from_row(row) for row in rows]

    def _snapshot_records(
        self,
        connection: sqlite3.Connection,
        memory_snapshot_id: str,
    ) -> list[MemoryRecord]:
        rows = connection.execute(
            """
            SELECT record_json FROM memory_snapshot_items
            WHERE memory_snapshot_id = ?
            ORDER BY position
            """,
            (memory_snapshot_id,),
        ).fetchall()
        return [
            _memory_from_payload(cast(dict[str, Any], json.loads(str(row["record_json"]))))
            for row in rows
        ]

    def _validate_scope(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        principal: Principal,
        draft: MemoryDraft,
    ) -> tuple[str, str | None]:
        if draft.scope == "project":
            return "project", None
        if draft.scope == "subject":
            return principal.user_id, principal.user_id
        assert draft.conversation_id is not None
        self._require_conversation_project(
            connection,
            conversation_id=draft.conversation_id,
            project_id=project_id,
        )
        return draft.conversation_id, None

    def _validate_source(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        source_type: MemorySourceType,
        source_ref: str,
        source_hash: str,
    ) -> None:
        if source_type in {"message", "user_confirmation"}:
            row = connection.execute(
                """
                SELECT conversation.project_id, message.role, message.content
                FROM messages AS message
                JOIN conversations AS conversation
                  ON conversation.id = message.conversation_id
                WHERE message.id = ?
                """,
                (source_ref,),
            ).fetchone()
            if (
                row is None
                or str(row["project_id"]) != project_id
                or (
                    source_type == "user_confirmation"
                    and str(row["role"]) != "user"
                )
            ):
                raise ValueError("记忆消息来源不存在、跨项目或不是用户确认")
            expected_hash = hashlib.sha256(
                str(row["content"]).encode("utf-8")
            ).hexdigest()
            if source_hash != expected_hash:
                raise ValueError("记忆消息来源 hash 不匹配")
            return
        table_queries = {
            "artifact": """
                SELECT conversation.project_id, NULL AS source_hash
                FROM artifacts AS resource
                JOIN conversations AS conversation
                  ON conversation.id = resource.conversation_id
                WHERE resource.id = ?
            """,
            "evidence": """
                SELECT run.project_id, resource.result_hash AS source_hash
                FROM evidence AS resource
                JOIN task_runs AS run ON run.run_id = resource.run_id
                WHERE resource.evidence_id = ?
            """,
            "invocation": """
                SELECT run.project_id, resource.result_hash AS source_hash
                FROM tool_invocations AS resource
                JOIN task_runs AS run ON run.run_id = resource.run_id
                WHERE resource.invocation_id = ?
            """,
        }
        row = connection.execute(table_queries[source_type], (source_ref,)).fetchone()
        if row is None or str(row["project_id"]) != project_id:
            raise ValueError("记忆来源不存在或跨项目")
        result_hash = _optional_text(row["source_hash"])
        if source_type in {"evidence", "invocation"} and result_hash is None:
            raise ValueError("记忆来源尚无确定结果 hash")
        if result_hash is not None and source_hash != result_hash:
            raise ValueError("记忆来源 hash 不匹配")

    def _validate_target(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        target_type: MemoryLinkTarget,
        target_ref: str,
    ) -> None:
        queries = {
            "conversation": "SELECT project_id FROM conversations WHERE id = ?",
            "message": """
                SELECT conversation.project_id
                FROM messages AS resource
                JOIN conversations AS conversation
                  ON conversation.id = resource.conversation_id
                WHERE resource.id = ?
            """,
            "task_run": "SELECT project_id FROM task_runs WHERE run_id = ?",
            "dataset": "SELECT project_id FROM datasets WHERE ref = ?",
            "artifact": """
                SELECT conversation.project_id
                FROM artifacts AS resource
                JOIN conversations AS conversation
                  ON conversation.id = resource.conversation_id
                WHERE resource.id = ?
            """,
            "claim": """
                SELECT run.project_id
                FROM claims AS resource
                JOIN task_runs AS run ON run.run_id = resource.run_id
                WHERE resource.claim_id = ?
            """,
            "evidence": """
                SELECT run.project_id
                FROM evidence AS resource
                JOIN task_runs AS run ON run.run_id = resource.run_id
                WHERE resource.evidence_id = ?
            """,
            "invocation": """
                SELECT run.project_id
                FROM tool_invocations AS resource
                JOIN task_runs AS run ON run.run_id = resource.run_id
                WHERE resource.invocation_id = ?
            """,
        }
        if target_type not in queries:
            raise ValueError(f"不支持的记忆关联目标类型: {target_type}")
        row = connection.execute(queries[target_type], (target_ref,)).fetchone()
        if row is None or str(row["project_id"]) != project_id:
            raise ValueError("记忆关联目标不存在或跨项目")

    def _require_conversation_project(
        self,
        connection: sqlite3.Connection,
        *,
        conversation_id: str,
        project_id: str,
    ) -> None:
        row = connection.execute(
            "SELECT project_id FROM conversations WHERE id = ?",
            (conversation_id,),
        ).fetchone()
        if row is None or str(row["project_id"]) != project_id:
            raise ValueError("记忆对话不存在或跨项目")

    def _require_project_role(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        principal: Principal,
        write: bool,
    ) -> str:
        row = connection.execute(
            """
            SELECT role FROM project_memberships
            WHERE project_id = ? AND user_id = ? AND tenant_id = ?
            """,
            (project_id, principal.user_id, principal.tenant_scope),
        ).fetchone()
        if row is None:
            raise MemoryAccessDenied("记忆项目不存在")
        role = str(row["role"])
        if write and role not in {"owner", "editor"}:
            raise MemoryAccessDenied("记忆项目不存在")
        return role

    def _assert_subject_visible(
        self,
        record: MemoryRecord,
        principal: Principal,
    ) -> None:
        if (
            record.tenant_id != principal.tenant_scope
            or (
                record.scope == "subject"
                and record.subject_user_id != principal.user_id
            )
        ):
            raise MemoryAccessDenied("记忆不存在")

    def _operation_replay(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        idempotency_key: str,
        operation_type: str,
        request_hash: str,
    ) -> MemoryRecord | None:
        row = connection.execute(
            """
            SELECT operation_type, request_hash, result_ref, result_version
            FROM memory_operations
            WHERE project_id = ? AND idempotency_key = ?
            """,
            (project_id, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        if (
            str(row["operation_type"]) != operation_type
            or str(row["request_hash"]) != request_hash
        ):
            raise MemoryIdempotencyConflict("幂等键已绑定到不同记忆操作")
        result = connection.execute(
            """
            SELECT * FROM memory_records
            WHERE memory_id = ? AND version = ?
            """,
            (str(row["result_ref"]), int(row["result_version"])),
        ).fetchone()
        if result is None:
            raise RuntimeError("记忆操作指向的结果不存在")
        return _memory_from_row(result)

    def _insert_operation(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        principal: Principal,
        idempotency_key: str,
        operation_type: str,
        request_hash: str,
        result: MemoryRecord,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_operations(
                operation_id, tenant_id, project_id, actor_user_id,
                idempotency_key, operation_type, request_hash,
                result_ref, result_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                uuid.uuid4().hex,
                principal.tenant_scope,
                project_id,
                principal.user_id,
                idempotency_key,
                operation_type,
                request_hash,
                result.memory_id,
                result.version,
                now,
            ),
        )

    def _next_version(
        self,
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        project_id: str,
        scope: MemoryScope,
        scope_key: str,
        semantic_key: str,
    ) -> int:
        row = connection.execute(
            """
            SELECT MAX(version) AS version FROM memory_records
            WHERE tenant_id = ? AND project_id = ? AND scope = ?
              AND scope_key = ? AND semantic_key = ?
            """,
            (tenant_id, project_id, scope, scope_key, semantic_key),
        ).fetchone()
        return 1 if row is None or row["version"] is None else int(row["version"]) + 1

    def _insert_record(
        self,
        connection: sqlite3.Connection,
        record: MemoryRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_records(
                memory_id, tenant_id, project_id, scope, scope_key,
                conversation_id, subject_user_id, kind, semantic_key,
                content_summary, source_type, source_ref, source_hash,
                confidence, valid_from, expires_at, version, status,
                supersedes_id, conflicts_with_id, created_by_user_id,
                created_at, updated_at, deleted_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?
            )
            """,
            (
                record.memory_id,
                record.tenant_id,
                record.project_id,
                record.scope,
                record.scope_key,
                record.conversation_id,
                record.subject_user_id,
                record.kind,
                record.semantic_key,
                record.content_summary,
                record.source_type,
                record.source_ref,
                record.source_hash,
                record.confidence,
                record.valid_from,
                record.expires_at,
                record.version,
                record.status,
                record.supersedes_id,
                record.conflicts_with_id,
                record.created_by_user_id,
                record.created_at,
                record.updated_at,
                record.deleted_at,
            ),
        )

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


def _same_memory(
    record: MemoryRecord,
    draft: MemoryDraft,
    *,
    ignore_valid_from: bool = False,
) -> bool:
    return (
        record.kind == draft.kind
        and record.content_summary == draft.content_summary
        and record.source_type == draft.source_type
        and record.source_ref == draft.source_ref
        and record.source_hash == draft.source_hash
        and record.confidence == draft.confidence
        and (ignore_valid_from or record.valid_from == draft.valid_from)
        and record.expires_at == draft.expires_at
    )


def _content_hash(records: list[MemoryRecord]) -> str:
    payload = [
        {
            "memory_id": record.memory_id,
            "version": record.version,
            "source_hash": record.source_hash,
            "content_summary": record.content_summary,
        }
        for record in records
    ]
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _request_hash(
    operation: str,
    project_id: str,
    principal: Principal,
    payload: dict[str, Any],
) -> str:
    value = {
        "operation": operation,
        "tenant_id": principal.tenant_scope,
        "project_id": project_id,
        "actor_user_id": principal.user_id,
        **payload,
    }
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _idempotency_key(value: str) -> str:
    return _required_text(value, "Idempotency-Key", maximum=200)


def _required_text(value: str, label: str, *, maximum: int) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} 不能为空")
    if len(clean) > maximum:
        raise ValueError(f"{label} 超过 {maximum} 字符")
    return clean


def _memory_from_row(row: sqlite3.Row) -> MemoryRecord:
    return _memory_from_payload(dict(row))


def _memory_from_payload(row: dict[str, Any]) -> MemoryRecord:
    return MemoryRecord(
        memory_id=str(row["memory_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        scope=cast(MemoryScope, str(row["scope"])),
        scope_key=str(row["scope_key"]),
        conversation_id=_optional_text(row["conversation_id"]),
        subject_user_id=_optional_text(row["subject_user_id"]),
        kind=cast(MemoryKind, str(row["kind"])),
        semantic_key=str(row["semantic_key"]),
        content_summary=str(row["content_summary"]),
        source_type=cast(MemorySourceType, str(row["source_type"])),
        source_ref=str(row["source_ref"]),
        source_hash=str(row["source_hash"]),
        confidence=float(row["confidence"]),
        valid_from=str(row["valid_from"]),
        expires_at=_optional_text(row["expires_at"]),
        version=int(row["version"]),
        status=cast(MemoryStatus, str(row["status"])),
        supersedes_id=_optional_text(row["supersedes_id"]),
        conflicts_with_id=_optional_text(row["conflicts_with_id"]),
        created_by_user_id=str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        deleted_at=_optional_text(row["deleted_at"]),
    )


def _snapshot_from_row(row: sqlite3.Row) -> MemorySnapshot:
    return MemorySnapshot(
        memory_snapshot_id=str(row["memory_snapshot_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        subject_user_id=str(row["subject_user_id"]),
        conversation_id=_optional_text(row["conversation_id"]),
        run_id=_optional_text(row["run_id"]),
        policy_version=str(row["policy_version"]),
        selection_hash=str(row["selection_hash"]),
        content_hash=str(row["content_hash"]),
        record_count=int(row["record_count"]),
        created_at=str(row["created_at"]),
    )


def _link_from_row(row: sqlite3.Row) -> MemoryLink:
    return MemoryLink(
        link_id=str(row["link_id"]),
        memory_id=str(row["memory_id"]),
        project_id=str(row["project_id"]),
        target_type=cast(MemoryLinkTarget, row["target_type"]),
        target_ref=str(row["target_ref"]),
        created_by_user_id=str(row["created_by_user_id"]),
        tenant_id=str(row["tenant_id"]),
        created_at=str(row["created_at"]),
    )


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
