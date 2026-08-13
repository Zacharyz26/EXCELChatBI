"""Stage 6C-2 deterministic selected-hypothesis lifecycle tests."""

from __future__ import annotations

import pytest
from apps.orchestrator.control.hypothesis_lifecycle import (
    bind_hypothesis_to_plan,
    finalize_hypothesis_execution,
    hypothesis_evidence_collected,
    hypothesis_invocation_failed,
    hypothesis_invocation_started,
)
from packages.session.task_models import TaskStepRecord


def _selection(*, kind: str = "trend", capability: str = "stats.trend") -> dict[str, object]:
    return {
        "schema": "chatbi-hypothesis-selection-v1",
        "schema_version": 1,
        "question_id": "analysis_goal",
        "hypothesis_id": "hyp_0123456789abcdef",
        "kind": kind,
        "statement": "候选假设",
        "capability": capability,
        "expected_evidence": "确定性 Evidence",
        "dataset_ref": "d" * 32,
        "plan_version": 1,
        "data_version_hash": "a" * 64,
        "run_state_version": 4,
        "selected_at": "2026-08-11T00:00:00Z",
        "tested": False,
    }


def _step(*, capability: str = "stats.trend") -> TaskStepRecord:
    return TaskStepRecord(
        step_id="persisted-step",
        plan_id="plan-2",
        run_id="run",
        position=0,
        logical_id="test-hypothesis",
        status="pending",
        definition={
            "step_id": "test-hypothesis",
            "purpose": "验证候选",
            "capability": capability,
            "dependencies": [],
            "expected_evidence": ["Evidence"],
            "completion_conditions": ["工具成功"],
            "fallback": [{"when": "失败", "action": "block"}],
        },
        started_at=None,
        completed_at=None,
    )


def _planned(*, kind: str = "trend", capability: str = "stats.trend") -> dict[str, object]:
    result = bind_hypothesis_to_plan(
        selection=_selection(kind=kind, capability=capability),
        existing=None,
        plan_id="plan-2",
        plan_version=2,
        steps=[_step(capability=capability)],
        updated_at="2026-08-11T00:00:01Z",
    )
    assert result is not None
    return result


def test_supported_signal_requires_evidence_and_verifier_pass() -> None:
    planned = _planned()
    running = hypothesis_invocation_started(
        planned,
        persisted_step_id="persisted-step",
        invocation_id="invocation-1",
        updated_at="2026-08-11T00:00:02Z",
    )
    evidence = hypothesis_evidence_collected(
        running,
        persisted_step_id="persisted-step",
        invocation_id="invocation-1",
        evidence_id="evidence-1",
        ledger_sequence=1,
        result={"direction": "上升"},
        updated_at="2026-08-11T00:00:03Z",
    )

    assert running is not None and running["status"] == "running"
    assert evidence is not None
    assert evidence["status"] == "evidence_collected"
    assert evidence["evidence_outcome"] == "supported"
    assert evidence["outcome"] == "untested"
    final = finalize_hypothesis_execution(
        evidence,
        run_status="completed",
        verification_payload={"verdict": "PASS", "checks": []},
        verification_sequence=9,
        terminal_reason=None,
        updated_at="2026-08-11T00:00:04Z",
    )
    assert final is not None
    assert final["status"] == "supported"
    assert final["outcome"] == "supported"


def test_flat_trend_and_zero_anomalies_are_not_supported_after_pass() -> None:
    cases = [
        (_planned(), {"direction": "平稳"}),
        (_planned(kind="anomaly", capability="stats.anomaly"), {"n_anomalies": 0}),
    ]
    for planned, result in cases:
        evidence = hypothesis_evidence_collected(
            planned,
            persisted_step_id="persisted-step",
            invocation_id="invocation-1",
            evidence_id="evidence-1",
            ledger_sequence=1,
            result=result,
            updated_at="2026-08-11T00:00:03Z",
        )
        final = finalize_hypothesis_execution(
            evidence,
            run_status="completed",
            verification_payload={"verdict": "PASS", "checks": []},
            verification_sequence=9,
            terminal_reason=None,
            updated_at="2026-08-11T00:00:04Z",
        )
        assert final is not None
        assert final["status"] == "not_supported"
        assert final["outcome"] == "not_supported"


def test_failed_verification_keeps_collected_evidence_partial() -> None:
    evidence = hypothesis_evidence_collected(
        _planned(kind="correlation", capability="stats.correlation"),
        persisted_step_id="persisted-step",
        invocation_id="invocation-1",
        evidence_id="evidence-1",
        ledger_sequence=1,
        result={"top_pairs": [{"significant": True}]},
        updated_at="2026-08-11T00:00:03Z",
    )
    final = finalize_hypothesis_execution(
        evidence,
        run_status="blocked",
        verification_payload={
            "verdict": "NEEDS_ACTION",
            "checks": [{"code": "unsupported_numeric_claim"}],
        },
        verification_sequence=9,
        terminal_reason="unsupported_numeric_claim",
        updated_at="2026-08-11T00:00:04Z",
    )
    assert final is not None
    assert final["status"] == "partial"
    assert final["outcome"] == "inconclusive"
    assert final["evidence_outcome"] == "supported"


@pytest.mark.parametrize(
    ("significant", "expected"),
    [(True, "supported"), (False, "not_supported")],
)
def test_group_compare_evidence_drives_segment_outcome(
    significant: bool,
    expected: str,
) -> None:
    evidence = hypothesis_evidence_collected(
        _planned(kind="segment_comparison", capability="stats.group_compare"),
        persisted_step_id="persisted-step",
        invocation_id="invocation-1",
        evidence_id="evidence-1",
        ledger_sequence=1,
        result={"overall": {"significant": significant}},
        updated_at="2026-08-13T00:00:03Z",
    )

    assert evidence is not None
    assert evidence["evidence_outcome"] == expected


def test_failure_without_evidence_and_cancellation_remain_distinct() -> None:
    failed = hypothesis_invocation_failed(
        _planned(),
        persisted_step_id="persisted-step",
        invocation_id="invocation-1",
        failure_code="tool_execution_failed",
        updated_at="2026-08-11T00:00:03Z",
    )
    terminal_failed = finalize_hypothesis_execution(
        failed,
        run_status="failed",
        verification_payload=None,
        verification_sequence=None,
        terminal_reason="tool_execution_failed",
        updated_at="2026-08-11T00:00:04Z",
    )
    cancelled = finalize_hypothesis_execution(
        _planned(),
        run_status="cancelled",
        verification_payload=None,
        verification_sequence=None,
        terminal_reason="user_cancelled",
        updated_at="2026-08-11T00:00:04Z",
    )
    assert terminal_failed is not None and terminal_failed["status"] == "failed"
    assert terminal_failed["tested"] is False
    assert cancelled is not None and cancelled["status"] == "cancelled"
    assert cancelled["outcome"] == "untested"


def test_plan_binding_rejects_missing_or_duplicate_capability_steps() -> None:
    try:
        bind_hypothesis_to_plan(
            selection=_selection(),
            existing=None,
            plan_id="plan-2",
            plan_version=2,
            steps=[_step(capability="stats.anomaly")],
            updated_at="2026-08-11T00:00:01Z",
        )
    except ValueError as exc:
        assert "未进入执行计划" in str(exc)
    else:
        raise AssertionError("missing capability should fail closed")

    try:
        bind_hypothesis_to_plan(
            selection=_selection(),
            existing=None,
            plan_id="plan-2",
            plan_version=2,
            steps=[_step(), _step()],
            updated_at="2026-08-11T00:00:01Z",
        )
    except ValueError as exc:
        assert "唯一计划步骤" in str(exc)
    else:
        raise AssertionError("duplicate capability should fail closed")
