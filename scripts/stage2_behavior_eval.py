"""Evaluate the Stage 2 structured-plan Agent against the frozen v2.3 baseline.

The harness uses the same 20 observable-behavior cases and deterministic fixture
tools as the frozen baseline, while enabling the production TaskPlan, Executor,
Replanner and deterministic Verifier path. Real model calls are fail-closed
behind ``--confirm-paid-run``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.models.registry import ModelRegistry  # noqa: E402
from packages.models.types import Scenario  # noqa: E402

from scripts.v23_baseline_eval import (  # noqa: E402
    DEFAULT_CASES,
    load_cases,
    run_evaluation,
)

DEFAULT_BASELINE_REPORT = Path(
    ".data/evaluations/v2.4/stage0-acceptance-20260723/baseline/report.json"
)
DEFAULT_OUTPUT = Path(
    ".data/evaluations/v2.4/stage0-acceptance-20260723/stage2/report.json"
)
DEFAULT_CHECKPOINT = Path(
    ".data/evaluations/v2.4/stage0-acceptance-20260723/stage2/checkpoint.json"
)
EXPECTED_CASE_COUNT = 20
MIN_REPETITIONS = 3
_PRIVACY = {
    "raw_dataset_rows_stored": False,
    "full_prompts_stored": False,
    "full_model_responses_stored": False,
    "secrets_stored": False,
}


def evaluate_stage2_report(
    report: dict[str, Any],
    baseline_report: dict[str, Any],
) -> dict[str, Any]:
    """Return a deterministic Stage 2 comparison without changing source rows."""
    models = cast(list[object], report.get("models") or [])
    metrics_by_model = _object(report.get("metrics"), "stage2.metrics")
    baseline_metrics = _object(baseline_report.get("metrics"), "baseline.metrics")
    baseline_hash = baseline_report.get("scenario_set_hash")
    report_hash = report.get("scenario_set_hash")
    scenario_set_matches = (
        isinstance(baseline_hash, str)
        and bool(baseline_hash)
        and report_hash == baseline_hash
    )
    full_protocol = (
        int(report.get("case_count", 0)) == EXPECTED_CASE_COUNT
        and int(report.get("repetitions", 0)) >= MIN_REPETITIONS
        and report.get("execution_mode") == "stage2_structured_plan"
        and scenario_set_matches
    )
    expected_runs = int(report.get("case_count", 0)) * int(
        report.get("repetitions", 0)
    )
    rows = report.get("rows")
    if not isinstance(rows, list) or len(rows) != expected_runs * len(models):
        full_protocol = False

    comparisons: dict[str, dict[str, Any]] = {}
    blockers: list[str] = []
    for raw_model in models:
        model = str(raw_model)
        metrics = _object(metrics_by_model.get(model), f"stage2.metrics.{model}")
        baseline = _object(
            baseline_metrics.get(model),
            f"baseline.metrics.{model}",
        )
        task_rate = float(metrics.get("task_success_rate", 0.0))
        truthful_rate = float(metrics.get("truthful_terminal_rate", 0.0))
        baseline_task = float(baseline.get("task_success_rate", 0.0))
        baseline_truthful = float(baseline.get("truthful_terminal_rate", 0.0))
        safety_passed = int(metrics.get("forbidden_violations", -1)) == 0
        cost_available = metrics.get("cost_availability") == "available"
        task_improved = task_rate > baseline_task
        truthful_improved = truthful_rate > baseline_truthful
        runs = int(metrics.get("runs", 0))
        runs_complete = runs == expected_runs
        if not runs_complete:
            full_protocol = False
            blockers.append(f"{model}:run_count_incomplete")
        comparisons[model] = {
            "runs": runs,
            "expected_runs": expected_runs,
            "runs_complete": runs_complete,
            "task_success_rate": task_rate,
            "baseline_task_success_rate": baseline_task,
            "task_success_improved": task_improved,
            "truthful_terminal_rate": truthful_rate,
            "baseline_truthful_terminal_rate": baseline_truthful,
            "truthful_terminal_improved": truthful_improved,
            "forbidden_violations": int(metrics.get("forbidden_violations", -1)),
            "safety_passed": safety_passed,
            "cost_availability": metrics.get("cost_availability"),
            "cost_available": cost_available,
        }
        if not safety_passed:
            blockers.append(f"{model}:forbidden_violation")
        if not cost_available:
            blockers.append(f"{model}:cost_unavailable")
        if not task_improved:
            blockers.append(f"{model}:task_success_not_improved")
        if not truthful_improved:
            blockers.append(f"{model}:truthful_terminal_not_improved")

    if not models:
        blockers.append("stage2_models_missing")
        full_protocol = False
    if not full_protocol:
        blockers.insert(0, "stage2_full_protocol_incomplete")
    hard_failure = any(
        blocker.endswith(("forbidden_violation", "cost_unavailable"))
        for blocker in blockers
    )
    if not full_protocol:
        decision = "INCOMPLETE"
    elif hard_failure or blockers:
        decision = "NO_GO"
    else:
        decision = "PASS"
    return {
        "schema_version": 1,
        "gate": "v2.4-stage2-observable-behavior",
        "decision": decision,
        "full_protocol": full_protocol,
        "scenario_set_matches_baseline": scenario_set_matches,
        "minimum_repetitions": MIN_REPETITIONS,
        "expected_case_count": EXPECTED_CASE_COUNT,
        "models": comparisons,
        "blockers": blockers,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--registry", default="config/models.example.yaml")
    parser.add_argument(
        "--models",
        default="deepseek-v4-flash",
        help="逗号分隔的隔离 Agent 候选；默认只跑冻结门槛对应的 Flash",
    )
    parser.add_argument(
        "--planner-model",
        help="隔离 Planner 候选；默认使用 registry 的 complex_reasoning primary",
    )
    parser.add_argument("--repetitions", type=int, default=MIN_REPETITIONS)
    parser.add_argument(
        "--split",
        choices=("all", "public", "heldout"),
        default="all",
    )
    parser.add_argument("--case-ids", help="逗号分隔 case id")
    parser.add_argument("--baseline-report", type=Path, default=DEFAULT_BASELINE_REPORT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--checkpoint",
        type=Path,
        default=DEFAULT_CHECKPOINT,
        help="逐个 run 原子保存的恢复检查点",
    )
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument(
        "--confirm-paid-run",
        action="store_true",
        help="确认本次会调用真实模型并产生 API 成本",
    )
    args = parser.parse_args()
    if args.repetitions < 1:
        parser.error("--repetitions 必须大于 0")

    cases = load_cases(args.cases)
    if args.split != "all":
        cases = [case for case in cases if case["split"] == args.split]
    if args.case_ids:
        requested = {
            item.strip() for item in args.case_ids.split(",") if item.strip()
        }
        available = {str(case["id"]) for case in cases}
        missing = requested - available
        if missing:
            parser.error(f"case id 不存在或不属于当前 split: {sorted(missing)}")
        cases = [case for case in cases if case["id"] in requested]

    registry = ModelRegistry(args.registry)
    registry.load()
    model_names = [item.strip() for item in args.models.split(",") if item.strip()]
    if not model_names:
        parser.error("没有可评测 Agent 模型")
    planner_model = (
        args.planner_model
        or registry.resolve(Scenario.COMPLEX_REASONING).primary
    )
    for model_name in model_names:
        if not registry.get_model(model_name).supports_tools:
            parser.error(f"模型 {model_name} 不支持 tools，不能运行 Agent 评测")
    registry.get_model(planner_model)
    scenario_hash = hashlib.sha256(args.cases.read_bytes()).hexdigest()

    if args.validate_only:
        print(
            "Stage 2 evaluation preflight: "
            f"cases={len(cases)}, repetitions={args.repetitions}, "
            f"agent_models={','.join(model_names)}, planner_model={planner_model}, "
            f"paid_calls=disabled"
        )
        return 0
    if not args.confirm_paid_run:
        parser.error("真实阶段 2 评测会产生 API 成本；必须显式传入 --confirm-paid-run")
    if not args.baseline_report.is_file():
        parser.error(f"冻结基线报告不存在: {args.baseline_report}")

    checkpoint_protocol = {
        "schema_version": 1,
        "evaluation": "stage2_structured_agent_observable_behavior",
        "execution_mode": "stage2_structured_plan",
        "scenario_set_hash": scenario_hash,
        "repetitions": args.repetitions,
        "models": model_names,
        "planner_model": planner_model,
        "case_ids": [str(case["id"]) for case in cases],
    }
    existing_rows = _load_checkpoint_rows(
        args.checkpoint,
        expected_protocol=checkpoint_protocol,
    )

    def save_progress(rows: list[dict[str, Any]]) -> None:
        _write_checkpoint(
            args.checkpoint,
            protocol=checkpoint_protocol,
            rows=rows,
        )

    if existing_rows:
        print(
            f"Resuming Stage 2 evaluation: completed={len(existing_rows)}, "
            f"remaining={len(cases) * args.repetitions * len(model_names) - len(existing_rows)}"
        )
    report = asyncio.run(
        run_evaluation(
            cases=cases,
            registry=registry,
            model_names=model_names,
            repetitions=args.repetitions,
            enforce_plan=True,
            planner_model_name=planner_model,
            evaluation_name="stage2_structured_agent_observable_behavior",
            evaluation_label=(
                "v2.4 stage2 structured Planner/Executor/Replanner/"
                "deterministic Verifier"
            ),
            scenario_set_hash=scenario_hash,
            existing_rows=existing_rows,
            on_row_completed=save_progress,
        )
    )
    save_progress(cast(list[dict[str, Any]], report["rows"]))
    baseline_report = _load_object(args.baseline_report)
    comparison = evaluate_stage2_report(report, baseline_report)
    report["comparison"] = comparison
    report["completed_at"] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Stage 2 decision: {comparison['decision']}")
    for model, values in cast(
        dict[str, dict[str, Any]], comparison["models"]
    ).items():
        print(
            f"{model}: success={values['task_success_rate']:.1%} "
            f"(baseline={values['baseline_task_success_rate']:.1%}), "
            f"terminal={values['truthful_terminal_rate']:.1%} "
            f"(baseline={values['baseline_truthful_terminal_rate']:.1%}), "
            f"forbidden={values['forbidden_violations']}"
        )
    print(f"Report: {args.output}")
    return 0 if comparison["decision"] == "PASS" else 2


def _object(value: object, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return cast(dict[str, Any], value)


def _load_object(path: Path) -> dict[str, Any]:
    return _object(json.loads(path.read_text(encoding="utf-8")), str(path))


def _load_checkpoint_rows(
    path: Path,
    *,
    expected_protocol: dict[str, Any],
) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    checkpoint = _load_object(path)
    for field, expected in expected_protocol.items():
        if checkpoint.get(field) != expected:
            raise ValueError(
                f"Stage 2 检查点协议不匹配: {field}; "
                "请使用新的 --checkpoint 路径，禁止混用评测结果"
            )
    if checkpoint.get("privacy") != _PRIVACY:
        raise ValueError("Stage 2 检查点缺少隐私约束")
    raw_rows = checkpoint.get("rows")
    if not isinstance(raw_rows, list) or not all(
        isinstance(row, dict) for row in raw_rows
    ):
        raise ValueError("Stage 2 检查点 rows 必须是对象数组")
    return cast(list[dict[str, Any]], raw_rows)


def _write_checkpoint(
    path: Path,
    *,
    protocol: dict[str, Any],
    rows: list[dict[str, Any]],
) -> None:
    checkpoint = {
        **protocol,
        "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "completed_runs": len(rows),
        "rows": rows,
        "privacy": _PRIVACY,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(checkpoint, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
