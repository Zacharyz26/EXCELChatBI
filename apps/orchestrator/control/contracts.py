"""Compatibility exports for the shared orchestration contract package."""

from packages.orchestration.contracts import (
    CriterionKind,
    SuccessCriterion,
    TaskContract,
    build_minimal_contract,
)

__all__ = [
    "CriterionKind",
    "SuccessCriterion",
    "TaskContract",
    "build_minimal_contract",
]
