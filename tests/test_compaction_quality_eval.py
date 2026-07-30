"""v2.5 3B 领域中立上下文压缩质量门禁回归。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.compaction_quality_eval import DEFAULT_CASES, load_cases, run_evaluation

_ROOT = Path(__file__).resolve().parents[1]


def test_frozen_compaction_quality_set_is_domain_neutral_and_passes(
    tmp_path: Path,
) -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 6
    serialized = json.dumps(cases, ensure_ascii=False)
    assert all(
        commercial_term not in serialized
        for commercial_term in ("销售额", "利润", "复购率", "地区")
    )

    report = run_evaluation(cases, working_dir=tmp_path / "quality")

    assert report["passed"] is True
    assert report["case_count"] == 6
    assert set(report["metrics"].values()) == {1.0}
    assert all(row["passed"] for row in report["cases"])
    assert all("summary_text" not in row for row in report["cases"])


def test_quality_gate_detects_missing_required_fact(tmp_path: Path) -> None:
    case = dict(load_cases(DEFAULT_CASES)[0])
    case["required_summary_terms"] = ["不存在的冻结事实"]

    report = run_evaluation([case], working_dir=tmp_path / "failed-quality")

    assert report["passed"] is False
    assert report["metrics"]["key_retention_rate"] == 0.0
    assert report["cases"][0]["key_retention"] is False


def test_quality_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    line = DEFAULT_CASES.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(duplicate)


def test_backend_ci_enforces_and_uploads_compaction_quality_gate() -> None:
    workflow = (_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "scripts/compaction_quality_eval.py" in workflow
    assert "--enforce --json-output .data/compaction-quality.json" in workflow
    assert "name: compaction-quality" in workflow
