"""v2.5 阶段 4A 计划干预与 ApprovalRecord 协作契约回归。"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from apps.api.auth import current_principal_dep
from apps.api.deps import session_store_dep
from apps.api.main import app
from apps.api.routers import agent_runs
from apps.orchestrator.control.contracts import build_minimal_contract
from fastapi import Header
from packages.governance.permissions import Principal
from packages.session.migrations import CURRENT_SCHEMA_VERSION, v7, v8, v9, v10
from packages.session.store import SessionStore
from packages.session.task_store import (
    ControlConflict,
    IdempotencyConflict,
    TaskStore,
)

_ALICE_TOKEN = "collaboration-alice-token-00000001"
_BOB_TOKEN = "collaboration-bob-token-0000000002"
_HASH_A = hashlib.sha256(b"schema").hexdigest()
_HASH_B = hashlib.sha256(b"parameters").hexdigest()


def _approval_expiry() -> str:
    return (
        (datetime.now(UTC) + timedelta(hours=1))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _plan(*, summary: str = "分析后生成报告") -> dict[str, object]:
    return {
        "schema_version": 1,
        "summary": summary,
        "steps": [
            {
                "step_id": "analyze",
                "purpose": "分析数据",
                "capability": "stats.trend",
                "dependencies": [],
                "expected_evidence": ["趋势 Evidence"],
                "completion_conditions": ["趋势分析成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            },
            {
                "step_id": "report",
                "purpose": "生成报告",
                "capability": "report.generate",
                "dependencies": ["analyze"],
                "expected_evidence": ["报告 Artifact"],
                "completion_conditions": ["报告生成成功"],
                "fallback": [{"when": "失败", "action": "block"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }


def _paused_run(
    tmp_path: Path,
) -> tuple[SessionStore, TaskStore, str, str, int]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project(
        "阶段 4A",
        owner_user_id="alice",
        tenant_id="tenant-a",
    )
    conversation = store.create_conversation(project.id)
    message = store.append_message(
        conversation_id=conversation.id,
        role="user",
        content="分析后生成报告",
    )
    tasks = TaskStore(store.db_path)
    contract = build_minimal_contract(
        run_id="collaboration-run",
        user_text=message.content,
        chart_required=False,
        report_required=True,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    run, _, _, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=_plan(),
        reason="initial:template",
        planner={"route": "template"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, _, _ = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="pause-for-collaboration",
        command="pause",
        allowed_statuses={"running"},
        status="paused",
        event_type="run.paused",
        payload={"reason": "user_requested"},
        require_idle=True,
        checkpoint_reason="user_pause",
    )
    return store, tasks, project.id, run.run_id, run.state_version


def test_user_plan_revision_is_paused_versioned_and_idempotent(
    tmp_path: Path,
) -> None:
    _, tasks, _, run_id, state_version = _paused_run(tmp_path)
    revised = _plan(summary="保留分析，跳过报告")
    revised["steps"][0]["purpose"] = "按用户要求重新分析数据"  # type: ignore[index]

    run, plan, steps, event, created = tasks.revise_plan_by_user(
        run_id,
        expected_version=state_version,
        idempotency_key="revise-plan-once",
        plan=revised,
        reason="本轮不需要报告",
        skipped_step_ids={"report"},
    )
    replayed, replay_plan, replay_steps, replay_event, replay_created = tasks.revise_plan_by_user(
        run_id,
        expected_version=state_version,
        idempotency_key="revise-plan-once",
        plan=revised,
        reason="本轮不需要报告",
        skipped_step_ids={"report"},
    )

    assert created is True and replay_created is False
    assert run.status == "paused"
    assert plan.version == 2
    assert [step.status for step in steps] == ["pending", "skipped"]
    assert replayed == run
    assert replay_plan == plan
    assert replay_steps == steps
    assert replay_event == event
    assert event.payload["control"]["command"] == "revise_plan"
    assert tasks.get_latest_checkpoint(run_id).reason == "user_plan_revision"  # type: ignore[union-attr]

    changed = _plan(summary="不同计划")
    with pytest.raises(IdempotencyConflict):
        tasks.revise_plan_by_user(
            run_id,
            expected_version=run.state_version,
            idempotency_key="revise-plan-once",
            plan=changed,
            reason="不同请求",
        )
    changed["steps"][0]["capability"] = "code.execute"  # type: ignore[index]
    with pytest.raises(ControlConflict, match="capability"):
        tasks.revise_plan_by_user(
            run_id,
            expected_version=run.state_version,
            idempotency_key="expand-capability",
            plan=changed,
            reason="尝试扩张能力",
        )


def test_v6_database_is_backed_up_and_migrated_to_collaboration_schema(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v6.db"
    SessionStore(str(db_path))
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        for trigger in v10.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v10.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v10.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute("DELETE FROM schema_migrations WHERE version = ?", (v10.VERSION,))
        for trigger in v9.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v9.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v9.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v9.VERSION,),
        )
        for trigger in v8.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v8.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v8.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v8.VERSION,),
        )
        for trigger in v7.ADDED_TRIGGERS:
            connection.execute(f'DROP TRIGGER IF EXISTS "{trigger}"')
        for index in v7.ADDED_INDEXES:
            connection.execute(f'DROP INDEX IF EXISTS "{index}"')
        for table in v7.ADDED_TABLES:
            connection.execute(f'DROP TABLE IF EXISTS "{table}"')
        connection.execute(
            "DELETE FROM schema_migrations WHERE version = ?",
            (v7.VERSION,),
        )
        connection.execute(f"PRAGMA user_version = {v7.VERSION - 1}")

    migrated = SessionStore(str(db_path))

    assert migrated.schema_version == CURRENT_SCHEMA_VERSION
    backups = list(tmp_path.glob("v6.db.v6-backup.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(db_path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        migration = connection.execute(
            """
            SELECT checksum, source_version, source_sha256
            FROM schema_migrations WHERE version = ?
            """,
            (v7.VERSION,),
        ).fetchone()
    assert set(v7.ADDED_TABLES).issubset(tables)
    assert migration is not None
    assert migration[0] == v7.CHECKSUM
    assert migration[1] == v7.VERSION - 1
    assert migration[2] == hashlib.sha256(backups[0].read_bytes()).hexdigest()


def test_executor_approval_request_atomically_pauses_running_task(
    tmp_path: Path,
) -> None:
    _, tasks, _, run_id, state_version = _paused_run(tmp_path)
    running, _, _ = tasks.control_transition(
        run_id,
        expected_version=state_version,
        idempotency_key="resume-for-executor-approval",
        command="resume",
        allowed_statuses={"paused"},
        status="running",
        event_type="run.resumed",
        payload={"reason": "test"},
        require_checkpoint=True,
    )

    paused, approval, event, created = tasks.request_approval(
        run_id,
        expected_version=running.state_version,
        idempotency_key="executor-pauses-for-approval",
        tenant_id="tenant-a",
        subject_user_id="alice",
        requested_by_user_id="alice",
        step_id="analyze",
        tool_name="high_risk_export",
        tool_schema_hash=_HASH_A,
        parameter_summary_hash=_HASH_B,
        risk_level="critical",
        expires_at=_approval_expiry(),
        pause_run=True,
    )

    assert created is True
    assert paused.status == "paused"
    assert event.event_type == "approval.requested"
    assert event.payload["run_status"] == "paused"
    assert approval.status == "pending"
    assert tasks.has_valid_pending_approval(run_id) is True
    checkpoint = tasks.get_latest_checkpoint(run_id)
    assert checkpoint is not None
    assert checkpoint.reason == f"approval_requested:{approval.approval_id}"


def test_approval_binding_decision_and_consumption_are_fail_closed(
    tmp_path: Path,
) -> None:
    store, tasks, _, run_id, state_version = _paused_run(tmp_path)
    run, approval, event, created = tasks.request_approval(
        run_id,
        expected_version=state_version,
        idempotency_key="request-approval-once",
        tenant_id="tenant-a",
        subject_user_id="alice",
        requested_by_user_id="alice",
        step_id="analyze",
        tool_name="high_risk_export",
        tool_schema_hash=_HASH_A,
        parameter_summary_hash=_HASH_B,
        risk_level="high",
        expires_at=_approval_expiry(),
    )
    replayed_run, replayed, replay_event, replay_created = tasks.request_approval(
        run_id,
        expected_version=state_version,
        idempotency_key="request-approval-once",
        tenant_id="tenant-a",
        subject_user_id="alice",
        requested_by_user_id="alice",
        step_id="analyze",
        tool_name="high_risk_export",
        tool_schema_hash=_HASH_A,
        parameter_summary_hash=_HASH_B,
        risk_level="high",
        expires_at=approval.expires_at,
    )
    assert created is True and replay_created is False
    assert approval.status == "pending" and approval.version == 1
    assert replayed_run == run and replayed == approval and replay_event == event
    assert event.payload["parameter_summary_hash"] == _HASH_B
    assert "parameters" not in event.payload

    with pytest.raises(ControlConflict, match="绑定的授权主体"):
        tasks.decide_approval(
            approval.approval_id,
            expected_run_version=run.state_version,
            expected_approval_version=approval.version,
            idempotency_key="bob-cannot-decide",
            tenant_id="tenant-a",
            actor_user_id="bob",
            decision="approved",
            reason="越权批准",
        )

    run, approved, decision_event, decision_created = tasks.decide_approval(
        approval.approval_id,
        expected_run_version=run.state_version,
        expected_approval_version=approval.version,
        idempotency_key="approve-once",
        tenant_id="tenant-a",
        actor_user_id="alice",
        decision="approved",
        reason="已核对导出范围",
    )
    assert decision_created is True
    assert approved.status == "approved" and approved.version == 2
    assert decision_event.event_type == "approval.approved"

    with pytest.raises(ControlConflict, match="参数摘要"):
        tasks.consume_approval(
            approval.approval_id,
            expected_run_version=run.state_version,
            expected_approval_version=approved.version,
            idempotency_key="consume-wrong-parameters",
            tenant_id="tenant-a",
            actor_user_id="alice",
            tool_name="high_risk_export",
            tool_schema_hash=_HASH_A,
            parameter_summary_hash=hashlib.sha256(b"changed").hexdigest(),
        )
    run, consumed, consume_event, consume_created = tasks.consume_approval(
        approval.approval_id,
        expected_run_version=run.state_version,
        expected_approval_version=approved.version,
        idempotency_key="consume-once",
        tenant_id="tenant-a",
        actor_user_id="alice",
        tool_name="high_risk_export",
        tool_schema_hash=_HASH_A,
        parameter_summary_hash=_HASH_B,
    )
    replayed_run, replayed, replay_event, replay_created = tasks.consume_approval(
        approval.approval_id,
        expected_run_version=run.state_version - 1,
        expected_approval_version=approved.version,
        idempotency_key="consume-once",
        tenant_id="tenant-a",
        actor_user_id="alice",
        tool_name="high_risk_export",
        tool_schema_hash=_HASH_A,
        parameter_summary_hash=_HASH_B,
    )
    assert consume_created is True and replay_created is False
    assert consumed.status == "consumed" and consumed.version == 3
    assert consume_event.event_type == "approval.consumed"
    assert replayed_run == run and replayed == consumed and replay_event == consume_event
    with pytest.raises(IdempotencyConflict):
        tasks.consume_approval(
            approval.approval_id,
            expected_run_version=run.state_version - 1,
            expected_approval_version=approved.version,
            idempotency_key="consume-once",
            tenant_id="tenant-a",
            actor_user_id="bob",
            tool_name="high_risk_export",
            tool_schema_hash=_HASH_A,
            parameter_summary_hash=_HASH_B,
        )

    with sqlite3.connect(store.db_path) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            connection.execute(
                """
                UPDATE approval_records
                SET tool_name = 'tampered'
                WHERE approval_id = ?
                """,
                (approval.approval_id,),
            )


@pytest.fixture
def collaboration_api(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TaskStore, str, str, int]]:
    store, tasks, project_id, run_id, state_version = _paused_run(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            INSERT INTO project_memberships(
                project_id, user_id, tenant_id, role, created_at
            ) VALUES (?, 'bob', 'tenant-a', 'editor', ?)
            """,
            (project_id, "2026-07-31T00:00:00Z"),
        )

    async def store_override() -> SessionStore:
        return store

    async def principal_override(
        authorization: str = Header(alias="Authorization"),
    ) -> Principal:
        token = authorization.removeprefix("Bearer ")
        user_id = "alice" if token == _ALICE_TOKEN else "bob"
        return Principal(user_id=user_id, tenant_id="tenant-a")

    async def execution_override() -> object:
        return object()

    async def direct_threadpool(
        function: Callable[..., object],
        *args: object,
        **kwargs: object,
    ) -> object:
        return function(*args, **kwargs)

    app.dependency_overrides[session_store_dep] = store_override
    app.dependency_overrides[current_principal_dep] = principal_override
    app.dependency_overrides[
        agent_runs._run_execution_services_dep  # noqa: SLF001
    ] = execution_override
    monkeypatch.setattr(agent_runs, "run_in_threadpool", direct_threadpool)
    try:
        yield tasks, project_id, run_id, state_version
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_collaboration_api_enforces_subject_version_and_idempotency(
    collaboration_api: tuple[TaskStore, str, str, int],
) -> None:
    tasks, _, run_id, state_version = collaboration_api
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    revision_payload = {
        "plan": _plan(summary="通过 API 修改计划"),
        "reason": "用户调整分析说明",
        "skipped_step_ids": [],
    }
    headers = {
        "Authorization": f"Bearer {_ALICE_TOKEN}",
        "If-Match": str(state_version),
        "Idempotency-Key": "api-plan-revision",
    }
    response = await client.post(
        f"/agent/runs/{run_id}/plan/revisions",
        json=revision_payload,
        headers=headers,
    )
    assert response.status_code == 200
    assert response.json()["plan"]["version"] == 2
    assert response.json()["replayed"] is False
    replay = await client.post(
        f"/agent/runs/{run_id}/plan/revisions",
        json=revision_payload,
        headers=headers,
    )
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    current = tasks.get_run(run_id)
    assert current is not None

    run, approval, _, _ = tasks.request_approval(
        run_id,
        expected_version=current.state_version,
        idempotency_key="seed-api-approval",
        tenant_id="tenant-a",
        subject_user_id="alice",
        requested_by_user_id="alice",
        step_id="analyze",
        tool_name="high_risk_export",
        tool_schema_hash=_HASH_A,
        parameter_summary_hash=_HASH_B,
        risk_level="critical",
        expires_at=_approval_expiry(),
    )
    blocked_resume = await client.post(
        f"/agent/runs/{run_id}/resume/stream",
        headers={
            "Authorization": f"Bearer {_ALICE_TOKEN}",
            "If-Match": str(run.state_version),
            "Idempotency-Key": "resume-before-approval",
        },
    )
    assert blocked_resume.status_code == 409
    assert "待决定" in blocked_resume.json()["detail"]
    alice_headers = {"Authorization": f"Bearer {_ALICE_TOKEN}"}
    bob_headers = {"Authorization": f"Bearer {_BOB_TOKEN}"}
    approvals = await client.get(
        f"/agent/runs/{run_id}/approvals",
        headers=alice_headers,
    )
    assert len(approvals.json()) == 1
    bob_approvals = await client.get(
        f"/agent/runs/{run_id}/approvals",
        headers=bob_headers,
    )
    assert bob_approvals.json() == []

    decision_path = f"/agent/runs/{run_id}/approvals/{approval.approval_id}/decision"
    decision_headers = {
        "Authorization": f"Bearer {_ALICE_TOKEN}",
        "If-Match": str(run.state_version),
        "Idempotency-Key": "api-approval-decision",
    }
    bob_decision_headers = {
        **decision_headers,
        "Authorization": f"Bearer {_BOB_TOKEN}",
    }
    assert (
        await client.post(
            decision_path,
            json={
                "expected_version": approval.version,
                "decision": "approved",
                "reason": "越权",
            },
            headers=bob_decision_headers,
        )
    ).status_code == 409
    decision = await client.post(
        decision_path,
        json={
            "expected_version": approval.version,
            "decision": "approved",
            "reason": "确认执行",
        },
        headers=decision_headers,
    )
    assert decision.status_code == 200
    assert decision.json()["approval"]["status"] == "approved"
    assert decision.json()["replayed"] is False
    assert (
        await client.post(
            decision_path,
            json={
                "expected_version": approval.version,
                "decision": "approved",
                "reason": "确认执行",
            },
            headers=bob_decision_headers,
        )
    ).status_code == 409
    replay = await client.post(
        decision_path,
        json={
            "expected_version": approval.version,
            "decision": "approved",
            "reason": "确认执行",
        },
        headers=decision_headers,
    )
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    await client.aclose()
