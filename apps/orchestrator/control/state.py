"""Compatibility exports for the shared orchestration lifecycle state."""

from packages.orchestration.state import (
    TERMINAL_STATUSES,
    AgentState,
    InvalidStateTransition,
    ensure_transition,
)

__all__ = [
    "TERMINAL_STATUSES",
    "AgentState",
    "InvalidStateTransition",
    "ensure_transition",
]
