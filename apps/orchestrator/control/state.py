"""Minimal AgentState and guarded lifecycle transitions."""

from __future__ import annotations

from dataclasses import dataclass

from packages.session.models import JsonObject
from packages.session.task_models import RunStatus, TaskRun

TERMINAL_STATUSES: frozenset[RunStatus] = frozenset(
    {"completed", "blocked", "failed", "cancelled"}
)

_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    "planning": frozenset({"running", "waiting_user", "failed", "cancelled"}),
    "waiting_user": frozenset({"running", "cancelled", "failed"}),
    "running": frozenset({"verifying", "waiting_user", "paused", "blocked", "failed", "cancelled"}),
    "verifying": frozenset(
        {"running", "completed", "waiting_user", "blocked", "failed", "cancelled"}
    ),
    "paused": frozenset({"running", "cancelled", "failed"}),
    "completed": frozenset(),
    "blocked": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
}


class InvalidStateTransition(RuntimeError):
    """Raised when code attempts to bypass the Agent lifecycle."""


@dataclass(frozen=True, slots=True)
class AgentState:
    run_id: str
    goal: str
    status: RunStatus
    state_version: int
    plan_version: int
    budget: JsonObject
    usage: JsonObject
    terminal_reason: str | None

    @classmethod
    def from_run(cls, run: TaskRun) -> AgentState:
        return cls(
            run_id=run.run_id,
            goal=run.goal,
            status=run.status,
            state_version=run.state_version,
            plan_version=run.plan_version,
            budget=run.budget,
            usage=run.usage,
            terminal_reason=run.terminal_reason,
        )

    def to_dict(self) -> JsonObject:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "state_version": self.state_version,
            "plan_version": self.plan_version,
            "budget": self.budget,
            "usage": self.usage,
            "terminal_reason": self.terminal_reason,
        }


def ensure_transition(current: RunStatus, target: RunStatus) -> None:
    if target not in _TRANSITIONS[current]:
        raise InvalidStateTransition(f"不允许 Agent 状态从 {current} 转为 {target}")
