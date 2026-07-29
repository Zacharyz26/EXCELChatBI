"""G7 盲评签字门禁测试。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from scripts.g7_review_gate import evaluate_g7


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
        encoding="utf-8",
    )


def _fixture(root: Path, *, reviewed: bool) -> None:
    row = {
        "case_id": "B01",
        "suite": "behavior",
        "repetition": 1,
        "configured_model": "flash",
        "response_hash": "response",
        "error_type": None,
        "selected_route": "llm",
        "plan": {"schema_version": 1},
    }
    candidate_id = hashlib.sha256(
        b"B01:behavior:1:flash:response"
    ).hexdigest()[:16]
    _write_json(
        root / "planner" / "report.json",
        {
            "eligible_models": ["flash"],
            "disqualified_models": ["pro"],
            "metrics": {"flash": {"hard_failures": 0}},
            "rows": [row],
        },
    )
    _write_json(
        root / "verifier" / "report.json",
        {
            "decision": "NO_GO",
            "rows": [
                {
                    "case_id": "S01",
                    "repetition": 1,
                    "configured_model": "flash",
                    "response_hash": "verifier-response",
                    "error_type": None,
                }
            ],
        },
    )
    _write_json(
        root / "baseline" / "report.json",
        {
            "scenario_set_hash": "frozen-cases",
            "metrics": {
                "deepseek-v4-flash": {
                    "forbidden_violations": 0,
                    "task_success_rate": 0.26666666666666666,
                    "truthful_terminal_rate": 0.36666666666666664,
                },
                "deepseek-v4-pro": {
                    "forbidden_violations": 0,
                    "task_success_rate": 0.2,
                    "truthful_terminal_rate": 0.23333333333333334,
                },
            }
        },
    )
    _write_json(
        root / "stage2" / "report.json",
        {
            "execution_mode": "stage2_structured_plan",
            "scenario_set_hash": "frozen-cases",
            "case_count": 20,
            "repetitions": 3,
            "models": ["deepseek-v4-flash"],
            "rows": [{} for _ in range(60)],
            "metrics": {
                "deepseek-v4-flash": {
                    "runs": 60,
                    "task_success_rate": 0.6,
                    "truthful_terminal_rate": 0.7,
                    "forbidden_violations": 0,
                    "cost_availability": "available",
                }
            },
        },
    )
    rating = 2 if reviewed else None
    verifier_candidate_id = hashlib.sha256(
        b"S01:1:flash:verifier-response"
    ).hexdigest()[:16]
    _write_jsonl(
        root / "planner" / "blind_review.jsonl",
        [
            {
                "candidate_id": candidate_id,
                "condition_specificity": rating,
                "fallback_actionability": rating,
            }
        ],
    )
    _write_jsonl(
        root / "verifier" / "blind_review.jsonl",
        [
            {
                "candidate_id": verifier_candidate_id,
                "coverage_rating": rating,
                "overclaim_rating": rating,
            }
        ],
    )


def test_g7_gate_refuses_approval_while_blind_ratings_are_missing(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, reviewed=False)

    result = evaluate_g7(tmp_path, reviewer="owner", approve=True)

    assert result["status"] == "review_required"
    assert result["approved"] is False
    assert "planner_blind_reviews_missing:1" in result["blockers"]
    assert "verifier_blind_reviews_missing:1" in result["blockers"]


def test_g7_gate_accepts_complete_review_with_frozen_hard_gates(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, reviewed=True)

    result = evaluate_g7(tmp_path, reviewer="owner", approve=True)

    assert result["status"] == "accepted"
    assert result["approved"] is True
    assert result["blockers"] == []
    assert result["planner"]["soft_gate_passed"] is True
    assert result["semantic_verifier"]["production_enabled"] is False


def test_g7_gate_detects_a_rating_row_removed_from_frozen_report(
    tmp_path: Path,
) -> None:
    _fixture(tmp_path, reviewed=True)
    _write_jsonl(tmp_path / "planner" / "blind_review.jsonl", [])

    result = evaluate_g7(tmp_path, reviewer="owner", approve=True)

    assert result["status"] == "review_required"
    assert result["planner"]["missing_ratings"] == 1
    assert "planner_blind_reviews_missing:1" in result["blockers"]


def test_g7_gate_requires_passing_stage2_behavior_report(tmp_path: Path) -> None:
    _fixture(tmp_path, reviewed=True)
    (tmp_path / "stage2" / "report.json").unlink()

    result = evaluate_g7(tmp_path, reviewer="owner", approve=True)

    assert result["status"] == "review_required"
    assert result["stage2"]["status"] == "missing"
    assert "stage2_behavior_report_missing" in result["blockers"]
