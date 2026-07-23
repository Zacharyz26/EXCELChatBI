"""Hybrid Planner contract and evaluation harness regression tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from apps.orchestrator.control.planner_contract import (
    parse_task_plan,
    validate_task_plan,
)
from scripts.agent_planner_eval import (
    CAPABILITY_CATALOG,
    DEFAULT_CASES,
    _decision_note,
    _model_verdict,
    build_deterministic_plan,
    choose_route,
    load_cases,
)


def test_default_cases_route_without_reading_expected_labels() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 20
    assert {case["split"] for case in cases} == {"public", "heldout"}
    assert {
        str(case["id"]): choose_route(
            {key: value for key, value in case.items() if key != "expected"}
        )
        for case in cases
    } == {
        str(case["id"]): case["expected"]["route"] for case in cases
    }


def test_fast_and_template_paths_share_valid_task_plan_schema() -> None:
    capabilities = {str(item["name"]) for item in CAPABILITY_CATALOG}
    for case in load_cases(DEFAULT_CASES):
        route = choose_route(case)
        if route == "llm":
            continue
        plan = build_deterministic_plan(case, route)
        validation = validate_task_plan(plan, capabilities=capabilities)
        assert validation.valid, (case["id"], validation.issues)


def test_plan_validation_rejects_unknown_capability_and_cycle() -> None:
    plan: dict[str, Any] = {
        "schema_version": 1,
        "summary": "非法计划",
        "steps": [
            {
                "step_id": "a",
                "purpose": "测试",
                "capability": "invented.tool",
                "dependencies": ["b"],
                "expected_evidence": ["e"],
                "completion_conditions": ["c"],
                "fallback": [{"when": "失败", "action": "block"}],
            },
            {
                "step_id": "b",
                "purpose": "测试",
                "capability": "data.profile",
                "dependencies": ["a"],
                "expected_evidence": ["e"],
                "completion_conditions": ["c"],
                "fallback": [{"when": "失败", "action": "block"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }

    result = validate_task_plan(plan, capabilities={"data.profile"})

    assert result.schema_valid is True
    assert result.dependencies_valid is False
    assert result.capability_valid is False
    assert "dependencies:cycle" in result.issues


def test_strict_plan_parser_does_not_accept_markdown_fence() -> None:
    with pytest.raises(ValueError, match="严格 JSON"):
        parse_task_plan("```json\n{}\n```")


def test_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    first = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    path = tmp_path / "duplicate.jsonl"
    path.write_text(f"{first}\n{first}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(path)


def test_fixture_contains_no_raw_rows() -> None:
    for line in DEFAULT_CASES.read_text(encoding="utf-8").splitlines():
        case = json.loads(line)
        assert "rows" not in json.dumps(case["context"], ensure_ascii=False).lower()


def _score(hard: int = 0, protocol: int = 0, model: int = 0) -> dict[str, Any]:
    return {"hard_failures": hard, "protocol_errors": protocol, "model_errors": model}


def test_model_verdict_eligible_when_no_hard_failures() -> None:
    verdict = _model_verdict(_score())
    assert verdict["eligible"] is True
    assert verdict["verdict"] == "ELIGIBLE_PENDING_BLIND_REVIEW"
    assert verdict["disqualifiers"] == []


def test_model_verdict_disqualified_lists_every_reason() -> None:
    verdict = _model_verdict(_score(hard=3, protocol=1, model=2))
    assert verdict["eligible"] is False
    assert verdict["verdict"] == "DISQUALIFIED"
    assert set(verdict["disqualifiers"]) == {"hard_failure", "protocol_error", "model_error"}


def test_decision_note_is_per_model_not_global() -> None:
    # 一个模型硬失败不应牵连另一个合格模型（按模型选型）。
    note = _decision_note(["deepseek-v4-flash"], ["deepseek-v4-pro"])
    assert "deepseek-v4-flash" in note
    assert "deepseek-v4-pro" in note
    assert "禁止承担 Planner 路由" in note


def test_decision_note_no_go_only_when_no_eligible_model() -> None:
    note = _decision_note([], ["deepseek-v4-flash", "deepseek-v4-pro"])
    assert "无模型可承担" in note
