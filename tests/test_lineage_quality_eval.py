"""v2.5 阶段 3E 领域中立血缘质量门禁回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.lineage_quality_eval import DEFAULT_CASES, load_cases, run_evaluation

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_lineage_quality_set_is_domain_neutral_and_passes(
    tmp_path: Path,
) -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 6
    assert {case["fixture"] for case in cases} == {
        "profile",
        "complete",
        "deletion",
        "isolation",
        "bounded",
        "drift",
    }
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        term not in serialized
        for term in ("销售额", "利润", "复购率", "地区")
    )

    report = run_evaluation(cases, working_dir=tmp_path / "quality")

    assert report["passed"] is True
    assert report["case_count"] == 6
    assert report["metrics"] == {
        "contract_rate": 1.0,
        "deterministic_reopen_rate": 1.0,
        "safe_metadata_rate": 1.0,
        "isolation_rate": 1.0,
        "deleted_anchor_retention_rate": 1.0,
        "integrity_detection_rate": 1.0,
    }
    assert all(row["passed"] for row in report["cases"])
    assert report["contains_resource_ids"] is False
    assert report["contains_content"] is False


def test_lineage_quality_gate_detects_contract_drift(tmp_path: Path) -> None:
    case = dict(load_cases(DEFAULT_CASES)[0])
    case["expected_relations"] = []

    report = run_evaluation([case], working_dir=tmp_path / "drift")

    assert report["passed"] is False
    assert report["metrics"]["contract_rate"] == 0.0


def test_lineage_quality_case_loader_rejects_duplicate_ids(
    tmp_path: Path,
) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_lineage_quality_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/lineage_quality_eval.py" in workflow
    assert "--enforce --json-output .data/lineage-quality.json" in workflow
    assert "name: lineage-quality" in workflow
