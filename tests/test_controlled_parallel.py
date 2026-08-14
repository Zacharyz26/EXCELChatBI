"""v2.5 stage 6A-3 durable budget, data version and cancellation tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from apps.api.routers.agent_runs import (
    _execution_control_payload,
    _tool_audit_payloads,
)
from apps.orchestrator.control.contracts import build_minimal_contract
from packages.session.store import SessionStore
from packages.session.task_models import TaskRun, TaskStepRecord
from packages.session.task_store import ControlConflict, TaskStore

_SOURCE_REF = "a" * 32
_DERIVED_REF = "b" * 32
_LATE_REF = "c" * 32
_RIGHT_REF = "d" * 32


def _running_parallel_run(
    tmp_path: Path,
    *,
    max_tool_calls: int = 2,
    include_right: bool = False,
) -> tuple[SessionStore, TaskStore, TaskRun, list[TaskStepRecord]]:
    session = SessionStore(str(tmp_path / "chatbi.db"))
    project = session.create_project("controlled parallel")
    session.register_dataset(
        ref=_SOURCE_REF,
        project_id=project.id,
        filename="source.xlsx",
        profile={"columns": [{"name": "period"}, {"name": "value"}]},
    )
    if include_right:
        session.register_dataset(
            ref=_RIGHT_REF,
            project_id=project.id,
            filename="right.xlsx",
            profile={"columns": [{"name": "period"}]},
        )
    conversation = session.create_conversation(project.id)
    _, message = session.start_user_turn(
        conversation_id=conversation.id,
        content="parallel analysis",
        suggested_title="parallel analysis",
    )
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="parallel-run",
        user_text="parallel analysis",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    run, _ = tasks.create_run(
        project_id=project.id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={
            "max_tool_calls": max_tool_calls,
            "max_parallelism": 2,
        },
    )
    plan = {
        "schema_version": 1,
        "summary": "two independent reads",
        "steps": [
            {
                "step_id": "profile",
                "purpose": "profile",
                "capability": "data.profile",
                "dependencies": [],
                "expected_evidence": ["profile"],
                "completion_conditions": ["done"],
                "fallback": [{"when": "failed", "action": "retry"}],
            },
            {
                "step_id": "trend",
                "purpose": "trend",
                "capability": "stats.trend",
                "dependencies": [],
                "expected_evidence": ["trend"],
                "completion_conditions": ["done"],
                "fallback": [{"when": "failed", "action": "retry"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }
    run, _, steps, _ = tasks.save_plan(
        run.run_id,
        expected_version=run.state_version,
        plan=plan,
        reason="initial:test",
        planner={"route": "test"},
    )
    run, _ = tasks.transition(
        run.run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    return session, tasks, run, steps


def _requests(steps: list[TaskStepRecord]) -> list[dict[str, object]]:
    return [
        {
            "tool_call_id": "call-profile",
            "tool_name": "get_data_profile",
            "arguments": {"dataset_ref": _SOURCE_REF},
            "idempotency_key": "parallel-profile",
            "policy_decision": {"allowed": True},
            "step_id": steps[0].step_id,
        },
        {
            "tool_call_id": "call-trend",
            "tool_name": "trend_analysis",
            "arguments": {"dataset_ref": _SOURCE_REF},
            "idempotency_key": "parallel-trend",
            "policy_decision": {"allowed": True},
            "step_id": steps[1].step_id,
        },
    ]


def test_parallel_batch_atomically_shares_budget_data_version_and_ledger(
    tmp_path: Path,
) -> None:
    session, tasks, running, steps = _running_parallel_run(tmp_path)
    run, invocations, events, created = tasks.reserve_parallel_invocations(
        run_id=running.run_id,
        expected_version=running.state_version,
        requests=_requests(steps),
    )

    assert created is True
    assert len(invocations) == len(events) == 2
    assert run.usage["tool_attempts"] == 2
    nodes = tasks.list_cancellation_nodes(run.run_id)
    assert len(nodes) == 3
    branch_hashes = {node.data_version_hash for node in nodes if node.parent_node_id is not None}
    assert branch_hashes == {tasks.data_version_hash(run.run_id)}

    session.register_dataset(
        ref=_LATE_REF,
        project_id=run.project_id,
        filename="late.xlsx",
        profile={"columns": []},
    )
    with pytest.raises(ControlConflict, match="固定数据版本"):
        tasks.start_invocation(
            run_id=run.run_id,
            tool_call_id="late-call",
            tool_name="get_data_profile",
            arguments={"dataset_ref": _LATE_REF},
            idempotency_key="late-dataset",
        )
    with pytest.raises(ControlConflict, match="tool_budget_exhausted"):
        tasks.start_invocation(
            run_id=run.run_id,
            tool_call_id="over-budget",
            tool_name="kb_search",
            arguments={"query": "x"},
            idempotency_key="over-budget",
        )

    assistant = session.append_message(
        conversation_id=run.conversation_id,
        role="assistant",
        content="parallel tools",
    )
    run, _, second_evidence, _, _, _ = tasks.commit_tool_success(
        invocations[1].invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result={"series": [{"period": "p1", "value": 1}]},
        evidence_kind="tool_result",
        evidence_source={"tool": "trend_analysis"},
        evidence_summary={"summary": "trend"},
        artifact_draft=None,
    )
    run, _, first_evidence, _, _, _ = tasks.commit_tool_success(
        invocations[0].invocation_id,
        expected_version=run.state_version,
        assistant_message_id=assistant.id,
        result={"profile": {"row_count": 1}},
        evidence_kind="tool_result",
        evidence_source={"tool": "get_data_profile"},
        evidence_summary={"summary": "profile"},
        artifact_draft=None,
    )

    ledger = tasks.list_evidence_ledger(run.run_id)
    assert [entry.sequence for entry in ledger] == [1, 2]
    assert [entry.evidence_id for entry in ledger] == [
        second_evidence.evidence_id,
        first_evidence.evidence_id,
    ]
    assert [item.evidence_id for item in tasks.list_evidence(run.run_id)] == [
        second_evidence.evidence_id,
        first_evidence.evidence_id,
    ]
    audits = _tool_audit_payloads(
        tasks.list_invocations(run.run_id),
        tasks.list_evidence(run.run_id),
        tasks.list_events_by_type(run.run_id, "step.started"),
        tasks.list_cancellation_nodes(run.run_id),
        ledger,
    )
    assert [item["parallel"] for item in audits] == [True, True]
    assert [item["evidence_ledger_sequence"] for item in audits] == [2, 1]
    assert {item["cancellation_status"] for item in audits} == {"completed"}
    assert {item["data_version_hash"] for item in audits} == branch_hashes
    assert len({item["branch_node_id"] for item in audits}) == 2
    control = _execution_control_payload(
        tasks.get_execution_scope(run.run_id),
        tasks.list_dataset_bindings(run.run_id),
        tasks.list_cancellation_nodes(run.run_id),
        ledger,
        tasks.data_version_hash(run.run_id),
    )
    assert control == {
        "schema_version": 1,
        "max_tool_calls": 2,
        "max_parallelism": 2,
        "data_version_hash": next(iter(branch_hashes)),
        "dataset_version_count": 1,
        "evidence_ledger_version": 2,
        "root_status": "active",
        "active_branch_count": 0,
        "cancel_requested_branch_count": 0,
    }
    with pytest.raises(ValueError, match="running"):
        tasks.commit_tool_success(
            invocations[0].invocation_id,
            expected_version=run.state_version,
            assistant_message_id=assistant.id,
            result={"profile": {"row_count": 1}},
            evidence_kind="tool_result",
            evidence_source={"tool": "get_data_profile"},
            evidence_summary={"summary": "duplicate"},
            artifact_draft=None,
        )


def test_cancel_propagates_to_every_reserved_branch(tmp_path: Path) -> None:
    _, tasks, running, steps = _running_parallel_run(tmp_path)
    run, invocations, _, _ = tasks.reserve_parallel_invocations(
        run_id=running.run_id,
        expected_version=running.state_version,
        requests=_requests(steps),
    )

    cancelled, event, created = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="cancel-parallel",
        command="cancel",
        allowed_statuses={"running"},
        status="cancelled",
        event_type="run.cancelled",
        payload={"reason": "test"},
        terminal_reason="user_cancelled",
    )

    assert created is True
    assert cancelled.status == "cancelled"
    assert event.payload["unknown_invocations"] == 2
    assert {item.status for item in tasks.list_invocations(run.run_id)} == {"unknown"}
    assert {item.invocation_id for item in tasks.list_invocations(run.run_id)} == {
        item.invocation_id for item in invocations
    }
    assert {node.status for node in tasks.list_cancellation_nodes(run.run_id)} == {
        "cancel_requested"
    }


def test_derived_dataset_is_append_only_but_unrelated_late_dataset_is_denied(
    tmp_path: Path,
) -> None:
    session, tasks, running, _ = _running_parallel_run(tmp_path, max_tool_calls=3)
    invocation, _ = tasks.start_invocation(
        run_id=running.run_id,
        tool_call_id="transform",
        tool_name="transform_dataset",
        arguments={"dataset_ref": _SOURCE_REF, "drop_duplicates": True},
        idempotency_key="transform-once",
    )
    session.register_dataset(
        ref=_DERIVED_REF,
        project_id=running.project_id,
        filename="derived.xlsx",
        profile={"columns": []},
        parent_ref=_SOURCE_REF,
        transform={"drop_duplicates": True},
    )
    assistant = session.append_message(
        conversation_id=running.conversation_id,
        role="assistant",
        content="transform",
    )
    run, _, _, _, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=running.state_version,
        assistant_message_id=assistant.id,
        result={
            "dataset_ref": _DERIVED_REF,
            "parent_ref": _SOURCE_REF,
            "registered": True,
        },
        evidence_kind="tool_result",
        evidence_source={"tool": "transform_dataset"},
        evidence_summary={"summary": "derived"},
        artifact_draft=None,
    )
    bindings = tasks.list_dataset_bindings(run.run_id)
    assert [(item.dataset_ref, item.binding_kind) for item in bindings] == [
        (_SOURCE_REF, "initial"),
        (_DERIVED_REF, "derived"),
    ]
    derived_call, created = tasks.start_invocation(
        run_id=run.run_id,
        tool_call_id="profile-derived",
        tool_name="get_data_profile",
        arguments={"dataset_ref": _DERIVED_REF},
        idempotency_key="profile-derived",
    )
    assert created is True
    assert derived_call.args["dataset_ref"] == _DERIVED_REF


def test_join_result_binds_both_frozen_parents_to_task_data_version(
    tmp_path: Path,
) -> None:
    session, tasks, running, _ = _running_parallel_run(
        tmp_path,
        max_tool_calls=1,
        include_right=True,
    )
    invocation, _ = tasks.start_invocation(
        run_id=running.run_id,
        tool_call_id="join",
        tool_name="join_datasets",
        arguments={
            "left_dataset_ref": _SOURCE_REF,
            "right_dataset_ref": _RIGHT_REF,
            "left_key": "period",
            "right_key": "period",
            "join_type": "inner",
        },
        idempotency_key="join-once",
    )
    session.register_dataset(
        ref=_DERIVED_REF,
        project_id=running.project_id,
        filename="joined.xlsx",
        profile={"columns": []},
        parent_ref=_SOURCE_REF,
        parent_refs=(_SOURCE_REF, _RIGHT_REF),
        transform={"operation": "join"},
    )
    assistant = session.append_message(
        conversation_id=running.conversation_id,
        role="assistant",
        content="join",
    )

    tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=running.state_version,
        assistant_message_id=assistant.id,
        result={
            "dataset_ref": _DERIVED_REF,
            "parent_ref": _SOURCE_REF,
            "parent_refs": [_SOURCE_REF, _RIGHT_REF],
            "registered": True,
        },
        evidence_kind="tool_result",
        evidence_source={"tool": "join_datasets"},
        evidence_summary={"summary": "joined"},
        artifact_draft=None,
    )

    with sqlite3.connect(session.db_path) as connection:
        parents = connection.execute(
            """
            SELECT parent_ref, ordinal
            FROM task_dataset_binding_parents
            WHERE run_id = ? AND dataset_ref = ? ORDER BY ordinal
            """,
            (running.run_id, _DERIVED_REF),
        ).fetchall()
    assert parents == [(_SOURCE_REF, 0), (_RIGHT_REF, 1)]
