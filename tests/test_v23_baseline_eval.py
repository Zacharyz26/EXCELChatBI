"""Reactive Agent baseline harness regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from packages.models.registry import ModelRegistry
from scripts.v23_baseline_eval import (
    DEFAULT_CASES,
    _FixtureRegistry,
    _score_rows,
    load_cases,
    run_evaluation,
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


@pytest.mark.asyncio
async def test_evaluation_checkpoints_successes_and_resumes_only_missing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import scripts.v23_baseline_eval as baseline_eval

    cases = load_cases(DEFAULT_CASES)[:2]
    registry = ModelRegistry("config/models.example.yaml")
    registry.load()
    calls: list[str] = []
    fail_second = True

    async def fake_run_case(
        case: dict[str, object],
        **kwargs: object,
    ) -> dict[str, object]:
        nonlocal fail_second
        case_id = str(case["id"])
        calls.append(case_id)
        if case_id == str(cases[1]["id"]) and fail_second:
            raise TimeoutError
        return {
            "case_id": case_id,
            "configured_model": str(kwargs["model_name"]),
            "repetition": int(kwargs["repetition"]),
            "task_satisfied": True,
            "required_artifacts": {},
            "numeric_claims_supported": True,
            "terminal_truthful": True,
            "clarification": "none",
            "tool_calls": 0,
            "invalid_tool_calls": 0,
            "forbidden_violations": [],
            "model_calls": 1,
            "agent_model_calls": 1,
            "planner_model_calls": 0,
            "plan_steps_total": 1,
            "plan_steps_completed": 1,
            "plan_revisions": 0,
            "prompt_tokens": 1,
            "completion_tokens": 1,
            "latency_ms": 1.0,
            "cost": 0.001,
            "cost_currency": "USD",
        }

    monkeypatch.setattr(baseline_eval, "_run_case", fake_run_case)
    checkpoints: list[list[dict[str, object]]] = []
    first = await run_evaluation(
        cases=cases,
        registry=registry,
        model_names=["deepseek-v4-flash"],
        repetitions=1,
        on_row_completed=lambda rows: checkpoints.append(rows),
    )

    assert first["completed_runs"] == 1
    assert first["execution_failures"] == [
        {
            "case_id": cases[1]["id"],
            "configured_model": "deepseek-v4-flash",
            "repetition": 1,
            "error_type": "TimeoutError",
        }
    ]
    assert len(checkpoints) == 1

    calls.clear()
    fail_second = False
    resumed = await run_evaluation(
        cases=cases,
        registry=registry,
        model_names=["deepseek-v4-flash"],
        repetitions=1,
        existing_rows=first["rows"],
    )

    assert calls == [cases[1]["id"]]
    assert resumed["completed_runs"] == 2
    assert resumed["execution_failures"] == []
