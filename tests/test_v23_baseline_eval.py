"""Reactive Agent baseline harness regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.v23_baseline_eval import (
    DEFAULT_CASES,
    _FixtureRegistry,
    _score_rows,
    load_cases,
)


def test_default_baseline_cases_are_complete_and_row_free() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 20
    assert {case["split"] for case in cases} == {"public", "heldout"}
    for case in cases:
        encoded = json.dumps(case["context"], ensure_ascii=False).lower()
        assert '"rows"' not in encoded


def test_fixture_registry_enforces_failure_scenarios(tmp_path: Path) -> None:
    chart = _FixtureRegistry("B18", tmp_path)
    with pytest.raises(ValueError, match="renderer_unavailable"):
        chart.execute(
            "gen_chart",
            '{"dataset_ref":"d1","chart_type":"bar","encoding":{}}',
        )

    forecast = _FixtureRegistry("B17", tmp_path)
    with pytest.raises(ValueError, match="样本不足"):
        forecast.execute(
            "trend_analysis",
            '{"dataset_ref":"d1","time_col":"月份","value_col":"销售额"}',
        )


def test_baseline_scoring_preserves_unavailable_cost() -> None:
    row = {
        "task_satisfied": False,
        "required_artifacts": {"chart": False},
        "numeric_claims_supported": True,
        "terminal_truthful": False,
        "clarification": "missed",
        "tool_calls": 1,
        "invalid_tool_calls": 1,
        "forbidden_violations": ["text_substituted_required_artifact"],
        "model_calls": 2,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 10.0,
        "cost": None,
        "cost_currency": None,
    }

    metrics = _score_rows([row])

    assert metrics["task_success_rate"] == 0.0
    assert metrics["artifact_delivery_rate"] == 0.0
    assert metrics["cost"] is None
    assert metrics["cost_availability"] == "unavailable"


def test_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "duplicate.jsonl"
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(path)
