"""v2.5 6D-4 anonymous governed-forecast quality gate regression."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
import scripts.forecast_quality_eval as quality_eval
from scripts.forecast_quality_eval import DEFAULT_CASES, load_cases, run_evaluation

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_forecast_set_is_anonymous_representative_and_passes() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 14
    assert {case["series"]["kind"] for case in cases} == {
        "linear",
        "constant",
        "seasonal",
    }
    assert {case["frequency"] for case in cases} == {"D", "W", "MS"}
    assert {case["expected"]["status"] for case in cases} == {"success", "error"}
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        term not in serialized
        for term in ("销售额", "利润", "复购率", "客户", "订单", "地区")
    )

    report = run_evaluation(cases)

    assert report["passed"] is True
    assert report["case_count"] == 14
    assert report["success_case_count"] == 8
    assert report["failure_case_count"] == 6
    assert report["metrics"] == {
        "expected_outcome_rate": 1.0,
        "output_contract_rate": 1.0,
        "leakage_guard_rate": 1.0,
        "baseline_reliability_rate": 1.0,
        "metric_availability_rate": 1.0,
        "failure_closed_rate": 1.0,
        "bounded_method_violations": 0,
    }
    assert report["reads_user_raw_rows"] is False
    assert report["synthetic_series_only"] is True
    assert report["tool_calls"] == 14
    assert report["model_calls"] == 0
    zero_target = next(
        row for row in report["cases"] if row["id"] == "FQ06_zero_target_metric"
    )
    assert zero_target["mape_available"] is False
    assert all(
        value is False
        for key, value in report.items()
        if key.startswith("contains_")
    )
    assert "event_time" not in _collect_strings(report)
    assert "signal_value" not in _collect_strings(report)


def test_forecast_gate_detects_reliability_overclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality_eval.forecast_tool

    def unsafe_forecast(arguments: dict[str, Any]) -> dict[str, Any]:
        result = original(arguments)
        result["reliability"] = "moderate"
        result["baseline"]["beats_baseline"] = False
        return result

    monkeypatch.setattr(quality_eval, "forecast_tool", unsafe_forecast)
    case = copy.deepcopy(load_cases(DEFAULT_CASES)[1])

    report = run_evaluation([case])

    assert report["passed"] is False
    assert report["metrics"]["baseline_reliability_rate"] == 0.0
    assert "baseline_reliability_rate" in report["misses"]


def test_forecast_gate_detects_output_contract_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = quality_eval.forecast_tool

    def drifting_forecast(arguments: dict[str, Any]) -> dict[str, Any]:
        result = original(arguments)
        result.pop("prediction_interval")
        return result

    monkeypatch.setattr(quality_eval, "forecast_tool", drifting_forecast)

    report = run_evaluation([copy.deepcopy(load_cases(DEFAULT_CASES)[0])])

    assert report["passed"] is False
    assert report["metrics"]["output_contract_rate"] == 0.0
    assert "output_contract_rate" in report["misses"]


def test_forecast_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_forecast_quality_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/forecast_quality_eval.py" in workflow
    assert "--enforce --json-output .data/forecast-quality.json" in workflow
    assert "name: forecast-quality" in workflow


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
