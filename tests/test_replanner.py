"""阶段 2B Observation Replanner 测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from apps.orchestrator.agent_tools import AgentToolRegistry, AgentToolSpec
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.replanner import (
    conditional_skip_after_success,
    create_replan,
)
from mcp_servers.common.contracts import ToolCapabilityMetadata
from packages.models.types import Message, ModelResponse, Scenario
from packages.session.task_models import StepStatus, TaskStepRecord


def _definition(
    logical_id: str,
    capability: str,
    dependencies: list[str],
    *,
    fallback: str,
) -> dict[str, Any]:
    return {
        "step_id": logical_id,
        "purpose": logical_id,
        "capability": capability,
        "dependencies": dependencies,
        "expected_evidence": [f"{logical_id} Evidence"],
        "completion_conditions": [f"{logical_id} 完成"],
        "fallback": [{"when": "失败", "action": fallback}],
    }


def _step(
    definition: dict[str, Any],
    *,
    status: StepStatus,
    position: int,
) -> TaskStepRecord:
    return TaskStepRecord(
        step_id=f"db-{definition['step_id']}",
        plan_id="plan",
        run_id="run",
        position=position,
        logical_id=str(definition["step_id"]),
        status=status,
        definition=definition,
        started_at="start" if status != "pending" else None,
        completed_at="end" if status in {"completed", "failed", "skipped"} else None,
    )


def _plan(steps: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "summary": "测试重规划",
        "steps": steps,
        "assumptions": [],
        "clarifications": [],
    }


def _registry() -> AgentToolRegistry:
    return AgentToolRegistry(
        [
            AgentToolSpec(
                name="profile",
                description="画像",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("data.profile",)),
            ),
            AgentToolSpec(
                name="trend",
                description="趋势",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("stats.trend",)),
            ),
            AgentToolSpec(
                name="correlation",
                description="相关",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("stats.correlation",)),
            ),
        ]
    )


class _Gateway:
    def __init__(self, plans: list[dict[str, Any]]) -> None:
        self.plans = plans
        self.calls: list[list[Message]] = []

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        assert scenario == Scenario.COMPLEX_REASONING
        assert params is not None
        self.calls.append(messages)
        return ModelResponse(
            content=json.dumps(self.plans.pop(0), ensure_ascii=False),
            model="planner",
        )


def _observation(step_id: str, *, code: str = "tool_execution_failed") -> dict[str, Any]:
    return {
        "observation_id": "obs-1",
        "step_id": step_id,
        "source": "tool",
        "status": "error",
        "code": code,
        "summary": "字段不存在",
        "retryable": True,
        "payload_ref": None,
    }


@pytest.mark.asyncio
async def test_retry_fallback_creates_deterministic_revision() -> None:
    trend = _definition("trend", "stats.trend", [], fallback="correct_parameters")
    plan = _plan([trend])
    contract = build_minimal_contract(
        run_id="run",
        user_text="分析趋势",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    decision = await create_replan(
        contract=contract,
        current_plan=plan,
        current_steps=[_step(trend, status="failed", position=0)],
        observation=_observation("trend"),
        datasets=[],
        artifacts=[],
        registry=_registry(),
        gateway=None,
    )

    assert decision.disposition == "revised"
    assert decision.plan == plan
    assert decision.step_status_overrides == {}
    assert decision.audit["action"] == "correct_parameters"
    assert decision.reason == "observation:obs-1:tool_execution_failed"


@pytest.mark.asyncio
async def test_block_fallback_persists_failed_step_as_blocked() -> None:
    trend = _definition("trend", "stats.trend", [], fallback="block")
    contract = build_minimal_contract(
        run_id="run",
        user_text="分析趋势",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    decision = await create_replan(
        contract=contract,
        current_plan=_plan([trend]),
        current_steps=[_step(trend, status="failed", position=0)],
        observation=_observation("trend"),
        datasets=[],
        artifacts=[],
        registry=_registry(),
        gateway=None,
    )

    assert decision.disposition == "blocked"
    assert decision.step_status_overrides == {"trend": "blocked"}


@pytest.mark.asyncio
async def test_llm_replanner_repairs_plan_that_drops_completed_step() -> None:
    profile = _definition("profile", "data.profile", [], fallback="retry")
    trend = _definition(
        "trend",
        "stats.trend",
        ["profile"],
        fallback="use_alternative_capability",
    )
    alternative = _definition(
        "correlation",
        "stats.correlation",
        ["profile"],
        fallback="block",
    )
    # 标准 schema/依赖校验可通过，但违反“已完成步骤不可删除”的修订约束。
    invalid_alternative = _definition(
        "correlation",
        "stats.correlation",
        [],
        fallback="block",
    )
    invalid = _plan([invalid_alternative])
    repaired = _plan([profile, alternative])
    gateway = _Gateway([invalid, repaired])
    contract = build_minimal_contract(
        run_id="run",
        user_text="分析关系",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    decision = await create_replan(
        contract=contract,
        current_plan=_plan([profile, trend]),
        current_steps=[
            _step(profile, status="completed", position=0),
            _step(trend, status="failed", position=1),
        ],
        observation=_observation("trend"),
        datasets=[],
        artifacts=[],
        registry=_registry(),
        gateway=gateway,
    )

    assert decision.disposition == "revised"
    assert decision.plan == repaired
    assert decision.audit["route"] == "llm"
    assert decision.audit["repaired"] is True
    assert len(gateway.calls) == 2
    assert "字段不存在" in gateway.calls[0][-1].content


def test_zero_anomaly_observation_skips_conditional_transform() -> None:
    detect = _definition("detect", "stats.anomaly", [], fallback="retry")
    transform = _definition(
        "transform",
        "dataset.transform",
        ["detect"],
        fallback="block",
    )

    result = conditional_skip_after_success(
        completed_step=_step(detect, status="completed", position=0),
        tool_name="anomaly_detect",
        result={"n_anomalies": 0, "anomalies": []},
        current_steps=[
            _step(detect, status="completed", position=0),
            _step(transform, status="pending", position=1),
        ],
    )

    assert result == (
        {"transform": "skipped"},
        "observation:no_anomalies:skip_conditional_transform",
    )


def test_conditional_skip_rejects_internally_inconsistent_anomaly_result() -> None:
    detect = _definition("detect", "stats.anomaly", [], fallback="retry")
    transform = _definition(
        "transform",
        "dataset.transform",
        ["detect"],
        fallback="block",
    )

    result = conditional_skip_after_success(
        completed_step=_step(detect, status="completed", position=0),
        tool_name="anomaly_detect",
        result={"n_anomalies": 0, "anomalies": [{"row": 2}]},
        current_steps=[
            _step(detect, status="completed", position=0),
            _step(transform, status="pending", position=1),
        ],
    )

    assert result is None
