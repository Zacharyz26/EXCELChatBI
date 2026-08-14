"""v2.5 6E-4 anonymous governed-Join quality gate regression."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import scripts.join_quality_eval as quality_eval
from scripts.join_quality_eval import DEFAULT_CASES, load_cases, run_evaluation

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_join_set_is_domain_neutral_representative_and_passes() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 17
    assert {case["left"]["kind"] for case in cases} == {"number", "text"}
    assert {case["join_type"] for case in cases} == {"inner", "left", "right", "full"}
    assert {case["expected"]["status"] for case in cases} == {
        "ready",
        "requires_confirmation",
        "blocked",
        "error",
    }
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        term not in serialized
        for term in ("销售额", "利润", "客户", "订单", "地区", "邮箱", "手机号")
    )

    report = run_evaluation(cases)

    assert report["passed"] is True
    assert report["case_count"] == 17
    assert report["preflight_case_count"] == 14
    assert report["guarded_case_count"] == 6
    assert report["execution_case_count"] == 5
    assert report["metrics"] == {
        "expected_outcome_rate": 1.0,
        "output_contract_rate": 1.0,
        "risk_classification_rate": 1.0,
        "read_only_preflight_rate": 1.0,
        "failure_closed_rate": 1.0,
        "execution_contract_rate": 1.0,
        "two_parent_result_rate": 1.0,
        "bounded_join_violations": 0,
    }
    assert report["reads_user_raw_rows"] is False
    assert report["synthetic_generators_only"] is True
    assert report["preflight_tool_calls"] == 17
    assert report["execution_tool_calls"] == 5
    assert report["model_calls"] == 0
    many_to_many = next(
        row for row in report["cases"] if row["id"] == "JQ07_many_to_many_confirmation"
    )
    assert many_to_many["actual_status"] == "requires_confirmation"
    assert many_to_many["risk_codes"] == ["many_to_many"]
    expansion = next(
        row for row in report["cases"] if row["id"] == "JQ08_row_expansion_confirmation"
    )
    assert expansion["risk_codes"] == ["many_to_many", "row_expansion"]
    assert all(
        value is False
        for key, value in report.items()
        if key.startswith("contains_")
    )
    report_strings = _collect_strings(report)
    assert "left_key" not in report_strings
    assert "right_key" not in report_strings


def test_join_gate_detects_confirmation_bypass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality_eval.preflight_tool

    def unsafe_preflight(arguments: dict[str, Any]) -> dict[str, Any]:
        result = original(arguments)
        result["status"] = "ready"
        result["requires_confirmation"] = False
        return result

    monkeypatch.setattr(quality_eval, "preflight_tool", unsafe_preflight)
    case = copy.deepcopy(load_cases(DEFAULT_CASES)[6])

    report = run_evaluation([case])

    assert report["passed"] is False
    assert report["metrics"]["expected_outcome_rate"] == 0.0
    assert "expected_outcome_rate" in report["misses"]


def test_join_gate_detects_preflight_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality_eval.preflight_tool

    def drifting_preflight(arguments: dict[str, Any]) -> dict[str, Any]:
        result = original(arguments)
        result.pop("raw_rows_returned")
        return result

    monkeypatch.setattr(quality_eval, "preflight_tool", drifting_preflight)

    report = run_evaluation([copy.deepcopy(load_cases(DEFAULT_CASES)[0])])

    assert report["passed"] is False
    assert report["metrics"]["output_contract_rate"] == 0.0
    assert "output_contract_rate" in report["misses"]


def test_join_gate_detects_two_parent_result_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality_eval.execute_tool

    def drifting_execute(arguments: dict[str, Any]) -> dict[str, Any]:
        result = original(arguments)
        result["parent_refs"] = list(reversed(result["parent_refs"]))
        return result

    monkeypatch.setattr(quality_eval, "execute_tool", drifting_execute)

    report = run_evaluation([copy.deepcopy(load_cases(DEFAULT_CASES)[0])])

    assert report["passed"] is False
    assert report["metrics"]["two_parent_result_rate"] == 0.0
    assert "two_parent_result_rate" in report["misses"]


def test_join_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_join_quality_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/join_quality_eval.py" in workflow
    assert "--enforce --json-output .data/join-quality.json" in workflow
    assert "name: join-quality" in workflow


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
