"""LLM Planner prompt protocol tests with an offline fake gateway."""

from __future__ import annotations

import json
from typing import Any

import pytest
from apps.orchestrator.control.planner_prompt import (
    PlannerProtocolError,
    generate_plan,
)
from packages.models.types import Message, ModelResponse, Scenario


class _Gateway:
    def __init__(self, contents: list[str]) -> None:
        self.contents = contents
        self.calls: list[tuple[Scenario, list[Message], dict[str, object] | None]] = []

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        self.calls.append((scenario, messages, params))
        return ModelResponse(
            content=self.contents.pop(0),
            model="planner-test",
            prompt_tokens=10,
            completion_tokens=5,
            usage_available=True,
            latency_ms=2.5,
            cost=0.001,
            cost_currency="USD",
            pricing_effective_date="2026-04-24",
        )


def _plan() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "summary": "检查质量",
        "steps": [
            {
                "step_id": "quality",
                "purpose": "检查数据质量",
                "capability": "data.quality",
                "dependencies": [],
                "expected_evidence": ["质量概况"],
                "completion_conditions": ["产生绑定当前版本的质量 Evidence"],
                "fallback": [{"when": "数据不足", "action": "block"}],
            }
        ],
        "assumptions": [],
        "clarifications": [],
    }


@pytest.mark.asyncio
async def test_generate_plan_uses_strict_json_and_shared_contract() -> None:
    gateway = _Gateway([json.dumps(_plan(), ensure_ascii=False)])

    result = await generate_plan(
        gateway,
        contract={
            "goal": "检查质量",
            "success_criteria": [{"criterion_id": "goal.coverage"}],
        },
        context={"datasets": [{"ref": "d1", "columns": ["销售额"]}]},
        capability_catalog=[
            {"name": "data.quality", "allowed": True, "risk": "read_only"}
        ],
        observations=[],
        criterion_capabilities={"goal.coverage": {"data.quality"}},
        temperature=0.0,
    )

    assert result.validation.valid is True
    assert result.repaired is False
    assert result.cost == 0.001
    assert gateway.calls[0][0] == Scenario.COMPLEX_REASONING
    assert gateway.calls[0][2] == {
        "response_format": {"type": "json_object"},
        "temperature": 0.0,
        "max_tokens": 2048,
    }
    payload = json.loads(gateway.calls[0][1][1].content)
    assert payload["task_plan_schema"]["additionalProperties"] is False
    assert "rows" not in payload["context"]


@pytest.mark.asyncio
async def test_generate_plan_repairs_once_and_aggregates_usage() -> None:
    gateway = _Gateway(["not-json", json.dumps(_plan(), ensure_ascii=False)])

    result = await generate_plan(
        gateway,
        contract={"goal": "检查质量", "success_criteria": []},
        context={},
        capability_catalog=[
            {"name": "data.quality", "allowed": True, "risk": "read_only"}
        ],
        observations=[],
        criterion_capabilities={},
        temperature=0.3,
    )

    assert result.repaired is True
    assert result.prompt_tokens == 20
    assert result.completion_tokens == 10
    assert result.cost == 0.002
    assert len(gateway.calls) == 2


@pytest.mark.asyncio
async def test_generate_plan_fails_closed_after_one_repair() -> None:
    gateway = _Gateway(["{}", "{}"])

    with pytest.raises(PlannerProtocolError, match="一次修复"):
        await generate_plan(
            gateway,
            contract={"goal": "检查质量", "success_criteria": []},
            context={},
            capability_catalog=[
                {"name": "data.quality", "allowed": True, "risk": "read_only"}
            ],
            observations=[],
            criterion_capabilities={},
            temperature=0.0,
        )

    assert len(gateway.calls) == 2
