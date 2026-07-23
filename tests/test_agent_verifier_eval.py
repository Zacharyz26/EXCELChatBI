"""Semantic Verifier evaluation harness regression tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.agent_verifier_eval import (
    DEFAULT_CASES,
    LEGACY_V2_CASES,
    _score_rows,
    load_cases,
)


def test_default_semantic_cases_are_paired_and_split() -> None:
    cases = load_cases(DEFAULT_CASES)

    assert len(cases) == 16
    assert {case["split"] for case in cases} == {"public", "heldout"}
    assert {case["expected_verdict"] for case in cases} == {
        "PASS",
        "NEEDS_ACTION",
        "WAITING_USER",
    }
    by_category: dict[str, set[str]] = {}
    for case in cases:
        by_category.setdefault(str(case["category"]), set()).add(
            str(case["expected_verdict"])
        )
    for category in (
        "coverage",
        "overclaim",
        "limitations",
        "claim_scope",
        "method_disclosure",
        "filter_scope",
        "alternative_explanation",
    ):
        assert by_category[category] == {"PASS", "NEEDS_ACTION"}
    assert by_category["clarification"] == {"WAITING_USER", "NEEDS_ACTION"}


def test_legacy_v2_fixture_remains_reproducible() -> None:
    legacy = load_cases(LEGACY_V2_CASES)

    assert len(legacy) == 14
    assert {case["id"] for case in legacy} == {f"SV{i:02d}" for i in range(1, 15)}


def test_case_loader_rejects_duplicate_ids(tmp_path: Path) -> None:
    case = {
        "id": "duplicate",
        "split": "public",
        "category": "coverage",
        "expected_verdict": "PASS",
        "input": {
            "goal": "回答目标",
            "criteria": [{"criterion_id": "c1", "description": "覆盖目标"}],
            "claims": [
                {
                    "claim_id": "claim-1",
                    "text": "已覆盖目标",
                    "evidence_ids": ["e1"],
                }
            ],
            "evidence": [{"evidence_id": "e1", "summary": {"result": "ok"}}],
        },
    }
    path = tmp_path / "duplicate.jsonl"
    line = json.dumps(case, ensure_ascii=False)
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="case id 重复"):
        load_cases(path)


def test_scoring_exposes_false_pass_as_hard_failure_signal() -> None:
    metrics = _score_rows(
        [
            {
                "matched": False,
                "predicted_verdict": "PASS",
                "expected_verdict": "NEEDS_ACTION",
                "error_type": None,
            },
            {
                "matched": False,
                "predicted_verdict": "PROTOCOL_ERROR",
                "expected_verdict": "PASS",
                "error_type": "protocol_error",
            },
        ]
    )

    assert metrics["false_passes"] == 1
    assert metrics["false_blocks"] == 1
    assert metrics["protocol_errors"] == 1
    assert metrics["exact_match_rate"] == 0.0
