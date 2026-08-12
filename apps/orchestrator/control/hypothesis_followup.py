"""Deterministic 6C-3 follow-up decisions for a tested hypothesis.

The policy consumes only durable screening, execution, budget and cancellation
facts.  It never executes a tool: a bounded follow-up is exposed as a proposal
that requires an explicit user-created analysis branch.
"""

from __future__ import annotations

from typing import Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from packages.session.models import JsonObject
from packages.session.task_models import RunStatus

HypothesisFollowupDecision = Literal[
    "stop",
    "degrade",
    "supplement_evidence",
    "propose_next",
]

HYPOTHESIS_FOLLOWUP_SCHEMA = "chatbi-hypothesis-followup-v1"
_TERMINAL_RUN_STATUSES = frozenset({"completed", "blocked", "failed", "cancelled"})
_TERMINAL_EXECUTION_STATUSES = frozenset(
    {"supported", "not_supported", "inconclusive", "partial", "failed", "cancelled"}
)

_FOLLOWUP_SCHEMA: JsonObject = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema",
        "schema_version",
        "hypothesis_id",
        "data_version_hash",
        "source_status",
        "source_outcome",
        "decision",
        "reason_codes",
        "automatic_execution",
        "requires_user_confirmation",
        "proposed_candidate",
        "suggested_goal",
        "limits",
        "updated_at",
    ],
    "properties": {
        "schema": {"const": HYPOTHESIS_FOLLOWUP_SCHEMA},
        "schema_version": {"const": 1},
        "hypothesis_id": {"type": "string", "pattern": "^hyp_[0-9a-f]{16}$"},
        "data_version_hash": {"type": "string", "minLength": 1, "maxLength": 128},
        "source_status": {"enum": sorted(_TERMINAL_EXECUTION_STATUSES)},
        "source_outcome": {
            "enum": ["untested", "supported", "not_supported", "inconclusive"]
        },
        "decision": {
            "enum": ["stop", "degrade", "supplement_evidence", "propose_next"]
        },
        "reason_codes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        "automatic_execution": {"const": False},
        "requires_user_confirmation": {"type": "boolean"},
        "proposed_candidate": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "required": [
                "hypothesis_id",
                "kind",
                "statement",
                "capability",
                "expected_evidence",
                "priority",
            ],
            "properties": {
                "hypothesis_id": {
                    "type": "string",
                    "pattern": "^hyp_[0-9a-f]{16}$",
                },
                "kind": {
                    "enum": ["trend", "anomaly", "segment_comparison", "correlation"]
                },
                "statement": {"type": "string", "minLength": 1, "maxLength": 300},
                "capability": {"type": "string", "minLength": 1, "maxLength": 100},
                "expected_evidence": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 300,
                },
                "priority": {"type": "integer", "minimum": 1, "maximum": 4},
            },
        },
        "suggested_goal": {"type": ["string", "null"], "maxLength": 1200},
        "limits": {
            "type": "object",
            "additionalProperties": False,
            "required": [
                "tool_attempts_used",
                "max_tool_calls",
                "tool_calls_remaining",
                "replans_used",
                "max_replans",
                "replans_remaining",
                "cancellation_root_status",
            ],
            "properties": {
                "tool_attempts_used": {"type": "integer", "minimum": 0},
                "max_tool_calls": {"type": "integer", "minimum": 0},
                "tool_calls_remaining": {"type": "integer", "minimum": 0},
                "replans_used": {"type": "integer", "minimum": 0},
                "max_replans": {"type": "integer", "minimum": 0},
                "replans_remaining": {"type": "integer", "minimum": 0},
                "cancellation_root_status": {
                    "enum": ["active", "completed", "cancel_requested", "missing"]
                },
            },
        },
        "updated_at": {"type": "string", "minLength": 1, "maxLength": 64},
    },
}
_FOLLOWUP_VALIDATOR = Draft202012Validator(_FOLLOWUP_SCHEMA)


def decide_hypothesis_followup(
    *,
    screening: JsonObject | None,
    execution: JsonObject,
    run_status: RunStatus,
    tool_attempts_used: int,
    max_tool_calls: int,
    replans_used: int,
    max_replans: int,
    cancellation_root_status: str | None,
    latest_observation: JsonObject | None,
    updated_at: str,
) -> JsonObject | None:
    """Choose one convergent follow-up from durable terminal facts.

    ``None`` means the hypothesis is not terminal yet.  Every actionable result
    remains a proposal: this function cannot schedule or invoke a capability.
    """
    source_status = execution.get("status")
    if run_status not in _TERMINAL_RUN_STATUSES or source_status not in (
        _TERMINAL_EXECUTION_STATUSES
    ):
        return None

    hypothesis_id = _required_string(execution, "hypothesis_id")
    data_version_hash = _required_string(execution, "data_version_hash")
    source_outcome = _required_string(execution, "outcome")
    attempts = _nonnegative(tool_attempts_used)
    tool_limit = _nonnegative(max_tool_calls)
    replan_count = _nonnegative(replans_used)
    replan_limit = _nonnegative(max_replans)
    tool_remaining = max(0, tool_limit - attempts)
    replan_remaining = max(0, replan_limit - replan_count)
    root_status = (
        cancellation_root_status
        if cancellation_root_status in {"active", "completed", "cancel_requested"}
        else "missing"
    )
    limits: JsonObject = {
        "tool_attempts_used": attempts,
        "max_tool_calls": tool_limit,
        "tool_calls_remaining": tool_remaining,
        "replans_used": replan_count,
        "max_replans": replan_limit,
        "replans_remaining": replan_remaining,
        "cancellation_root_status": root_status,
    }

    decision: HypothesisFollowupDecision
    reasons: list[str]
    candidate: JsonObject | None = None
    suggested_goal: str | None = None

    if run_status == "cancelled" or source_status == "cancelled" or root_status == (
        "cancel_requested"
    ):
        decision, reasons = "stop", ["cancellation_requested"]
    elif root_status in {"active", "missing"}:
        decision, reasons = "stop", ["cancellation_boundary_unsettled"]
    elif source_status == "supported":
        decision, reasons = "stop", ["hypothesis_supported", "post_hoc_expansion_blocked"]
    elif source_status == "not_supported":
        next_candidate, candidate_reason = _next_candidate(
            screening=screening,
            execution=execution,
        )
        if tool_remaining == 0:
            decision, reasons = "stop", ["tool_budget_exhausted"]
        elif replan_remaining == 0:
            decision, reasons = "stop", ["replan_budget_exhausted"]
        elif next_candidate is None:
            decision, reasons = "stop", [candidate_reason]
        else:
            candidate = next_candidate
            decision, reasons = "propose_next", [
                "hypothesis_not_supported",
                "next_eligible_candidate",
            ]
            suggested_goal = _next_candidate_goal(candidate, execution)
    elif _retryable_observation(latest_observation):
        if tool_remaining == 0:
            decision, reasons = "degrade", [
                "evidence_incomplete",
                "tool_budget_exhausted",
            ]
        elif replan_remaining == 0:
            decision, reasons = "degrade", [
                "evidence_incomplete",
                "replan_budget_exhausted",
            ]
        else:
            decision, reasons = "supplement_evidence", [
                "retryable_observation",
                "bounded_retry_available",
            ]
            candidate = _execution_candidate(execution)
            suggested_goal = _supplement_goal(execution)
    else:
        reasons = ["evidence_inconclusive"]
        failure_code = execution.get("last_failure_code")
        if isinstance(failure_code, str) and failure_code:
            reasons.append("non_retryable_failure")
        decision = "degrade"

    followup: JsonObject = {
        "schema": HYPOTHESIS_FOLLOWUP_SCHEMA,
        "schema_version": 1,
        "hypothesis_id": hypothesis_id,
        "data_version_hash": data_version_hash,
        "source_status": source_status,
        "source_outcome": source_outcome,
        "decision": decision,
        "reason_codes": reasons,
        "automatic_execution": False,
        "requires_user_confirmation": decision in {
            "supplement_evidence",
            "propose_next",
        },
        "proposed_candidate": candidate,
        "suggested_goal": suggested_goal,
        "limits": limits,
        "updated_at": updated_at,
    }
    return validate_hypothesis_followup(followup)


def validate_hypothesis_followup(value: JsonObject) -> JsonObject:
    normalized = dict(value)
    errors = sorted(
        _FOLLOWUP_VALIDATOR.iter_errors(normalized), key=lambda item: list(item.path)
    )
    if errors:
        error = best_match(errors)
        path = ".".join(str(part) for part in error.path) or "<root>"
        raise ValueError(f"候选假设跟进契约校验失败 @ {path}: {error.message}")
    _validate_cross_fields(normalized)
    return normalized


def _next_candidate(
    *,
    screening: JsonObject | None,
    execution: JsonObject,
) -> tuple[JsonObject | None, str]:
    if screening is None:
        return None, "screening_unavailable"
    if (
        screening.get("schema") != "chatbi-hypothesis-screening-v1"
        or screening.get("data_version_hash") != execution.get("data_version_hash")
        or screening.get("dataset_ref") != execution.get("dataset_ref")
    ):
        return None, "screening_context_mismatch"
    raw_candidates = screening.get("candidates")
    candidates = (
        [cast(JsonObject, item) for item in raw_candidates if isinstance(item, dict)]
        if isinstance(raw_candidates, list)
        else []
    )
    eligible = [
        item
        for item in candidates
        if item.get("status") == "eligible"
        and item.get("tested") is False
        and item.get("hypothesis_id") != execution.get("hypothesis_id")
    ]
    eligible.sort(key=lambda item: (_priority(item), str(item.get("hypothesis_id", ""))))
    if not eligible:
        return None, "candidate_set_exhausted"
    return _screened_candidate(eligible[0]), "next_eligible_candidate"


def _screened_candidate(candidate: JsonObject) -> JsonObject:
    return {
        "hypothesis_id": _required_string(candidate, "hypothesis_id"),
        "kind": _required_string(candidate, "kind"),
        "statement": _required_string(candidate, "statement"),
        "capability": _required_string(candidate, "capability"),
        "expected_evidence": _required_string(candidate, "expected_evidence"),
        "priority": _priority(candidate),
    }


def _execution_candidate(execution: JsonObject) -> JsonObject:
    return {
        "hypothesis_id": _required_string(execution, "hypothesis_id"),
        "kind": _required_string(execution, "kind"),
        "statement": _required_string(execution, "statement"),
        "capability": _required_string(execution, "capability"),
        "expected_evidence": "补齐原候选所需的受治理工具 Evidence",
        "priority": 1,
    }


def _next_candidate_goal(candidate: JsonObject, execution: JsonObject) -> str:
    return (
        f"验证有界候选：{candidate['statement']} "
        f"仅使用 capability {candidate['capability']}，绑定数据集 "
        f"{execution['dataset_ref']} 和数据版本 {execution['data_version_hash']}；"
        "取得 Evidence 并经 Verifier PASS 后才能形成结论，不得自动扩展其他候选。"
    )


def _supplement_goal(execution: JsonObject) -> str:
    return (
        f"为有界候选补充一次 Evidence：{execution['statement']} "
        f"仅重试 capability {execution['capability']}，绑定数据集 "
        f"{execution['dataset_ref']} 和数据版本 {execution['data_version_hash']}；"
        "若仍失败或结果未知则停止，不得自动重试或扩展目标。"
    )


def _retryable_observation(observation: JsonObject | None) -> bool:
    return (
        observation is not None
        and observation.get("status") in {"error", "partial"}
        and observation.get("retryable") is True
    )


def _priority(candidate: JsonObject) -> int:
    value = candidate.get("priority")
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 4:
        raise ValueError("候选假设 priority 非法")
    return value


def _required_string(value: JsonObject, key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"候选假设跟进缺少 {key}")
    return raw


def _nonnegative(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        return 0
    return max(0, value)


def _validate_cross_fields(value: JsonObject) -> None:
    actionable = value["decision"] in {"supplement_evidence", "propose_next"}
    if value["requires_user_confirmation"] is not actionable:
        raise ValueError("候选假设跟进的人工确认标记与决策不一致")
    if actionable != (value["proposed_candidate"] is not None):
        raise ValueError("候选假设跟进的候选与决策不一致")
    if actionable != (value["suggested_goal"] is not None):
        raise ValueError("候选假设跟进的分支目标与决策不一致")
    candidate = value["proposed_candidate"]
    if value["decision"] == "supplement_evidence" and candidate[
        "hypothesis_id"
    ] != value["hypothesis_id"]:
        raise ValueError("补证候选必须与原假设一致")
    if value["decision"] == "propose_next" and candidate["hypothesis_id"] == value[
        "hypothesis_id"
    ]:
        raise ValueError("下一候选不能重复原假设")
    limits = value["limits"]
    if limits["tool_calls_remaining"] != max(
        0,
        limits["max_tool_calls"] - limits["tool_attempts_used"],
    ):
        raise ValueError("候选假设跟进的工具预算余量不一致")
    if limits["replans_remaining"] != max(
        0,
        limits["max_replans"] - limits["replans_used"],
    ):
        raise ValueError("候选假设跟进的重规划余量不一致")
