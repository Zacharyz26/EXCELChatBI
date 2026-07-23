"""Stage-0 TaskPlan contract and deterministic validation.

This module defines the shared shape used by the fast, template and LLM planner
spikes.  It does not participate in the production Agent loop until stage 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, cast

from jsonschema import Draft202012Validator
from packages.session.models import JsonObject

TASK_PLAN_SCHEMA_VERSION = 1

TASK_PLAN_SCHEMA: JsonObject = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "schema_version",
        "summary",
        "steps",
        "assumptions",
        "clarifications",
    ],
    "properties": {
        "schema_version": {"const": TASK_PLAN_SCHEMA_VERSION},
        "summary": {"type": "string", "minLength": 1, "maxLength": 500},
        "steps": {
            "type": "array",
            "maxItems": 24,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "step_id",
                    "purpose",
                    "capability",
                    "dependencies",
                    "expected_evidence",
                    "completion_conditions",
                    "fallback",
                ],
                "properties": {
                    "step_id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    },
                    "purpose": {"type": "string", "minLength": 1, "maxLength": 500},
                    "capability": {"type": "string", "minLength": 1, "maxLength": 100},
                    "dependencies": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                    },
                    "expected_evidence": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1, "maxLength": 300},
                    },
                    "completion_conditions": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string", "minLength": 1, "maxLength": 300},
                    },
                    "fallback": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["when", "action"],
                            "properties": {
                                "when": {
                                    "type": "string",
                                    "minLength": 1,
                                    "maxLength": 300,
                                },
                                "action": {
                                    "enum": [
                                        "correct_parameters",
                                        "use_alternative_capability",
                                        "degrade_method",
                                        "request_clarification",
                                        "retry",
                                        "block",
                                    ]
                                },
                            },
                        },
                    },
                },
            },
        },
        "assumptions": {
            "type": "array",
            "items": {"type": "string", "minLength": 1, "maxLength": 300},
        },
        "clarifications": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["question_id", "about", "question", "blocking"],
                "properties": {
                    "question_id": {
                        "type": "string",
                        "pattern": "^[a-z][a-z0-9_-]{0,63}$",
                    },
                    "about": {"type": "string", "minLength": 1, "maxLength": 100},
                    "question": {"type": "string", "minLength": 1, "maxLength": 500},
                    "blocking": {"type": "boolean"},
                },
            },
        },
    },
}

_TASK_PLAN_VALIDATOR = Draft202012Validator(TASK_PLAN_SCHEMA)


@dataclass(frozen=True, slots=True)
class PlanValidation:
    schema_valid: bool
    dependencies_valid: bool
    capability_valid: bool
    criteria_coverage: bool
    budget_valid: bool
    issues: tuple[str, ...]

    @property
    def valid(self) -> bool:
        return (
            self.schema_valid
            and self.dependencies_valid
            and self.capability_valid
            and self.criteria_coverage
            and self.budget_valid
        )


def parse_task_plan(content: str) -> JsonObject:
    """Parse strict JSON without accepting Markdown fences or free-text recovery."""
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError("Planner 未返回严格 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("TaskPlan 顶层必须是对象")
    return cast(JsonObject, parsed)


def validate_task_plan(
    plan: JsonObject,
    *,
    capabilities: set[str],
    criterion_capabilities: dict[str, set[str]] | None = None,
    max_steps: int = 12,
    allow_waiting_user: bool = True,
) -> PlanValidation:
    """Apply schema, graph, catalog, criterion and budget checks."""
    issues: list[str] = []
    schema_errors = sorted(
        _TASK_PLAN_VALIDATOR.iter_errors(plan), key=lambda item: list(item.path)
    )
    schema_valid = not schema_errors
    if schema_errors:
        issues.append(f"schema:{schema_errors[0].message}")
        return PlanValidation(
            schema_valid=False,
            dependencies_valid=False,
            capability_valid=False,
            criteria_coverage=False,
            budget_valid=False,
            issues=tuple(issues),
        )

    steps = cast(list[dict[str, Any]], plan["steps"])
    step_ids = [str(step["step_id"]) for step in steps]
    duplicate_ids = len(step_ids) != len(set(step_ids))
    unknown_dependencies = {
        str(dep)
        for step in steps
        for dep in cast(list[str], step["dependencies"])
        if dep not in step_ids
    }
    cyclic = _has_cycle(steps)
    dependencies_valid = not duplicate_ids and not unknown_dependencies and not cyclic
    if duplicate_ids:
        issues.append("dependencies:duplicate_step_id")
    if unknown_dependencies:
        issues.append(
            f"dependencies:unknown={','.join(sorted(unknown_dependencies))}"
        )
    if cyclic:
        issues.append("dependencies:cycle")

    used_capabilities = {str(step["capability"]) for step in steps}
    unknown_capabilities = used_capabilities - capabilities
    capability_valid = not unknown_capabilities
    if unknown_capabilities:
        issues.append(f"capability:unknown={','.join(sorted(unknown_capabilities))}")

    clarifications = cast(list[dict[str, Any]], plan["clarifications"])
    waiting_user = any(bool(item["blocking"]) for item in clarifications)
    requirements = criterion_capabilities or {}
    missing_criteria = {
        criterion_id
        for criterion_id, accepted in requirements.items()
        if accepted and not used_capabilities.intersection(accepted)
    }
    criteria_coverage = not missing_criteria or (
        allow_waiting_user and waiting_user and not steps
    )
    if not criteria_coverage:
        issues.append(f"criteria:missing={','.join(sorted(missing_criteria))}")

    budget_valid = len(steps) <= max_steps
    if not budget_valid:
        issues.append(f"budget:steps={len(steps)}>{max_steps}")

    return PlanValidation(
        schema_valid=schema_valid,
        dependencies_valid=dependencies_valid,
        capability_valid=capability_valid,
        criteria_coverage=criteria_coverage,
        budget_valid=budget_valid,
        issues=tuple(issues),
    )


def plan_signature(plan: JsonObject) -> str:
    """Return a stable structure signature used for repeatability scoring."""
    steps = cast(list[dict[str, Any]], plan.get("steps", []))
    value = [
        {
            "capability": step.get("capability"),
            "dependencies": sorted(
                str(item) for item in cast(list[object], step.get("dependencies", []))
            ),
        }
        for step in steps
    ]
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _has_cycle(steps: list[dict[str, Any]]) -> bool:
    graph = {
        str(step["step_id"]): {
            str(dep) for dep in cast(list[str], step["dependencies"])
        }
        for step in steps
    }
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> bool:
        if node in visiting:
            return True
        if node in visited:
            return False
        visiting.add(node)
        if any(dep in graph and visit(dep) for dep in graph[node]):
            return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(visit(node) for node in graph)
