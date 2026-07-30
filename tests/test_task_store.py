"""v2.4 stage-1 migration and TaskStore tests."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.session.migrations import downgrade_v2_to_v1
from packages.session.models import ArtifactDraft
from packages.session.store import _SCHEMA_V1, SessionStore
from packages.session.task_models import ClaimDraft, InvocationStatus, ObservationSource
from packages.session.task_store import (
    ControlConflict,
    IdempotencyConflict,
    StateVersionConflict,
    TaskStore,
    invocation_idempotency_key,
)


def _workspace(tmp_path: Path) -> tuple[SessionStore, TaskStore, str, str, str]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project("Agent 控制面")
    conversation = session.create_conversation(project.id)
    _, message = session.start_user_turn(
        conversation_id=conversation.id,
        content="生成销售图表",
        suggested_title="生成销售图表",
    )
    return session, TaskStore(session.db_path), project.id, conversation.id, message.id


def test_real_v1_database_is_backed_up_and_migrated(tmp_path: Path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(_SCHEMA_V1)
        connection.execute(
            "INSERT INTO projects(id, name, created_at) VALUES ('p1', '旧项目', 'now')"
        )

    store = SessionStore(str(db_path))

    assert store.schema_version == 4
    assert store.get_project("p1") is not None
    backups = list(tmp_path.glob("legacy.db.v1-backup.*.sqlite3"))
    assert len(backups) == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT source_sha256, backup_path FROM schema_migrations WHERE version = 2"
        ).fetchone()
    assert row is not None
    assert row[0] == hashlib.sha256(backups[0].read_bytes()).hexdigest()
    assert row[1] == str(backups[0])


def test_migration_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "tampered.db"
    SessionStore(str(db_path))
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 2"
        )

    with pytest.raises(RuntimeError, match="checksum 不匹配"):
        SessionStore(str(db_path))


def test_unversioned_nonempty_database_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "unversioned.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE foreign_data(id INTEGER PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="未标版本的非空数据库"):
        SessionStore(str(db_path))


def test_task_run_events_transition_and_optimistic_version(tmp_path: Path) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="run-1",
        user_text="生成销售图表",
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )
    run, goal_event = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )

    assert run.status == "planning" and run.state_version == 1
    assert goal_event.sequence == 1 and goal_event.event_type == "goal"
    running, event = tasks.transition(
        run.run_id,
        expected_version=1,
        status="running",
        event_type="run.started",
        payload={"reason": "contract_created"},
    )
    assert running.status == "running" and running.state_version == 2
    assert event.sequence == 2
    assert [item.sequence for item in tasks.list_events(run.run_id)] == [1, 2]
    with pytest.raises(StateVersionConflict):
        tasks.transition(
            run.run_id,
            expected_version=1,
            status="verifying",
            event_type="verification.started",
            payload={},
        )


def test_task_plan_and_steps_are_versioned_and_persisted_atomically(
    tmp_path: Path,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="planned-run",
        user_text="检查质量并生成图表",
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    plan = {
        "schema_version": 1,
        "summary": "先画像再出图",
        "steps": [
            {
                "step_id": "profile",
                "purpose": "取得字段与质量画像",
                "capability": "data.profile",
                "dependencies": [],
                "expected_evidence": ["画像 Evidence"],
                "completion_conditions": ["画像调用成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            },
            {
                "step_id": "chart",
                "purpose": "生成图表",
                "capability": "visualization.chart",
                "dependencies": ["profile"],
                "expected_evidence": ["图表 Artifact"],
                "completion_conditions": ["图表文件已持久化"],
                "fallback": [{"when": "失败", "action": "retry"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }

    updated, saved, steps, event = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:template",
        planner={"route": "template", "response_hash": "hash"},
    )

    assert updated.plan_version == 1
    assert updated.state_version == run.state_version + 1
    assert saved.version == 1 and saved.plan == plan
    assert [step.logical_id for step in steps] == ["profile", "chart"]
    assert [step.status for step in steps] == ["pending", "pending"]
    assert event.event_type == "plan.created"
    assert event.payload["planner"]["route"] == "template"
    assert tasks.get_active_plan(run.run_id) == saved
    assert tasks.list_plans(run.run_id) == [saved]
    assert tasks.list_plan_steps(run.run_id) == steps
    snapshot = tasks.get_snapshot(run.run_id)
    assert snapshot is not None
    assert snapshot["active_plan_id"] == saved.plan_id
    assert snapshot["active_plan"] == plan

    running, _ = tasks.transition(
        run.run_id,
        expected_version=updated.state_version,
        status="running",
        event_type="run.started",
        payload={"reason": "plan_created"},
    )
    running_snapshot = tasks.get_snapshot(run.run_id)
    assert running.status == "running"
    assert running_snapshot is not None
    assert running_snapshot["active_plan_id"] == saved.plan_id
    assert running_snapshot["active_plan"] == plan

    revised_plan = {**plan, "summary": "修订后的计划", "steps": plan["steps"][:1]}
    revised_run, revised, revised_steps, revised_event = tasks.save_plan(
        run.run_id,
        expected_version=running.state_version,
        plan=revised_plan,
        reason="observation:column_changed",
        planner={"route": "template", "response_hash": "revised"},
    )
    assert revised_run.plan_version == 2
    assert revised.version == 2
    assert revised_event.event_type == "plan.revised"
    assert [item.version for item in tasks.list_plans(run.run_id)] == [1, 2]
    assert tasks.list_plan_steps(run.run_id) == revised_steps
    assert tasks.list_plan_steps(run.run_id, plan_version=1) == steps


def test_task_plan_save_rejects_version_conflict_without_partial_rows(
    tmp_path: Path,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="plan-conflict",
        user_text="检查数据",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    plan = {
        "schema_version": 1,
        "summary": "检查数据",
        "steps": [],
        "assumptions": [],
        "clarifications": [],
    }

    with pytest.raises(StateVersionConflict):
        tasks.save_plan(
            run.run_id,
            expected_version=run.state_version + 1,
            plan=plan,
            reason="stale",
            planner={"route": "fast"},
        )

    assert tasks.list_plans(run.run_id) == []
    assert tasks.list_plan_steps(run.run_id) == []


def test_plan_revision_preserves_completed_steps_and_can_explicitly_skip_pending(
    tmp_path: Path,
) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="preserved-plan",
        user_text="检查异常后按需清洗",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    plan = {
        "schema_version": 1,
        "summary": "先检查后清洗",
        "steps": [
            {
                "step_id": "detect",
                "purpose": "检测异常",
                "capability": "stats.anomaly",
                "dependencies": [],
                "expected_evidence": ["异常 Evidence"],
                "completion_conditions": ["异常检测成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            },
            {
                "step_id": "clean",
                "purpose": "有异常时创建衍生数据集",
                "capability": "dataset.transform",
                "dependencies": ["detect"],
                "expected_evidence": ["衍生数据集"],
                "completion_conditions": ["登记血缘"],
                "fallback": [{"when": "没有异常", "action": "block"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, _, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:llm",
        planner={"route": "llm"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="detect-call",
        tool_name="anomaly_detect",
        arguments={"dataset_ref": "d" * 32, "columns": ["销售额"]},
        idempotency_key="detect-once",
        policy_decision={"allowed": True},
        step_id=steps[0].step_id,
    )
    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="执行异常检测",
    )
    run, _, _, _, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result={"n_anomalies": 0, "anomalies": []},
        evidence_kind="tool_result",
        evidence_source={"tool": "anomaly_detect"},
        evidence_summary={"summary": "未发现异常"},
        artifact_draft=None,
    )

    revised_run, _, revised_steps, event = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan={**plan, "summary": "未发现异常，跳过清洗"},
        reason="observation:no_anomalies",
        planner={"route": "template"},
        step_status_overrides={"clean": "skipped"},
    )

    assert revised_run.plan_version == 2
    assert [item.status for item in revised_steps] == ["completed", "skipped"]
    assert revised_steps[0].definition == steps[0].definition
    assert event.payload["supersedes_version"] == 1
    assert [item["status"] for item in event.payload["steps"]] == [
        "completed",
        "skipped",
    ]


def test_plan_revision_cannot_remove_a_completed_step(tmp_path: Path) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="immutable-completed-step",
        user_text="检查画像",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    plan = {
        "schema_version": 1,
        "summary": "读取画像",
        "steps": [
            {
                "step_id": "profile",
                "purpose": "读取画像",
                "capability": "data.profile",
                "dependencies": [],
                "expected_evidence": ["画像"],
                "completion_conditions": ["画像成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, _, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="profile-call",
        tool_name="get_data_profile",
        arguments={"dataset_ref": "d" * 32},
        idempotency_key="profile-once",
        policy_decision={"allowed": True},
        step_id=steps[0].step_id,
    )
    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="执行画像",
    )
    run, _, _, _, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result={"profile": {"row_count": 3}},
        evidence_kind="tool_result",
        evidence_source={"tool": "get_data_profile"},
        evidence_summary={"summary": "画像完成"},
        artifact_draft=None,
    )
    empty_plan = {
        "schema_version": 1,
        "summary": "非法删除已完成步骤",
        "steps": [],
        "assumptions": [],
        "clarifications": [],
    }

    with pytest.raises(ValueError, match="不能删除已completed步骤"):
        tasks.save_plan(
            run.run_id,
            expected_version=run.state_version,
            plan=empty_plan,
            reason="invalid",
            planner={"route": "template"},
        )

    assert len(tasks.list_plans(run.run_id)) == 1


def test_user_turn_run_contract_and_goal_are_created_atomically(tmp_path: Path) -> None:
    session = SessionStore(str(tmp_path / "atomic.db"))
    project = session.create_project("原子任务")
    conversation = session.create_conversation(project.id)
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="atomic-run",
        user_text="分析销售额",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    updated, message, run, event = tasks.start_run_with_user_turn(
        project_id=project.id,
        conversation_id=conversation.id,
        content="  分析销售额  ",
        suggested_title="分析销售额",
        contract=contract,
        budget={"max_tool_calls": 3},
    )

    assert updated.title == "分析销售额"
    assert message.content == "分析销售额"
    assert run.user_message_id == message.id
    assert event.event_type == "goal"
    assert session.list_messages(conversation.id) == [message]
    assert tasks.get_contract(run.run_id) == contract.to_dict()


def test_atomic_task_start_rolls_back_user_message_on_run_failure(tmp_path: Path) -> None:
    db_path = tmp_path / "rollback.db"
    session = SessionStore(str(db_path))
    project = session.create_project("回滚任务")
    conversation = session.create_conversation(project.id)
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="must-rollback",
        user_text="分析销售额",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_task_run
            BEFORE INSERT ON task_runs
            BEGIN
                SELECT RAISE(ABORT, 'forced task failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced task failure"):
        tasks.start_run_with_user_turn(
            project_id=project.id,
            conversation_id=conversation.id,
            content="分析销售额",
            suggested_title="分析销售额",
            contract=contract,
            budget={"max_tool_calls": 3},
        )

    assert session.list_messages(conversation.id) == []
    unchanged = session.get_conversation(conversation.id)
    assert unchanged is not None and unchanged.title == "新对话"
    assert tasks.get_run(contract.run_id) is None


def test_invocation_is_idempotent_and_success_creates_evidence(tmp_path: Path) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="run-2",
        user_text="生成销售图表",
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    arguments = {"dataset_ref": "sales", "chart_type": "bar"}
    key = invocation_idempotency_key("run-2", "call-1", "gen_chart", arguments)
    invocation, created = tasks.start_invocation(
        run_id="run-2",
        tool_call_id="call-1",
        tool_name="gen_chart",
        arguments=arguments,
        idempotency_key=key,
    )
    repeated, repeated_created = tasks.start_invocation(
        run_id="run-2",
        tool_call_id="call-1",
        tool_name="gen_chart",
        arguments=arguments,
        idempotency_key=key,
    )
    assert created is True and repeated_created is False
    assert repeated.invocation_id == invocation.invocation_id
    with pytest.raises(ValueError, match="commit_tool_success"):
        tasks.complete_invocation(invocation.invocation_id, status="succeeded")

    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="生成图表",
    )
    _, completed, evidence, artifact, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=running.state_version,
        assistant_message_id=assistant.id,
        result={"option": {"series": [{"data": [1]}]}},
        evidence_kind="tool_result",
        evidence_source={"tool": "gen_chart", "tool_call_id": "call-1"},
        evidence_summary={"summary": "图表已生成"},
        artifact_draft=ArtifactDraft(
            type="chart",
            payload={"option": {"series": [{"data": [1]}]}},
            file_ref=None,
            source_tool="gen_chart",
            params=None,
            dataset_ref=None,
        ),
    )

    assert artifact is not None
    assert completed.status == "succeeded"
    assert completed.artifact_id == artifact.id
    assert evidence is not None and evidence.result_hash
    assert tasks.list_evidence("run-2") == [evidence]

    claims = tasks.replace_claims(
        "run-2",
        [
            ClaimDraft(
                statement="图表包含 1 个数据点。",
                claim_kind="numeric",
                value_refs=(
                    {
                        "token": "1",
                        "supported": True,
                        "evidence_id": evidence.evidence_id,
                        "path": "$.option.series[0].data[0]",
                    },
                ),
                evidence_ids=(evidence.evidence_id,),
            )
        ],
    )
    assert tasks.list_claims("run-2") == claims


@pytest.mark.parametrize(
    ("status", "source", "observation_status"),
    [("failed", "tool", "error"), ("unknown", "system", "partial")],
)
def test_invocation_start_and_failure_observation_are_atomic_events(
    tmp_path: Path,
    status: InvocationStatus,
    source: ObservationSource,
    observation_status: str,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id=f"run-{status}",
        user_text="执行分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 4},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    arguments = {"dataset_ref": "sales"}
    key = invocation_idempotency_key(
        running.run_id, "call-1", "get_data_profile", arguments
    )

    started_run, invocation, started, created = tasks.start_invocation_with_event(
        run_id=running.run_id,
        expected_version=running.state_version,
        tool_call_id="call-1",
        tool_name="get_data_profile",
        arguments=arguments,
        idempotency_key=key,
        policy_decision={
            "allowed": True,
            "code": "policy_allowed",
            "arguments_hash": "hash-only",
        },
    )

    assert created is True
    assert started is not None and started.event_type == "step.started"
    assert started.payload["arguments_hash"] == invocation.args_hash
    assert "arguments" not in started.payload
    assert started_run.state_version == running.state_version + 1

    failed_run, failed_invocation, completed = tasks.commit_tool_failure(
        invocation.invocation_id,
        expected_version=started_run.state_version,
        status=status,
        error_code="simulated_failure",
        error_text="模拟工具失败",
        source=source,
        retryable=status == "failed",
    )

    assert completed is not None and completed.event_type == "step.completed"
    assert completed.payload["status"] == status
    assert completed.payload["evidence_ids"] == []
    observation = completed.payload["observation"]
    assert observation["status"] == observation_status
    assert observation["source"] == source
    assert observation["code"] == "simulated_failure"
    assert failed_invocation.status == status
    assert tasks.list_evidence(running.run_id) == []
    snapshot = tasks.get_snapshot(running.run_id)
    assert snapshot is not None
    assert snapshot["last_observation"]["observation_id"] == observation["observation_id"]
    assert failed_run.state_version == started_run.state_version + 1


def test_failure_observation_rolls_back_with_event_insert(tmp_path: Path) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="failure-rollback",
        user_text="执行分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    arguments = {"dataset_ref": "sales"}
    started_run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=running.run_id,
        expected_version=running.state_version,
        tool_call_id="call-rollback",
        tool_name="get_data_profile",
        arguments=arguments,
        idempotency_key=invocation_idempotency_key(
            running.run_id, "call-rollback", "get_data_profile", arguments
        ),
        policy_decision={"allowed": True, "code": "policy_allowed"},
    )
    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_failure_observation
            BEFORE INSERT ON task_events
            WHEN NEW.event_type = 'step.completed'
            BEGIN
                SELECT RAISE(ABORT, 'forced observation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced observation failure"):
        tasks.commit_tool_failure(
            invocation.invocation_id,
            expected_version=started_run.state_version,
            status="failed",
            error_code="forced",
            error_text="模拟失败",
            source="tool",
            retryable=True,
        )

    persisted = tasks.list_invocations(running.run_id)[0]
    assert persisted.status == "running"
    unchanged_run = tasks.get_run(running.run_id)
    assert unchanged_run is not None
    assert unchanged_run.state_version == started_run.state_version
    assert [item.event_type for item in tasks.list_events(running.run_id)][-1] == (
        "step.started"
    )


def test_tool_success_atomically_commits_artifact_evidence_event_and_checkpoint(
    tmp_path: Path,
) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="atomic-tool-success",
        user_text="检查数据画像",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="读取画像",
    )
    invocation, _ = tasks.start_invocation(
        run_id=running.run_id,
        tool_call_id="profile-call",
        tool_name="get_data_profile",
        arguments={"dataset_ref": "sales"},
        idempotency_key="atomic-tool-success-key",
    )

    updated, completed, evidence, artifact, event, checkpoint = (
        tasks.commit_tool_success(
            invocation.invocation_id,
            expected_version=running.state_version,
            assistant_message_id=assistant.id,
            result={"profile": {"row_count": 3}},
            evidence_kind="tool_result",
            evidence_source={"tool": "get_data_profile"},
            evidence_summary={
                "summary": "共 3 行",
                "value_index": [{"path": "$.profile.row_count", "value": "3"}],
            },
            artifact_draft=ArtifactDraft(
                type="profile",
                payload={"profile": {"row_count": 3}},
                file_ref=None,
                source_tool="get_data_profile",
                params={"analysis_id": "profile-analysis"},
                dataset_ref="sales",
            ),
        )
    )

    assert updated.state_version == running.state_version + 1
    assert completed.status == "succeeded"
    assert artifact is not None and completed.artifact_id == artifact.id
    assert evidence.artifact_id == artifact.id
    assert evidence.summary["artifact_id"] == artifact.id
    assert session.list_artifacts(conversation_id) == [artifact]
    assert event.event_type == "step.completed"
    assert event.payload["evidence_ids"] == [evidence.evidence_id]
    assert checkpoint.state_version == updated.state_version
    assert checkpoint.state["last_sequence"] == event.sequence
    assert tasks.get_snapshot(updated.run_id) == checkpoint.state

    with pytest.raises(ValueError, match="Evidence 引用"):
        session.delete_artifact(artifact.id)
    assert session.delete_conversation(conversation_id) is True
    assert tasks.get_run(updated.run_id) is None


def test_tool_success_transaction_rolls_back_artifact_on_evidence_failure(
    tmp_path: Path,
) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="rollback-tool-success",
        user_text="检查数据画像",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="读取画像",
    )
    invocation, _ = tasks.start_invocation(
        run_id=running.run_id,
        tool_call_id="profile-call",
        tool_name="get_data_profile",
        arguments={},
        idempotency_key="rollback-tool-success-key",
    )
    with sqlite3.connect(session.db_path) as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_evidence
            BEFORE INSERT ON evidence
            BEGIN
                SELECT RAISE(ABORT, 'forced evidence failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced evidence failure"):
        tasks.commit_tool_success(
            invocation.invocation_id,
            expected_version=running.state_version,
            assistant_message_id=assistant.id,
            result={"profile": {"row_count": 3}},
            evidence_kind="tool_result",
            evidence_source={"tool": "get_data_profile"},
            evidence_summary={"summary": "共 3 行"},
            artifact_draft=ArtifactDraft(
                type="profile",
                payload={"profile": {"row_count": 3}},
                file_ref=None,
                source_tool="get_data_profile",
                params=None,
                dataset_ref=None,
            ),
        )

    current = tasks.get_run(running.run_id)
    assert current is not None and current.state_version == running.state_version
    assert tasks.list_invocations(running.run_id)[0].status == "running"
    assert tasks.list_evidence(running.run_id) == []
    assert session.list_artifacts(conversation_id) == []
    assert [item.event_type for item in tasks.list_events(running.run_id)] == [
        "goal",
        "run.started",
    ]
    with sqlite3.connect(session.db_path) as connection:
        checkpoint_count = connection.execute(
            "SELECT COUNT(*) FROM checkpoints WHERE run_id = ?", (running.run_id,)
        ).fetchone()
    assert checkpoint_count is not None and checkpoint_count[0] == 0


def test_claim_cannot_link_evidence_from_another_run(tmp_path: Path) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    first = build_minimal_contract(
        run_id="claim-run-1",
        user_text="第一次分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    second = build_minimal_contract(
        run_id="claim-run-2",
        user_text="第二次分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    first_planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=first,
        budget={"max_tool_calls": 1},
    )
    first_running, _ = tasks.transition(
        first_planning.run_id,
        expected_version=first_planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=second,
        budget={"max_tool_calls": 1},
    )
    invocation, _ = tasks.start_invocation(
        run_id="claim-run-1",
        tool_call_id="call-1",
        tool_name="get_data_profile",
        arguments={},
        idempotency_key="claim-run-1-key",
    )
    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="读取画像",
    )
    _, _, evidence, _, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=first_running.state_version,
        assistant_message_id=assistant.id,
        result={"row_count": 3},
        evidence_kind="tool_result",
        evidence_source={"tool": "get_data_profile"},
        evidence_summary={"value_index": [{"path": "$.row_count", "value": "3"}]},
        artifact_draft=None,
    )

    original = tasks.replace_claims(
        "claim-run-2",
        [
            ClaimDraft(
                statement="等待可验证结论。",
                claim_kind="status",
                value_refs=(),
                evidence_ids=(),
            )
        ],
    )

    with pytest.raises(ValueError, match="其他任务或不存在"):
        tasks.replace_claims(
            "claim-run-2",
            [
                ClaimDraft(
                    statement="共有 3 行。",
                    claim_kind="numeric",
                    value_refs=(),
                    evidence_ids=(evidence.evidence_id,),
                )
            ],
        )
    assert tasks.list_claims("claim-run-2") == original


def test_downgrade_requires_terminal_runs_and_preserves_export(tmp_path: Path) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="run-3",
        user_text="回答问题",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 1},
    )
    db_path = tmp_path / "chatbi.db"
    with pytest.raises(RuntimeError, match="未终止"):
        downgrade_v2_to_v1(db_path)
    running, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="failed",
        event_type="run.failed",
        payload={"reason": "test"},
        terminal_reason="test",
    )
    assert running.status == "failed"

    export_path = downgrade_v2_to_v1(db_path)

    assert export_path.exists()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='task_runs'"
        ).fetchone()[0] == 0
    with sqlite3.connect(export_path) as connection:
        assert connection.execute("SELECT status FROM task_runs").fetchone()[0] == "failed"


def test_startup_recovery_marks_active_invocations_unknown(tmp_path: Path) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="recovery-run",
        user_text="执行长任务",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    plan = {
        "schema_version": 1,
        "summary": "执行检索",
        "steps": [
            {
                "step_id": "search",
                "purpose": "执行知识检索",
                "capability": "knowledge.search",
                "dependencies": [],
                "expected_evidence": ["引用 Evidence"],
                "completion_conditions": ["检索完成"],
                "fallback": [{"when": "失败", "action": "block"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, _, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="slow-call",
        tool_name="kb_search",
        arguments={"query": "测试"},
        idempotency_key="recovery-key",
        policy_decision={"allowed": True},
        step_id=steps[0].step_id,
    )

    recovered = tasks.recover_stale_runs(stale_after_seconds=0)

    assert len(recovered) == 1
    recovered_run, event = recovered[0]
    assert recovered_run.status == "failed"
    assert recovered_run.terminal_reason == "process_recovery"
    assert event.event_type == "run.failed"
    assert event.payload["unknown_invocations"] == 1
    saved_invocation = tasks.list_invocations(run.run_id)[0]
    assert saved_invocation.invocation_id == invocation.invocation_id
    assert saved_invocation.status == "unknown"
    recovered_steps = tasks.list_plan_steps(run.run_id)
    assert recovered_steps[0].status == "blocked"


def test_startup_recovery_preserves_waiting_user_runs(tmp_path: Path) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="waiting-run",
        user_text="比较趋势",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="waiting_user",
        event_type="waiting_user",
        payload={"question": "请选择指标"},
    )

    assert tasks.recover_stale_runs(stale_after_seconds=0) == []
    saved = tasks.get_run(run.run_id)
    assert saved is not None and saved.status == "waiting_user"


def test_startup_recovery_pauses_idle_running_run_with_checkpoint(
    tmp_path: Path,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="idle-recovery-run",
        user_text="继续分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    plan = {
        "schema_version": 1,
        "summary": "等待执行",
        "steps": [
            {
                "step_id": "search",
                "purpose": "检索资料",
                "capability": "knowledge.search",
                "dependencies": [],
                "expected_evidence": ["检索结果"],
                "completion_conditions": ["检索成功"],
                "fallback": [{"when": "失败", "action": "block"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, _, _, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )

    recovered = tasks.recover_stale_runs(stale_after_seconds=0)

    assert len(recovered) == 1
    paused, event = recovered[0]
    assert paused.status == "paused"
    assert event.event_type == "run.paused"
    assert event.payload["reason"] == "process_recovery"
    checkpoint = tasks.get_latest_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.reason == "process_recovery"
    assert checkpoint.state["plan_version"] == 1
    assert checkpoint.state["completed_step_ids"] == []


def test_control_transition_is_idempotent_and_version_guarded(
    tmp_path: Path,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="controlled-run",
        user_text="比较趋势",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )

    paused, first_event, created = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="pause-request-1",
        command="pause",
        allowed_statuses={"running"},
        status="paused",
        event_type="run.paused",
        payload={"reason": "user_requested"},
        require_idle=True,
        checkpoint_reason="user_pause",
    )
    replayed, replay_event, replay_created = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="pause-request-1",
        command="pause",
        allowed_statuses={"running"},
        status="paused",
        event_type="run.paused",
        payload={"reason": "user_requested"},
        require_idle=True,
    )

    assert created is True
    assert replay_created is False
    assert paused.status == "paused"
    assert replayed == paused
    assert replay_event == first_event
    assert len(
        [
            event
            for event in tasks.list_events(run.run_id)
            if event.event_type == "run.paused"
        ]
    ) == 1
    with pytest.raises(IdempotencyConflict):
        tasks.control_transition(
            run.run_id,
            expected_version=paused.state_version,
            idempotency_key="pause-request-1",
            command="cancel",
            allowed_statuses={"paused"},
            status="cancelled",
            event_type="run.cancelled",
            payload={"reason": "user_requested"},
        )
    checkpoint = tasks.get_latest_checkpoint(run.run_id)
    assert checkpoint is not None
    assert checkpoint.reason == "user_pause"
    assert checkpoint.state["completed_step_ids"] == []
    resumed, resume_event, resume_created = tasks.control_transition(
        run.run_id,
        expected_version=paused.state_version,
        idempotency_key="resume-request-1",
        command="resume",
        allowed_statuses={"paused"},
        status="running",
        event_type="run.resumed",
        payload={"reason": "user_requested"},
        require_checkpoint=True,
    )
    assert resume_created is True
    assert resumed.status == "running"
    assert resume_event.payload["checkpoint"]["checkpoint_id"] == (
        checkpoint.checkpoint_id
    )
    with pytest.raises(StateVersionConflict):
        tasks.control_transition(
            run.run_id,
            expected_version=run.state_version,
            idempotency_key="cancel-request-stale",
            command="cancel",
            allowed_statuses={"paused"},
            status="cancelled",
            event_type="run.cancelled",
            payload={"reason": "user_requested"},
        )


def test_pause_rejects_active_invocation_and_cancel_marks_it_unknown(
    tmp_path: Path,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="active-tool-run",
        user_text="检索资料",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    plan = {
        "schema_version": 1,
        "summary": "检索",
        "steps": [
            {
                "step_id": "search",
                "purpose": "检索资料",
                "capability": "knowledge.search",
                "dependencies": [],
                "expected_evidence": ["检索结果"],
                "completion_conditions": ["检索成功"],
                "fallback": [{"when": "失败", "action": "block"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, _, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="search-call",
        tool_name="kb_search",
        arguments={"query": "测试"},
        idempotency_key="active-tool",
        policy_decision={"allowed": True},
        step_id=steps[0].step_id,
    )

    with pytest.raises(ControlConflict, match="安全边界"):
        tasks.control_transition(
            run.run_id,
            expected_version=run.state_version,
            idempotency_key="pause-active-tool",
            command="pause",
            allowed_statuses={"running"},
            status="paused",
            event_type="run.paused",
            payload={"reason": "user_requested"},
            require_idle=True,
        )

    cancelled, event, created = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="cancel-active-tool",
        command="cancel",
        allowed_statuses={"running"},
        status="cancelled",
        event_type="run.cancelled",
        payload={"reason": "user_requested"},
        terminal_reason="user_cancelled",
    )
    assert created is True
    assert cancelled.status == "cancelled"
    assert event.payload["unknown_invocations"] == 1
    saved_invocation = tasks.list_invocations(run.run_id)[0]
    assert saved_invocation.invocation_id == invocation.invocation_id
    assert saved_invocation.status == "unknown"
    assert tasks.list_plan_steps(run.run_id)[0].status == "blocked"


def test_clarification_answer_validates_token_and_is_idempotent(
    tmp_path: Path,
) -> None:
    session, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="clarification-run",
        user_text="比较趋势",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="waiting_user",
        event_type="waiting_user",
        payload={
            "question_id": "q_metric",
            "question": "请选择指标",
            "resume_token": "resume-token-for-test",
        },
        checkpoint_reason="waiting_user",
    )

    with pytest.raises(ControlConflict, match="resume_token"):
        tasks.answer_clarification(
            run.run_id,
            expected_version=run.state_version,
            idempotency_key="answer-invalid-token",
            question_id="q_metric",
            resume_token="wrong-token-for-test",
            answer="销售额",
        )

    planning, first_event, created = tasks.answer_clarification(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="answer-request-1",
        question_id="q_metric",
        resume_token="resume-token-for-test",
        answer="销售额",
    )
    replayed, replay_event, replay_created = tasks.answer_clarification(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="answer-request-1",
        question_id="q_metric",
        resume_token="resume-token-for-test",
        answer="销售额",
    )

    assert created is True
    assert replay_created is False
    assert planning.status == "planning"
    assert replayed == planning
    assert replay_event == first_event
    answers = [
        message
        for message in session.list_messages(conversation_id)
        if message.content.startswith("澄清回答")
    ]
    assert len(answers) == 1
    snapshot = tasks.get_snapshot(run.run_id)
    assert snapshot is not None
    assert snapshot["clarification_answers"] == {"q_metric": "销售额"}


def test_user_step_retry_creates_one_immutable_plan_revision(
    tmp_path: Path,
) -> None:
    _, tasks, project_id, conversation_id, message_id = _workspace(tmp_path)
    contract = build_minimal_contract(
        run_id="step-retry-run",
        user_text="检索资料",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        contract=contract,
        budget={"max_tool_calls": 3},
    )
    plan = {
        "schema_version": 1,
        "summary": "检索后总结",
        "steps": [
            {
                "step_id": "search",
                "purpose": "检索资料",
                "capability": "knowledge.search",
                "dependencies": [],
                "expected_evidence": ["检索结果"],
                "completion_conditions": ["检索成功"],
                "fallback": [{"when": "失败", "action": "retry"}],
            },
            {
                "step_id": "summarize",
                "purpose": "总结结果",
                "capability": "data.profile",
                "dependencies": ["search"],
                "expected_evidence": ["总结"],
                "completion_conditions": ["总结成功"],
                "fallback": [{"when": "失败", "action": "block"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, original_plan, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:fast",
        planner={"route": "fast"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    run, invocation, _, _ = tasks.start_invocation_with_event(
        run_id=run.run_id,
        expected_version=run.state_version,
        tool_call_id="search-call",
        tool_name="kb_search",
        arguments={"query": "测试"},
        idempotency_key="failed-search-call",
        policy_decision={"allowed": True},
        step_id=steps[0].step_id,
    )
    run, _, failure_event = tasks.commit_tool_failure(
        invocation.invocation_id,
        status="failed",
        expected_version=run.state_version,
        error_code="tool_execution_failed",
        error_text="temporary failure",
        source="tool",
        retryable=True,
    )
    assert failure_event is not None
    run, _, _ = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="pause-after-failure",
        command="pause",
        allowed_statuses={"running"},
        status="paused",
        event_type="run.paused",
        payload={"reason": "user_requested"},
        require_idle=True,
    )

    retried, revised_plan, revised_steps, event, created = tasks.retry_step(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="retry-search-step",
        step_id="search",
    )
    replayed, replay_plan, replay_steps, replay_event, replay_created = (
        tasks.retry_step(
            run.run_id,
            expected_version=run.state_version,
            idempotency_key="retry-search-step",
            step_id="search",
        )
    )

    assert created is True
    assert replay_created is False
    assert retried.status == "running"
    assert revised_plan.version == 2
    assert replayed == retried
    assert replay_plan == revised_plan
    assert replay_steps == revised_steps
    assert replay_event == event
    assert [step.status for step in revised_steps] == ["pending", "pending"]
    assert tasks.list_plans(run.run_id) == [original_plan, revised_plan]
    original_steps = tasks.list_plan_steps(run.run_id, plan_version=1)
    assert [step.status for step in original_steps] == ["failed", "pending"]
    assert event.payload["retry_step_id"] == "search"
