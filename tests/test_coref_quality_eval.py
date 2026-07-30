"""v2.5 3C 领域中立指代质量门禁回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.coref_quality_eval import DEFAULT_CASES, load_cases, run_evaluation

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_coref_quality_set_is_domain_neutral_and_passes(
    tmp_path: Path,
) -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 23
    assert {case["resolver"] for case in cases} == {"coref", "memory"}
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        commercial_term not in serialized
        for commercial_term in ("销售额", "利润", "复购率", "地区")
    )

    report = run_evaluation(cases, working_dir=tmp_path / "quality")

    assert report["passed"] is True
    assert report["case_count"] == 23
    assert report["metrics"] == {
        "deterministic_resolution_rate": 1.0,
        "misbinding_rate": 0.0,
        "clarification_recall": 1.0,
        "cross_project_leak_count": 0,
    }
    assert all(row["passed"] for row in report["cases"])
    assert report["contains_query_text"] is False
    assert report["contains_resource_ids"] is False


def test_coref_quality_gate_detects_misbinding(tmp_path: Path) -> None:
    case = dict(load_cases(DEFAULT_CASES)[0])
    case["expected_targets"] = ["artifact:chart1"]

    report = run_evaluation([case], working_dir=tmp_path / "misbinding")

    assert report["passed"] is False
    assert report["metrics"]["deterministic_resolution_rate"] == 0.0
    assert report["metrics"]["misbinding_rate"] == 1.0
    assert report["cases"][0]["misbound"] is True


def test_coref_quality_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_coref_quality_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/coref_quality_eval.py" in workflow
    assert "--enforce --json-output .data/coref-quality.json" in workflow
    assert "name: coref-quality" in workflow
