"""Validate scenario-scoped audit facts emitted by the Compose model fixture."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from typing import Any

EXPECTED_PARALLEL_SCENARIOS = frozenset({"full-stack", "report-parallel"})


def verify_initial_model_audit(audit: Mapping[str, Any]) -> None:
    """Require one and only one 6A tool batch per browser scenario."""
    if _integer(audit, "agent_stream_calls") < 1:
        raise ValueError("requests did not reach the single Agent model boundary")
    if audit.get("feedback_marker_seen_in_agent") is not True:
        raise ValueError("bounded parent feedback did not reach the Agent request")
    if _integer(audit, "branch_profile_tool_calls") != 1:
        raise ValueError(
            f"branch profile tool was requested {audit.get('branch_profile_tool_calls')} times"
        )
    if _integer(audit, "hypothesis_anomaly_tool_calls") != 1:
        raise ValueError(
            "6C anomaly hypothesis was requested "
            f"{audit.get('hypothesis_anomaly_tool_calls')} times"
        )

    scenario_counts = audit.get("multi_tool_batches_by_scenario")
    if not isinstance(scenario_counts, dict):
        raise ValueError("6A scenario audit is missing or malformed")
    if set(scenario_counts) != EXPECTED_PARALLEL_SCENARIOS:
        raise ValueError(
            "6A scenario audit mismatch: "
            f"expected {sorted(EXPECTED_PARALLEL_SCENARIOS)}, got {sorted(scenario_counts)}"
        )
    for scenario in sorted(EXPECTED_PARALLEL_SCENARIOS):
        count = scenario_counts.get(scenario)
        if not isinstance(count, int) or isinstance(count, bool) or count != 1:
            raise ValueError(f"6A scenario {scenario} requested {count} multi-tool batches")
    total = _integer(audit, "multi_tool_batches")
    if total != sum(scenario_counts.values()):
        raise ValueError(
            f"6A global/scenario audit mismatch: total={total}, scenarios={scenario_counts}"
        )


def verify_recovery_model_audit(
    initial: Mapping[str, Any],
    recovered: Mapping[str, Any],
) -> None:
    """Recovery is read-only and must not add model fixture calls to the initial audit."""
    verify_initial_model_audit(initial)
    if dict(recovered) != dict(initial):
        raise ValueError("recovery changed the model fixture audit")


def _integer(audit: Mapping[str, Any], key: str) -> int:
    value = audit.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"model fixture audit field {key} is not an integer")
    return value


def _load_environment_json(name: str) -> dict[str, Any]:
    raw = os.environ.get(name)
    if raw is None:
        raise ValueError(f"missing {name}")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError(f"{name} must contain a JSON object")
    return payload


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("phase", choices=("initial", "recovery"))
    args = parser.parse_args()
    if args.phase == "initial":
        verify_initial_model_audit(_load_environment_json("MODEL_AUDIT_JSON"))
        return
    verify_recovery_model_audit(
        _load_environment_json("INITIAL_MODEL_AUDIT_JSON"),
        _load_environment_json("RECOVERY_MODEL_AUDIT_JSON"),
    )


if __name__ == "__main__":
    main()
