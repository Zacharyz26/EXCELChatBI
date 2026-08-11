"""v2.5 6B-3 匿名数据角色与质量门禁回归。"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import scripts.data_role_quality_eval as quality_eval
from scripts.data_role_quality_eval import DEFAULT_CASES, load_cases, run_evaluation

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_data_role_quality_set_is_anonymous_representative_and_passes() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 16
    assert sum(len(case["profile"]["columns"]) for case in cases) == 65
    assert {
        expectation["primary_role"]
        for case in cases
        for expectation in case["expected_roles"].values()
    } == {"time", "metric", "dimension", "identifier", "unknown"}
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        domain_term not in serialized
        for domain_term in ("销售额", "利润", "复购率", "地区", "客户")
    )

    report = run_evaluation(cases)

    assert report["passed"] is True
    assert report["case_count"] == 16
    assert report["column_count"] == 65
    assert report["metrics"] == {
        "role_accuracy": pytest.approx(64 / 65),
        "ambiguity_recall": 1.0,
        "high_confidence_error_rate": 0.0,
        "auto_mutation_violation_count": 0,
    }
    assert report["contains_column_names"] is False
    assert report["contains_profile_metadata"] is False
    assert report["reads_raw_rows"] is False
    column_names = {
        column["name"]
        for case in cases
        for column in case["profile"]["columns"]
    }
    report_strings = _collect_strings(report)
    assert column_names.isdisjoint(report_strings)


def test_data_role_quality_gate_detects_high_confidence_role_error() -> None:
    case = copy.deepcopy(load_cases(DEFAULT_CASES)[0])
    case["expected_roles"]["duration_ms"] = {
        "primary_role": "dimension",
        "ambiguous": False,
    }

    report = run_evaluation([case])

    assert report["passed"] is False
    assert report["metrics"]["high_confidence_error_rate"] == pytest.approx(0.2)
    assert "high_confidence_error_rate" in report["misses"]


def test_data_role_quality_gate_detects_unsafe_automatic_advice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality_eval.diagnose_data_quality

    def unsafe_advisor(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original(*args, **kwargs)
        result["mutates_data"] = True
        return result

    monkeypatch.setattr(quality_eval, "diagnose_data_quality", unsafe_advisor)

    report = run_evaluation([load_cases(DEFAULT_CASES)[0]])

    assert report["passed"] is False
    assert report["metrics"]["auto_mutation_violation_count"] == 1
    assert "auto_mutation_violation_count" in report["misses"]


def test_data_role_quality_gate_detects_quality_contract_drift() -> None:
    case = copy.deepcopy(load_cases(DEFAULT_CASES)[0])
    case["expected_quality_codes"] = ["missing_values"]

    report = run_evaluation([case])

    assert report["passed"] is False
    assert report["misses"]["quality_contract"] == {"failed_case_count": 1}


def test_data_role_quality_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_data_role_quality_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/data_role_quality_eval.py" in workflow
    assert "--enforce --json-output .data/data-role-quality.json" in workflow
    assert "name: data-role-quality" in workflow


def _collect_strings(value: object) -> set[str]:
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return {
            item
            for nested in value.values()
            for item in _collect_strings(nested)
        }
    if isinstance(value, list):
        return {item for nested in value for item in _collect_strings(nested)}
    return set()
