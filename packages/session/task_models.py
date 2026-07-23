"""Persisted Agent-run records for the v2.4 control plane."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
InvocationStatus = Literal["running", "succeeded", "failed", "unknown"]
ObservationSource = Literal["tool", "user", "policy", "system"]
ObservationStatus = Literal["ok", "error", "partial"]


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


@dataclass(frozen=True, slots=True)
class TaskEvent:
    event_id: str
    run_id: str
    sequence: int
    event_type: str
    payload: JsonObject
    occurred_at: str


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
