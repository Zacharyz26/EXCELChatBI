"""v2.5 6C-4 anonymous hypothesis exploration consistency gate.

The gate runs the real deterministic screening, selected-hypothesis lifecycle
and result-driven follow-up policy.  Cases contain anonymous profile metadata
and synthetic structured tool outcomes only; reports omit columns, statements,
dataset references, evidence payloads and resource identifiers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.control.hypotheses import (  # noqa: E402
    screen_candidate_hypotheses,
)
from apps.orchestrator.control.hypothesis_followup import (  # noqa: E402
    decide_hypothesis_followup,
)
from apps.orchestrator.control.hypothesis_lifecycle import (  # noqa: E402
    bind_hypothesis_to_plan,
    finalize_hypothesis_execution,
    hypothesis_evidence_collected,
    hypothesis_invocation_failed,
)
from packages.session.models import Dataset, JsonObject  # noqa: E402
from packages.session.task_models import RunStatus, TaskStepRecord  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "hypothesis_exploration_eval_set.jsonl"
THRESHOLDS: dict[str, float | int] = {
    "screening_contract_rate": 1.0,
    "selection_binding_rate": 1.0,
    "evidence_outcome_rate": 1.0,
    "followup_decision_rate": 1.0,
    "boundary_convergence_rate": 1.0,
    "unverified_conclusion_violations": 0,
    "automatic_execution_violations": 0,
}

_KINDS = ("trend", "anomaly", "segment_comparison", "correlation")
_STATUSES = frozenset({"eligible", "needs_confirmation", "rejected"})
_EXECUTION_STATUSES = frozenset(
    {"supported", "not_supported", "inconclusive", "partial", "failed", "cancelled"}
)
_FOLLOWUP_DECISIONS = frozenset(
    {"stop", "degrade", "supplement_evidence", "propose_next"}
)
_CASE_KEYS = frozenset(
    {
        "id",
        "profile",
        "capabilities",
        "expected_statuses",
        "selected_kind",
        "evidence_result",
        "expected_execution_status",
        "expected_followup_decision",
        "expected_followup_kind",
        "limits",
        "failure",
        "cancelled",
    }
)
_PROFILE_KEYS = frozenset({"row_count", "columns"})
_COLUMN_KEYS = frozenset({"name", "dtype", "null_ratio", "distinct_count"})
_LIMIT_KEYS = frozenset(
    {"tool_attempts_used", "max_tool_calls", "replans_used", "max_replans"}
)
_FAILURE_KEYS = frozenset({"code", "retryable"})
_CAPABILITIES = frozenset(
    {"stats.trend", "stats.anomaly", "data.aggregate", "stats.correlation"}
)


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict) or set(raw) - _CASE_KEYS:
            raise ValueError(f"第 {line_number} 行字段集合无效")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"第 {line_number} 行缺少 case id")
        if case_id in seen:
            raise ValueError(f"case id 重复: {case_id}")
        seen.add(case_id)
        _validate_case(raw, case_id)
        cases.append(raw)
    if not cases:
        raise ValueError("候选假设评测用例为空")
    return cases


def _validate_case(case: dict[str, Any], case_id: str) -> None:
    required = _CASE_KEYS - {"limits", "failure", "cancelled"}
    if not required.issubset(case):
        raise ValueError(f"{case_id}: 缺少必填字段")
    profile = case.get("profile")
    if not isinstance(profile, dict) or set(profile) != _PROFILE_KEYS:
        raise ValueError(f"{case_id}: profile 契约无效")
    row_count = profile.get("row_count")
    columns = profile.get("columns")
    if (
        not isinstance(row_count, int)
        or isinstance(row_count, bool)
        or row_count < 2
        or not isinstance(columns, list)
        or not 2 <= len(columns) <= 16
    ):
        raise ValueError(f"{case_id}: profile 规模无效")
    names: set[str] = set()
    for column in columns:
        if not isinstance(column, dict) or set(column) != _COLUMN_KEYS:
            raise ValueError(f"{case_id}: column 契约无效")
        name = column.get("name")
        dtype = column.get("dtype")
        null_ratio = column.get("null_ratio")
        distinct_count = column.get("distinct_count")
        if not isinstance(name, str) or not name or name in names:
            raise ValueError(f"{case_id}: column 名称无效")
        names.add(name)
        if dtype not in {"int", "float", "str", "datetime", "bool"}:
            raise ValueError(f"{case_id}: dtype 无效")
        if (
            not isinstance(null_ratio, int | float)
            or isinstance(null_ratio, bool)
            or not 0 <= float(null_ratio) <= 1
            or not isinstance(distinct_count, int)
            or isinstance(distinct_count, bool)
            or not 0 <= distinct_count <= row_count
        ):
            raise ValueError(f"{case_id}: profile 统计无效")
    capabilities = case.get("capabilities")
    if (
        not isinstance(capabilities, list)
        or set(capabilities) - _CAPABILITIES
        or len(capabilities) != len(set(capabilities))
    ):
        raise ValueError(f"{case_id}: capability 集合无效")
    expected_statuses = case.get("expected_statuses")
    if (
        not isinstance(expected_statuses, dict)
        or set(expected_statuses) != set(_KINDS)
        or set(expected_statuses.values()) - _STATUSES
    ):
        raise ValueError(f"{case_id}: expected_statuses 无效")
    selected_kind = case.get("selected_kind")
    if selected_kind not in _KINDS or expected_statuses[selected_kind] != "eligible":
        raise ValueError(f"{case_id}: selected_kind 必须可执行")
    if case.get("expected_execution_status") not in _EXECUTION_STATUSES:
        raise ValueError(f"{case_id}: expected_execution_status 无效")
    decision = case.get("expected_followup_decision")
    if decision not in _FOLLOWUP_DECISIONS:
        raise ValueError(f"{case_id}: expected_followup_decision 无效")
    expected_kind = case.get("expected_followup_kind")
    if expected_kind is not None and expected_kind not in _KINDS:
        raise ValueError(f"{case_id}: expected_followup_kind 无效")
    if (decision in {"supplement_evidence", "propose_next"}) != (
        expected_kind is not None
    ):
        raise ValueError(f"{case_id}: 跟进动作与候选类型不一致")
    limits = case.get("limits")
    if limits is not None:
        if not isinstance(limits, dict) or set(limits) != _LIMIT_KEYS:
            raise ValueError(f"{case_id}: limits 无效")
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in limits.values()
        ):
            raise ValueError(f"{case_id}: limits 必须是非负整数")
    failure = case.get("failure")
    if failure is not None and (
        not isinstance(failure, dict)
        or set(failure) != _FAILURE_KEYS
        or not isinstance(failure.get("code"), str)
        or not failure.get("code")
        or not isinstance(failure.get("retryable"), bool)
    ):
        raise ValueError(f"{case_id}: failure 无效")
    if case.get("evidence_result") is None and failure is None and not case.get("cancelled"):
        raise ValueError(f"{case_id}: 无 Evidence 时必须声明失败或取消")
    if "cancelled" in case and not isinstance(case.get("cancelled"), bool):
        raise ValueError(f"{case_id}: cancelled 必须是布尔值")


def run_evaluation(cases: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [_evaluate_case(case) for case in cases]
    count = len(rows)
    metrics: dict[str, float | int] = {
        name: sum(bool(row[field]) for row in rows) / count
        for name, field in (
            ("screening_contract_rate", "screening_contract"),
            ("selection_binding_rate", "selection_binding"),
            ("evidence_outcome_rate", "evidence_outcome"),
            ("followup_decision_rate", "followup_decision"),
            ("boundary_convergence_rate", "boundary_convergence"),
        )
    }
    metrics["unverified_conclusion_violations"] = sum(
        int(row["unverified_conclusion_violations"]) for row in rows
    )
    metrics["automatic_execution_violations"] = sum(
        int(row["automatic_execution_violations"]) for row in rows
    )
    misses = {
        name: {"actual": metrics[name], "required": required}
        for name, required in THRESHOLDS.items()
        if metrics[name] != required
    }
    return {
        "evaluation": "v2.5_hypothesis_exploration",
        "case_set_sha256": _stable_hash(cases),
        "case_count": count,
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses and all(row["passed"] for row in rows),
        "misses": misses,
        "contains_column_names": False,
        "contains_statements": False,
        "contains_dataset_refs": False,
        "contains_evidence_payloads": False,
        "contains_resource_ids": False,
        "reads_raw_rows": False,
        "model_calls": 0,
        "tool_calls": 0,
    }


def _evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    case_id = str(case["id"])
    dataset_ref = hashlib.sha256(f"dataset:{case_id}".encode()).hexdigest()[:32]
    data_version_hash = hashlib.sha256(f"version:{case_id}".encode()).hexdigest()
    profile = cast(dict[str, Any], case["profile"])
    dataset = Dataset(
        ref=dataset_ref,
        project_id="anonymous-project",
        filename=f"{case_id}.parquet",
        profile={
            "row_count": profile["row_count"],
            "column_count": len(cast(list[object], profile["columns"])),
            "columns": profile["columns"],
        },
        parent_ref=None,
        transform=None,
        created_at="2026-08-12T00:00:00Z",
    )
    allowed = set(cast(list[str], case["capabilities"]))
    catalog = [
        {"name": capability, "allowed": capability in allowed}
        for capability in _CAPABILITIES
    ]
    screening = screen_candidate_hypotheses(
        user_text="分析数据",
        datasets=[dataset],
        capability_catalog=catalog,
        data_version_hash=data_version_hash,
    )
    if screening is None:
        raise RuntimeError(f"{case_id}: 开放探索未触发")
    candidates = cast(list[JsonObject], screening["candidates"])
    by_kind = {str(candidate["kind"]): candidate for candidate in candidates}
    statuses = {kind: by_kind[kind]["status"] for kind in _KINDS}
    screening_contract = (
        statuses == case["expected_statuses"]
        and len(candidates) <= 4
        and screening["raw_rows_read"] is False
        and all(candidate["tested"] is False for candidate in candidates)
    )
    selected = by_kind[str(case["selected_kind"])]
    selection: JsonObject = {
        "schema": "chatbi-hypothesis-selection-v1",
        "schema_version": 1,
        "question_id": "analysis_goal",
        "hypothesis_id": selected["hypothesis_id"],
        "kind": selected["kind"],
        "statement": selected["statement"],
        "capability": selected["capability"],
        "expected_evidence": selected["expected_evidence"],
        "dataset_ref": dataset_ref,
        "plan_version": 1,
        "data_version_hash": data_version_hash,
        "run_state_version": 4,
        "selected_at": "2026-08-12T00:00:01Z",
        "tested": False,
    }
    step = TaskStepRecord(
        step_id=f"step-{case_id}",
        plan_id=f"plan-{case_id}",
        run_id=f"run-{case_id}",
        position=0,
        logical_id=f"verify-{case['selected_kind']}",
        status="pending",
        definition={
            "step_id": f"verify-{case['selected_kind']}",
            "purpose": "验证候选",
            "capability": selected["capability"],
            "dependencies": [],
            "expected_evidence": [selected["expected_evidence"]],
            "completion_conditions": ["受治理工具 Evidence 已持久化"],
            "fallback": [{"when": "失败", "action": "block"}],
        },
        started_at=None,
        completed_at=None,
    )
    execution = bind_hypothesis_to_plan(
        selection=selection,
        existing=None,
        plan_id=step.plan_id,
        plan_version=2,
        steps=[step],
        updated_at="2026-08-12T00:00:02Z",
    )
    if execution is None:
        raise RuntimeError(f"{case_id}: 候选未绑定执行链")
    selection_binding = (
        execution["hypothesis_id"] == selected["hypothesis_id"]
        and execution["capability"] == step.definition["capability"]
        and execution["persisted_step_id"] == step.step_id
        and execution["data_version_hash"] == screening["data_version_hash"]
    )

    failure = cast(dict[str, Any] | None, case.get("failure"))
    evidence_result = case.get("evidence_result")
    run_status: RunStatus = "completed"
    verification: JsonObject | None = {"verdict": "PASS", "checks": []}
    latest_observation: JsonObject | None = None
    if case.get("cancelled") is True:
        run_status = "cancelled"
        verification = None
    elif failure is not None:
        execution = hypothesis_invocation_failed(
            execution,
            persisted_step_id=step.step_id,
            invocation_id="anonymous-invocation",
            failure_code=str(failure["code"]),
            updated_at="2026-08-12T00:00:03Z",
        )
        run_status = "blocked"
        verification = None
        latest_observation = {
            "status": "error",
            "code": failure["code"],
            "retryable": failure["retryable"],
        }
    else:
        execution = hypothesis_evidence_collected(
            execution,
            persisted_step_id=step.step_id,
            invocation_id="anonymous-invocation",
            evidence_id="anonymous-evidence",
            ledger_sequence=1,
            result=evidence_result,
            updated_at="2026-08-12T00:00:03Z",
        )
    assert execution is not None
    prefinal_outcome = execution["outcome"]
    final = finalize_hypothesis_execution(
        execution,
        run_status=run_status,
        verification_payload=verification,
        verification_sequence=9 if verification is not None else None,
        terminal_reason=(str(failure["code"]) if failure is not None else None),
        updated_at="2026-08-12T00:00:04Z",
    )
    assert final is not None
    evidence_outcome = final["status"] == case["expected_execution_status"]
    unverified_violations = int(
        evidence_result is not None and prefinal_outcome != "untested"
    )
    limits = cast(dict[str, int], case.get("limits") or {})
    followup = decide_hypothesis_followup(
        screening=screening,
        execution=final,
        run_status=run_status,
        tool_attempts_used=limits.get("tool_attempts_used", 1),
        max_tool_calls=limits.get("max_tool_calls", 4),
        replans_used=limits.get("replans_used", 0),
        max_replans=limits.get("max_replans", 2),
        cancellation_root_status=(
            "cancel_requested" if run_status == "cancelled" else "completed"
        ),
        latest_observation=latest_observation,
        updated_at="2026-08-12T00:00:05Z",
    )
    if followup is None:
        raise RuntimeError(f"{case_id}: 终态未生成跟进投影")
    proposed = cast(JsonObject | None, followup["proposed_candidate"])
    proposed_kind = proposed.get("kind") if proposed is not None else None
    followup_decision = (
        followup["decision"] == case["expected_followup_decision"]
        and proposed_kind == case["expected_followup_kind"]
    )
    automatic_violations = int(followup["automatic_execution"] is not False)
    actionable = followup["decision"] in {"supplement_evidence", "propose_next"}
    boundary_convergence = (
        (not actionable and followup["requires_user_confirmation"] is False)
        or (
            actionable
            and followup["requires_user_confirmation"] is True
            and proposed is not None
            and followup["suggested_goal"] is not None
        )
    )
    checks = {
        "screening_contract": screening_contract,
        "selection_binding": selection_binding,
        "evidence_outcome": evidence_outcome,
        "followup_decision": followup_decision,
        "boundary_convergence": boundary_convergence,
    }
    return {
        "id": case_id,
        **checks,
        "unverified_conclusion_violations": unverified_violations,
        "automatic_execution_violations": automatic_violations,
        "candidate_count": len(candidates),
        "eligible_count": len(screening["eligible_candidate_ids"]),
        "execution_status": final["status"],
        "followup_action": followup["decision"],
        "proposed_kind": proposed_kind,
        "passed": (
            all(checks.values())
            and unverified_violations == 0
            and automatic_violations == 0
        ),
    }


def _stable_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _print_human(report: dict[str, Any]) -> None:
    metrics = cast(dict[str, float | int], report["metrics"])
    print(f"候选假设探索：{report['case_count']} 个匿名场景")
    for metric in THRESHOLDS:
        value = metrics[metric]
        print(f"- {metric}: {value:.1%}" if isinstance(value, float) else f"- {metric}: {value}")
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 6C-4 候选假设探索门禁")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--json-output")
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print(f"候选假设探索用例契约有效：{len(cases)} cases")
        return 0
    report = run_evaluation(cases)
    _print_human(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"JSON 报告：{output}")
    if args.enforce and not report["passed"]:
        print(f"候选假设探索门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("候选假设探索门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
