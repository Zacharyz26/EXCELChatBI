"""Strict 6C-2 lifecycle projection for one user-selected hypothesis.

The projection binds a selection to one immutable capability step and advances
only from durable invocation, Evidence Ledger and Verifier facts.  Raw tool
results are consumed once to derive a deterministic evidence signal; that
signal is not exposed as a final outcome until deterministic verification
passes.
"""

from __future__ import annotations

from typing import Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from packages.session.models import JsonObject
from packages.session.task_models import RunStatus, TaskStepRecord

HypothesisExecutionStatus = Literal[
    "planned",
    "running",
    "evidence_collected",
    "supported",
    "not_supported",
    "inconclusive",
    "partial",
    "failed",
    "cancelled",
]
HypothesisOutcome = Literal["untested", "supported", "not_supported", "inconclusive"]

HYPOTHESIS_EXECUTION_SCHEMA = "chatbi-hypothesis-execution-v1"
_FINAL_EXECUTION_STATUSES = frozenset(
    {"supported", "not_supported", "inconclusive", "failed", "cancelled"}
)
_HYPOTHESIS_KINDS = frozenset(
    {"trend", "anomaly", "segment_comparison", "correlation"}
)

_EXECUTION_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "hypothesis_id",
        "kind",
        "statement",
        "capability",
        "dataset_ref",
        "data_version_hash",
        "selection_plan_version",
        "execution_plan_id",
        "execution_plan_version",
        "logical_step_id",
        "persisted_step_id",
        "status",
        "tested",
        "evidence_outcome",
        "outcome",
        "invocation_ids",
        "failed_invocation_ids",
        "evidence_ids",
        "evidence_ledger_sequences",
        "verification",
        "last_failure_code",
        "updated_at",
    ],
    "properties": {
        "schema": {"const": HYPOTHESIS_EXECUTION_SCHEMA},
        "schema_version": {"const": 1},
        "hypothesis_id": {"type": "string", "pattern": "^hyp_[0-9a-f]{16}$"},
        "kind": {"enum": sorted(_HYPOTHESIS_KINDS)},
        "statement": {"type": "string", "minLength": 1, "maxLength": 300},
        "capability": {"type": "string", "minLength": 1, "maxLength": 100},
        "dataset_ref": {"type": "string", "minLength": 1, "maxLength": 256},
        "data_version_hash": {"type": "string", "minLength": 1, "maxLength": 128},
        "selection_plan_version": {"type": "integer", "minimum": 1},
        "execution_plan_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "execution_plan_version": {"type": "integer", "minimum": 1},
        "logical_step_id": {"type": "string", "minLength": 1, "maxLength": 100},
        "persisted_step_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "status": {
            "enum": [
                "planned",
                "running",
                "evidence_collected",
                "supported",
                "not_supported",
                "inconclusive",
                "partial",
                "failed",
                "cancelled",
            ]
        },
        "tested": {"type": "boolean"},
        "evidence_outcome": {
            "enum": ["untested", "supported", "not_supported", "inconclusive"]
        },
        "outcome": {
            "enum": ["untested", "supported", "not_supported", "inconclusive"]
        },
        "invocation_ids": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "failed_invocation_ids": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "evidence_ids": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 128},
        },
        "evidence_ledger_sequences": {
            "type": "array",
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 1},
        },
        "verification": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": ["verdict", "check_codes", "event_sequence"],
            "properties": {
                "verdict": {
                    "enum": ["PASS", "NEEDS_ACTION", "WAITING_USER", "BLOCKED", "FAILED"]
                },
                "check_codes": {
                    "type": "array",
                    "maxItems": 64,
                    "items": {"type": "string", "minLength": 1, "maxLength": 100},
                },
                "event_sequence": {"type": "integer", "minimum": 1},
            },
        },
        "last_failure_code": {"type": ["string", "null"], "maxLength": 200},
        "updated_at": {"type": "string", "minLength": 1, "maxLength": 64},
    },
}
_EXECUTION_VALIDATOR = Draft202012Validator(_EXECUTION_SCHEMA)


def bind_hypothesis_to_plan(
    *,
    selection: JsonObject,
    existing: JsonObject | None,
    plan_id: str,
    plan_version: int,
    steps: list[TaskStepRecord],
    updated_at: str,
) -> JsonObject | None:
    """Bind one selection to exactly one capability step in an executable plan."""
    selected = _validated_selection(selection)
    if existing is not None and existing.get("status") in _FINAL_EXECUTION_STATUSES:
        return validate_hypothesis_execution(existing)
    matching = [
        step
        for step in steps
        if step.definition.get("capability") == selected["capability"]
    ]
    if not matching:
        if not steps:
            return existing
        raise ValueError("选中候选假设的 capability 未进入执行计划")
    if len(matching) != 1:
        raise ValueError("选中候选假设必须绑定到唯一计划步骤")
    step = matching[0]
    base = dict(existing or {})
    execution: JsonObject = {
        "schema": HYPOTHESIS_EXECUTION_SCHEMA,
        "schema_version": 1,
        "hypothesis_id": selected["hypothesis_id"],
        "kind": selected["kind"],
        "statement": selected["statement"],
        "capability": selected["capability"],
        "dataset_ref": selected["dataset_ref"],
        "data_version_hash": selected["data_version_hash"],
        "selection_plan_version": selected["plan_version"],
        "execution_plan_id": plan_id,
        "execution_plan_version": plan_version,
        "logical_step_id": step.logical_id,
        "persisted_step_id": step.step_id,
        "status": _nonterminal_rebind_status(base),
        "tested": bool(base.get("evidence_ids")),
        "evidence_outcome": base.get("evidence_outcome", "untested"),
        "outcome": "untested",
        "invocation_ids": list(base.get("invocation_ids", [])),
        "failed_invocation_ids": list(base.get("failed_invocation_ids", [])),
        "evidence_ids": list(base.get("evidence_ids", [])),
        "evidence_ledger_sequences": list(base.get("evidence_ledger_sequences", [])),
        "verification": None,
        "last_failure_code": base.get("last_failure_code"),
        "updated_at": updated_at,
    }
    return validate_hypothesis_execution(execution)


def hypothesis_invocation_started(
    execution: JsonObject | None,
    *,
    persisted_step_id: str | None,
    invocation_id: str,
    updated_at: str,
) -> JsonObject | None:
    if execution is None or persisted_step_id != execution.get("persisted_step_id"):
        return execution
    current = validate_hypothesis_execution(execution)
    if current["status"] in _FINAL_EXECUTION_STATUSES:
        return current
    current["invocation_ids"] = _append_unique(current["invocation_ids"], invocation_id)
    current["status"] = "running"
    current["updated_at"] = updated_at
    return validate_hypothesis_execution(current)


def hypothesis_invocation_failed(
    execution: JsonObject | None,
    *,
    persisted_step_id: str | None,
    invocation_id: str | None,
    failure_code: str,
    updated_at: str,
) -> JsonObject | None:
    if execution is None or persisted_step_id != execution.get("persisted_step_id"):
        return execution
    current = validate_hypothesis_execution(execution)
    if current["status"] in _FINAL_EXECUTION_STATUSES:
        return current
    if invocation_id is not None:
        current["invocation_ids"] = _append_unique(current["invocation_ids"], invocation_id)
        current["failed_invocation_ids"] = _append_unique(
            current["failed_invocation_ids"], invocation_id
        )
    current["status"] = "partial"
    current["last_failure_code"] = failure_code[:200]
    current["updated_at"] = updated_at
    return validate_hypothesis_execution(current)


def hypothesis_evidence_collected(
    execution: JsonObject | None,
    *,
    persisted_step_id: str | None,
    invocation_id: str,
    evidence_id: str,
    ledger_sequence: int,
    result: object,
    updated_at: str,
) -> JsonObject | None:
    if execution is None or persisted_step_id != execution.get("persisted_step_id"):
        return execution
    current = validate_hypothesis_execution(execution)
    if current["status"] in _FINAL_EXECUTION_STATUSES:
        return current
    current["invocation_ids"] = _append_unique(current["invocation_ids"], invocation_id)
    current["evidence_ids"] = _append_unique(current["evidence_ids"], evidence_id)
    current["evidence_ledger_sequences"] = _append_unique(
        current["evidence_ledger_sequences"], ledger_sequence
    )
    current["status"] = "evidence_collected"
    current["tested"] = True
    current["evidence_outcome"] = _evidence_outcome(str(current["kind"]), result)
    current["outcome"] = "untested"
    current["last_failure_code"] = None
    current["updated_at"] = updated_at
    return validate_hypothesis_execution(current)


def finalize_hypothesis_execution(
    execution: JsonObject | None,
    *,
    run_status: RunStatus,
    verification_payload: JsonObject | None,
    verification_sequence: int | None,
    terminal_reason: str | None,
    updated_at: str,
) -> JsonObject | None:
    """Project a terminal TaskRun without upgrading unverified evidence."""
    if execution is None:
        return None
    current = validate_hypothesis_execution(execution)
    if run_status not in {"completed", "blocked", "failed", "cancelled"}:
        return current
    verified = verification_payload or {}
    verdict = verified.get("verdict")
    if isinstance(verdict, str) and verification_sequence is not None:
        raw_checks = verified.get("checks")
        checks = cast(list[JsonObject], raw_checks) if isinstance(raw_checks, list) else []
        current["verification"] = {
            "verdict": verdict,
            "check_codes": [
                str(item["code"])
                for item in checks
                if isinstance(item.get("code"), str) and item["code"]
            ],
            "event_sequence": verification_sequence,
        }
    current["updated_at"] = updated_at
    current["last_failure_code"] = terminal_reason
    if run_status == "cancelled":
        current["status"] = "cancelled"
        current["outcome"] = "inconclusive" if current["tested"] else "untested"
    elif run_status == "completed" and verdict == "PASS" and current["tested"]:
        evidence_outcome = cast(HypothesisOutcome, current["evidence_outcome"])
        current["outcome"] = evidence_outcome
        current["status"] = (
            evidence_outcome if evidence_outcome != "untested" else "inconclusive"
        )
        current["last_failure_code"] = None
    elif current["tested"]:
        current["status"] = "partial"
        current["outcome"] = "inconclusive"
    else:
        current["status"] = "failed"
        current["outcome"] = "untested"
    return validate_hypothesis_execution(current)


def validate_hypothesis_execution(value: JsonObject) -> JsonObject:
    normalized = dict(value)
    errors = sorted(
        _EXECUTION_VALIDATOR.iter_errors(normalized), key=lambda item: list(item.path)
    )
    if errors:
        error = best_match(errors)
        path = ".".join(str(part) for part in error.path) or "<root>"
        raise ValueError(f"候选假设执行契约校验失败 @ {path}: {error.message}")
    return normalized


def _validated_selection(selection: JsonObject) -> JsonObject:
    required = {
        "hypothesis_id": str,
        "kind": str,
        "statement": str,
        "capability": str,
        "dataset_ref": str,
        "data_version_hash": str,
        "plan_version": int,
    }
    if selection.get("schema") != "chatbi-hypothesis-selection-v1" or any(
        not isinstance(selection.get(key), expected) for key, expected in required.items()
    ):
        raise ValueError("选中候选假设格式非法")
    if selection.get("kind") not in _HYPOTHESIS_KINDS or selection.get("tested") is not False:
        raise ValueError("选中候选假设类型或 tested 状态非法")
    return selection


def _nonterminal_rebind_status(existing: JsonObject) -> HypothesisExecutionStatus:
    if existing.get("evidence_ids"):
        return "evidence_collected"
    if existing.get("status") == "partial":
        return "planned"
    if existing.get("invocation_ids"):
        return "running"
    return "planned"


def _evidence_outcome(kind: str, result: object) -> HypothesisOutcome:
    if not isinstance(result, dict):
        return "inconclusive"
    if kind == "trend":
        direction = result.get("direction")
        if direction in {"上升", "下降", "up", "down", "increasing", "decreasing"}:
            return "supported"
        if direction in {"平稳", "flat", "stable"}:
            return "not_supported"
        return "inconclusive"
    if kind == "anomaly":
        count = result.get("n_anomalies")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            return "supported" if count > 0 else "not_supported"
        return "inconclusive"
    if kind == "correlation":
        pairs = result.get("top_pairs")
        if not isinstance(pairs, list) or not pairs:
            return "inconclusive"
        significance = [
            item.get("significant") for item in pairs if isinstance(item, dict)
        ]
        if any(value is True for value in significance):
            return "supported"
        if significance and all(value is False for value in significance):
            return "not_supported"
        return "inconclusive"
    if kind == "segment_comparison" and isinstance(result.get("overall"), dict):
        significant = result["overall"].get("significant")
        if significant is True:
            return "supported"
        if significant is False:
            return "not_supported"
        return "inconclusive"
    rows = result.get("rows")
    if not isinstance(rows, list) or len(rows) < 2:
        return "inconclusive"
    values = [item.get("value") for item in rows if isinstance(item, dict)]
    if len(values) < 2 or any(isinstance(value, dict | list) for value in values):
        return "inconclusive"
    return "supported" if len({str(value) for value in values}) > 1 else "not_supported"


def _append_unique(values: object, value: object) -> list[object]:
    current = list(values) if isinstance(values, list) else []
    if value not in current:
        current.append(value)
    return current
