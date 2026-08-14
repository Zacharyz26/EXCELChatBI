"""Deterministic collaboration projection for governed multi-dataset joins."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, cast

from packages.session.models import JsonObject
from packages.session.task_models import (
    ApprovalRecord,
    EvidenceRecord,
    TaskEvent,
    ToolInvocation,
)
from packages.session.task_store import invocation_arguments_hash

JOIN_CONTEXT_SCHEMA = "chatbi-join-evidence-context-v1"
JOIN_COLLABORATION_SCHEMA = "chatbi-join-collaboration-v1"
_JOIN_ARGUMENT_KEYS = (
    "left_dataset_ref",
    "right_dataset_ref",
    "left_key",
    "right_key",
    "join_type",
)
_PREFLIGHT_STATUSES = {"ready", "requires_confirmation", "blocked"}


@dataclass(frozen=True, slots=True)
class JoinExecutionGuard:
    """Host-side result for the exact preflight and data-version dependency."""

    allowed: bool
    code: str
    message: str
    preflight: JsonObject | None


def build_join_evidence_context(
    *,
    tool_name: str,
    arguments: Mapping[str, object],
    result: object,
    data_version_hash: str,
) -> JsonObject | None:
    """Persist a bounded, row-free Join projection beside canonical Evidence."""
    if tool_name not in {"join_preflight", "join_datasets"} or not isinstance(result, dict):
        return None
    clean_arguments = _join_arguments(arguments)
    if clean_arguments is None or not data_version_hash:
        return None
    phase: Literal["preflight", "execute"] = (
        "preflight" if tool_name == "join_preflight" else "execute"
    )
    raw_status = result.get("status" if phase == "preflight" else "preflight_status")
    status = raw_status if raw_status in _PREFLIGHT_STATUSES else "blocked"
    preflight: JsonObject = {
        "status": cast(str, status),
        "relationship": _text(result.get("relationship")),
        "estimated_output_rows": _integer(result.get("estimated_output_rows")),
        "expansion_ratio": _number(result.get("expansion_ratio")),
        "matching_key_count": _integer(result.get("matching_key_count")),
        "matched_left_rows": _integer(result.get("matched_left_rows")),
        "matched_right_rows": _integer(result.get("matched_right_rows")),
        "requires_confirmation": bool(result.get("requires_confirmation", False)),
        "risks": _safe_risks(result.get("risks")),
        "left": _safe_side(result.get("left")),
        "right": _safe_side(result.get("right")),
    }
    output: JsonObject | None = None
    if phase == "execute":
        parent_refs = result.get("parent_refs")
        output = {
            "dataset_ref": _text(result.get("dataset_ref")),
            "parent_refs": (
                [item for item in parent_refs if isinstance(item, str) and item]
                if isinstance(parent_refs, list)
                else []
            ),
            "rows": _integer(result.get("rows")),
        }
    return {
        "schema": JOIN_CONTEXT_SCHEMA,
        "schema_version": 1,
        "phase": phase,
        "data_version_hash": data_version_hash,
        "parameter_summary_hash": invocation_arguments_hash(clean_arguments),
        "arguments": clean_arguments,
        "preflight": preflight,
        "output": output,
        "raw_rows_returned": False,
    }


def evaluate_join_execution_guard(
    *,
    arguments: Mapping[str, object],
    invocations: Sequence[ToolInvocation],
    evidence: Sequence[EvidenceRecord],
    current_data_version_hash: str,
) -> JoinExecutionGuard:
    """Require exact successful preflight Evidence from the current data version."""
    clean_arguments = _join_arguments(arguments)
    if clean_arguments is None:
        return JoinExecutionGuard(
            allowed=False,
            code="join_preflight_evidence_required",
            message="Join 执行参数不完整，必须重新完成受治理预检。",
            preflight=None,
        )
    context = _matching_preflight(
        parameter_hash=invocation_arguments_hash(clean_arguments),
        invocations=invocations,
        evidence=evidence,
    )
    if context is None:
        return JoinExecutionGuard(
            allowed=False,
            code="join_preflight_evidence_required",
            message="Join 执行缺少与完整参数一致的成功预检 Evidence，必须重新预检。",
            preflight=None,
        )
    preflight = _object(context.get("preflight"))
    status = preflight.get("status")
    if status == "blocked":
        codes = [
            str(item.get("code"))
            for item in _object_list(preflight.get("risks"))
            if item.get("code")
        ]
        suffix = f"（{', '.join(codes)}）" if codes else ""
        return JoinExecutionGuard(
            allowed=False,
            code="join_preflight_blocked",
            message=f"Join 预检已阻塞{suffix}，当前任务不会执行写入。",
            preflight=context,
        )
    if context.get("data_version_hash") != current_data_version_hash:
        return JoinExecutionGuard(
            allowed=False,
            code="join_data_version_drift",
            message="Join 预检绑定的数据版本已漂移，授权和执行均已停止；请创建新任务重新预检。",
            preflight=context,
        )
    if status not in {"ready", "requires_confirmation"}:
        return JoinExecutionGuard(
            allowed=False,
            code="join_preflight_evidence_required",
            message="Join 预检状态不可执行，必须重新完成受治理预检。",
            preflight=context,
        )
    return JoinExecutionGuard(
        allowed=True,
        code="join_preflight_verified",
        message="Join 预检 Evidence 与当前数据版本完全匹配。",
        preflight=context,
    )


def build_join_collaboration_projection(
    *,
    invocations: Sequence[ToolInvocation],
    evidence: Sequence[EvidenceRecord],
    approvals: Sequence[ApprovalRecord],
    step_events: Sequence[TaskEvent],
    current_data_version_hash: str,
    dataset_parents: Mapping[str, Sequence[str]],
) -> JsonObject | None:
    """Rebuild the browser collaboration state exclusively from durable truth."""
    contexts = _join_contexts(invocations=invocations, evidence=evidence)
    preflight_items = [item for item in contexts if item[1].get("phase") == "preflight"]
    if not preflight_items:
        return None
    preflight_evidence, context = preflight_items[-1]
    arguments = _object(context.get("arguments"))
    parameter_hash = _text(context.get("parameter_summary_hash"))
    if not parameter_hash:
        return None
    preflight = _object(context.get("preflight"))
    matching_approvals = [
        item
        for item in approvals
        if item.tool_name == "join_datasets"
        and item.parameter_summary_hash == parameter_hash
    ]
    approval = matching_approvals[-1] if matching_approvals else None
    approval_expired = approval is not None and _approval_expired(approval)
    matching_executions = [
        item
        for item in invocations
        if item.tool_name == "join_datasets" and item.args_hash == parameter_hash
    ]
    execution = matching_executions[-1] if matching_executions else None
    execute_contexts = [
        item
        for item in contexts
        if item[1].get("phase") == "execute"
        and item[1].get("parameter_summary_hash") == parameter_hash
    ]
    execute_evidence, execute_context = (
        execute_contexts[-1] if execute_contexts else (None, None)
    )
    output = _object(execute_context.get("output")) if execute_context is not None else {}
    parent_refs = [
        item
        for item in output.get("parent_refs", [])
        if isinstance(item, str) and item
    ] if isinstance(output.get("parent_refs"), list) else []
    output_ref = _text(output.get("dataset_ref"))
    actual_parent_refs = list(dataset_parents.get(output_ref, ())) if output_ref else []
    expected_parent_refs = [
        _text(arguments.get("left_dataset_ref")),
        _text(arguments.get("right_dataset_ref")),
    ]
    preflight_matches_current = (
        context.get("data_version_hash") == current_data_version_hash
    )
    execution_matches_preflight = bool(
        execute_context is not None
        and execute_context.get("data_version_hash") == context.get("data_version_hash")
    )
    data_version_matches = (
        execution_matches_preflight if output_ref else preflight_matches_current
    )
    failure = _join_failure(execution, step_events)
    preflight_status = _text(preflight.get("status")) or "blocked"

    status: str
    if execute_context is not None and output_ref:
        status = "completed"
    elif failure is not None:
        status = (
            "version_drift"
            if failure.get("code") == "join_data_version_drift"
            else "failed"
        )
    elif not preflight_matches_current:
        status = "version_drift"
        failure = {
            "code": "join_data_version_drift",
            "message": "预检绑定版本与当前 TaskRun 数据版本不一致，执行已停止。",
            "retryable": False,
        }
    elif preflight_status == "blocked":
        status = "blocked"
    elif approval is None:
        status = "preflight_ready"
    elif approval.status == "pending" and not approval_expired:
        status = "awaiting_approval"
    elif approval.status == "pending":
        status = "preflight_ready"
    elif approval.status == "approved":
        status = "approved"
    elif approval.status == "consumed":
        status = "executing"
    elif approval.status in {"denied", "revoked"}:
        status = "blocked"
    else:
        status = "preflight_ready"

    left = _input_projection("left", arguments, preflight)
    right = _input_projection("right", arguments, preflight)
    approval_payload: JsonObject | None = None
    if approval is not None:
        approval_payload = {
            "approval_id": approval.approval_id,
            "status": approval.status,
            "plan_version": approval.plan_version,
            "step_id": approval.step_logical_id,
            "expires_at": approval.expires_at,
            "decision_reason": approval.decision_reason,
            "expired": approval_expired,
        }
    output_payload: JsonObject | None = None
    if output_ref:
        output_payload = {
            "dataset_ref": output_ref,
            "rows": _integer(output.get("rows")),
            "parent_refs": parent_refs,
            "parents": [
                {
                    "dataset_ref": parent_ref,
                    "ordinal": ordinal,
                    "role": "left" if ordinal == 0 else "right",
                }
                for ordinal, parent_ref in enumerate(parent_refs)
            ],
            "lineage_complete": (
                parent_refs == expected_parent_refs
                and actual_parent_refs == expected_parent_refs
            ),
        }
    updated_candidates = [preflight_evidence.created_at]
    if approval is not None:
        updated_candidates.append(approval.updated_at)
    if execution is not None:
        updated_candidates.append(execution.completed_at or execution.started_at)
    if execute_evidence is not None:
        updated_candidates.append(execute_evidence.created_at)
    return {
        "schema": JOIN_COLLABORATION_SCHEMA,
        "schema_version": 1,
        "status": status,
        "join_type": _text(arguments.get("join_type")),
        "relationship": _text(preflight.get("relationship")),
        "left": left,
        "right": right,
        "estimated_output_rows": _integer(preflight.get("estimated_output_rows")),
        "expansion_ratio": _number(preflight.get("expansion_ratio")),
        "matching_key_count": _integer(preflight.get("matching_key_count")),
        "risks": _object_list(preflight.get("risks")),
        "requires_confirmation": bool(preflight.get("requires_confirmation", False)),
        "preflight_status": preflight_status,
        "preflight_evidence_id": preflight_evidence.evidence_id,
        "data_version_hash": _text(context.get("data_version_hash")),
        "current_data_version_hash": current_data_version_hash,
        "data_version_matches": data_version_matches,
        "approval": approval_payload,
        "output": output_payload,
        "failure": failure,
        "updated_at": max(updated_candidates),
        "raw_rows_returned": False,
    }


def _matching_preflight(
    *,
    parameter_hash: str,
    invocations: Sequence[ToolInvocation],
    evidence: Sequence[EvidenceRecord],
) -> JsonObject | None:
    matches = [
        context
        for _record, context in _join_contexts(invocations=invocations, evidence=evidence)
        if context.get("phase") == "preflight"
        and context.get("parameter_summary_hash") == parameter_hash
    ]
    return matches[-1] if matches else None


def _join_contexts(
    *,
    invocations: Sequence[ToolInvocation],
    evidence: Sequence[EvidenceRecord],
) -> list[tuple[EvidenceRecord, JsonObject]]:
    successful = {
        item.invocation_id: item
        for item in invocations
        if item.status == "succeeded"
        and item.tool_name in {"join_preflight", "join_datasets"}
    }
    contexts: list[tuple[EvidenceRecord, JsonObject]] = []
    for record in evidence:
        invocation = successful.get(record.invocation_id)
        raw = record.source.get("join")
        if invocation is None or not isinstance(raw, dict):
            continue
        context = cast(JsonObject, raw)
        if (
            context.get("schema") != JOIN_CONTEXT_SCHEMA
            or context.get("parameter_summary_hash") != invocation.args_hash
        ):
            continue
        contexts.append((record, context))
    return contexts


def _join_failure(
    invocation: ToolInvocation | None,
    step_events: Sequence[TaskEvent],
) -> JsonObject | None:
    if invocation is None or invocation.status not in {"failed", "unknown"}:
        return None
    code = "join_execution_failed"
    retryable = False
    message = invocation.error_text or "Join 执行失败，未登记衍生数据集。"
    for event in reversed(step_events):
        if event.payload.get("invocation_id") != invocation.invocation_id:
            continue
        observation = _object(event.payload.get("observation"))
        code = _text(observation.get("code")) or code
        retryable = bool(observation.get("retryable", False))
        message = _text(observation.get("summary")) or message
        break
    return {"code": code, "message": message, "retryable": retryable}


def _input_projection(side: str, arguments: JsonObject, preflight: JsonObject) -> JsonObject:
    stats = _object(preflight.get(side))
    return {
        "dataset_ref": _text(arguments.get(f"{side}_dataset_ref")),
        "key": _text(arguments.get(f"{side}_key")),
        "row_count": _integer(stats.get("row_count")),
        "null_count": _integer(stats.get("null_count")),
        "distinct_count": _integer(stats.get("distinct_count")),
    }


def _join_arguments(arguments: Mapping[str, object]) -> JsonObject | None:
    values: JsonObject = {}
    for key in _JOIN_ARGUMENT_KEYS:
        value = arguments.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
        values[key] = value.strip()
    return values


def _safe_side(value: object) -> JsonObject:
    source = _object(value)
    return {
        "row_count": _integer(source.get("row_count")),
        "null_count": _integer(source.get("null_count")),
        "distinct_count": _integer(source.get("distinct_count")),
    }


def _safe_risks(value: object) -> list[JsonObject]:
    return [
        {
            "code": _text(item.get("code"))[:100],
            "severity": _text(item.get("severity"))[:32],
            "message": _text(item.get("message"))[:500],
        }
        for item in _object_list(value)[:16]
    ]


def _object(value: object) -> JsonObject:
    return cast(JsonObject, value) if isinstance(value, dict) else {}


def _object_list(value: object) -> list[JsonObject]:
    if not isinstance(value, list):
        return []
    return [cast(JsonObject, item) for item in value if isinstance(item, dict)]


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _number(value: object) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _approval_expired(approval: ApprovalRecord) -> bool:
    normalized = (
        approval.expires_at[:-1] + "+00:00"
        if approval.expires_at.endswith("Z")
        else approval.expires_at
    )
    try:
        expires_at = datetime.fromisoformat(normalized)
    except ValueError:
        return True
    return (
        expires_at.tzinfo is None
        or expires_at.astimezone(UTC) <= datetime.now(UTC)
    )
