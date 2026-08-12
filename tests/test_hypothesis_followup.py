"""Stage 6C-3 deterministic result-driven follow-up tests."""

from __future__ import annotations

import pytest
from apps.orchestrator.control.hypothesis_followup import (
    decide_hypothesis_followup,
    validate_hypothesis_followup,
)


def _execution(
    *,
    status: str,
    outcome: str,
    hypothesis_id: str = "hyp_0123456789abcdef",
    last_failure_code: str | None = None,
) -> dict[str, object]:
    return {
        "schema": "chatbi-hypothesis-execution-v1",
        "schema_version": 1,
        "hypothesis_id": hypothesis_id,
        "kind": "trend",
        "statement": "销售额可能随月份变化",
        "capability": "stats.trend",
        "dataset_ref": "d" * 32,
        "data_version_hash": "a" * 64,
        "selection_plan_version": 1,
        "execution_plan_id": "plan-2",
        "execution_plan_version": 2,
        "logical_step_id": "verify-trend",
        "persisted_step_id": "step-2",
        "status": status,
        "tested": status not in {"failed", "cancelled"},
        "evidence_outcome": outcome,
        "outcome": outcome,
        "invocation_ids": ["invocation-1"],
        "failed_invocation_ids": [],
        "evidence_ids": ["evidence-1"] if status != "failed" else [],
        "evidence_ledger_sequences": [1] if status != "failed" else [],
        "verification": None,
        "last_failure_code": last_failure_code,
        "updated_at": "2026-08-12T00:00:00Z",
    }


def _screening() -> dict[str, object]:
    return {
        "schema": "chatbi-hypothesis-screening-v1",
        "schema_version": 1,
        "triggered": True,
        "data_version_hash": "a" * 64,
        "dataset_ref": "d" * 32,
        "candidate_limit": 4,
        "candidates": [
            {
                "hypothesis_id": "hyp_0123456789abcdef",
                "kind": "trend",
                "statement": "销售额可能随月份变化",
                "capability": "stats.trend",
                "required_roles": [],
                "expected_evidence": "趋势 Evidence",
                "status": "eligible",
                "reason_codes": ["roles_available"],
                "priority": 1,
                "tested": False,
            },
            {
                "hypothesis_id": "hyp_fedcba9876543210",
                "kind": "anomaly",
                "statement": "销售额可能存在异常点",
                "capability": "stats.anomaly",
                "required_roles": [],
                "expected_evidence": "异常 Evidence",
                "status": "eligible",
                "reason_codes": ["roles_available"],
                "priority": 2,
                "tested": False,
            },
        ],
        "eligible_candidate_ids": [
            "hyp_0123456789abcdef",
            "hyp_fedcba9876543210",
        ],
        "requires_confirmation": True,
        "blocking_reason": "user_selection_required",
        "raw_rows_read": False,
    }


def _decide(
    execution: dict[str, object],
    *,
    screening: dict[str, object] | None = None,
    run_status: str = "completed",
    attempts: int = 1,
    max_tools: int = 4,
    replans: int = 0,
    max_replans: int = 2,
    root_status: str = "completed",
    observation: dict[str, object] | None = None,
) -> dict[str, object]:
    result = decide_hypothesis_followup(  # type: ignore[arg-type]
        screening=screening,
        execution=execution,
        run_status=run_status,
        tool_attempts_used=attempts,
        max_tool_calls=max_tools,
        replans_used=replans,
        max_replans=max_replans,
        cancellation_root_status=root_status,
        latest_observation=observation,
        updated_at="2026-08-12T00:00:01Z",
    )
    assert result is not None
    return result


def test_supported_hypothesis_stops_without_post_hoc_expansion() -> None:
    result = _decide(
        _execution(status="supported", outcome="supported"),
        screening=_screening(),
    )

    assert result["decision"] == "stop"
    assert result["reason_codes"] == [
        "hypothesis_supported",
        "post_hoc_expansion_blocked",
    ]
    assert result["automatic_execution"] is False
    assert result["proposed_candidate"] is None


def test_not_supported_proposes_exactly_one_remaining_candidate() -> None:
    result = _decide(
        _execution(status="not_supported", outcome="not_supported"),
        screening=_screening(),
    )

    assert result["decision"] == "propose_next"
    assert result["requires_user_confirmation"] is True
    assert result["proposed_candidate"] == {
        "hypothesis_id": "hyp_fedcba9876543210",
        "kind": "anomaly",
        "statement": "销售额可能存在异常点",
        "capability": "stats.anomaly",
        "expected_evidence": "异常 Evidence",
        "priority": 2,
    }
    assert "不得自动扩展其他候选" in str(result["suggested_goal"])


def test_retryable_failure_only_proposes_bounded_supplement_with_budget() -> None:
    result = _decide(
        _execution(
            status="partial",
            outcome="inconclusive",
            last_failure_code="tool_execution_failed",
        ),
        observation={
            "status": "error",
            "code": "tool_execution_failed",
            "retryable": True,
        },
    )

    assert result["decision"] == "supplement_evidence"
    assert result["proposed_candidate"]["hypothesis_id"] == (  # type: ignore[index]
        "hyp_0123456789abcdef"
    )
    assert result["limits"] == {
        "tool_attempts_used": 1,
        "max_tool_calls": 4,
        "tool_calls_remaining": 3,
        "replans_used": 0,
        "max_replans": 2,
        "replans_remaining": 2,
        "cancellation_root_status": "completed",
    }


@pytest.mark.parametrize(
    ("kwargs", "expected_reason"),
    [
        ({"attempts": 4}, "tool_budget_exhausted"),
        ({"replans": 2}, "replan_budget_exhausted"),
        ({"observation": None}, "non_retryable_failure"),
    ],
)
def test_incomplete_evidence_degrades_when_supplement_is_not_safe(
    kwargs: dict[str, object],
    expected_reason: str,
) -> None:
    default_observation = {
        "status": "error",
        "code": "tool_execution_failed",
        "retryable": True,
    }
    if "observation" not in kwargs:
        kwargs["observation"] = default_observation
    result = _decide(
        _execution(
            status="partial",
            outcome="inconclusive",
            last_failure_code="tool_execution_failed",
        ),
        **kwargs,  # type: ignore[arg-type]
    )

    assert result["decision"] == "degrade"
    assert expected_reason in result["reason_codes"]
    assert result["suggested_goal"] is None


def test_cancelled_run_wins_over_retryable_observation() -> None:
    result = _decide(
        _execution(status="cancelled", outcome="untested"),
        run_status="cancelled",
        root_status="cancel_requested",
        observation={"status": "error", "retryable": True},
    )

    assert result["decision"] == "stop"
    assert result["reason_codes"] == ["cancellation_requested"]


def test_missing_cancellation_root_stops_fail_closed() -> None:
    result = _decide(
        _execution(status="not_supported", outcome="not_supported"),
        screening=_screening(),
        root_status="missing",
    )

    assert result["decision"] == "stop"
    assert result["reason_codes"] == ["cancellation_boundary_unsettled"]
    assert result["proposed_candidate"] is None


def test_followup_cross_field_validation_fails_closed() -> None:
    result = _decide(_execution(status="supported", outcome="supported"))
    result["requires_user_confirmation"] = True

    with pytest.raises(ValueError, match="人工确认标记"):
        validate_hypothesis_followup(result)
