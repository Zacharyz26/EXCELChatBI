"""SQLite repository for TaskRun, events, invocations and Evidence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from apps.orchestrator.control.contracts import TaskContract
from apps.orchestrator.control.state import AgentState, ensure_transition

from packages.session.models import Artifact, ArtifactDraft, Conversation, JsonObject, Message
from packages.session.task_models import (
    Checkpoint,
    ClaimDraft,
    ClaimRecord,
    EvidenceRecord,
    InvocationStatus,
    Observation,
    ObservationSource,
    RunStatus,
    TaskEvent,
    TaskRun,
    ToolInvocation,
)


class StateVersionConflict(RuntimeError):
    """The persisted run changed since the caller read it."""


class IdempotencyConflict(RuntimeError):
    """An idempotency key was reused for a different invocation."""


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
    ) -> tuple[TaskRun, TaskEvent]:
        now = _utc_now()
        run, event, snapshot = _new_run_records(
            project_id=project_id,
            conversation_id=conversation_id,
            user_message_id=user_message_id,
            contract=contract,
            budget=budget,
            now=now,
        )
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            _insert_run_records(connection, run, contract, event, snapshot)
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
        run, event, snapshot = _new_run_records(
            project_id=project_id,
            conversation_id=conversation_id,
            user_message_id=message.id,
            contract=contract,
            budget=budget,
            now=now,
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
            _insert_run_records(connection, run, contract, event, snapshot)

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
            snapshot = AgentState.from_run(updated).to_dict()
            snapshot["last_sequence"] = sequence
            connection.execute(
                """
                UPDATE task_snapshots
                SET state_version = ?, state_json = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (next_version, _dump_json(snapshot), now, run_id),
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
            snapshot = AgentState.from_run(updated).to_dict()
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

            invocation = ToolInvocation(
                invocation_id=uuid.uuid4().hex,
                run_id=run_id,
                step_id=None,
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
                ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, 'running', NULL, NULL, NULL, ?, NULL)
                """,
                (
                    invocation.invocation_id,
                    invocation.run_id,
                    invocation.tool_call_id,
                    invocation.tool_name,
                    invocation.idempotency_key,
                    invocation.args_hash,
                    args_json,
                    invocation.started_at,
                ),
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
                    "step_id": tool_call_id,
                    "attempt": 1,
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
            snapshot = AgentState.from_run(updated_run).to_dict()
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

            connection.execute(
                """
                UPDATE tool_invocations
                SET status = ?, result_hash = NULL, error_text = ?, artifact_id = NULL,
                    completed_at = ?
                WHERE invocation_id = ? AND status = 'running'
                """,
                (status, clean_error, now, invocation_id),
            )
            observation = Observation(
                observation_id=uuid.uuid4().hex,
                run_id=current_run.run_id,
                step_id=invocation.tool_call_id,
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
                    "step_id": invocation.tool_call_id,
                    "attempt": 1,
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
            snapshot = AgentState.from_run(updated_run).to_dict()
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
                step_id=invocation.tool_call_id,
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
                    "step_id": invocation.tool_call_id,
                    "attempt": 1,
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
            snapshot = AgentState.from_run(updated_run).to_dict()
            snapshot.update(
                {
                    "last_sequence": event.sequence,
                    "last_completed_invocation_id": invocation.invocation_id,
                    "last_evidence_ids": [evidence.evidence_id],
                    "last_artifact_ids": [artifact.id] if artifact is not None else [],
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
            sequence = _next_checkpoint_sequence(connection, run_id)
            checkpoint = Checkpoint(
                checkpoint_id=uuid.uuid4().hex,
                run_id=run_id,
                sequence=sequence,
                state_version=int(snapshot["state_version"]),
                state=_load_object(str(snapshot["state_json"])),
                reason=reason,
                created_at=_utc_now(),
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


def _new_run_records(
    *,
    project_id: str,
    conversation_id: str,
    user_message_id: str,
    contract: TaskContract,
    budget: JsonObject,
    now: str,
) -> tuple[TaskRun, TaskEvent, JsonObject]:
    run = TaskRun(
        run_id=contract.run_id,
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        parent_run_id=None,
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
    return run, event, snapshot


def _insert_run_records(
    connection: sqlite3.Connection,
    run: TaskRun,
    contract: TaskContract,
    event: TaskEvent,
    snapshot: JsonObject,
) -> None:
    connection.execute(
        """
        INSERT INTO task_runs(
            run_id, project_id, conversation_id, user_message_id, parent_run_id,
            goal, status, state_version, plan_version, budget_json, usage_json,
            terminal_reason, created_at, updated_at, finished_at
        ) VALUES (?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, NULL, ?, ?, NULL)
        """,
        (
            run.run_id,
            run.project_id,
            run.conversation_id,
            run.user_message_id,
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


def _event_from_row(row: sqlite3.Row) -> TaskEvent:
    return TaskEvent(
        event_id=str(row["event_id"]),
        run_id=str(row["run_id"]),
        sequence=int(row["sequence"]),
        event_type=str(row["event_type"]),
        payload=_load_object(str(row["payload_json"])),
        occurred_at=str(row["occurred_at"]),
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


def _dump_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _dump_json_value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _load_object(value: str) -> JsonObject:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("数据库中的 JSON 字段不是对象")
    return cast(JsonObject, parsed)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _required_text(value: str, label: str) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label}不能为空")
    return clean


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
