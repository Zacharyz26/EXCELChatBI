"""生产混合 Planner 的路由、最小上下文与能力约束测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from apps.orchestrator.agent_tools import AgentToolRegistry, AgentToolSpec
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.production_planner import (
    build_planner_context,
    create_production_plan,
)
from mcp_servers.common.contracts import ToolCapabilityMetadata
from packages.models.types import Message, ModelResponse, Scenario
from packages.session.models import Dataset


def _registry() -> AgentToolRegistry:
    return AgentToolRegistry(
        [
            AgentToolSpec(
                name="get_data_profile",
                description="读取画像与质量",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("data.profile",)),
            ),
            AgentToolSpec(
                name="anomaly_detect",
                description="检测异常",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("stats.anomaly",)),
            ),
            AgentToolSpec(
                name="transform_dataset",
                description="创建衍生数据集",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(
                    capabilities=("dataset.transform",),
                    read_only=False,
                    idempotent=False,
                    risk_level="medium",
                ),
            ),
        ]
    )


def _dataset() -> Dataset:
    return Dataset(
        ref="d" * 32,
        project_id="project",
        filename="orders.xlsx",
        profile={
            "row_count": 10,
            "column_count": 2,
            "columns": [{"name": "订单号"}, {"name": "销售额"}],
            "sample_rows": [{"订单号": "SECRET-ROW", "销售额": 999}],
        },
        parent_ref=None,
        transform=None,
        created_at="now",
    )


class _PlannerGateway:
    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
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
            content=json.dumps(self.plan, ensure_ascii=False),
            model="eligible-planner",
            prompt_tokens=20,
            completion_tokens=10,
            cost=0.001,
            cost_currency="USD",
        )


def test_planner_context_excludes_sample_rows_and_file_paths() -> None:
    context = build_planner_context(datasets=[_dataset()], artifacts=[])
    encoded = json.dumps(context, ensure_ascii=False)

    assert "SECRET-ROW" not in encoded
    assert "sample_rows" not in encoded
    assert context["datasets"][0]["columns"] == ["订单号", "销售额"]


@pytest.mark.asyncio
async def test_fast_path_produces_valid_shared_plan_without_model() -> None:
    contract = build_minimal_contract(
        run_id="fast-run",
        user_text="介绍这份数据的规模和质量",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=[],
        registry=_registry(),
        gateway=None,
        blocking_clarification=None,
    )

    assert result.route == "fast"
    assert result.validation.valid is True
    assert result.capabilities == {"data.profile"}
    assert result.audit["model"] is None


@pytest.mark.asyncio
async def test_deterministic_path_fails_closed_when_requested_capability_is_missing(
) -> None:
    contract = build_minimal_contract(
        run_id="missing-capability",
        user_text="检测销售额异常",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    registry = AgentToolRegistry(
        [
            AgentToolSpec(
                name="get_data_profile",
                description="读取画像",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("data.profile",)),
            )
        ]
    )

    with pytest.raises(ValueError, match="stats.anomaly"):
        await create_production_plan(
            user_text=contract.goal,
            contract=contract,
            datasets=[_dataset()],
            artifacts=[],
            registry=registry,
            gateway=None,
            blocking_clarification=None,
        )


@pytest.mark.asyncio
async def test_generic_report_plan_creates_source_analysis_before_report() -> None:
    report_registry = AgentToolRegistry(
        [
            AgentToolSpec(
                name="get_data_profile",
                description="读取画像",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("data.profile",)),
            ),
            AgentToolSpec(
                name="generate_report",
                description="生成报告",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("report.generate",)),
            ),
        ]
    )
    contract = build_minimal_contract(
        run_id="report-plan",
        user_text="请生成 PDF 报告",
        chart_required=False,
        report_required=True,
        pdf_required=True,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=[],
        registry=report_registry,
        gateway=None,
        blocking_clarification=None,
    )

    steps = result.plan["steps"]
    assert [step["capability"] for step in steps] == [
        "data.profile",
        "report.generate",
    ]
    assert steps[1]["dependencies"] == [steps[0]["step_id"]]


@pytest.mark.asyncio
async def test_blocking_clarification_persists_a_step_free_plan() -> None:
    contract = build_minimal_contract(
        run_id="waiting-run",
        user_text="分析数据",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=[],
        registry=_registry(),
        gateway=None,
        blocking_clarification={
            "question_id": "metric",
            "about": "metric",
            "question": "请选择指标。",
            "reason": "不同指标会改变结果。",
        },
    )

    assert result.validation.valid is True
    assert result.plan["steps"] == []
    assert result.plan["clarifications"][0] == {
        "question_id": "metric",
        "about": "metric",
        "question": "请选择指标。",
        "blocking": True,
    }


@pytest.mark.asyncio
async def test_complex_route_uses_llm_and_records_only_hashes_and_usage() -> None:
    contract = build_minimal_contract(
        run_id="llm-run",
        user_text="先检测异常，然后排除这些行再分析",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    plan = {
        "schema_version": 1,
        "summary": "检测后排除异常",
        "steps": [
            {
                "step_id": "detect",
                "purpose": "检测异常",
                "capability": "stats.anomaly",
                "dependencies": [],
                "expected_evidence": ["异常行引用"],
                "completion_conditions": ["返回异常行 Evidence"],
                "fallback": [{"when": "失败", "action": "correct_parameters"}],
            },
            {
                "step_id": "exclude",
                "purpose": "排除已识别异常",
                "capability": "dataset.transform",
                "dependencies": ["detect"],
                "expected_evidence": ["衍生数据集引用"],
                "completion_conditions": ["登记衍生数据集与血缘"],
                "fallback": [{"when": "失败", "action": "block"}],
            },
        ],
        "assumptions": [],
        "clarifications": [],
    }
    gateway = _PlannerGateway(plan)

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=[],
        registry=_registry(),
        gateway=gateway,
        blocking_clarification=None,
    )

    assert result.route == "llm"
    assert result.validation.valid is True
    assert result.audit["model"] == "eligible-planner"
    assert result.audit["prompt_tokens"] == 20
    assert result.audit["response_hash"]
    assert len(gateway.calls) == 1
    request_payload = gateway.calls[0][-1].content
    assert "SECRET-ROW" not in request_payload
