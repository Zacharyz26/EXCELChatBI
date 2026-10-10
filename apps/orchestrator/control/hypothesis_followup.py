"""Compatibility exports for shared hypothesis follow-up policy."""

from packages.orchestration.hypothesis_followup import (
    decide_hypothesis_followup,
    validate_hypothesis_followup,
)

__all__ = ["decide_hypothesis_followup", "validate_hypothesis_followup"]
