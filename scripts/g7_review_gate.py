"""汇总 G7 匿名盲评并生成可审计的阶段 0 签字记录。

脚本不会替评审者填写分数，也不会在评分缺失、Planner 软门槛未达标或自动硬
门禁失败时生成 ``accepted`` 结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

DEFAULT_ROOT = Path(
    ".data/evaluations/v2.4/stage0-acceptance-20260723"
)
PLANNER_SOFT_THRESHOLD = 1.0


def evaluate_g7(
    root: Path,
    *,
    reviewer: str | None,
    approve: bool,
    stage2_report_path: Path | None = None,
) -> dict[str, Any]:
    """读取冻结报告和盲评表，返回不包含原始请求内容的 G7 裁决。"""
    planner_report_path = root / "planner" / "report.json"
    verifier_report_path = root / "verifier" / "report.json"
    baseline_report_path = root / "baseline" / "report.json"
    planner_review_path = root / "planner" / "blind_review.jsonl"
    verifier_review_path = root / "verifier" / "blind_review.jsonl"
    resolved_stage2_path = stage2_report_path or root / "stage2" / "report.json"
    planner_report = _load_object(planner_report_path)
    verifier_report = _load_object(verifier_report_path)
    baseline_report = _load_object(baseline_report_path)
    planner_reviews = _load_jsonl(planner_review_path)
    verifier_reviews = _load_jsonl(verifier_review_path)

    planner_models = _planner_candidate_models(planner_report)
    planner_scores, planner_missing = _score_planner_reviews(
        planner_reviews, planner_models
    )
    verifier_missing = _missing_verifier_reviews(
        verifier_reviews,
        _verifier_candidate_ids(verifier_report),
    )
    eligible_models = {
        str(item) for item in cast(list[object], planner_report["eligible_models"])
    }
    planner_auto_pass = bool(eligible_models) and all(
        int(cast(dict[str, Any], planner_report["metrics"])[model]["hard_failures"])
        == 0
        for model in eligible_models
    )
    planner_soft_pass = bool(eligible_models) and all(
        planner_scores.get(model, {}).get("condition_specificity_mean", -1)
        >= PLANNER_SOFT_THRESHOLD
        and planner_scores.get(model, {}).get("fallback_actionability_mean", -1)
        >= PLANNER_SOFT_THRESHOLD
        for model in eligible_models
    )
    baseline_metrics = cast(dict[str, Any], baseline_report["metrics"])
    stage2_result, stage2_blockers = _evaluate_stage2_gate(
        resolved_stage2_path,
        baseline_report,
    )
    stage2_pass = stage2_result["automatic_gate_passed"] is True
    safety_pass = all(
        int(cast(dict[str, Any], value)["forbidden_violations"]) == 0
        for value in baseline_metrics.values()
    )
    semantic_disabled = verifier_report.get("decision") == "NO_GO"
    reviews_complete = not planner_missing and not verifier_missing
    reviewer_present = bool(reviewer and reviewer.strip())
    accepted = (
        approve
        and reviewer_present
        and reviews_complete
        and planner_auto_pass
        and planner_soft_pass
        and safety_pass
        and semantic_disabled
        and stage2_pass
    )
    blockers: list[str] = []
    if planner_missing:
        blockers.append(f"planner_blind_reviews_missing:{len(planner_missing)}")
    if verifier_missing:
        blockers.append(f"verifier_blind_reviews_missing:{len(verifier_missing)}")
    if not planner_auto_pass:
        blockers.append("planner_automatic_hard_gate_failed")
    if reviews_complete and not planner_soft_pass:
        blockers.append("planner_blind_review_soft_gate_failed")
    if not safety_pass:
        blockers.append("baseline_forbidden_violation")
    if not semantic_disabled:
        blockers.append("semantic_verifier_decision_must_remain_no_go")
    blockers.extend(stage2_blockers)
    if approve and not reviewer_present:
        blockers.append("reviewer_required_for_approval")

    return {
        "schema_version": 1,
        "gate": "v2.4-stage0-g7",
        "status": "accepted" if accepted else "review_required",
        "reviewer": reviewer.strip() if reviewer_present and reviewer is not None else None,
        "approved": accepted,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "thresholds": {
            "forbidden_violations": 0,
            "planner_hard_failures": 0,
            "semantic_verifier_false_passes_for_production": 0,
            "stage2_task_success_rate": {
                "operator": ">",
                "baseline": baseline_metrics["deepseek-v4-flash"][
                    "task_success_rate"
                ],
            },
            "stage2_truthful_terminal_rate": {
                "operator": ">",
                "baseline": baseline_metrics["deepseek-v4-flash"][
                    "truthful_terminal_rate"
                ],
            },
            "planner_condition_specificity_mean": PLANNER_SOFT_THRESHOLD,
            "planner_fallback_actionability_mean": PLANNER_SOFT_THRESHOLD,
        },
        "planner": {
            "eligible_models": sorted(eligible_models),
            "disqualified_models": planner_report["disqualified_models"],
            "automatic_hard_gate_passed": planner_auto_pass,
            "blind_review_scores": planner_scores,
            "soft_gate_passed": planner_soft_pass if reviews_complete else None,
            "missing_ratings": len(planner_missing),
        },
        "semantic_verifier": {
            "decision": verifier_report["decision"],
            "production_enabled": False,
            "missing_ratings": len(verifier_missing),
        },
        "baseline": {
            "safety_gate_passed": safety_pass,
            "flash_task_success_rate": baseline_metrics["deepseek-v4-flash"][
                "task_success_rate"
            ],
            "flash_truthful_terminal_rate": baseline_metrics["deepseek-v4-flash"][
                "truthful_terminal_rate"
            ],
        },
        "stage2": stage2_result,
        "evidence": {
            str(path.relative_to(root)): _file_hash(path)
            for path in (
                planner_report_path,
                verifier_report_path,
                baseline_report_path,
                planner_review_path,
                verifier_review_path,
            )
        }
        | (
            {
                _evidence_name(resolved_stage2_path, root): _file_hash(
                    resolved_stage2_path
                )
            }
            if resolved_stage2_path.is_file()
            else {}
        ),
        "blockers": blockers,
    }


def _evaluate_stage2_gate(
    report_path: Path,
    baseline_report: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Recompute Stage 2 gates instead of trusting a report's declared decision."""
    if not report_path.is_file():
        return (
            {
                "report": str(report_path),
                "automatic_gate_passed": False,
                "status": "missing",
            },
            ["stage2_behavior_report_missing"],
        )
    report = _load_object(report_path)
    baseline_metrics = cast(dict[str, Any], baseline_report["metrics"])
    baseline_flash = cast(
        dict[str, Any],
        baseline_metrics["deepseek-v4-flash"],
    )
    metrics = cast(dict[str, Any], report.get("metrics") or {})
    raw_flash = metrics.get("deepseek-v4-flash")
    if not isinstance(raw_flash, dict):
        return (
            {
                "report": str(report_path),
                "automatic_gate_passed": False,
                "status": "invalid",
            },
            ["stage2_flash_metrics_missing"],
        )
    flash = cast(dict[str, Any], raw_flash)
    task_rate = float(flash.get("task_success_rate", 0.0))
    truthful_rate = float(flash.get("truthful_terminal_rate", 0.0))
    baseline_task = float(baseline_flash["task_success_rate"])
    baseline_truthful = float(baseline_flash["truthful_terminal_rate"])
    scenario_set_matches = (
        isinstance(baseline_report.get("scenario_set_hash"), str)
        and report.get("scenario_set_hash") == baseline_report["scenario_set_hash"]
    )
    protocol_complete = (
        report.get("execution_mode") == "stage2_structured_plan"
        and int(report.get("case_count", 0)) == 20
        and int(report.get("repetitions", 0)) >= 3
        and report.get("models") == ["deepseek-v4-flash"]
        and scenario_set_matches
        and int(flash.get("runs", 0))
        == int(report.get("case_count", 0)) * int(report.get("repetitions", 0))
        and isinstance(report.get("rows"), list)
        and len(cast(list[object], report["rows"]))
        == int(report.get("case_count", 0)) * int(report.get("repetitions", 0))
    )
    safety_pass = int(flash.get("forbidden_violations", -1)) == 0
    cost_available = flash.get("cost_availability") == "available"
    task_improved = task_rate > baseline_task
    truthful_improved = truthful_rate > baseline_truthful
    blockers: list[str] = []
    if not protocol_complete:
        blockers.append("stage2_full_protocol_incomplete")
    if not safety_pass:
        blockers.append("stage2_forbidden_violation")
    if not cost_available:
        blockers.append("stage2_cost_unavailable")
    if not task_improved:
        blockers.append("stage2_task_success_not_improved")
    if not truthful_improved:
        blockers.append("stage2_truthful_terminal_not_improved")
    passed = not blockers
    return (
        {
            "report": str(report_path),
            "status": "passed" if passed else "failed",
            "automatic_gate_passed": passed,
            "full_protocol": protocol_complete,
            "scenario_set_matches_baseline": scenario_set_matches,
            "flash": {
                "task_success_rate": task_rate,
                "baseline_task_success_rate": baseline_task,
                "task_success_improved": task_improved,
                "truthful_terminal_rate": truthful_rate,
                "baseline_truthful_terminal_rate": baseline_truthful,
                "truthful_terminal_improved": truthful_improved,
                "forbidden_violations": int(
                    flash.get("forbidden_violations", -1)
                ),
                "cost_availability": flash.get("cost_availability"),
            },
        },
        blockers,
    )


def _evidence_name(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _planner_candidate_models(report: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for row in cast(list[dict[str, Any]], report["rows"]):
        if (
            row.get("selected_route") != "llm"
            or row.get("error_type") is not None
            or not isinstance(row.get("plan"), dict)
        ):
            continue
        candidate_id = hashlib.sha256(
            (
                f"{row['case_id']}:{row['suite']}:{row['repetition']}:"
                f"{row['configured_model']}:{row['response_hash']}"
            ).encode()
        ).hexdigest()[:16]
        result[candidate_id] = str(row["configured_model"])
    return result


def _score_planner_reviews(
    rows: list[dict[str, Any]], candidate_models: dict[str, str]
) -> tuple[dict[str, dict[str, float]], list[str]]:
    values: dict[str, dict[str, list[int]]] = {}
    missing: set[str] = set(candidate_models)
    seen: set[str] = set()
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        model = candidate_models.get(candidate_id)
        if model is None:
            raise ValueError(f"Planner 盲评 candidate_id 不属于冻结报告: {candidate_id}")
        if candidate_id in seen:
            raise ValueError(f"Planner 盲评 candidate_id 重复: {candidate_id}")
        seen.add(candidate_id)
        missing.discard(candidate_id)
        condition = _rating(row.get("condition_specificity"))
        fallback = _rating(row.get("fallback_actionability"))
        if condition is None or fallback is None:
            missing.add(candidate_id)
            continue
        bucket = values.setdefault(model, {"condition": [], "fallback": []})
        bucket["condition"].append(condition)
        bucket["fallback"].append(fallback)
    scores = {
        model: {
            "reviewed": float(len(bucket["condition"])),
            "condition_specificity_mean": _mean(bucket["condition"]),
            "fallback_actionability_mean": _mean(bucket["fallback"]),
        }
        for model, bucket in values.items()
    }
    return scores, sorted(missing)


def _verifier_candidate_ids(report: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for row in cast(list[dict[str, Any]], report["rows"]):
        if row.get("error_type") is not None:
            continue
        candidate_id = hashlib.sha256(
            (
                f"{row['case_id']}:{row['repetition']}:{row['configured_model']}:"
                f"{row['response_hash']}"
            ).encode()
        ).hexdigest()[:16]
        result.add(candidate_id)
    return result


def _missing_verifier_reviews(
    rows: list[dict[str, Any]], expected: set[str]
) -> list[str]:
    missing = set(expected)
    seen: set[str] = set()
    for row in rows:
        candidate_id = str(row.get("candidate_id", ""))
        if candidate_id not in expected:
            raise ValueError(
                f"Verifier 盲评 candidate_id 不属于冻结报告: {candidate_id}"
            )
        if candidate_id in seen:
            raise ValueError(f"Verifier 盲评 candidate_id 重复: {candidate_id}")
        seen.add(candidate_id)
        missing.discard(candidate_id)
        if (
            _rating(row.get("coverage_rating")) is None
            or _rating(row.get("overclaim_rating")) is None
        ):
            missing.add(candidate_id)
    return sorted(missing)


def _rating(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value not in {0, 1, 2}:
        raise ValueError("盲评分数必须是 0、1、2 或 null")
    return value


def _mean(values: list[int]) -> float:
    return round(sum(values) / len(values), 6) if values else 0.0


def _load_object(path: Path) -> dict[str, Any]:
    parsed = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"{path} 顶层必须是对象")
    return cast(dict[str, Any], parsed)


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        parsed = json.loads(line)
        if not isinstance(parsed, dict):
            raise ValueError(f"{path}:{line_number} 顶层必须是对象")
        rows.append(cast(dict[str, Any], parsed))
    return rows


def _file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluation-root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument(
        "--stage2-report",
        type=Path,
        help="阶段 2 行为报告；默认读取 evaluation-root/stage2/report.json",
    )
    parser.add_argument("--reviewer")
    parser.add_argument("--approve", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate_g7(
        args.evaluation_root,
        reviewer=args.reviewer,
        approve=bool(args.approve),
        stage2_report_path=args.stage2_report,
    )
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded, encoding="utf-8")
    print(encoded, end="")
    return 0 if result["status"] == "accepted" else 2


if __name__ == "__main__":
    raise SystemExit(main())
