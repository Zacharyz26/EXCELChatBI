"""SQLite repository for TaskRun, events, invocations and Evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from apps.orchestrator.control.contracts import TaskContract
from apps.orchestrator.control.planner_contract import validate_task_plan
from apps.orchestrator.control.state import AgentState, ensure_transition

from packages.common.identifiers import validate_report_id
from packages.session.models import Artifact, ArtifactDraft, Conversation, JsonObject, Message
from packages.session.task_models import (
    ApprovalRecord,
    ApprovalRiskLevel,
    ApprovalStatus,
    CapabilityCatalogSnapshot,
    Checkpoint,
    ClaimDraft,
    ClaimRecord,
    EvidenceRecord,
    InvocationStatus,
    Observation,
    ObservationSource,
    RunStatus,
    StepStatus,
    TaskEvent,
    TaskPlanRecord,
    TaskRun,
    TaskStepRecord,
    ToolInvocation,
)


class StateVersionConflict(RuntimeError):
    """The persisted run changed since the caller read it."""


class IdempotencyConflict(RuntimeError):
    """An idempotency key was reused for a different invocation."""


class ControlConflict(RuntimeError):
    """A task-control command is unsafe for the current persisted state."""


_EMPTY_CAPABILITY_CATALOG: JsonObject = {
    "schema": "chatbi-capability-catalog-v1",
    "capabilities": [],
    "tools": [],
}


class TaskStore:
    """Task control-plane persistence sharing the SessionStore SQLite file."""

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)

    def create_run(
        self,
        *,
        project_id: str,
        conversation_id: str,
        user_message_id: str,
        contract: TaskContract,
        budget: JsonObject,
        parent_run_id: str | None = None,
        capability_catalog: JsonObject | None = None,
    ) -> tuple[TaskRun, TaskEvent]:
        now = _utc_now()
        run, event, snapshot, capability_snapshot = _new_run_records(
            project_id=project_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            contract=contract,
            budget=budget,
            parent_run_id=parent_run_id,
            now=now,
            capability_catalog=capability_catalog,
        )
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _insert_run_records(
                connection,
                run,
                contract,
                event,
                snapshot,
                capability_snapshot,
            )
        return run, event

    def start_run_with_user_turn(
        self,
        *,
        project_id: str,
        conversation_id: str,
        content: str,
        suggested_title: str,
        contract: TaskContract,
        budget: JsonObject,
        parent_run_id: str | None = None,
        capability_catalog: JsonObject | None = None,
    ) -> tuple[Conversation, Message, TaskRun, TaskEvent]:
        """Atomically create the user message, run, contract, goal and snapshot."""
        clean_content = _required_text(content, "消息内容")
        clean_title = _required_text(suggested_title, "对话标题")[:200]
        now = _utc_now()
        message = Message(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            role="user",
            content=clean_content,
            tool_calls=None,
            created_at=now,
        )
        run, event, snapshot, capability_snapshot = _new_run_records(
            project_id=project_id,
            conversation_id=conversation_id,
            user_message_id=message.id,
            contract=contract,
            budget=budget,
            parent_run_id=parent_run_id,
            now=now,
            capability_catalog=capability_catalog,
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
            if str(row["project_id"]) != project_id:
                raise ValueError("对话不属于指定项目")
            has_user_message = connection.execute(
                """
                SELECT 1 FROM messages
                WHERE conversation_id = ? AND role = 'user'
                LIMIT 1
                """,
                (conversation_id,),
            ).fetchone()
            current_title = str(row["title"])
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
            _insert_run_records(
                connection,
                run,
                contract,
                event,
                snapshot,
                capability_snapshot,
            )

        conversation = Conversation(
            id=str(row["id"]),
            project_id=str(row["project_id"]),
            title=title,
            created_at=str(row["created_at"]),
            updated_at=now,
        )
        return conversation, message, run, event

    def get_run(self, run_id: str) -> TaskRun | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def get_capability_catalog_snapshot(
        self,
        run_id: str,
    ) -> CapabilityCatalogSnapshot | None:
        """Return the immutable executable catalog bound to a TaskRun."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM capability_catalog_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return _capability_catalog_snapshot_from_row(row) if row is not None else None

    def ensure_capability_catalog_snapshot(
        self,
        run_id: str,
        catalog: JsonObject,
    ) -> CapabilityCatalogSnapshot:
        """Backfill a pre-v9 TaskRun once; never replace an existing snapshot."""
        normalized = _normalize_capability_catalog(catalog)
        content_hash = _hash_text(_dump_json(normalized))
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT 1 FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            row = connection.execute(
                "SELECT * FROM capability_catalog_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is not None:
                snapshot = _capability_catalog_snapshot_from_row(row)
                if snapshot.content_hash != content_hash:
                    raise ControlConflict("TaskRun capability 目录快照已冻结，禁止替换")
                return snapshot
            snapshot = CapabilityCatalogSnapshot(
                snapshot_id=uuid.uuid4().hex,
                run_id=run_id,
                schema_version=1,
                catalog=normalized,
                content_hash=content_hash,
                created_at=now,
            )
            _insert_capability_catalog_snapshot(connection, snapshot)
        return snapshot

    def get_latest_run_for_conversation(
        self,
        conversation_id: str,
    ) -> TaskRun | None:
        """返回对话最近创建的 TaskRun；相同时间戳以 SQLite 插入顺序稳定决胜。"""
        clean_conversation_id = _required_text(
            conversation_id,
            "conversation_id",
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM task_runs
                WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (clean_conversation_id,),
            ).fetchone()
        return _run_from_row(row) if row is not None else None

    def list_runs_for_conversation(
        self,
        conversation_id: str,
        *,
        limit: int = 50,
    ) -> list[TaskRun]:
        """返回同一对话的最近 TaskRun，供受控分支比较。"""
        clean_conversation_id = _required_text(conversation_id, "conversation_id")
        bounded_limit = min(max(limit, 1), 100)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM task_runs
                WHERE conversation_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (clean_conversation_id, bounded_limit),
            ).fetchall()
        return [_run_from_row(row) for row in rows]

    def terminate_active_run(
        self,
        run_id: str,
        *,
        status: RunStatus,
        reason: str,
        event_type: str,
        expected_version: int | None = None,
    ) -> tuple[TaskRun, TaskEvent] | None:
        """原子收敛活动任务，并把仍在 running 的调用标记为 unknown。

        线程池工作无法被 Python 安全强杀，因此超时/断线后不能把调用记作 failed；
        unknown 会阻止 Verifier 把任务误判为成功，也避免自动重试潜在副作用。
        """
        if status not in {"failed", "cancelled", "blocked"}:
            raise ValueError("强制终止只接受 failed/cancelled/blocked")
        clean_reason = _required_text(reason, "终止原因")[:200]
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                return None
            current = _run_from_row(row)
            if current.status in {"completed", "blocked", "failed", "cancelled"}:
                return None
            if expected_version is not None and current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            ensure_transition(current.status, status)
            running_invocations = connection.execute(
                """
                SELECT invocation_id, step_id FROM tool_invocations
                WHERE run_id = ? AND status = 'running'
                """,
                (run_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE task_steps
                SET status = 'blocked', completed_at = ?
                WHERE step_id IN (
                    SELECT step_id FROM tool_invocations
                    WHERE run_id = ? AND status = 'running' AND step_id IS NOT NULL
                )
                """,
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'unknown', error_text = ?, completed_at = ?
                WHERE run_id = ? AND status = 'running'
                """,
                (clean_reason, now, run_id),
            )
            next_version = current.state_version + 1
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, state_version = ?, terminal_reason = ?,
                    updated_at = ?, finished_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    status,
                    next_version,
                    clean_reason,
                    now,
                    now,
                    run_id,
                    current.state_version,
                ),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type=event_type,
                payload={
                    "reason": clean_reason,
                    "unknown_invocations": len(running_invocations),
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "active_invocation_id": None,
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
        return updated, event

    def recover_stale_runs(
        self,
        *,
        stale_after_seconds: int,
        limit: int = 1000,
    ) -> list[tuple[TaskRun, TaskEvent]]:
        """启动时恢复失去执行宿主的任务。

        没有活动工具调用的 ``running`` 任务停在可恢复的 ``paused`` + Checkpoint；
        有活动调用时外部副作用无法确认，仍收敛为 failed/unknown。``planning`` 和
        ``verifying`` 尚无可重建执行边界，继续失败关闭。
        """
        cutoff = (
            datetime.now(UTC) - timedelta(seconds=max(stale_after_seconds, 0))
        ).isoformat(timespec="microseconds").replace("+00:00", "Z")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT run_id, status, state_version FROM task_runs
                WHERE status IN ('planning', 'running', 'verifying')
                  AND updated_at <= ?
                ORDER BY updated_at
                LIMIT ?
                """,
                (cutoff, min(max(limit, 1), 10_000)),
            ).fetchall()
        recovered: list[tuple[TaskRun, TaskEvent]] = []
        for row in rows:
            if str(row["status"]) == "running":
                try:
                    paused, event, _ = self.control_transition(
                        str(row["run_id"]),
                        expected_version=int(row["state_version"]),
                        idempotency_key=(
                            f"process-recovery:{row['run_id']}:"
                            f"{row['state_version']}"
                        ),
                        command="recover_pause",
                        allowed_statuses={"running"},
                        status="paused",
                        event_type="run.paused",
                        payload={"reason": "process_recovery"},
                        require_idle=True,
                        checkpoint_reason="process_recovery",
                    )
                except (ControlConflict, StateVersionConflict):
                    pass
                else:
                    recovered.append((paused, event))
                    continue
            result = self.terminate_active_run(
                str(row["run_id"]),
                status="failed",
                reason="process_recovery",
                event_type="run.failed",
            )
            if result is not None:
                recovered.append(result)
        return recovered

    def get_contract(self, run_id: str) -> JsonObject | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT contract_json FROM task_contracts WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _load_object(str(row[0])) if row is not None else None

    def get_snapshot(self, run_id: str) -> JsonObject | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT state_json FROM task_snapshots WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _load_object(str(row[0])) if row is not None else None

    def get_latest_checkpoint(self, run_id: str) -> Checkpoint | None:
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE run_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        return _checkpoint_from_row(row) if row is not None else None

    def control_transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        command: str,
        allowed_statuses: set[RunStatus],
        status: RunStatus,
        event_type: str,
        payload: JsonObject,
        terminal_reason: str | None = None,
        require_idle: bool = False,
        checkpoint_reason: str | None = None,
        require_checkpoint: bool = False,
    ) -> tuple[TaskRun, TaskEvent, bool]:
        """原子执行一个可幂等重放的 pause/resume/cancel 控制命令。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_command = _required_text(command, "控制命令")[:100]
        request_hash = _control_request_hash(clean_command, payload)
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(row)
            replay = _control_replay(
                connection,
                run_id=run_id,
                idempotency_key=clean_key,
                command=clean_command,
                request_hash=request_hash,
            )
            if replay is not None:
                return current, replay, False
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            if current.status not in allowed_statuses:
                raise ControlConflict(
                    f"TaskRun 状态 {current.status} 不能执行 {clean_command}"
                )
            if require_idle:
                active = connection.execute(
                    """
                    SELECT 1 FROM tool_invocations
                    WHERE run_id = ? AND status IN ('running', 'unknown')
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if active is not None:
                    raise ControlConflict(
                        "任务存在执行中或结果未知的工具调用，不能进入可恢复安全边界"
                    )
            checkpoint: Checkpoint | None = None
            if require_checkpoint:
                checkpoint_row = connection.execute(
                    """
                    SELECT * FROM checkpoints
                    WHERE run_id = ?
                    ORDER BY sequence DESC
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if checkpoint_row is None:
                    raise ControlConflict("TaskRun 没有可用 Checkpoint")
                checkpoint = _checkpoint_from_row(checkpoint_row)
                _validate_checkpoint(connection, current, checkpoint)
            ensure_transition(current.status, status)

            unknown_invocations = 0
            if status == "cancelled":
                running_rows = connection.execute(
                    """
                    SELECT invocation_id FROM tool_invocations
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (run_id,),
                ).fetchall()
                unknown_invocations = len(running_rows)
                connection.execute(
                    """
                    UPDATE task_steps
                    SET status = 'blocked', completed_at = ?
                    WHERE step_id IN (
                        SELECT step_id FROM tool_invocations
                        WHERE run_id = ? AND status = 'running'
                          AND step_id IS NOT NULL
                    )
                    """,
                    (now, run_id),
                )
                connection.execute(
                    """
                    UPDATE tool_invocations
                    SET status = 'unknown', error_text = 'user_cancelled',
                        completed_at = ?
                    WHERE run_id = ? AND status = 'running'
                    """,
                    (now, run_id),
                )

            next_version = current.state_version + 1
            finished_at = now if status in {
                "completed",
                "blocked",
                "failed",
                "cancelled",
            } else None
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, state_version = ?, terminal_reason = ?,
                    updated_at = ?, finished_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    status,
                    next_version,
                    terminal_reason,
                    now,
                    finished_at,
                    run_id,
                    current.state_version,
                ),
            )
            event_payload: JsonObject = {
                **payload,
                "control": {
                    "command": clean_command,
                    "idempotency_key": clean_key,
                    "request_hash": request_hash,
                },
            }
            if checkpoint is not None:
                event_payload["checkpoint"] = {
                    "checkpoint_id": checkpoint.checkpoint_id,
                    "sequence": checkpoint.sequence,
                    "state_version": checkpoint.state_version,
                    "last_event_sequence": checkpoint.state.get(
                        "last_sequence", 0
                    ),
                    "completed_step_ids": checkpoint.state.get(
                        "completed_step_ids", []
                    ),
                }
            if status == "cancelled":
                event_payload["unknown_invocations"] = unknown_invocations
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type=event_type,
                payload=event_payload,
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "completed_step_ids": _completed_step_ids(
                        connection, run_id
                    ),
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
            if checkpoint_reason is not None:
                _insert_checkpoint(
                    connection,
                    run_id=run_id,
                    state_version=next_version,
                    state=snapshot,
                    reason=checkpoint_reason,
                    created_at=now,
                )
        return updated, event, True

    def answer_clarification(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        question_id: str,
        resume_token: str,
        answer: object,
    ) -> tuple[TaskRun, TaskEvent, bool]:
        """校验并消费 waiting_user 问题，同时把回答作为用户消息和 Observation 落库。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_question_id = _required_text(question_id, "question_id")[:100]
        clean_token = _required_text(resume_token, "resume_token")[:200]
        if not _valid_clarification_answer(answer):
            raise ControlConflict("澄清答案必须是非空且有界的 JSON 标量或对象")
        request_payload: JsonObject = {
            "question_id": clean_question_id,
            "resume_token_hash": _hash_text(clean_token),
            "answer": answer,
        }
        request_hash = _control_request_hash(
            "answer_clarification",
            request_payload,
        )
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(row)
            replay = _control_replay(
                connection,
                run_id=run_id,
                idempotency_key=clean_key,
                command="answer_clarification",
                request_hash=request_hash,
            )
            if replay is not None:
                return current, replay, False
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            if current.status != "waiting_user":
                raise ControlConflict(
                    f"TaskRun 状态 {current.status} 不能回答澄清问题"
                )
            waiting_rows = connection.execute(
                """
                SELECT payload_json FROM task_events
                WHERE run_id = ? AND event_type = 'waiting_user'
                ORDER BY sequence DESC
                """,
                (run_id,),
            ).fetchall()
            waiting_payload = next(
                (
                    item
                    for item in (
                        _load_object(str(waiting_row["payload_json"]))
                        for waiting_row in waiting_rows
                    )
                    if item.get("question_id") == clean_question_id
                ),
                None,
            )
            if waiting_payload is None:
                raise ControlConflict("澄清问题不属于当前任务")
            if waiting_payload.get("resume_token") != clean_token:
                raise ControlConflict("resume_token 无效或已不属于当前问题")
            checkpoint_row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE run_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if checkpoint_row is None:
                raise ControlConflict("澄清任务缺少可恢复 Checkpoint")
            _validate_checkpoint(
                connection,
                current,
                _checkpoint_from_row(checkpoint_row),
            )

            ensure_transition(current.status, "planning")
            answer_text = _dump_json_value(answer)
            message = Message(
                id=uuid.uuid4().hex,
                conversation_id=current.conversation_id,
                role="user",
                content=f"澄清回答（{clean_question_id}）：{answer_text}",
                tool_calls=None,
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO messages(
                    id, conversation_id, role, content, tool_calls_json, created_at
                ) VALUES (?, ?, 'user', ?, NULL, ?)
                """,
                (
                    message.id,
                    message.conversation_id,
                    message.content,
                    message.created_at,
                ),
            )
            connection.execute(
                "UPDATE conversations SET updated_at = ? WHERE id = ?",
                (now, current.conversation_id),
            )
            next_version = current.state_version + 1
            connection.execute(
                """
                UPDATE task_runs
                SET status = 'planning', state_version = ?,
                    terminal_reason = NULL, updated_at = ?, finished_at = NULL
                WHERE run_id = ? AND state_version = ?
                """,
                (next_version, now, run_id, current.state_version),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type="clarification.answered",
                payload={
                    "question_id": clean_question_id,
                    "answer": answer,
                    "user_message_id": message.id,
                    "control": {
                        "command": "answer_clarification",
                        "idempotency_key": clean_key,
                        "request_hash": request_hash,
                    },
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            answers = snapshot.get("clarification_answers")
            answer_map = dict(answers) if isinstance(answers, dict) else {}
            answer_map[clean_question_id] = answer
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "clarification_answers": answer_map,
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
        return updated, event, True

    def retry_step(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        step_id: str,
    ) -> tuple[
        TaskRun,
        TaskPlanRecord,
        list[TaskStepRecord],
        TaskEvent,
        bool,
    ]:
        """从 paused 安全边界创建不可变计划修订并只重置指定失败步骤。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_step_id = _required_text(step_id, "step_id")[:100]
        request_payload: JsonObject = {"step_id": clean_step_id}
        request_hash = _control_request_hash("retry_step", request_payload)
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(row)
            replay = _control_replay(
                connection,
                run_id=run_id,
                idempotency_key=clean_key,
                command="retry_step",
                request_hash=request_hash,
            )
            if replay is not None:
                replay_version = replay.payload.get("plan_version")
                if not isinstance(replay_version, int):
                    raise RuntimeError("步骤重试事件缺少 plan_version")
                replay_plan_row = connection.execute(
                    """
                    SELECT * FROM task_plans
                    WHERE run_id = ? AND version = ?
                    """,
                    (run_id, replay_version),
                ).fetchone()
                if replay_plan_row is None:
                    raise RuntimeError("步骤重试对应的计划版本不存在")
                replay_step_rows = connection.execute(
                    """
                    SELECT step.* FROM task_steps AS step
                    JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                    WHERE step.run_id = ? AND plan.version = ?
                    ORDER BY step.position
                    """,
                    (run_id, replay_version),
                ).fetchall()
                return (
                    current,
                    _plan_from_row(replay_plan_row),
                    [_step_from_row(item) for item in replay_step_rows],
                    replay,
                    False,
                )
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            if current.status != "paused":
                raise ControlConflict(
                    f"TaskRun 状态 {current.status} 不能重试步骤"
                )
            active_invocation = connection.execute(
                """
                SELECT 1 FROM tool_invocations
                WHERE run_id = ? AND status = 'running'
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if active_invocation is not None:
                raise ControlConflict("工具正在执行，不能安全重试步骤")
            plan_row = connection.execute(
                """
                SELECT plan.* FROM task_plans AS plan
                JOIN task_runs AS run
                  ON run.run_id = plan.run_id AND run.plan_version = plan.version
                WHERE plan.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if plan_row is None:
                raise ControlConflict("TaskRun 尚无可重试的活动计划")
            previous_plan = _plan_from_row(plan_row)
            previous_step_rows = connection.execute(
                """
                SELECT * FROM task_steps
                WHERE run_id = ? AND plan_id = ?
                ORDER BY position
                """,
                (run_id, previous_plan.plan_id),
            ).fetchall()
            previous_steps = [_step_from_row(item) for item in previous_step_rows]
            target = next(
                (item for item in previous_steps if item.logical_id == clean_step_id),
                None,
            )
            if target is None:
                raise ControlConflict("重试步骤不属于当前活动计划")
            if target.status not in {"failed", "blocked"}:
                raise ControlConflict(
                    f"步骤 {clean_step_id} 状态 {target.status} 不能重试"
                )
            running_step = next(
                (item for item in previous_steps if item.status == "running"),
                None,
            )
            if running_step is not None:
                raise ControlConflict("活动计划仍有 running 步骤，不能安全重试")

            ensure_transition(current.status, "running")
            version = current.plan_version + 1
            plan_record = TaskPlanRecord(
                plan_id=uuid.uuid4().hex,
                run_id=run_id,
                version=version,
                reason=f"user_retry:{clean_step_id}"[:200],
                plan=previous_plan.plan,
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO task_plans(
                    plan_id, run_id, version, reason, plan_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_record.plan_id,
                    plan_record.run_id,
                    plan_record.version,
                    plan_record.reason,
                    _dump_json(plan_record.plan),
                    plan_record.created_at,
                ),
            )
            steps: list[TaskStepRecord] = []
            for previous in previous_steps:
                if previous.status in {"completed", "skipped"}:
                    status = previous.status
                    started_at = previous.started_at
                    completed_at = previous.completed_at
                elif previous.logical_id == clean_step_id:
                    status = "pending"
                    started_at = None
                    completed_at = None
                elif previous.status in {"failed", "blocked"}:
                    status = "blocked"
                    started_at = None
                    completed_at = now
                else:
                    status = "pending"
                    started_at = None
                    completed_at = None
                step = TaskStepRecord(
                    step_id=uuid.uuid4().hex,
                    plan_id=plan_record.plan_id,
                    run_id=run_id,
                    position=previous.position,
                    logical_id=previous.logical_id,
                    status=status,
                    definition=previous.definition,
                    started_at=started_at,
                    completed_at=completed_at,
                )
                connection.execute(
                    """
                    INSERT INTO task_steps(
                        step_id, plan_id, run_id, position, status, step_json,
                        started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step.step_id,
                        step.plan_id,
                        step.run_id,
                        step.position,
                        step.status,
                        _dump_json(step.definition),
                        step.started_at,
                        step.completed_at,
                    ),
                )
                steps.append(step)

            next_version = current.state_version + 1
            connection.execute(
                """
                UPDATE task_runs
                SET status = 'running', state_version = ?, plan_version = ?,
                    terminal_reason = NULL, updated_at = ?, finished_at = NULL
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    next_version,
                    version,
                    now,
                    run_id,
                    current.state_version,
                ),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type="plan.revised",
                payload={
                    "plan_id": plan_record.plan_id,
                    "plan_version": version,
                    "supersedes_version": version - 1,
                    "reason": plan_record.reason,
                    "summary": str(previous_plan.plan.get("summary", ""))[:500],
                    "steps": [
                        {
                            "step_id": step.logical_id,
                            "purpose": step.definition["purpose"],
                            "capability": step.definition["capability"],
                            "dependencies": step.definition["dependencies"],
                            "status": step.status,
                        }
                        for step in steps
                    ],
                    "assumptions": previous_plan.plan.get("assumptions", []),
                    "clarifications": previous_plan.plan.get(
                        "clarifications", []
                    ),
                    "planner": {
                        "route": "user",
                        "phase": "step_retry",
                    },
                    "retry_step_id": clean_step_id,
                    "control": {
                        "command": "retry_step",
                        "idempotency_key": clean_key,
                        "request_hash": request_hash,
                    },
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "active_plan_id": plan_record.plan_id,
                    "active_plan": previous_plan.plan,
                    "retry_step_id": clean_step_id,
                    "completed_step_ids": _completed_step_ids(
                        connection, run_id
                    ),
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
            _insert_checkpoint(
                connection,
                run_id=run_id,
                state_version=next_version,
                state=snapshot,
                reason=f"user_step_retry:{clean_step_id}",
                created_at=now,
            )
        return updated, plan_record, steps, event, True

    def request_approval(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        tenant_id: str,
        subject_user_id: str,
        requested_by_user_id: str,
        step_id: str,
        tool_name: str,
        tool_schema_hash: str,
        parameter_summary_hash: str,
        risk_level: ApprovalRiskLevel,
        expires_at: str,
        pause_run: bool = False,
    ) -> tuple[TaskRun, ApprovalRecord, TaskEvent, bool]:
        """请求固定授权；Executor 可在无活动调用时原子进入 paused。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_tenant = _required_text(tenant_id, "tenant_id")[:200]
        clean_subject = _required_text(subject_user_id, "subject_user_id")[:200]
        clean_requester = _required_text(
            requested_by_user_id, "requested_by_user_id"
        )[:200]
        clean_step_id = _required_text(step_id, "step_id")[:100]
        clean_tool = _required_text(tool_name, "tool_name")[:200]
        schema_hash = _required_sha256(tool_schema_hash, "tool_schema_hash")
        parameter_hash = _required_sha256(
            parameter_summary_hash,
            "parameter_summary_hash",
        )
        if risk_level not in {"high", "critical"}:
            raise ValueError("ApprovalRecord 只接受 high/critical 风险")
        expiry = _normalized_timestamp(expires_at, "expires_at")
        now = _utc_now()
        expiry_time = _timestamp(expiry)
        now_time = _timestamp(now)
        if expiry_time <= now_time:
            raise ValueError("ApprovalRecord.expires_at 必须晚于当前时间")
        if expiry_time - now_time > timedelta(hours=24):
            raise ValueError("ApprovalRecord 有效期不能超过 24 小时")
        request_payload: JsonObject = {
            "tenant_id": clean_tenant,
            "subject_user_id": clean_subject,
            "requested_by_user_id": clean_requester,
            "step_id": clean_step_id,
            "tool_name": clean_tool,
            "tool_schema_hash": schema_hash,
            "parameter_summary_hash": parameter_hash,
            "risk_level": risk_level,
            "expires_at": expiry,
            "pause_run": pause_run,
        }
        request_hash = _control_request_hash("request_approval", request_payload)
        approval_id = uuid.uuid4().hex
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(run_row)
            replay_row = connection.execute(
                """
                SELECT * FROM approval_records
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, clean_key),
            ).fetchone()
            if replay_row is not None:
                replayed = _approval_from_row(replay_row)
                if replayed.request_hash != request_hash:
                    raise IdempotencyConflict(
                        "Idempotency-Key 已绑定到不同授权请求"
                    )
                event_row = connection.execute(
                    """
                    SELECT event_id, run_id, sequence, event_type,
                           payload_json, occurred_at
                    FROM task_events WHERE event_id = ?
                    """,
                    (replayed.request_event_id,),
                ).fetchone()
                if event_row is None:
                    raise RuntimeError("授权请求事件不存在")
                return current, replayed, _event_from_row(event_row), False
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            required_status: RunStatus = "running" if pause_run else "paused"
            if current.status != required_status:
                raise ControlConflict(
                    f"TaskRun 状态 {current.status} 不能请求高风险授权"
                )
            if pause_run:
                ensure_transition(current.status, "paused")
            active = connection.execute(
                """
                SELECT 1 FROM tool_invocations
                WHERE run_id = ? AND status IN ('running', 'unknown')
                LIMIT 1
                """,
                (run_id,),
            ).fetchone()
            if active is not None:
                raise ControlConflict(
                    "任务存在执行中或结果未知的工具调用，不能请求授权"
                )
            plan_row = connection.execute(
                """
                SELECT plan.* FROM task_plans AS plan
                JOIN task_runs AS run
                  ON run.run_id = plan.run_id AND run.plan_version = plan.version
                WHERE plan.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if plan_row is None:
                raise ControlConflict("TaskRun 没有活动计划")
            plan_record = _plan_from_row(plan_row)
            step_row = connection.execute(
                """
                SELECT step.* FROM task_steps AS step
                WHERE step.plan_id = ?
                  AND json_extract(step.step_json, '$.step_id') = ?
                """,
                (plan_record.plan_id, clean_step_id),
            ).fetchone()
            if step_row is None:
                raise ControlConflict("授权步骤不属于当前活动计划")
            step = _step_from_row(step_row)
            if step.status not in {"pending", "failed", "blocked"}:
                raise ControlConflict(
                    f"步骤状态 {step.status} 不能请求高风险授权"
                )
            for user_id in {clean_subject, clean_requester}:
                membership = connection.execute(
                    """
                    SELECT 1 FROM project_memberships
                    WHERE project_id = ? AND user_id = ? AND tenant_id = ?
                    """,
                    (current.project_id, user_id, clean_tenant),
                ).fetchone()
                if membership is None:
                    raise ControlConflict("授权主体不是当前项目成员")

            next_state_version = current.state_version + 1
            connection.execute(
                """
                UPDATE task_runs
                SET status = 'paused', state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (next_state_version, now, run_id, current.state_version),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type="approval.requested",
                payload={
                    "approval_id": approval_id,
                    "plan_id": plan_record.plan_id,
                    "plan_version": plan_record.version,
                    "step_id": step.logical_id,
                    "tool_name": clean_tool,
                    "tool_schema_hash": schema_hash,
                    "parameter_summary_hash": parameter_hash,
                    "risk_level": risk_level,
                    "expires_at": expiry,
                    "run_status": "paused",
                    "control": {
                        "command": "request_approval",
                        "idempotency_key": clean_key,
                        "request_hash": request_hash,
                    },
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            connection.execute(
                """
                INSERT INTO approval_records(
                    approval_id, tenant_id, project_id, run_id, plan_id,
                    plan_version, task_step_id, step_logical_id,
                    subject_user_id, requested_by_user_id, tool_name,
                    tool_schema_hash, parameter_summary_hash, risk_level,
                    status, version, expires_at, decision_reason,
                    decided_by_user_id, requested_at, updated_at, decided_at,
                    consumed_at, idempotency_key, request_hash, request_event_id
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', 1,
                    ?, NULL, NULL, ?, ?, NULL, NULL, ?, ?, ?
                )
                """,
                (
                    approval_id,
                    clean_tenant,
                    current.project_id,
                    run_id,
                    plan_record.plan_id,
                    plan_record.version,
                    step.step_id,
                    step.logical_id,
                    clean_subject,
                    clean_requester,
                    clean_tool,
                    schema_hash,
                    parameter_hash,
                    risk_level,
                    expiry,
                    now,
                    now,
                    clean_key,
                    request_hash,
                    event.event_id,
                ),
            )
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            approval_row = connection.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            assert updated_row is not None and approval_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "pending_approval_id": approval_id,
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_state_version, _dump_json(snapshot), now, run_id),
            )
            _insert_checkpoint(
                connection,
                run_id=run_id,
                state_version=next_state_version,
                state=snapshot,
                reason=f"approval_requested:{approval_id}",
                created_at=now,
            )
        return updated, _approval_from_row(approval_row), event, True

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        """按 ID 读取 ApprovalRecord。"""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
        return _approval_from_row(row) if row is not None else None

    def list_approvals(
        self,
        run_id: str,
        *,
        tenant_id: str,
        subject_user_id: str,
    ) -> list[ApprovalRecord]:
        """只返回绑定当前认证主体的任务授权记录。"""
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approval_records
                WHERE run_id = ? AND tenant_id = ? AND subject_user_id = ?
                ORDER BY requested_at, approval_id
                """,
                (run_id, tenant_id, subject_user_id),
            ).fetchall()
        return [_approval_from_row(row) for row in rows]

    def find_execution_approval(
        self,
        run_id: str,
        *,
        tenant_id: str,
        subject_user_id: str,
        plan_version: int,
        step_id: str,
        tool_name: str,
        tool_schema_hash: str,
        parameter_summary_hash: str,
    ) -> ApprovalRecord | None:
        """读取完全匹配当前执行绑定的最新授权记录，包括拒绝/过期记录。"""
        schema_hash = _required_sha256(tool_schema_hash, "tool_schema_hash")
        parameter_hash = _required_sha256(
            parameter_summary_hash,
            "parameter_summary_hash",
        )
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM approval_records
                WHERE run_id = ?
                  AND tenant_id = ?
                  AND subject_user_id = ?
                  AND plan_version = ?
                  AND step_logical_id = ?
                  AND tool_name = ?
                  AND tool_schema_hash = ?
                  AND parameter_summary_hash = ?
                ORDER BY requested_at DESC, approval_id DESC
                LIMIT 1
                """,
                (
                    run_id,
                    tenant_id,
                    subject_user_id,
                    plan_version,
                    step_id,
                    tool_name,
                    schema_hash,
                    parameter_hash,
                ),
            ).fetchone()
        return _approval_from_row(row) if row is not None else None

    def has_valid_pending_approval(self, run_id: str) -> bool:
        """判断任务是否仍有未过期的 pending 授权，防止任意成员绕过等待。"""
        now = _utc_now()
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM approval_records
                WHERE run_id = ? AND status = 'pending' AND expires_at > ?
                LIMIT 1
                """,
                (run_id, now),
            ).fetchone()
        return row is not None

    def decide_approval(
        self,
        approval_id: str,
        *,
        expected_run_version: int,
        expected_approval_version: int,
        idempotency_key: str,
        tenant_id: str,
        actor_user_id: str,
        decision: Literal["approved", "denied"],
        reason: str,
    ) -> tuple[TaskRun, ApprovalRecord, TaskEvent, bool]:
        """由绑定 subject 在 paused 边界批准或拒绝一次高风险调用。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_tenant = _required_text(tenant_id, "tenant_id")[:200]
        clean_actor = _required_text(actor_user_id, "actor_user_id")[:200]
        clean_reason = _required_text(reason, "审批原因")[:500]
        if decision not in {"approved", "denied"}:
            raise ValueError("审批决定只接受 approved/denied")
        request_payload: JsonObject = {
            "approval_id": approval_id,
            "expected_approval_version": expected_approval_version,
            "decision": decision,
            "reason": clean_reason,
        }
        request_hash = _control_request_hash("decide_approval", request_payload)
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            approval_row = connection.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                raise ValueError("ApprovalRecord 不存在")
            current_approval = _approval_from_row(approval_row)
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (current_approval.run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError("ApprovalRecord 对应的 TaskRun 不存在")
            current_run = _run_from_row(run_row)
            replay = _approval_operation_replay(
                connection,
                approval_id=approval_id,
                project_id=current_approval.project_id,
                tenant_id=clean_tenant,
                actor_user_id=clean_actor,
                idempotency_key=clean_key,
                operation_type="decide",
                request_hash=request_hash,
            )
            if replay is not None:
                record, event = replay
                return current_run, record, event, False
            if current_run.state_version != expected_run_version:
                raise StateVersionConflict(
                    f"TaskRun {current_run.run_id} 版本冲突: "
                    f"期望 {expected_run_version}，实际 {current_run.state_version}"
                )
            if current_run.status != "paused":
                raise ControlConflict("高风险审批只能在 paused 安全边界决定")
            if current_approval.version != expected_approval_version:
                raise StateVersionConflict(
                    f"ApprovalRecord 版本冲突: 期望 {expected_approval_version}，"
                    f"实际 {current_approval.version}"
                )
            if current_approval.status != "pending":
                raise ControlConflict(
                    f"ApprovalRecord 状态 {current_approval.status} 不能再次决定"
                )
            if (
                current_approval.tenant_id != clean_tenant
                or current_approval.subject_user_id != clean_actor
            ):
                raise ControlConflict("只有绑定的授权主体可以决定该请求")
            if _timestamp(current_approval.expires_at) <= _timestamp(now):
                raise ControlConflict("ApprovalRecord 已过期")

            next_approval_version = current_approval.version + 1
            next_run_version = current_run.state_version + 1
            connection.execute(
                """
                UPDATE approval_records
                SET status = ?, version = ?, decision_reason = ?,
                    decided_by_user_id = ?, decided_at = ?, updated_at = ?
                WHERE approval_id = ? AND version = ? AND status = 'pending'
                """,
                (
                    decision,
                    next_approval_version,
                    clean_reason,
                    clean_actor,
                    now,
                    now,
                    approval_id,
                    current_approval.version,
                ),
            )
            connection.execute(
                """
                UPDATE task_runs
                SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    next_run_version,
                    now,
                    current_run.run_id,
                    current_run.state_version,
                ),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                sequence=_next_sequence(connection, current_run.run_id),
                event_type=f"approval.{decision}",
                payload={
                    "approval_id": approval_id,
                    "plan_id": current_approval.plan_id,
                    "plan_version": current_approval.plan_version,
                    "step_id": current_approval.step_logical_id,
                    "decision": decision,
                    "reason": clean_reason,
                    "control": {
                        "command": "decide_approval",
                        "idempotency_key": clean_key,
                        "request_hash": request_hash,
                    },
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            connection.execute(
                """
                INSERT INTO approval_operations(
                    operation_id, approval_id, tenant_id, project_id,
                    actor_user_id, idempotency_key, operation_type,
                    request_hash, result_status, result_version, event_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'decide', ?, ?, ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    approval_id,
                    clean_tenant,
                    current_approval.project_id,
                    clean_actor,
                    clean_key,
                    request_hash,
                    decision,
                    next_approval_version,
                    event.event_id,
                    now,
                ),
            )
            updated_run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (current_run.run_id,),
            ).fetchone()
            updated_approval_row = connection.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            assert updated_run_row is not None and updated_approval_row is not None
            updated_run = _run_from_row(updated_run_row)
            updated_approval = _approval_from_row(updated_approval_row)
            snapshot = _merged_snapshot(connection, updated_run)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "pending_approval_id": None,
                    "last_approval_id": approval_id,
                    "last_approval_status": decision,
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    next_run_version,
                    _dump_json(snapshot),
                    now,
                    current_run.run_id,
                ),
            )
            _insert_checkpoint(
                connection,
                run_id=current_run.run_id,
                state_version=next_run_version,
                state=snapshot,
                reason=f"approval_{decision}:{approval_id}",
                created_at=now,
            )
        return updated_run, updated_approval, event, True

    def consume_approval(
        self,
        approval_id: str,
        *,
        expected_run_version: int,
        expected_approval_version: int,
        idempotency_key: str,
        tenant_id: str,
        actor_user_id: str,
        tool_name: str,
        tool_schema_hash: str,
        parameter_summary_hash: str,
    ) -> tuple[TaskRun, ApprovalRecord, TaskEvent, bool]:
        """执行前精确匹配并一次性消费已批准的授权。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_tenant = _required_text(tenant_id, "tenant_id")[:200]
        clean_actor = _required_text(actor_user_id, "actor_user_id")[:200]
        clean_tool = _required_text(tool_name, "tool_name")[:200]
        schema_hash = _required_sha256(tool_schema_hash, "tool_schema_hash")
        parameter_hash = _required_sha256(
            parameter_summary_hash,
            "parameter_summary_hash",
        )
        request_payload: JsonObject = {
            "approval_id": approval_id,
            "expected_approval_version": expected_approval_version,
            "tool_name": clean_tool,
            "tool_schema_hash": schema_hash,
            "parameter_summary_hash": parameter_hash,
        }
        request_hash = _control_request_hash("consume_approval", request_payload)
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            approval_row = connection.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if approval_row is None:
                raise ValueError("ApprovalRecord 不存在")
            current_approval = _approval_from_row(approval_row)
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (current_approval.run_id,),
            ).fetchone()
            if run_row is None:
                raise ValueError("ApprovalRecord 对应的 TaskRun 不存在")
            current_run = _run_from_row(run_row)
            replay = _approval_operation_replay(
                connection,
                approval_id=approval_id,
                project_id=current_approval.project_id,
                tenant_id=clean_tenant,
                actor_user_id=clean_actor,
                idempotency_key=clean_key,
                operation_type="consume",
                request_hash=request_hash,
            )
            if replay is not None:
                record, event = replay
                return current_run, record, event, False
            if current_run.state_version != expected_run_version:
                raise StateVersionConflict(
                    f"TaskRun {current_run.run_id} 版本冲突: "
                    f"期望 {expected_run_version}，实际 {current_run.state_version}"
                )
            if current_approval.version != expected_approval_version:
                raise StateVersionConflict(
                    f"ApprovalRecord 版本冲突: 期望 {expected_approval_version}，"
                    f"实际 {current_approval.version}"
                )
            if current_approval.status != "approved":
                raise ControlConflict("只有 approved 授权可以被消费")
            if (
                current_approval.tenant_id != clean_tenant
                or current_approval.subject_user_id != clean_actor
            ):
                raise ControlConflict("授权消费主体不匹配")
            if current_run.plan_version != current_approval.plan_version:
                raise ControlConflict("活动计划版本已变化，旧授权不可消费")
            if _timestamp(current_approval.expires_at) <= _timestamp(now):
                raise ControlConflict("ApprovalRecord 已过期")
            if (
                current_approval.tool_name != clean_tool
                or current_approval.tool_schema_hash != schema_hash
                or current_approval.parameter_summary_hash != parameter_hash
            ):
                raise ControlConflict("工具、schema 或参数摘要与授权绑定不一致")

            next_approval_version = current_approval.version + 1
            next_run_version = current_run.state_version + 1
            connection.execute(
                """
                UPDATE approval_records
                SET status = 'consumed', version = ?, consumed_at = ?, updated_at = ?
                WHERE approval_id = ? AND version = ? AND status = 'approved'
                """,
                (
                    next_approval_version,
                    now,
                    now,
                    approval_id,
                    current_approval.version,
                ),
            )
            connection.execute(
                """
                UPDATE task_runs
                SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    next_run_version,
                    now,
                    current_run.run_id,
                    current_run.state_version,
                ),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                sequence=_next_sequence(connection, current_run.run_id),
                event_type="approval.consumed",
                payload={
                    "approval_id": approval_id,
                    "plan_id": current_approval.plan_id,
                    "plan_version": current_approval.plan_version,
                    "step_id": current_approval.step_logical_id,
                    "tool_name": clean_tool,
                    "tool_schema_hash": schema_hash,
                    "parameter_summary_hash": parameter_hash,
                    "control": {
                        "command": "consume_approval",
                        "idempotency_key": clean_key,
                        "request_hash": request_hash,
                    },
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            connection.execute(
                """
                INSERT INTO approval_operations(
                    operation_id, approval_id, tenant_id, project_id,
                    actor_user_id, idempotency_key, operation_type,
                    request_hash, result_status, result_version, event_id,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'consume', ?, 'consumed', ?, ?, ?)
                """,
                (
                    uuid.uuid4().hex,
                    approval_id,
                    clean_tenant,
                    current_approval.project_id,
                    clean_actor,
                    clean_key,
                    request_hash,
                    next_approval_version,
                    event.event_id,
                    now,
                ),
            )
            updated_run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (current_run.run_id,),
            ).fetchone()
            updated_approval_row = connection.execute(
                "SELECT * FROM approval_records WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            assert updated_run_row is not None and updated_approval_row is not None
            updated_run = _run_from_row(updated_run_row)
            updated_approval = _approval_from_row(updated_approval_row)
            snapshot = _merged_snapshot(connection, updated_run)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "last_approval_id": approval_id,
                    "last_approval_status": "consumed",
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    next_run_version,
                    _dump_json(snapshot),
                    now,
                    current_run.run_id,
                ),
            )
        return updated_run, updated_approval, event, True

    def save_plan(
        self,
        run_id: str,
        *,
        expected_version: int,
        plan: JsonObject,
        reason: str,
        planner: JsonObject,
        step_status_overrides: dict[str, StepStatus] | None = None,
    ) -> tuple[TaskRun, TaskPlanRecord, list[TaskStepRecord], TaskEvent]:
        """原子保存 Planner 生成的不可变计划版本。"""
        run, record, steps, event, _ = self._save_plan(
            run_id,
            expected_version=expected_version,
            plan=plan,
            reason=reason,
            planner=planner,
            step_status_overrides=step_status_overrides,
            allowed_statuses={"planning", "running", "verifying"},
        )
        return run, record, steps, event

    def revise_plan_by_user(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        plan: JsonObject,
        reason: str,
        skipped_step_ids: set[str] | None = None,
    ) -> tuple[
        TaskRun,
        TaskPlanRecord,
        list[TaskStepRecord],
        TaskEvent,
        bool,
    ]:
        """在 paused 安全边界创建受 capability 约束的用户计划修订。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_reason = _required_text(reason, "修改原因")[:500]
        current_plan = self.get_active_plan(run_id)
        if current_plan is None:
            raise ControlConflict("TaskRun 没有可修改的活动计划")
        capabilities = {
            str(item.get("capability"))
            for item in _validated_plan_steps(current_plan.plan)
        }
        validation = validate_task_plan(
            plan,
            capabilities=capabilities,
            max_steps=24,
            allow_waiting_user=False,
        )
        if not validation.valid:
            raise ControlConflict(
                "用户计划修订未通过契约校验: " + "; ".join(validation.issues)
            )
        skipped = set(skipped_step_ids or set())
        request_payload: JsonObject = {
            "plan": plan,
            "reason": clean_reason,
            "skipped_step_ids": sorted(skipped),
        }
        request_hash = _control_request_hash("revise_plan", request_payload)
        return self._save_plan(
            run_id,
            expected_version=expected_version,
            plan=plan,
            reason=f"user:{clean_reason}"[:200],
            planner={"route": "user", "phase": "collaboration"},
            step_status_overrides={step_id: "skipped" for step_id in skipped},
            allowed_statuses={"paused"},
            control=(clean_key, "revise_plan", request_hash),
            checkpoint_reason="user_plan_revision",
        )

    def _save_plan(
        self,
        run_id: str,
        *,
        expected_version: int,
        plan: JsonObject,
        reason: str,
        planner: JsonObject,
        step_status_overrides: dict[str, StepStatus] | None = None,
        allowed_statuses: set[RunStatus],
        control: tuple[str, str, str] | None = None,
        checkpoint_reason: str | None = None,
    ) -> tuple[
        TaskRun,
        TaskPlanRecord,
        list[TaskStepRecord],
        TaskEvent,
        bool,
    ]:
        """原子保存一个不可变计划版本、步骤、事件和当前快照。"""
        clean_reason = _required_text(reason, "计划原因")[:200]
        step_definitions = _validated_plan_steps(plan)
        definitions_by_id = {
            str(definition["step_id"]): definition for definition in step_definitions
        }
        overrides = dict(step_status_overrides or {})
        unsupported_overrides = set(overrides.values()) - {"skipped", "blocked"}
        if unsupported_overrides:
            raise ValueError("计划修订只允许显式覆盖为 skipped 或 blocked")
        unknown_overrides = set(overrides) - set(definitions_by_id)
        if unknown_overrides:
            raise ValueError(
                "计划状态覆盖引用未知步骤: " + ", ".join(sorted(unknown_overrides))
            )
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(row)
            if control is not None:
                clean_key, command, request_hash = control
                replay = _control_replay(
                    connection,
                    run_id=run_id,
                    idempotency_key=clean_key,
                    command=command,
                    request_hash=request_hash,
                )
                if replay is not None:
                    replay_version = replay.payload.get("plan_version")
                    if not isinstance(replay_version, int):
                        raise RuntimeError("计划修改事件缺少 plan_version")
                    replay_plan_row = connection.execute(
                        """
                        SELECT * FROM task_plans
                        WHERE run_id = ? AND version = ?
                        """,
                        (run_id, replay_version),
                    ).fetchone()
                    if replay_plan_row is None:
                        raise RuntimeError("计划修改对应的计划版本不存在")
                    replay_step_rows = connection.execute(
                        """
                        SELECT step.* FROM task_steps AS step
                        JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                        WHERE step.run_id = ? AND plan.version = ?
                        ORDER BY step.position
                        """,
                        (run_id, replay_version),
                    ).fetchall()
                    return (
                        current,
                        _plan_from_row(replay_plan_row),
                        [_step_from_row(item) for item in replay_step_rows],
                        replay,
                        False,
                    )
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            if current.status not in allowed_statuses:
                message = f"TaskRun 状态 {current.status} 不能保存计划"
                if control is not None:
                    raise ControlConflict(message)
                raise ValueError(message)
            if current.status == "paused":
                active = connection.execute(
                    """
                    SELECT 1 FROM tool_invocations
                    WHERE run_id = ? AND status IN ('running', 'unknown')
                    LIMIT 1
                    """,
                    (run_id,),
                ).fetchone()
                if active is not None:
                    raise ControlConflict(
                        "任务存在执行中或结果未知的工具调用，不能修改计划"
                    )

            version = current.plan_version + 1
            previous_steps: dict[str, TaskStepRecord] = {}
            if current.plan_version > 0:
                previous_rows = connection.execute(
                    """
                    SELECT step.* FROM task_steps AS step
                    JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                    WHERE step.run_id = ? AND plan.version = ?
                    ORDER BY step.position
                    """,
                    (run_id, current.plan_version),
                ).fetchall()
                previous_steps = {
                    step.logical_id: step
                    for step in (_step_from_row(item) for item in previous_rows)
                }
                for logical_id, previous in previous_steps.items():
                    if previous.status not in {"completed", "skipped"}:
                        continue
                    revised_definition = definitions_by_id.get(logical_id)
                    if revised_definition is None:
                        raise ValueError(
                            f"计划修订不能删除已{previous.status}步骤: {logical_id}"
                        )
                    if revised_definition != previous.definition:
                        raise ValueError(
                            f"计划修订不能修改已{previous.status}步骤: {logical_id}"
                        )

            plan_record = TaskPlanRecord(
                plan_id=uuid.uuid4().hex,
                run_id=run_id,
                version=version,
                reason=clean_reason,
                plan=plan,
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO task_plans(
                    plan_id, run_id, version, reason, plan_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_record.plan_id,
                    plan_record.run_id,
                    plan_record.version,
                    plan_record.reason,
                    _dump_json(plan_record.plan),
                    plan_record.created_at,
                ),
            )
            steps: list[TaskStepRecord] = []
            for position, definition in enumerate(step_definitions):
                logical_id = str(definition["step_id"])
                previous_step = previous_steps.get(logical_id)
                if (
                    previous_step is not None
                    and previous_step.status in {"completed", "skipped"}
                ):
                    status = previous_step.status
                    started_at = previous_step.started_at
                    completed_at = previous_step.completed_at
                else:
                    status = overrides.get(logical_id, "pending")
                    started_at = None
                    completed_at = now if status in {"skipped", "blocked"} else None
                step = TaskStepRecord(
                    step_id=uuid.uuid4().hex,
                    plan_id=plan_record.plan_id,
                    run_id=run_id,
                    position=position,
                    logical_id=logical_id,
                    status=status,
                    definition=definition,
                    started_at=started_at,
                    completed_at=completed_at,
                )
                connection.execute(
                    """
                    INSERT INTO task_steps(
                        step_id, plan_id, run_id, position, status, step_json,
                        started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step.step_id,
                        step.plan_id,
                        step.run_id,
                        step.position,
                        step.status,
                        _dump_json(step.definition),
                        step.started_at,
                        step.completed_at,
                    ),
                )
                steps.append(step)

            next_state_version = current.state_version + 1
            connection.execute(
                """
                UPDATE task_runs
                SET state_version = ?, plan_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    next_state_version,
                    version,
                    now,
                    run_id,
                    expected_version,
                ),
            )
            event_payload: JsonObject = {
                "plan_id": plan_record.plan_id,
                "plan_version": version,
                "supersedes_version": version - 1 if version > 1 else None,
                "reason": clean_reason,
                "summary": str(plan.get("summary", ""))[:500],
                "steps": [
                    {
                        "step_id": step.logical_id,
                        "purpose": step.definition["purpose"],
                        "capability": step.definition["capability"],
                        "dependencies": step.definition["dependencies"],
                        "status": step.status,
                    }
                    for step in steps
                ],
                "assumptions": plan.get("assumptions", []),
                "clarifications": plan.get("clarifications", []),
                "planner": planner,
            }
            if control is not None:
                clean_key, command, request_hash = control
                event_payload["control"] = {
                    "command": command,
                    "idempotency_key": clean_key,
                    "request_hash": request_hash,
                }
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type="plan.created" if version == 1 else "plan.revised",
                payload=event_payload,
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "active_plan_id": plan_record.plan_id,
                    "active_plan": plan,
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_state_version, _dump_json(snapshot), now, run_id),
            )
            if checkpoint_reason is not None:
                _insert_checkpoint(
                    connection,
                    run_id=run_id,
                    state_version=next_state_version,
                    state=snapshot,
                    reason=checkpoint_reason,
                    created_at=now,
                )
        return updated, plan_record, steps, event, True

    def get_active_plan(self, run_id: str) -> TaskPlanRecord | None:
        """读取当前 ``plan_version`` 指向的计划。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT plan.* FROM task_plans AS plan
                JOIN task_runs AS run
                  ON run.run_id = plan.run_id AND run.plan_version = plan.version
                WHERE plan.run_id = ?
                """,
                (run_id,),
            ).fetchone()
        return _plan_from_row(row) if row is not None else None

    def list_plans(self, run_id: str) -> list[TaskPlanRecord]:
        """按版本返回一个任务的完整计划历史。"""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM task_plans WHERE run_id = ? ORDER BY version",
                (run_id,),
            ).fetchall()
        return [_plan_from_row(row) for row in rows]

    def list_plan_steps(
        self, run_id: str, *, plan_version: int | None = None
    ) -> list[TaskStepRecord]:
        """返回指定计划版本的步骤；默认返回当前计划。"""
        with self._connection() as connection:
            if plan_version is None:
                rows = connection.execute(
                    """
                    SELECT step.* FROM task_steps AS step
                    JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                    JOIN task_runs AS run
                      ON run.run_id = plan.run_id AND run.plan_version = plan.version
                    WHERE step.run_id = ?
                    ORDER BY step.position
                    """,
                    (run_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT step.* FROM task_steps AS step
                    JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                    WHERE step.run_id = ? AND plan.version = ?
                    ORDER BY step.position
                    """,
                    (run_id, plan_version),
                ).fetchall()
        return [_step_from_row(row) for row in rows]

    def list_events(
        self, run_id: str, *, after_sequence: int = 0, limit: int = 200
    ) -> list[TaskEvent]:
        bounded_limit = min(max(limit, 1), 1000)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, sequence, event_type, payload_json, occurred_at
                FROM task_events
                WHERE run_id = ? AND sequence > ?
                ORDER BY sequence
                LIMIT ?
                """,
                (run_id, max(after_sequence, 0), bounded_limit),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_events_by_type(self, run_id: str, event_type: str) -> list[TaskEvent]:
        """读取某类持久事件；用于服务端审计投影，不受浏览器分页窗口影响。"""
        clean_type = _required_text(event_type, "事件类型")
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, sequence, event_type, payload_json, occurred_at
                FROM task_events
                WHERE run_id = ? AND event_type = ?
                ORDER BY sequence
                """,
                (run_id, clean_type),
            ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_recent_events_by_type(
        self,
        run_id: str,
        event_type: str,
        *,
        limit: int = 100,
    ) -> list[TaskEvent]:
        """按时间正序返回最近一段同类事件，避免反馈投影无界增长。"""
        clean_type = _required_text(event_type, "事件类型")
        bounded_limit = min(max(limit, 1), 1000)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT event_id, run_id, sequence, event_type, payload_json, occurred_at
                FROM task_events
                WHERE run_id = ? AND event_type = ?
                ORDER BY sequence DESC
                LIMIT ?
                """,
                (run_id, clean_type, bounded_limit),
            ).fetchall()
        return [_event_from_row(row) for row in reversed(rows)]

    def record_user_feedback(
        self,
        run_id: str,
        *,
        expected_version: int,
        idempotency_key: str,
        subject_user_id: str,
        rating: Literal["helpful", "not_helpful"],
        comment: str | None,
        evidence_ids: tuple[str, ...] = (),
        artifact_ids: tuple[str, ...] = (),
    ) -> tuple[TaskRun, TaskEvent, bool]:
        """为终态 Run 追加可幂等反馈，不改写历史 Evidence 或 Artifact。"""
        clean_key = _required_text(idempotency_key, "Idempotency-Key")[:200]
        clean_subject = _required_text(subject_user_id, "反馈主体")[:200]
        if rating not in {"helpful", "not_helpful"}:
            raise ValueError("反馈评分无效")
        clean_comment = comment.strip()[:1000] if comment and comment.strip() else None
        clean_evidence_ids = tuple(dict.fromkeys(evidence_ids))
        clean_artifact_ids = tuple(dict.fromkeys(artifact_ids))
        request_payload: JsonObject = {
            "rating": rating,
            "comment": clean_comment,
            "evidence_ids": list(clean_evidence_ids),
            "artifact_ids": list(clean_artifact_ids),
        }
        request_hash = _control_request_hash(
            "record_user_feedback",
            {**request_payload, "subject_user_id": clean_subject},
        )
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(row)
            replay = _control_replay(
                connection,
                run_id=run_id,
                idempotency_key=clean_key,
                command="record_user_feedback",
                request_hash=request_hash,
            )
            if replay is not None:
                return current, replay, False
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            if current.status not in {"completed", "blocked", "failed", "cancelled"}:
                raise ControlConflict("只能对终态 TaskRun 提交反馈")
            _validate_feedback_references(
                connection,
                run_id=run_id,
                evidence_ids=clean_evidence_ids,
                artifact_ids=clean_artifact_ids,
            )
            next_version = current.state_version + 1
            connection.execute(
                """
                UPDATE task_runs SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (next_version, now, run_id, current.state_version),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type="user.feedback",
                payload={
                    "feedback_id": uuid.uuid4().hex,
                    "subject_user_id": clean_subject,
                    **request_payload,
                    "control": {
                        "command": "record_user_feedback",
                        "idempotency_key": clean_key,
                        "request_hash": request_hash,
                    },
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "last_feedback_id": event.payload["feedback_id"],
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
        return updated, event, True

    def transition(
        self,
        run_id: str,
        *,
        expected_version: int,
        status: RunStatus,
        event_type: str,
        payload: JsonObject,
        terminal_reason: str | None = None,
        usage: JsonObject | None = None,
        checkpoint_reason: str | None = None,
    ) -> tuple[TaskRun, TaskEvent]:
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current = _run_from_row(row)
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            ensure_transition(current.status, status)
            next_version = current.state_version + 1
            now = _utc_now()
            next_usage = current.usage if usage is None else usage
            finished_at = now if status in {"completed", "blocked", "failed", "cancelled"} else None
            connection.execute(
                """
                UPDATE task_runs
                SET status = ?, state_version = ?, terminal_reason = ?,
                    usage_json = ?, updated_at = ?, finished_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    status,
                    next_version,
                    terminal_reason,
                    _dump_json(next_usage),
                    now,
                    finished_at,
                    run_id,
                    expected_version,
                ),
            )
            sequence = _next_sequence(connection, run_id)
            event = TaskEvent(
                uuid.uuid4().hex, run_id, sequence, event_type, payload, now
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot.update(
                {
                    "last_sequence": sequence,
                    "completed_step_ids": _completed_step_ids(
                        connection, run_id
                    ),
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
            if checkpoint_reason is not None:
                _insert_checkpoint(
                    connection,
                    run_id=run_id,
                    state_version=next_version,
                    state=snapshot,
                    reason=checkpoint_reason,
                    created_at=now,
                )
        return updated, event

    def update_contract(
        self,
        contract: TaskContract,
        *,
        expected_version: int,
    ) -> tuple[TaskRun, TaskEvent]:
        """Persist a strengthened contract and its lifecycle event atomically."""
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (contract.run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"TaskRun 不存在: {contract.run_id}")
            current = _run_from_row(row)
            if current.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {contract.run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current.state_version}"
                )
            next_version = current.state_version + 1
            now = _utc_now()
            connection.execute(
                """
                UPDATE task_contracts
                SET contract_json = ?, contract_hash = ?
                WHERE run_id = ?
                """,
                (_dump_json(contract.to_dict()), contract.content_hash, contract.run_id),
            )
            connection.execute(
                """
                UPDATE task_runs
                SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (next_version, now, contract.run_id, expected_version),
            )
            event = TaskEvent(
                uuid.uuid4().hex,
                contract.run_id,
                _next_sequence(connection, contract.run_id),
                "goal",
                {
                    "goal": contract.goal,
                    "success_criteria": [
                        item.to_dict() for item in contract.success_criteria
                    ],
                    "constraints": list(contract.constraints),
                    "updated": True,
                },
                now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (contract.run_id,)
            ).fetchone()
            assert updated_row is not None
            updated = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated)
            snapshot["last_sequence"] = event.sequence
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, contract.run_id),
            )
        return updated, event

    def start_invocation(
        self,
        *,
        run_id: str,
        tool_call_id: str,
        tool_name: str,
        arguments: JsonObject,
        idempotency_key: str,
        step_id: str | None = None,
    ) -> tuple[ToolInvocation, bool]:
        args_json = _dump_json(arguments)
        args_hash = _hash_text(args_json)
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT * FROM tool_invocations
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                invocation = _invocation_from_row(existing)
                if invocation.tool_name != tool_name or invocation.args_hash != args_hash:
                    raise IdempotencyConflict("幂等键已绑定到不同的工具调用")
                return invocation, False
            invocation = ToolInvocation(
                invocation_id=uuid.uuid4().hex,
                run_id=run_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                args_hash=args_hash,
                args=arguments,
                status="running",
                result_hash=None,
                error_text=None,
                artifact_id=None,
                started_at=_utc_now(),
                completed_at=None,
            )
            connection.execute(
                """
                INSERT INTO tool_invocations(
                    invocation_id, run_id, step_id, tool_call_id, tool_name,
                    idempotency_key, args_hash, args_json, status, result_hash,
                    error_text, artifact_id, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL, ?, NULL)
                """,
                (
                    invocation.invocation_id,
                    invocation.run_id,
                    invocation.step_id,
                    invocation.tool_call_id,
                    invocation.tool_name,
                    invocation.idempotency_key,
                    invocation.args_hash,
                    args_json,
                    invocation.started_at,
                ),
            )
        return invocation, True

    def start_invocation_with_event(
        self,
        *,
        run_id: str,
        expected_version: int,
        tool_call_id: str,
        tool_name: str,
        arguments: JsonObject,
        idempotency_key: str,
        policy_decision: JsonObject,
        step_id: str | None = None,
    ) -> tuple[TaskRun, ToolInvocation, TaskEvent | None, bool]:
        """Atomically persist a running invocation and its ``step.started`` event."""
        args_json = _dump_json(arguments)
        args_hash = _hash_text(args_json)
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if run_row is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            current_run = _run_from_row(run_row)
            existing = connection.execute(
                """
                SELECT * FROM tool_invocations
                WHERE run_id = ? AND idempotency_key = ?
                """,
                (run_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                invocation = _invocation_from_row(existing)
                if invocation.tool_name != tool_name or invocation.args_hash != args_hash:
                    raise IdempotencyConflict("幂等键已绑定到不同的工具调用")
                return current_run, invocation, None, False
            if current_run.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current_run.state_version}"
                )
            if current_run.status != "running":
                raise ValueError("TaskRun 不在 running 状态，不能开始工具调用")
            logical_step_id = tool_call_id
            if step_id is not None:
                step_row = connection.execute(
                    """
                    SELECT step.* FROM task_steps AS step
                    JOIN task_plans AS plan ON plan.plan_id = step.plan_id
                    WHERE step.step_id = ? AND step.run_id = ?
                      AND plan.version = ?
                    """,
                    (step_id, run_id, current_run.plan_version),
                ).fetchone()
                if step_row is None:
                    raise ValueError("工具调用引用的 TaskStep 不属于当前计划")
                logical_step_id = _step_logical_id(step_row)
            attempt = (
                _logical_step_attempt_count(connection, run_id, logical_step_id) + 1
                if step_id is not None
                else 1
            )

            invocation = ToolInvocation(
                invocation_id=uuid.uuid4().hex,
                run_id=run_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                idempotency_key=idempotency_key,
                args_hash=args_hash,
                args=arguments,
                status="running",
                result_hash=None,
                error_text=None,
                artifact_id=None,
                started_at=now,
                completed_at=None,
            )
            connection.execute(
                """
                INSERT INTO tool_invocations(
                    invocation_id, run_id, step_id, tool_call_id, tool_name,
                    idempotency_key, args_hash, args_json, status, result_hash,
                    error_text, artifact_id, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL, ?, NULL)
                """,
                (
                    invocation.invocation_id,
                    invocation.run_id,
                    invocation.step_id,
                    invocation.tool_call_id,
                    invocation.tool_name,
                    invocation.idempotency_key,
                    invocation.args_hash,
                    args_json,
                    invocation.started_at,
                ),
            )
            if step_id is not None:
                connection.execute(
                    """
                    UPDATE task_steps
                    SET status = 'running', started_at = COALESCE(started_at, ?),
                        completed_at = NULL
                    WHERE step_id = ?
                    """,
                    (now, step_id),
                )
            next_version = current_run.state_version + 1
            connection.execute(
                """
                UPDATE task_runs SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (next_version, now, run_id, expected_version),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=_next_sequence(connection, run_id),
                event_type="step.started",
                payload={
                    "plan_version": current_run.plan_version,
                    "step_id": logical_step_id,
                    "persisted_step_id": step_id,
                    "attempt": attempt,
                    "tool": tool_name,
                    "invocation_id": invocation.invocation_id,
                    "arguments_hash": args_hash,
                    "policy": policy_decision,
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            assert updated_row is not None
            updated_run = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated_run)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "active_invocation_id": invocation.invocation_id,
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
            )
        return updated_run, invocation, event, True

    def complete_invocation(
        self,
        invocation_id: str,
        *,
        status: InvocationStatus,
        error_text: str | None = None,
    ) -> tuple[ToolInvocation, EvidenceRecord | None]:
        if status not in {"failed", "unknown"}:
            raise ValueError("成功工具结果必须使用 commit_tool_success 原子提交")
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"工具调用不存在: {invocation_id}")
            current = _invocation_from_row(row)
            if current.status != "running":
                return current, self._evidence_for_invocation(connection, invocation_id)
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = ?, result_hash = NULL, error_text = ?, artifact_id = NULL,
                    completed_at = ?
                WHERE invocation_id = ? AND status = 'running'
                """,
                (status, error_text, now, invocation_id),
            )
            updated = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            assert updated is not None
        return _invocation_from_row(updated), None

    def commit_tool_failure(
        self,
        invocation_id: str,
        *,
        expected_version: int,
        status: InvocationStatus,
        error_code: str,
        error_text: str,
        source: ObservationSource,
        retryable: bool,
    ) -> tuple[TaskRun, ToolInvocation, TaskEvent | None]:
        """Atomically persist failed/unknown invocation and its Observation event."""
        if status not in {"failed", "unknown"}:
            raise ValueError("失败提交只接受 failed 或 unknown")
        clean_code = _required_text(error_code, "Observation code")[:100]
        clean_error = _required_text(error_text, "工具错误")[:2000]
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            invocation_row = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?",
                (invocation_id,),
            ).fetchone()
            if invocation_row is None:
                raise ValueError(f"工具调用不存在: {invocation_id}")
            invocation = _invocation_from_row(invocation_row)
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (invocation.run_id,)
            ).fetchone()
            assert run_row is not None
            current_run = _run_from_row(run_row)
            if invocation.status != "running":
                return current_run, invocation, None
            if current_run.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {current_run.run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current_run.state_version}"
                )
            if current_run.status != "running":
                raise ValueError("TaskRun 不在 running 状态，不能提交工具失败")

            logical_step_id = invocation.tool_call_id
            if invocation.step_id is not None:
                step_row = connection.execute(
                    "SELECT * FROM task_steps WHERE step_id = ?",
                    (invocation.step_id,),
                ).fetchone()
                if step_row is None:
                    raise ValueError("工具调用引用的 TaskStep 不存在")
                logical_step_id = _step_logical_id(step_row)
            attempt = (
                _logical_step_attempt_count(
                    connection, current_run.run_id, logical_step_id
                )
                if invocation.step_id is not None
                else 1
            )
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = ?, result_hash = NULL, error_text = ?, artifact_id = NULL,
                    completed_at = ?
                WHERE invocation_id = ? AND status = 'running'
                """,
                (status, clean_error, now, invocation_id),
            )
            if invocation.step_id is not None:
                connection.execute(
                    """
                    UPDATE task_steps
                    SET status = 'failed', completed_at = ?
                    WHERE step_id = ?
                    """,
                    (now, invocation.step_id),
                )
            observation = Observation(
                observation_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                step_id=logical_step_id,
                invocation_id=invocation.invocation_id,
                source=source,
                status="partial" if status == "unknown" else "error",
                code=clean_code,
                summary=clean_error[:1000],
                retryable=retryable,
                payload_ref=None,
                created_at=now,
            )
            next_version = current_run.state_version + 1
            connection.execute(
                """
                UPDATE task_runs SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (
                    next_version,
                    now,
                    current_run.run_id,
                    expected_version,
                ),
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                sequence=_next_sequence(connection, current_run.run_id),
                event_type="step.completed",
                payload={
                    "plan_version": current_run.plan_version,
                    "step_id": logical_step_id,
                    "persisted_step_id": invocation.step_id,
                    "attempt": attempt,
                    "status": status,
                    "tool": invocation.tool_name,
                    "invocation_id": invocation.invocation_id,
                    "observation": observation.to_dict(),
                    "evidence_ids": [],
                    "artifact_ids": [],
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (current_run.run_id,)
            ).fetchone()
            assert updated_row is not None
            updated_run = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated_run)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "active_invocation_id": None,
                    "last_observation": observation.to_dict(),
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    next_version,
                    _dump_json(snapshot),
                    now,
                    current_run.run_id,
                ),
            )
            updated_invocation_row = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?",
                (invocation.invocation_id,),
            ).fetchone()
            assert updated_invocation_row is not None
        return updated_run, _invocation_from_row(updated_invocation_row), event

    def commit_tool_success(
        self,
        invocation_id: str,
        *,
        expected_version: int,
        assistant_message_id: str,
        result: Any,
        evidence_kind: str,
        evidence_source: JsonObject,
        evidence_summary: JsonObject,
        artifact_draft: ArtifactDraft | None,
    ) -> tuple[
        TaskRun,
        ToolInvocation,
        EvidenceRecord,
        Artifact | None,
        TaskEvent,
        Checkpoint,
    ]:
        """Atomically commit a successful invocation and all durable outputs."""
        result_hash = _hash_text(_dump_json_value(result))
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            invocation_row = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?", (invocation_id,)
            ).fetchone()
            if invocation_row is None:
                raise ValueError(f"工具调用不存在: {invocation_id}")
            invocation = _invocation_from_row(invocation_row)
            if invocation.status != "running":
                raise ValueError("只有 running 工具调用可以提交成功结果")

            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (invocation.run_id,)
            ).fetchone()
            assert run_row is not None
            current_run = _run_from_row(run_row)
            if current_run.state_version != expected_version:
                raise StateVersionConflict(
                    f"TaskRun {current_run.run_id} 版本冲突: 期望 {expected_version}，"
                    f"实际 {current_run.state_version}"
                )
            if current_run.status != "running":
                raise ValueError("TaskRun 不在 running 状态，不能提交工具结果")

            logical_step_id = invocation.tool_call_id
            if invocation.step_id is not None:
                step_row = connection.execute(
                    "SELECT * FROM task_steps WHERE step_id = ?",
                    (invocation.step_id,),
                ).fetchone()
                if step_row is None:
                    raise ValueError("工具调用引用的 TaskStep 不存在")
                logical_step_id = _step_logical_id(step_row)
            attempt = (
                _logical_step_attempt_count(
                    connection, current_run.run_id, logical_step_id
                )
                if invocation.step_id is not None
                else 1
            )
            message_row = connection.execute(
                "SELECT conversation_id FROM messages WHERE id = ?",
                (assistant_message_id,),
            ).fetchone()
            if message_row is None:
                raise ValueError(f"消息不存在: {assistant_message_id}")
            if str(message_row["conversation_id"]) != current_run.conversation_id:
                raise ValueError("工具 Artifact 消息不属于当前任务对话")

            artifact: Artifact | None = None
            if artifact_draft is not None:
                artifact_type = _required_text(artifact_draft.type, "工件类型")
                if artifact_draft.payload is None and artifact_draft.file_ref is None:
                    raise ValueError("工件必须包含 payload 或 file_ref")
                if artifact_draft.source_tool != invocation.tool_name:
                    raise ValueError("Artifact 来源工具与 Invocation 不一致")
                dataset_ref = artifact_draft.dataset_ref
                if dataset_ref is not None:
                    dataset_row = connection.execute(
                        "SELECT project_id FROM datasets WHERE ref = ?", (dataset_ref,)
                    ).fetchone()
                    if (
                        dataset_row is None
                        or str(dataset_row["project_id"]) != current_run.project_id
                    ):
                        # 保留 v2.3 兼容行为：经典页未登记的数据集不阻止工件落库。
                        dataset_ref = None
                artifact = Artifact(
                    id=uuid.uuid4().hex,
                    conversation_id=current_run.conversation_id,
                    message_id=assistant_message_id,
                    type=artifact_type,
                    payload=artifact_draft.payload,
                    file_ref=artifact_draft.file_ref,
                    source_tool=artifact_draft.source_tool,
                    params=artifact_draft.params,
                    dataset_ref=dataset_ref,
                    created_at=now,
                )
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
                        _dump_json(artifact.payload)
                        if artifact.payload is not None
                        else None,
                        artifact.file_ref,
                        artifact.source_tool,
                        _dump_json(artifact.params)
                        if artifact.params is not None
                        else None,
                        artifact.dataset_ref,
                        artifact.created_at,
                    ),
                )
                if artifact.type == "report":
                    payload = artifact.payload or {}
                    raw_report_id = payload.get("report_id")
                    if not isinstance(raw_report_id, str):
                        raise ValueError("报告 Artifact 缺少 report_id")
                    report_id = validate_report_id(raw_report_id)
                    connection.execute(
                        """
                        INSERT INTO report_publications(
                            report_id, project_id, conversation_id, created_at
                        ) VALUES (?, ?, ?, ?)
                        """,
                        (
                            report_id,
                            current_run.project_id,
                            current_run.conversation_id,
                            now,
                        ),
                    )
                connection.execute(
                    "UPDATE conversations SET updated_at = ? WHERE id = ?",
                    (now, current_run.conversation_id),
                )

            summary = dict(evidence_summary)
            summary["artifact_id"] = artifact.id if artifact is not None else None
            evidence = EvidenceRecord(
                evidence_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                invocation_id=invocation.invocation_id,
                artifact_id=artifact.id if artifact is not None else None,
                kind=evidence_kind,
                source=evidence_source,
                result_hash=result_hash,
                summary=summary,
                created_at=now,
            )
            connection.execute(
                """
                UPDATE tool_invocations
                SET status = 'succeeded', result_hash = ?, error_text = NULL,
                    artifact_id = ?, completed_at = ?
                WHERE invocation_id = ? AND status = 'running'
                """,
                (result_hash, evidence.artifact_id, now, invocation.invocation_id),
            )
            if invocation.step_id is not None:
                connection.execute(
                    """
                    UPDATE task_steps
                    SET status = 'completed', completed_at = ?
                    WHERE step_id = ?
                    """,
                    (now, invocation.step_id),
                )
            connection.execute(
                """
                INSERT INTO evidence(
                    evidence_id, run_id, invocation_id, artifact_id, kind,
                    source_json, result_hash, summary_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    evidence.run_id,
                    evidence.invocation_id,
                    evidence.artifact_id,
                    evidence.kind,
                    _dump_json(evidence.source),
                    evidence.result_hash,
                    _dump_json(evidence.summary),
                    evidence.created_at,
                ),
            )

            next_version = current_run.state_version + 1
            connection.execute(
                """
                UPDATE task_runs SET state_version = ?, updated_at = ?
                WHERE run_id = ? AND state_version = ?
                """,
                (next_version, now, current_run.run_id, expected_version),
            )
            observation = Observation(
                observation_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                step_id=logical_step_id,
                invocation_id=invocation.invocation_id,
                source="tool",
                status="ok",
                code="tool_succeeded",
                summary=str(summary.get("summary", ""))[:1000],
                retryable=False,
                payload_ref=evidence.evidence_id,
                created_at=now,
            )
            event = TaskEvent(
                event_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                sequence=_next_sequence(connection, current_run.run_id),
                event_type="step.completed",
                payload={
                    "plan_version": current_run.plan_version,
                    "step_id": logical_step_id,
                    "persisted_step_id": invocation.step_id,
                    "attempt": attempt,
                    "status": "completed",
                    "tool": invocation.tool_name,
                    "invocation_id": invocation.invocation_id,
                    "summary": str(summary.get("summary", ""))[:1000],
                    "observation": observation.to_dict(),
                    "evidence_ids": [evidence.evidence_id],
                    "artifact_ids": [artifact.id] if artifact is not None else [],
                },
                occurred_at=now,
            )
            _insert_event(connection, event)
            updated_row = connection.execute(
                "SELECT * FROM task_runs WHERE run_id = ?", (current_run.run_id,)
            ).fetchone()
            assert updated_row is not None
            updated_run = _run_from_row(updated_row)
            snapshot = _merged_snapshot(connection, updated_run)
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "last_completed_invocation_id": invocation.invocation_id,
                    "last_evidence_ids": [evidence.evidence_id],
                    "last_artifact_ids": [artifact.id] if artifact is not None else [],
                    "active_invocation_id": None,
                    "last_observation": observation.to_dict(),
                    "completed_step_ids": _completed_step_ids(
                        connection, current_run.run_id
                    ),
                }
            )
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, current_run.run_id),
            )
            checkpoint = Checkpoint(
                checkpoint_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                sequence=_next_checkpoint_sequence(connection, current_run.run_id),
                state_version=next_version,
                state=snapshot,
                reason=f"tool_succeeded:{invocation.tool_name}",
                created_at=now,
            )
            connection.execute(
                """
                INSERT INTO checkpoints(
                    checkpoint_id, run_id, sequence, state_version,
                    state_json, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.checkpoint_id,
                    checkpoint.run_id,
                    checkpoint.sequence,
                    checkpoint.state_version,
                    _dump_json(checkpoint.state),
                    checkpoint.reason,
                    checkpoint.created_at,
                ),
            )
            updated_invocation_row = connection.execute(
                "SELECT * FROM tool_invocations WHERE invocation_id = ?",
                (invocation.invocation_id,),
            ).fetchone()
            assert updated_invocation_row is not None
        return (
            updated_run,
            _invocation_from_row(updated_invocation_row),
            evidence,
            artifact,
            event,
            checkpoint,
        )

    def list_invocations(self, run_id: str) -> list[ToolInvocation]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tool_invocations WHERE run_id = ? ORDER BY started_at, rowid",
                (run_id,),
            ).fetchall()
        return [_invocation_from_row(row) for row in rows]

    def list_evidence(self, run_id: str) -> list[EvidenceRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            ).fetchall()
        return [_evidence_from_row(row) for row in rows]

    def evidence_ledger_version(self, run_id: str) -> int:
        """返回 TaskRun 追加式 Evidence ledger 的当前版本（即已提交条目数）。"""
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT run.run_id, COUNT(evidence.evidence_id)
                FROM task_runs AS run
                LEFT JOIN evidence ON evidence.run_id = run.run_id
                WHERE run.run_id = ?
                GROUP BY run.run_id
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"TaskRun 不存在: {run_id}")
        return int(row[1])

    def resolve_definition_execution(
        self,
        run_id: str,
        *,
        tool_name: str,
        arguments: JsonObject,
    ) -> JsonObject | None:
        """Match a data call to a prior immutable definition Evidence record.

        Once the current run has compiled a definition for the requested Tool,
        altered arguments fail closed instead of silently becoming an unbound
        model-authored calculation. Repeated resolution of the same immutable
        version is harmless; two distinct matching versions require clarification.
        """
        arguments_hash = invocation_arguments_hash(arguments)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT evidence_id, source_json
                FROM evidence
                WHERE run_id = ?
                ORDER BY created_at, rowid
                """,
                (run_id,),
            ).fetchall()
        candidates: list[JsonObject] = []
        compiled_for_tool = False
        for row in rows:
            source = _load_object(str(row["source_json"]))
            compiled = source.get("compiled_invocation")
            resource = source.get("definition_resource")
            if not isinstance(compiled, dict) or not isinstance(resource, dict):
                continue
            if compiled.get("tool_name") != tool_name:
                continue
            compiled_for_tool = True
            if (
                compiled.get("definition_match") is not True
                or compiled.get("definition_id") != resource.get("definition_id")
                or compiled.get("definition_version")
                != resource.get("definition_version")
                or compiled.get("formula_hash") != resource.get("formula_hash")
            ):
                continue
            if compiled.get("arguments_hash") != arguments_hash:
                continue
            binding = _definition_execution_binding(
                resource,
                definition_evidence_id=str(row["evidence_id"]),
                compiled_tool_name=tool_name,
                compiled_arguments_hash=arguments_hash,
            )
            if binding is not None:
                candidates.append(binding)
        unique = {
            (
                str(item["definition_id"]),
                int(item["definition_version"]),
                str(item["formula_hash"]),
                str(item["resource_uri"]),
            ): item
            for item in candidates
        }
        if len(unique) > 1:
            raise ControlConflict("数据调用同时匹配多个领域定义版本，必须先澄清")
        if len(unique) == 1:
            return next(iter(unique.values()))
        if compiled_for_tool:
            raise ControlConflict("数据调用与已解析领域公式或定义版本不一致")
        return None

    def replace_claims(
        self, run_id: str, claims: list[ClaimDraft]
    ) -> list[ClaimRecord]:
        """Replace the current candidate Claim ledger and validate Evidence scope."""
        created: list[ClaimRecord] = []
        now = _utc_now()
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM task_runs WHERE run_id = ?", (run_id,)
            ).fetchone() is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            connection.execute("DELETE FROM claims WHERE run_id = ?", (run_id,))
            for draft in claims:
                if draft.evidence_ids:
                    placeholders = ",".join("?" for _ in draft.evidence_ids)
                    rows = connection.execute(
                        f"""
                        SELECT evidence_id FROM evidence
                        WHERE run_id = ? AND evidence_id IN ({placeholders})
                        """,
                        (run_id, *draft.evidence_ids),
                    ).fetchall()
                    available = {str(row[0]) for row in rows}
                    if available != set(draft.evidence_ids):
                        raise ValueError("Claim 引用了其他任务或不存在的 Evidence")
                record = ClaimRecord(
                    claim_id=uuid.uuid4().hex,
                    run_id=run_id,
                    statement=draft.statement,
                    claim_kind=draft.claim_kind,
                    value_refs=draft.value_refs,
                    evidence_ids=draft.evidence_ids,
                    created_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO claims(
                        claim_id, run_id, statement, claim_kind,
                        value_refs_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.claim_id,
                        record.run_id,
                        record.statement,
                        record.claim_kind,
                        _dump_json(list(record.value_refs)),
                        record.created_at,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO claim_evidence(claim_id, evidence_id)
                    VALUES (?, ?)
                    """,
                    [
                        (record.claim_id, evidence_id)
                        for evidence_id in record.evidence_ids
                    ],
                )
                created.append(record)
        return created

    def list_claims(self, run_id: str) -> list[ClaimRecord]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM claims WHERE run_id = ? ORDER BY created_at, rowid",
                (run_id,),
            ).fetchall()
            records: list[ClaimRecord] = []
            for row in rows:
                evidence_rows = connection.execute(
                    """
                    SELECT evidence_id FROM claim_evidence
                    WHERE claim_id = ? ORDER BY rowid
                    """,
                    (str(row["claim_id"]),),
                ).fetchall()
                records.append(
                    _claim_from_row(
                        row, tuple(str(item["evidence_id"]) for item in evidence_rows)
                    )
                )
        return records

    def create_checkpoint(self, run_id: str, *, reason: str) -> Checkpoint:
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            snapshot = connection.execute(
                """
                SELECT state_version, state_json FROM task_snapshots WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if snapshot is None:
                raise ValueError(f"TaskRun 不存在: {run_id}")
            state = _load_object(str(snapshot["state_json"]))
            state["completed_step_ids"] = _completed_step_ids(
                connection, run_id
            )
            checkpoint = _insert_checkpoint(
                connection,
                run_id=run_id,
                state_version=int(snapshot["state_version"]),
                state=state,
                reason=reason,
                created_at=_utc_now(),
            )
        return checkpoint

    def _evidence_for_invocation(
        self, connection: sqlite3.Connection, invocation_id: str
    ) -> EvidenceRecord | None:
        row = connection.execute(
            "SELECT * FROM evidence WHERE invocation_id = ?", (invocation_id,)
        ).fetchone()
        return _evidence_from_row(row) if row is not None else None

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


def invocation_idempotency_key(
    run_id: str, tool_call_id: str, tool_name: str, arguments: JsonObject
) -> str:
    material = ":".join((run_id, tool_call_id, tool_name, _dump_json(arguments)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _validate_parent_run(connection: sqlite3.Connection, run: TaskRun) -> None:
    if run.parent_run_id is None:
        return
    if run.parent_run_id == run.run_id:
        raise ValueError("TaskRun 不能引用自身作为父分支")
    row = connection.execute(
        "SELECT project_id, conversation_id, status FROM task_runs WHERE run_id = ?",
        (run.parent_run_id,),
    ).fetchone()
    if row is None:
        raise ValueError("父 TaskRun 不存在")
    if (
        str(row["project_id"]) != run.project_id
        or str(row["conversation_id"]) != run.conversation_id
    ):
        raise ValueError("父 TaskRun 不属于当前项目和对话")
    if str(row["status"]) not in {"completed", "blocked", "failed", "cancelled"}:
        raise ControlConflict("只能从终态 TaskRun 创建分析分支")


def _validate_feedback_references(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    evidence_ids: tuple[str, ...],
    artifact_ids: tuple[str, ...],
) -> None:
    if evidence_ids:
        placeholders = ",".join("?" for _ in evidence_ids)
        rows = connection.execute(
            f"SELECT evidence_id FROM evidence "
            f"WHERE run_id = ? AND evidence_id IN ({placeholders})",
            (run_id, *evidence_ids),
        ).fetchall()
        if {str(row["evidence_id"]) for row in rows} != set(evidence_ids):
            raise ControlConflict("反馈引用了不属于当前 Run 的 Evidence")
    if artifact_ids:
        placeholders = ",".join("?" for _ in artifact_ids)
        rows = connection.execute(
            f"SELECT DISTINCT artifact_id FROM tool_invocations "
            f"WHERE run_id = ? AND artifact_id IN ({placeholders})",
            (run_id, *artifact_ids),
        ).fetchall()
        if {str(row["artifact_id"]) for row in rows} != set(artifact_ids):
            raise ControlConflict("反馈引用了不属于当前 Run 的 Artifact")


def _new_run_records(
    *,
    project_id: str,
    conversation_id: str,
    user_message_id: str,
    contract: TaskContract,
    budget: JsonObject,
    parent_run_id: str | None,
    now: str,
    capability_catalog: JsonObject | None,
) -> tuple[TaskRun, TaskEvent, JsonObject, CapabilityCatalogSnapshot]:
    run = TaskRun(
        run_id=contract.run_id,
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        parent_run_id=parent_run_id,
        goal=contract.goal,
        status="planning",
        state_version=1,
        plan_version=0,
        budget=budget,
        usage={"tool_calls": 0},
        terminal_reason=None,
        created_at=now,
        updated_at=now,
        finished_at=None,
    )
    goal_payload: JsonObject = {
        "goal": contract.goal,
        "success_criteria": [item.to_dict() for item in contract.success_criteria],
        "constraints": list(contract.constraints),
    }
    event = TaskEvent(uuid.uuid4().hex, run.run_id, 1, "goal", goal_payload, now)
    snapshot = AgentState.from_run(run).to_dict()
    snapshot["last_sequence"] = event.sequence
    normalized_catalog = _normalize_capability_catalog(
        _EMPTY_CAPABILITY_CATALOG if capability_catalog is None else capability_catalog
    )
    capability_snapshot = CapabilityCatalogSnapshot(
        snapshot_id=uuid.uuid4().hex,
        run_id=run.run_id,
        schema_version=1,
        catalog=normalized_catalog,
        content_hash=_hash_text(_dump_json(normalized_catalog)),
        created_at=now,
    )
    return run, event, snapshot, capability_snapshot


def _insert_run_records(
    connection: sqlite3.Connection,
    run: TaskRun,
    contract: TaskContract,
    event: TaskEvent,
    snapshot: JsonObject,
    capability_snapshot: CapabilityCatalogSnapshot,
) -> None:
    _validate_parent_run(connection, run)
    connection.execute(
        """
        INSERT INTO task_runs(
            run_id, project_id, conversation_id, user_message_id, parent_run_id,
            goal, status, state_version, plan_version, budget_json, usage_json,
            terminal_reason, created_at, updated_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
        """,
        (
            run.run_id,
            run.project_id,
            run.conversation_id,
            run.user_message_id,
            run.parent_run_id,
            run.goal,
            run.status,
            run.state_version,
            run.plan_version,
            _dump_json(run.budget),
            _dump_json(run.usage),
            run.created_at,
            run.updated_at,
        ),
    )
    connection.execute(
        """
        INSERT INTO task_contracts(run_id, contract_json, contract_hash, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (run.run_id, _dump_json(contract.to_dict()), contract.content_hash, run.created_at),
    )
    _insert_event(connection, event)
    connection.execute(
        """
        INSERT INTO task_snapshots(run_id, state_version, state_json, updated_at)
        VALUES (?, ?, ?, ?)
        """,
        (run.run_id, run.state_version, _dump_json(snapshot), run.updated_at),
    )
    _insert_capability_catalog_snapshot(connection, capability_snapshot)


def _insert_capability_catalog_snapshot(
    connection: sqlite3.Connection,
    snapshot: CapabilityCatalogSnapshot,
) -> None:
    connection.execute(
        """
        INSERT INTO capability_catalog_snapshots(
            snapshot_id, run_id, schema_version, catalog_json, content_hash, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            snapshot.snapshot_id,
            snapshot.run_id,
            snapshot.schema_version,
            _dump_json(snapshot.catalog),
            snapshot.content_hash,
            snapshot.created_at,
        ),
    )


def _insert_event(connection: sqlite3.Connection, event: TaskEvent) -> None:
    connection.execute(
        """
        INSERT INTO task_events(
            event_id, run_id, sequence, event_type, payload_json, occurred_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event.event_id,
            event.run_id,
            event.sequence,
            event.event_type,
            _dump_json(event.payload),
            event.occurred_at,
        ),
    )


def _merged_snapshot(connection: sqlite3.Connection, run: TaskRun) -> JsonObject:
    """刷新基础状态，同时保留活动计划和最近 Observation 等恢复扩展。"""
    row = connection.execute(
        "SELECT state_json FROM task_snapshots WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()
    snapshot = _load_object(str(row["state_json"])) if row is not None else {}
    snapshot.update(AgentState.from_run(run).to_dict())
    return snapshot


def _next_sequence(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM task_events WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row[0])


def _next_checkpoint_sequence(connection: sqlite3.Connection, run_id: str) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) + 1 FROM checkpoints WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    return int(row[0])


def _completed_step_ids(
    connection: sqlite3.Connection,
    run_id: str,
) -> list[str]:
    rows = connection.execute(
        """
        SELECT json_extract(step.step_json, '$.step_id') AS logical_id
        FROM task_steps AS step
        JOIN task_plans AS plan ON plan.plan_id = step.plan_id
        JOIN task_runs AS run
          ON run.run_id = plan.run_id AND run.plan_version = plan.version
        WHERE step.run_id = ? AND step.status = 'completed'
        ORDER BY step.position
        """,
        (run_id,),
    ).fetchall()
    return [str(row["logical_id"]) for row in rows]


def _insert_checkpoint(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    state_version: int,
    state: JsonObject,
    reason: str,
    created_at: str,
) -> Checkpoint:
    checkpoint = Checkpoint(
        checkpoint_id=uuid.uuid4().hex,
        run_id=run_id,
        sequence=_next_checkpoint_sequence(connection, run_id),
        state_version=state_version,
        state=state,
        reason=_required_text(reason, "Checkpoint 原因")[:200],
        created_at=created_at,
    )
    connection.execute(
        """
        INSERT INTO checkpoints(
            checkpoint_id, run_id, sequence, state_version,
            state_json, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            checkpoint.checkpoint_id,
            checkpoint.run_id,
            checkpoint.sequence,
            checkpoint.state_version,
            _dump_json(checkpoint.state),
            checkpoint.reason,
            checkpoint.created_at,
        ),
    )
    return checkpoint


def _validate_checkpoint(
    connection: sqlite3.Connection,
    run: TaskRun,
    checkpoint: Checkpoint,
) -> None:
    if checkpoint.run_id != run.run_id:
        raise ControlConflict("Checkpoint 不属于当前 TaskRun")
    checkpoint_plan = checkpoint.state.get("plan_version")
    if checkpoint_plan != run.plan_version:
        raise ControlConflict("Checkpoint 计划版本已过期")
    last_sequence = checkpoint.state.get("last_sequence")
    if (
        not isinstance(last_sequence, int)
        or isinstance(last_sequence, bool)
        or last_sequence < 1
    ):
        raise ControlConflict("Checkpoint 事件游标无效")
    latest_event_row = connection.execute(
        "SELECT COALESCE(MAX(sequence), 0) FROM task_events WHERE run_id = ?",
        (run.run_id,),
    ).fetchone()
    if latest_event_row is None or last_sequence > int(latest_event_row[0]):
        raise ControlConflict("Checkpoint 事件游标超出持久化日志")
    raw_completed = checkpoint.state.get("completed_step_ids")
    if not isinstance(raw_completed, list) or not all(
        isinstance(item, str) and item for item in raw_completed
    ):
        raise ControlConflict("Checkpoint 已完成步骤集合无效")
    persisted_completed = _completed_step_ids(connection, run.run_id)
    if set(raw_completed) != set(persisted_completed):
        raise ControlConflict("Checkpoint 与当前已完成步骤不一致")
    unknown = connection.execute(
        """
        SELECT 1 FROM tool_invocations
        WHERE run_id = ? AND status IN ('running', 'unknown')
        LIMIT 1
        """,
        (run.run_id,),
    ).fetchone()
    if unknown is not None:
        raise ControlConflict("TaskRun 存在结果未知的工具调用，禁止自动恢复")


def _run_from_row(row: sqlite3.Row) -> TaskRun:
    return TaskRun(
        run_id=str(row["run_id"]),
        project_id=str(row["project_id"]),
        conversation_id=str(row["conversation_id"]),
        user_message_id=str(row["user_message_id"]),
        parent_run_id=_optional_text(row["parent_run_id"]),
        goal=str(row["goal"]),
        status=cast(RunStatus, str(row["status"])),
        state_version=int(row["state_version"]),
        plan_version=int(row["plan_version"]),
        budget=_load_object(str(row["budget_json"])),
        usage=_load_object(str(row["usage_json"])),
        terminal_reason=_optional_text(row["terminal_reason"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        finished_at=_optional_text(row["finished_at"]),
    )


def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
    return Checkpoint(
        checkpoint_id=str(row["checkpoint_id"]),
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        state_version=int(row["state_version"]),
        state=_load_object(str(row["state_json"])),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        payload=_load_object(str(row["payload_json"])),
        occurred_at=str(row["occurred_at"]),
    )


def _plan_from_row(row: sqlite3.Row) -> TaskPlanRecord:
    return TaskPlanRecord(
        plan_id=str(row["plan_id"]),
        run_id=str(row["run_id"]),
        version=int(row["version"]),
        reason=_optional_text(row["reason"]),
        plan=_load_object(str(row["plan_json"])),
        created_at=str(row["created_at"]),
    )


def _step_from_row(row: sqlite3.Row) -> TaskStepRecord:
    definition = _load_object(str(row["step_json"]))
    return TaskStepRecord(
        step_id=str(row["step_id"]),
        plan_id=str(row["plan_id"]),
        run_id=str(row["run_id"]),
        position=int(row["position"]),
        logical_id=str(definition["step_id"]),
        status=cast(StepStatus, str(row["status"])),
        definition=definition,
        started_at=_optional_text(row["started_at"]),
        completed_at=_optional_text(row["completed_at"]),
    )


def _approval_from_row(row: sqlite3.Row) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id=str(row["approval_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        run_id=str(row["run_id"]),
        plan_id=str(row["plan_id"]),
        plan_version=int(row["plan_version"]),
        task_step_id=str(row["task_step_id"]),
        step_logical_id=str(row["step_logical_id"]),
        subject_user_id=str(row["subject_user_id"]),
        requested_by_user_id=str(row["requested_by_user_id"]),
        tool_name=str(row["tool_name"]),
        tool_schema_hash=str(row["tool_schema_hash"]),
        parameter_summary_hash=str(row["parameter_summary_hash"]),
        risk_level=cast(ApprovalRiskLevel, str(row["risk_level"])),
        status=cast(ApprovalStatus, str(row["status"])),
        version=int(row["version"]),
        expires_at=str(row["expires_at"]),
        decision_reason=(
            str(row["decision_reason"])
            if row["decision_reason"] is not None
            else None
        ),
        decided_by_user_id=(
            str(row["decided_by_user_id"])
            if row["decided_by_user_id"] is not None
            else None
        ),
        requested_at=str(row["requested_at"]),
        updated_at=str(row["updated_at"]),
        decided_at=str(row["decided_at"]) if row["decided_at"] is not None else None,
        consumed_at=(
            str(row["consumed_at"]) if row["consumed_at"] is not None else None
        ),
        idempotency_key=str(row["idempotency_key"]),
        request_hash=str(row["request_hash"]),
        request_event_id=str(row["request_event_id"]),
    )


def _step_logical_id(row: sqlite3.Row) -> str:
    definition = _load_object(str(row["step_json"]))
    value = definition.get("step_id")
    if not isinstance(value, str) or not value:
        raise ValueError("数据库中的 TaskStep 缺少逻辑 step_id")
    return value


def _logical_step_attempt_count(
    connection: sqlite3.Connection, run_id: str, logical_step_id: str
) -> int:
    """跨计划版本统计同一逻辑步骤已经持久化的 Invocation 次数。"""
    rows = connection.execute(
        """
        SELECT step.step_json
        FROM tool_invocations AS invocation
        JOIN task_steps AS step ON step.step_id = invocation.step_id
        WHERE invocation.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    return sum(
        1
        for row in rows
        if _step_logical_id(row) == logical_step_id
    )


def _invocation_from_row(row: sqlite3.Row) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=str(row["invocation_id"]),
        run_id=str(row["run_id"]),
        step_id=_optional_text(row["step_id"]),
        tool_call_id=str(row["tool_call_id"]),
        tool_name=str(row["tool_name"]),
        idempotency_key=str(row["idempotency_key"]),
        args_hash=str(row["args_hash"]),
        args=_load_object(str(row["args_json"])),
        status=cast(InvocationStatus, str(row["status"])),
        result_hash=_optional_text(row["result_hash"]),
        error_text=_optional_text(row["error_text"]),
        artifact_id=_optional_text(row["artifact_id"]),
        started_at=str(row["started_at"]),
        completed_at=_optional_text(row["completed_at"]),
    )


def _evidence_from_row(row: sqlite3.Row) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=str(row["evidence_id"]),
        run_id=str(row["run_id"]),
        invocation_id=str(row["invocation_id"]),
        artifact_id=_optional_text(row["artifact_id"]),
        kind=str(row["kind"]),
        source=_load_object(str(row["source_json"])),
        result_hash=str(row["result_hash"]),
        summary=_load_object(str(row["summary_json"])),
        created_at=str(row["created_at"]),
    )


def _claim_from_row(
    row: sqlite3.Row, evidence_ids: tuple[str, ...]
) -> ClaimRecord:
    raw_refs = json.loads(str(row["value_refs_json"]))
    if not isinstance(raw_refs, list) or not all(
        isinstance(item, dict) for item in raw_refs
    ):
        raise ValueError("数据库中的 Claim value_refs 格式非法")
    return ClaimRecord(
        claim_id=str(row["claim_id"]),
        run_id=str(row["run_id"]),
        statement=str(row["statement"]),
        claim_kind=str(row["claim_kind"]),
        value_refs=tuple(cast(list[JsonObject], raw_refs)),
        evidence_ids=evidence_ids,
        created_at=str(row["created_at"]),
    )


def _capability_catalog_snapshot_from_row(
    row: sqlite3.Row,
) -> CapabilityCatalogSnapshot:
    catalog = _normalize_capability_catalog(_load_object(str(row["catalog_json"])))
    content_hash = str(row["content_hash"])
    if _hash_text(_dump_json(catalog)) != content_hash:
        raise ValueError("数据库中的 capability 目录快照 hash 不匹配")
    return CapabilityCatalogSnapshot(
        snapshot_id=str(row["snapshot_id"]),
        run_id=str(row["run_id"]),
        schema_version=int(row["schema_version"]),
        catalog=catalog,
        content_hash=content_hash,
        created_at=str(row["created_at"]),
    )


def _normalize_capability_catalog(catalog: JsonObject) -> JsonObject:
    """Validate and detach the persisted v1 catalog from caller-owned objects."""
    if catalog.get("schema") != "chatbi-capability-catalog-v1":
        raise ValueError("capability 目录 schema 不受支持")
    raw_capabilities = catalog.get("capabilities")
    raw_tools = catalog.get("tools")
    if not isinstance(raw_capabilities, list) or not all(
        isinstance(item, dict) for item in raw_capabilities
    ):
        raise ValueError("capability 目录 capabilities 格式非法")
    if not isinstance(raw_tools, list) or not all(
        isinstance(item, dict) for item in raw_tools
    ):
        raise ValueError("capability 目录 tools 格式非法")
    capability_names = [item.get("name") for item in raw_capabilities]
    tool_names = [item.get("tool_name") for item in raw_tools]
    if not all(isinstance(item, str) and item.strip() for item in capability_names):
        raise ValueError("capability 目录包含非法能力名")
    if not all(isinstance(item, str) and item.strip() for item in tool_names):
        raise ValueError("capability 目录包含非法工具名")
    if len(set(capability_names)) != len(capability_names):
        raise ValueError("capability 目录包含重复能力")
    if len(set(tool_names)) != len(tool_names):
        raise ValueError("capability 目录包含重复工具")
    # Canonical JSON round-trip both validates serializability and deep-copies values.
    return _load_object(_dump_json(catalog))


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def invocation_arguments_hash(arguments: JsonObject) -> str:
    """Use the same canonical hash as persisted ToolInvocation arguments."""
    return _hash_text(_dump_json(arguments))


def _dump_json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_object(value: str) -> JsonObject:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("数据库中的 JSON 字段不是对象")
    return cast(JsonObject, parsed)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _definition_execution_binding(
    resource: JsonObject,
    *,
    definition_evidence_id: str,
    compiled_tool_name: str,
    compiled_arguments_hash: str,
) -> JsonObject | None:
    definition_id = resource.get("definition_id")
    version = resource.get("definition_version")
    formula_hash = resource.get("formula_hash")
    resource_uri = resource.get("resource_uri")
    semantic_key = resource.get("semantic_key")
    source_ref = resource.get("source_ref")
    if (
        not isinstance(definition_id, str)
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
        or not isinstance(formula_hash, str)
        or not isinstance(resource_uri, str)
        or resource_uri != f"chatbi://domain-definitions/{definition_id}"
        or not isinstance(semantic_key, str)
        or not isinstance(source_ref, str)
    ):
        return None
    return {
        "definition_evidence_id": definition_evidence_id,
        "definition_id": definition_id,
        "definition_version": version,
        "semantic_key": semantic_key,
        "formula_hash": formula_hash,
        "resource_uri": resource_uri,
        "source_ref": source_ref,
        "compiled_tool_name": compiled_tool_name,
        "compiled_arguments_hash": compiled_arguments_hash,
    }


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _required_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label}不能为空")
    return clean


def _validated_plan_steps(plan: JsonObject) -> list[JsonObject]:
    raw_steps = plan.get("steps")
    if not isinstance(raw_steps, list):
        raise ValueError("TaskPlan.steps 必须是数组")
    steps: list[JsonObject] = []
    logical_ids: set[str] = set()
    for raw in raw_steps:
        if not isinstance(raw, dict):
            raise ValueError("TaskPlan.steps 条目必须是对象")
        definition = cast(JsonObject, raw)
        logical_id = definition.get("step_id")
        if not isinstance(logical_id, str) or not logical_id.strip():
            raise ValueError("TaskStep.step_id 不能为空")
        if logical_id in logical_ids:
            raise ValueError(f"TaskStep.step_id 重复: {logical_id}")
        for field in ("purpose", "capability"):
            value = definition.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"TaskStep.{field} 不能为空")
        dependencies = definition.get("dependencies")
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) and item for item in dependencies
        ):
            raise ValueError("TaskStep.dependencies 必须是字符串数组")
        logical_ids.add(logical_id)
        steps.append(definition)
    return steps


def _control_request_hash(command: str, payload: JsonObject) -> str:
    return _hash_text(
        _dump_json(
            {
                "command": command,
                "payload": payload,
            }
        )
    )


def _control_replay(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    idempotency_key: str,
    command: str,
    request_hash: str,
) -> TaskEvent | None:
    """在同一写事务内查找控制命令；相同键的不同请求必须冲突关闭。"""
    rows = connection.execute(
        """
        SELECT event_id, run_id, sequence, event_type, payload_json, occurred_at
        FROM task_events
        WHERE run_id = ?
        ORDER BY sequence DESC
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        payload = _load_object(str(row["payload_json"]))
        control = payload.get("control")
        if not isinstance(control, dict):
            continue
        if control.get("idempotency_key") != idempotency_key:
            continue
        if (
            control.get("command") != command
            or control.get("request_hash") != request_hash
        ):
            raise IdempotencyConflict("Idempotency-Key 已绑定到不同控制命令")
        return _event_from_row(row)
    return None


def _approval_operation_replay(
    connection: sqlite3.Connection,
    *,
    approval_id: str,
    project_id: str,
    tenant_id: str,
    actor_user_id: str,
    idempotency_key: str,
    operation_type: str,
    request_hash: str,
) -> tuple[ApprovalRecord, TaskEvent] | None:
    row = connection.execute(
        """
        SELECT * FROM approval_operations
        WHERE project_id = ? AND idempotency_key = ?
        """,
        (project_id, idempotency_key),
    ).fetchone()
    if row is None:
        return None
    if (
        str(row["approval_id"]) != approval_id
        or str(row["tenant_id"]) != tenant_id
        or str(row["actor_user_id"]) != actor_user_id
        or str(row["operation_type"]) != operation_type
        or str(row["request_hash"]) != request_hash
    ):
        raise IdempotencyConflict(
            "Idempotency-Key 已绑定到不同 ApprovalRecord 操作"
        )
    approval_row = connection.execute(
        "SELECT * FROM approval_records WHERE approval_id = ?",
        (approval_id,),
    ).fetchone()
    event_row = connection.execute(
        """
        SELECT event_id, run_id, sequence, event_type, payload_json, occurred_at
        FROM task_events WHERE event_id = ?
        """,
        (str(row["event_id"]),),
    ).fetchone()
    if approval_row is None or event_row is None:
        raise RuntimeError("ApprovalRecord 幂等操作结果不完整")
    return _approval_from_row(approval_row), _event_from_row(event_row)


def _required_sha256(value: str, label: str) -> str:
    clean = _required_text(value, label).lower()
    if len(clean) != 64 or any(char not in "0123456789abcdef" for char in clean):
        raise ValueError(f"{label} 必须是 64 位小写 SHA-256")
    return clean


def _normalized_timestamp(value: str, label: str) -> str:
    clean = _required_text(value, label)
    parsed = _timestamp(clean)
    return parsed.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError("时间戳必须是 ISO 8601 格式") from exc
    if parsed.tzinfo is None:
        raise ValueError("时间戳必须包含时区")
    return parsed.astimezone(UTC)


def _valid_clarification_answer(value: object) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and len(value) <= 20_000
    try:
        encoded = _dump_json_value(value)
    except (TypeError, ValueError):
        return False
    return len(encoded) <= 20_000


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
