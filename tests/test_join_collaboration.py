"""Stage 6E-3 governed Join collaboration projection tests."""

from __future__ import annotations

import json

from apps.orchestrator.control.join_collaboration import (
    build_join_collaboration_projection,
    build_join_evidence_context,
    evaluate_join_execution_guard,
)
from packages.session.task_models import (
    ApprovalRecord,
    EvidenceRecord,
    TaskEvent,
    ToolInvocation,
)
from packages.session.task_store import invocation_arguments_hash

_LEFT = "1" * 32
_RIGHT = "2" * 32
_OUTPUT = "3" * 32
_DATA_HASH = "a" * 64
_ARGS = {
    "left_dataset_ref": _LEFT,
    "right_dataset_ref": _RIGHT,
    "left_key": "customer_id",
    "right_key": "customer_code",
    "join_type": "left",
}


def _preflight_result() -> dict[str, object]:
    return {
        "status": "requires_confirmation",
        "relationship": "many_to_many",
        "estimated_output_rows": 24,
        "expansion_ratio": 2.4,
        "matching_key_count": 8,
        "matched_left_rows": 10,
        "matched_right_rows": 12,
        "requires_confirmation": True,
        "risks": [
            {
                "code": "many_to_many",
                "severity": "warning",
                "message": "关联键为多对多关系，执行前必须人工确认。",
            }
        ],
        "left": {"row_count": 10, "null_count": 0, "distinct_count": 8},
        "right": {"row_count": 12, "null_count": 0, "distinct_count": 8},
        "raw_rows_returned": False,
    }


def _invocation(
    invocation_id: str,
    tool_name: str,
    *,
    status: str = "succeeded",
    error_text: str | None = None,
) -> ToolInvocation:
    return ToolInvocation(
        invocation_id=invocation_id,
        run_id="join-run",
        step_id=f"{invocation_id}-step",
        tool_call_id=f"{invocation_id}-call",
        tool_name=tool_name,
        idempotency_key=f"{invocation_id}-idempotency",
        args_hash=invocation_arguments_hash(_ARGS),
        args=_ARGS,
        status=status,  # type: ignore[arg-type]
        result_hash="f" * 64 if status == "succeeded" else None,
        error_text=error_text,
        artifact_id=None,
        started_at="2026-08-14T01:00:00Z",
        completed_at="2026-08-14T01:00:01Z",
    )


def _evidence(
    invocation: ToolInvocation,
    context: dict[str, object],
    *,
    evidence_id: str,
    created_at: str,
) -> EvidenceRecord:
    return EvidenceRecord(
        evidence_id=evidence_id,
        run_id=invocation.run_id,
        invocation_id=invocation.invocation_id,
        artifact_id=None,
        kind="tool_result",
        source={"tool": invocation.tool_name, "join": context},
        result_hash="e" * 64,
        summary={"summary": "safe join evidence"},
        created_at=created_at,
    )


def _approval(*, status: str) -> ApprovalRecord:
    return ApprovalRecord(
        approval_id="4" * 32,
        tenant_id="tenant-a",
        project_id="project-a",
        run_id="join-run",
        plan_id="plan-a",
        plan_version=1,
        task_step_id="execute-step-id",
        step_logical_id="execute-join",
        subject_user_id="alice",
        requested_by_user_id="alice",
        tool_name="join_datasets",
        tool_schema_hash="b" * 64,
        parameter_summary_hash=invocation_arguments_hash(_ARGS),
        risk_level="high",
        status=status,  # type: ignore[arg-type]
        version=1,
        expires_at="2099-08-14T02:00:00Z",
        decision_reason="确认双父版本和多对多风险" if status != "pending" else None,
        decided_by_user_id="alice" if status != "pending" else None,
        requested_at="2026-08-14T01:00:02Z",
        updated_at="2026-08-14T01:00:03Z",
        decided_at="2026-08-14T01:00:03Z" if status != "pending" else None,
        consumed_at="2026-08-14T01:00:04Z" if status == "consumed" else None,
        idempotency_key="join-approval-request",
        request_hash="c" * 64,
        request_event_id="5" * 32,
    )


def test_join_guard_requires_exact_preflight_and_stops_on_version_drift() -> None:
    invocation = _invocation("preflight", "join_preflight")
    context = build_join_evidence_context(
        tool_name="join_preflight",
        arguments=_ARGS,
        result=_preflight_result(),
        data_version_hash=_DATA_HASH,
    )
    assert context is not None
    evidence = _evidence(
        invocation,
        context,
        evidence_id="6" * 32,
        created_at="2026-08-14T01:00:01Z",
    )

    allowed = evaluate_join_execution_guard(
        arguments=_ARGS,
        invocations=[invocation],
        evidence=[evidence],
        current_data_version_hash=_DATA_HASH,
    )
    drifted = evaluate_join_execution_guard(
        arguments=_ARGS,
        invocations=[invocation],
        evidence=[evidence],
        current_data_version_hash="d" * 64,
    )
    changed = evaluate_join_execution_guard(
        arguments={**_ARGS, "right_key": "other_key"},
        invocations=[invocation],
        evidence=[evidence],
        current_data_version_hash=_DATA_HASH,
    )

    assert allowed.allowed is True and allowed.code == "join_preflight_verified"
    assert drifted.allowed is False and drifted.code == "join_data_version_drift"
    assert changed.allowed is False and changed.code == "join_preflight_evidence_required"


def test_join_projection_survives_approval_and_exposes_complete_two_parent_lineage() -> None:
    preflight_invocation = _invocation("preflight", "join_preflight")
    preflight_context = build_join_evidence_context(
        tool_name="join_preflight",
        arguments=_ARGS,
        result=_preflight_result(),
        data_version_hash=_DATA_HASH,
    )
    assert preflight_context is not None
    preflight_evidence = _evidence(
        preflight_invocation,
        preflight_context,
        evidence_id="6" * 32,
        created_at="2026-08-14T01:00:01Z",
    )

    pending = build_join_collaboration_projection(
        invocations=[preflight_invocation],
        evidence=[preflight_evidence],
        approvals=[_approval(status="pending")],
        step_events=[],
        current_data_version_hash=_DATA_HASH,
        dataset_parents={},
    )
    assert pending is not None
    assert pending["status"] == "awaiting_approval"
    assert pending["left"]["dataset_ref"] == _LEFT
    assert pending["right"]["key"] == "customer_code"
    assert pending["risks"][0]["code"] == "many_to_many"

    execute_invocation = _invocation("execute", "join_datasets")
    execute_context = build_join_evidence_context(
        tool_name="join_datasets",
        arguments=_ARGS,
        result={
            "preflight_status": "requires_confirmation",
            "relationship": "many_to_many",
            "dataset_ref": _OUTPUT,
            "parent_refs": [_LEFT, _RIGHT],
            "rows": 24,
            "risks": _preflight_result()["risks"],
            "raw_rows_returned": False,
        },
        data_version_hash=_DATA_HASH,
    )
    assert execute_context is not None
    execute_evidence = _evidence(
        execute_invocation,
        execute_context,
        evidence_id="7" * 32,
        created_at="2026-08-14T01:00:05Z",
    )
    completed = build_join_collaboration_projection(
        invocations=[preflight_invocation, execute_invocation],
        evidence=[preflight_evidence, execute_evidence],
        approvals=[_approval(status="consumed")],
        step_events=[],
        current_data_version_hash="9" * 64,
        dataset_parents={_OUTPUT: (_LEFT, _RIGHT)},
    )

    assert completed is not None and completed["status"] == "completed"
    assert completed["output"] == {
        "dataset_ref": _OUTPUT,
        "rows": 24,
        "parent_refs": [_LEFT, _RIGHT],
        "parents": [
            {"dataset_ref": _LEFT, "ordinal": 0, "role": "left"},
            {"dataset_ref": _RIGHT, "ordinal": 1, "role": "right"},
        ],
        "lineage_complete": True,
    }
    assert completed["raw_rows_returned"] is False
    assert "customer_id" not in json.dumps(completed["failure"])


def test_join_projection_explains_persisted_failure() -> None:
    preflight_invocation = _invocation("preflight", "join_preflight")
    preflight_context = build_join_evidence_context(
        tool_name="join_preflight",
        arguments=_ARGS,
        result=_preflight_result(),
        data_version_hash=_DATA_HASH,
    )
    assert preflight_context is not None
    preflight_evidence = _evidence(
        preflight_invocation,
        preflight_context,
        evidence_id="6" * 32,
        created_at="2026-08-14T01:00:01Z",
    )
    failed = _invocation(
        "execute",
        "join_datasets",
        status="failed",
        error_text="执行结果与预检不一致，输出已撤销。",
    )
    event = TaskEvent(
        event_id="8" * 32,
        run_id="join-run",
        sequence=8,
        event_type="step.completed",
        payload={
            "invocation_id": failed.invocation_id,
            "observation": {
                "code": "join_data_version_drift",
                "summary": "数据版本漂移，Join 已停止。",
                "retryable": False,
            },
        },
        occurred_at="2026-08-14T01:00:06Z",
    )
    projection = build_join_collaboration_projection(
        invocations=[preflight_invocation, failed],
        evidence=[preflight_evidence],
        approvals=[_approval(status="consumed")],
        step_events=[event],
        current_data_version_hash=_DATA_HASH,
        dataset_parents={},
    )

    assert projection is not None
    assert projection["status"] == "version_drift"
    assert projection["failure"] == {
        "code": "join_data_version_drift",
        "message": "数据版本漂移，Join 已停止。",
        "retryable": False,
    }
