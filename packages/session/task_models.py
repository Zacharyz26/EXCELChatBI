"""Persisted Agent-run records for the v2.4 control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

from packages.session.models import JsonObject

RunStatus = Literal[
    "planning",
    "waiting_user",
    "running",
    "verifying",
    "paused",
    "completed",
    "blocked",
    "failed",
    "cancelled",
]
AutonomyMode = Literal["assisted", "read_only", "autonomous"]
DEFAULT_AUTONOMY_MODE: AutonomyMode = "autonomous"
InvocationStatus = Literal["running", "succeeded", "failed", "unknown"]
ObservationSource = Literal["tool", "user", "policy", "system"]
ObservationStatus = Literal["ok", "error", "partial"]
StepStatus = Literal["pending", "running", "completed", "failed", "skipped", "blocked"]
ApprovalRiskLevel = Literal["high", "critical"]
ApprovalStatus = Literal["pending", "approved", "denied", "consumed", "revoked"]


@dataclass(frozen=True, slots=True)
class TaskRun:
    run_id: str
    project_id: str
    conversation_id: str
    user_message_id: str
    parent_run_id: str | None
    goal: str
    status: RunStatus
    state_version: int
    plan_version: int
    budget: JsonObject
    usage: JsonObject
    terminal_reason: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    @property
    def autonomy_mode(self) -> AutonomyMode:
        value = self.budget.get("autonomy_mode")
        if value in {"assisted", "read_only", "autonomous"}:
            return cast(AutonomyMode, value)
        return DEFAULT_AUTONOMY_MODE


@dataclass(frozen=True, slots=True)
class TaskEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: JsonObject
    occurred_at: str


@dataclass(frozen=True, slots=True)
class TaskPlanRecord:
    """一个 TaskRun 的不可变计划版本。"""

    plan_id: str
    run_id: str
    version: int
    reason: str | None
    plan: JsonObject
    created_at: str


@dataclass(frozen=True, slots=True)
class TaskStepRecord:
    """持久化计划中的一个步骤；``logical_id`` 仅在所属计划内唯一。"""

    step_id: str
    plan_id: str
    run_id: str
    position: int
    logical_id: str
    status: StepStatus
    definition: JsonObject
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    invocation_id: str
    run_id: str
    step_id: str | None
    tool_call_id: str
    tool_name: str
    idempotency_key: str
    args_hash: str
    args: JsonObject
    status: InvocationStatus
    result_hash: str | None
    error_text: str | None
    artifact_id: str | None
    started_at: str
    completed_at: str | None


@dataclass(frozen=True, slots=True)
class Observation:
    observation_id: str
    run_id: str
    step_id: str
    invocation_id: str | None
    source: ObservationSource
    status: ObservationStatus
    code: str
    summary: str
    retryable: bool
    payload_ref: str | None
    created_at: str

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": 1,
            "observation_id": self.observation_id,
            "run_id": self.run_id,
            "step_id": self.step_id,
            "invocation_id": self.invocation_id,
            "source": self.source,
            "status": self.status,
            "code": self.code,
            "summary": self.summary,
            "retryable": self.retryable,
            "payload_ref": self.payload_ref,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    run_id: str
    invocation_id: str
    artifact_id: str | None
    kind: str
    source: JsonObject
    result_hash: str
    summary: JsonObject
    created_at: str


@dataclass(frozen=True, slots=True)
class ClaimDraft:
    statement: str
    claim_kind: str
    value_refs: tuple[JsonObject, ...]
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    run_id: str
    statement: str
    claim_kind: str
    value_refs: tuple[JsonObject, ...]
    evidence_ids: tuple[str, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class Checkpoint:
    checkpoint_id: str
    run_id: str
    sequence: int
    state_version: int
    state: JsonObject
    reason: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """绑定到固定计划、步骤、工具契约和参数摘要的高风险授权记录。"""

    approval_id: str
    tenant_id: str
    project_id: str
    run_id: str
    plan_id: str
    plan_version: int
    task_step_id: str
    step_logical_id: str
    subject_user_id: str
    requested_by_user_id: str
    tool_name: str
    tool_schema_hash: str
    parameter_summary_hash: str
    risk_level: ApprovalRiskLevel
    status: ApprovalStatus
    version: int
    expires_at: str
    decision_reason: str | None
    decided_by_user_id: str | None
    requested_at: str
    updated_at: str
    decided_at: str | None
    consumed_at: str | None
    idempotency_key: str
    request_hash: str
    request_event_id: str
