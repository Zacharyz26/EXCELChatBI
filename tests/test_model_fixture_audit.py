"""Contracts for scenario-scoped Compose model fixture auditing."""

from __future__ import annotations

from copy import deepcopy

import pytest
from apps.e2e_model.audit import (
    verify_initial_model_audit,
    verify_recovery_model_audit,
)


def _audit(scenarios: dict[str, int]) -> dict[str, object]:
    return {
        "agent_stream_calls": 9,
        "feedback_marker_seen_in_agent": True,
        "branch_profile_tool_calls": 1,
        "multi_tool_batches": sum(scenarios.values()),
        "multi_tool_batches_by_scenario": scenarios,
        "hypothesis_anomaly_tool_calls": 1,
    }


def test_two_independent_parallel_scenarios_each_execute_one_batch() -> None:
    verify_initial_model_audit(_audit({"full-stack": 1, "report-parallel": 1}))


def test_duplicate_batch_in_one_parallel_scenario_is_rejected() -> None:
    with pytest.raises(
        ValueError,
        match="6A scenario full-stack requested 2 multi-tool batches",
    ):
        verify_initial_model_audit(_audit({"full-stack": 2, "report-parallel": 1}))


def test_missing_parallel_scenario_is_rejected() -> None:
    with pytest.raises(ValueError, match="6A scenario audit mismatch"):
        verify_initial_model_audit(_audit({"full-stack": 1}))


def test_recovery_uses_the_initial_audit_as_a_baseline() -> None:
    initial = _audit({"full-stack": 1, "report-parallel": 1})
    verify_recovery_model_audit(initial, deepcopy(initial))

    changed = deepcopy(initial)
    changed["agent_stream_calls"] = 10
    with pytest.raises(ValueError, match="recovery changed the model fixture audit"):
        verify_recovery_model_audit(initial, changed)
