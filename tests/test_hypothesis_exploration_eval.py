"""v2.5 6C-4 anonymous exploration and consistency gate regression."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from scripts.hypothesis_exploration_eval import (
    DEFAULT_CASES,
    load_cases,
    run_evaluation,
)

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_exploration_set_is_anonymous_representative_and_passes() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 14
    assert {case["selected_kind"] for case in cases} == {
        "trend",
        "anomaly",
        "segment_comparison",
        "correlation",
    }
    assert {case["expected_followup_decision"] for case in cases} == {
        "stop",
        "degrade",
        "supplement_evidence",
        "propose_next",
    }
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        domain_term not in serialized
        for domain_term in ("销售额", "利润", "复购率", "客户", "订单", "地区")
    )

    report = run_evaluation(cases)

    assert report["passed"] is True
    assert report["case_count"] == 14
    assert report["metrics"] == {
        "screening_contract_rate": 1.0,
        "selection_binding_rate": 1.0,
        "evidence_outcome_rate": 1.0,
        "followup_decision_rate": 1.0,
        "boundary_convergence_rate": 1.0,
        "unverified_conclusion_violations": 0,
        "automatic_execution_violations": 0,
    }
    assert report["reads_raw_rows"] is False
    assert report["model_calls"] == 0
    assert report["tool_calls"] == 0
    report_strings = _collect_strings(report)
    column_names = {
        column["name"] for case in cases for column in case["profile"]["columns"]
    }
    assert column_names.isdisjoint(report_strings)
    assert all(
        flag is False
        for key, flag in report.items()
        if key.startswith("contains_")
    )


def test_exploration_gate_detects_evidence_outcome_drift() -> None:
    case = copy.deepcopy(load_cases(DEFAULT_CASES)[0])
    case["expected_execution_status"] = "not_supported"

    report = run_evaluation([case])

    assert report["passed"] is False
    assert report["metrics"]["evidence_outcome_rate"] == 0.0
    assert "evidence_outcome_rate" in report["misses"]


def test_exploration_gate_detects_followup_drift() -> None:
    case = copy.deepcopy(load_cases(DEFAULT_CASES)[1])
    case["expected_followup_decision"] = "stop"
    case["expected_followup_kind"] = None

    report = run_evaluation([case])

    assert report["passed"] is False
    assert report["metrics"]["followup_decision_rate"] == 0.0
    assert "followup_decision_rate" in report["misses"]


def test_exploration_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_exploration_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/hypothesis_exploration_eval.py" in workflow
    assert "--enforce --json-output .data/hypothesis-exploration.json" in workflow
    assert "name: hypothesis-exploration" in workflow


def test_compose_gate_rechecks_hypothesis_projection_after_restart_and_restore() -> None:
    gate = (_ROOT / "scripts" / "run_compose_e2e.sh").read_text(encoding="utf-8")
    browser = (_ROOT / "apps" / "web" / "e2e" / "compose-full-stack.spec.ts").read_text(
        encoding="utf-8"
    )

    assert "verify_hypothesis_run" in gate
    assert gate.count("verify_hypothesis_run") >= 3
    assert "COMPOSE_6C_EXPLORATION" in browser
    assert "hypothesis_followup" in browser
    assert "automatic_execution" in browser
    assert "填入新分支目标" in browser
    assert 'restart api stats-tools report-tools web' in gate


def _collect_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {
            item for nested in value.values() for item in _collect_strings(nested)
        }
    if isinstance(value, list):
        return {item for nested in value for item in _collect_strings(nested)}
    return set()
