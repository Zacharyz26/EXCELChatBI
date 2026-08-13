"""生产混合 Planner 的路由、最小上下文与能力约束测试。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from apps.orchestrator.agent_tools import AgentToolRegistry, AgentToolSpec
from apps.orchestrator.control.contracts import build_minimal_contract
from apps.orchestrator.control.production_planner import (
    build_deterministic_plan,
    build_planner_context,
    create_production_plan,
)
from mcp_servers.common.contracts import ToolCapabilityMetadata
from packages.models.types import Message, ModelResponse, Scenario
from packages.session.models import Artifact, Dataset


def _registry() -> AgentToolRegistry:
    return AgentToolRegistry(
        [
            AgentToolSpec(
                name="get_data_profile",
                description="读取画像与质量",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(
                    capabilities=("data.profile", "data.roles", "data.quality")
                ),
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


def _artifact(
    artifact_id: str,
    artifact_type: str,
    *,
    analysis_id: str,
    dataset_ref: str | None = None,
    params: dict[str, Any] | None = None,
    file_ref: str | None = None,
) -> Artifact:
    return Artifact(
        id=artifact_id,
        conversation_id="conversation",
        message_id="message",
        type=artifact_type,
        payload={},
        file_ref=file_ref,
        source_tool={
            "stats": "trend_analysis",
            "chart": "gen_chart",
            "report": "generate_report",
        }.get(artifact_type),
        params={"analysis_id": analysis_id, **(params or {})},
        dataset_ref=dataset_ref,
        created_at="now",
    )


def _artifact_registry() -> AgentToolRegistry:
    return AgentToolRegistry(
        [
            AgentToolSpec(
                name="get_data_profile",
                description="读取画像",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("data.profile",)),
            ),
            AgentToolSpec(
                name="trend_analysis",
                description="趋势",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("stats.trend",)),
            ),
            AgentToolSpec(
                name="gen_chart",
                description="图表",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(
                    capabilities=("visualization.chart",)
                ),
            ),
            AgentToolSpec(
                name="generate_report",
                description="报告",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=("report.generate",)),
            ),
        ]
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
    report = _artifact(
        "report-1",
        "report",
        analysis_id="report-analysis",
        file_ref="/private/reports/customer-secret.pdf",
    )
    context = build_planner_context(datasets=[_dataset()], artifacts=[report])
    encoded = json.dumps(context, ensure_ascii=False)

    assert "SECRET-ROW" not in encoded
    assert "sample_rows" not in encoded
    assert "/private/reports/customer-secret.pdf" not in encoded
    assert context["datasets"][0]["columns"] == ["订单号", "销售额"]
    assert context["artifacts"][0]["file_available"] is False


@pytest.mark.asyncio
async def test_report_follow_up_reuses_existing_analysis_artifacts() -> None:
    artifacts = [
        _artifact(
            "stats-1",
            "stats",
            analysis_id="analysis-stats",
            dataset_ref=_dataset().ref,
            params={"grain": "month"},
        ),
        _artifact(
            "chart-1",
            "chart",
            analysis_id="analysis-chart",
            dataset_ref=_dataset().ref,
            params={"chart_type": "line"},
        ),
    ]
    contract = build_minimal_contract(
        run_id="report-follow-up",
        user_text="把刚才的趋势和图表生成 PDF 报告。",
        chart_required=True,
        report_required=True,
        pdf_required=True,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=artifacts,
        registry=_artifact_registry(),
        gateway=None,
        blocking_clarification=None,
    )

    assert [step["capability"] for step in result.plan["steps"]] == [
        "report.generate"
    ]


@pytest.mark.asyncio
async def test_report_follow_up_reuses_completed_profile_artifact() -> None:
    artifacts = [
        _artifact(
            "profile-1",
            "profile",
            analysis_id="analysis-profile",
            dataset_ref=_dataset().ref,
        )
    ]
    contract = build_minimal_contract(
        run_id="profile-report-follow-up",
        user_text=(
            "请把本次对话已完成的数据画像组装成一份报告，"
            "附要点解读，并导出 PDF。"
        ),
        chart_required=False,
        report_required=True,
        pdf_required=True,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=artifacts,
        registry=_artifact_registry(),
        gateway=None,
        blocking_clarification=None,
    )

    assert [step["capability"] for step in result.plan["steps"]] == [
        "report.generate"
    ]


@pytest.mark.asyncio
async def test_report_follow_up_recomputes_profile_when_explicitly_requested() -> None:
    artifacts = [
        _artifact(
            "profile-1",
            "profile",
            analysis_id="analysis-profile",
            dataset_ref=_dataset().ref,
        )
    ]
    contract = build_minimal_contract(
        run_id="profile-report-recompute",
        user_text="基于已完成的数据画像重新分析，并生成 PDF 报告。",
        chart_required=False,
        report_required=True,
        pdf_required=True,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=artifacts,
        registry=_artifact_registry(),
        gateway=None,
        blocking_clarification=None,
    )

    assert [step["capability"] for step in result.plan["steps"]] == [
        "data.profile",
        "report.generate",
    ]


@pytest.mark.asyncio
async def test_chart_revision_reuses_referenced_chart_lineage() -> None:
    artifacts = [
        _artifact(
            "chart-1",
            "chart",
            analysis_id="analysis-chart-1",
            dataset_ref=_dataset().ref,
            params={"grain": "day"},
        ),
        _artifact(
            "chart-2",
            "chart",
            analysis_id="analysis-chart-2",
            dataset_ref=_dataset().ref,
            params={"grain": "week"},
        ),
    ]
    contract = build_minimal_contract(
        run_id="chart-follow-up",
        user_text="把第二张图改成按月展示。",
        chart_required=True,
        report_required=False,
        pdf_required=False,
    )

    result = await create_production_plan(
        user_text=contract.goal,
        contract=contract,
        datasets=[_dataset()],
        artifacts=artifacts,
        registry=_artifact_registry(),
        gateway=None,
        blocking_clarification=None,
    )

    assert [step["capability"] for step in result.plan["steps"]] == [
        "visualization.chart"
    ]


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
async def test_fast_path_selects_role_capability_without_duplicate_profile_step() -> None:
    contract = build_minimal_contract(
        run_id="role-run",
        user_text="识别时间列、指标列和维度列，并说明不确定的数据角色",
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
    assert result.capabilities == {"data.roles"}
    assert len(result.plan["steps"]) == 1


@pytest.mark.asyncio
async def test_fast_path_selects_quality_capability_for_quality_only_request() -> None:
    contract = build_minimal_contract(
        run_id="quality-run",
        user_text="检查空值、重复和常量列，并给出清洗建议",
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
    assert result.capabilities == {"data.quality"}
    assert len(result.plan["steps"]) == 1


@pytest.mark.asyncio
async def test_explicit_cleaning_execution_keeps_transform_capability() -> None:
    contract = build_minimal_contract(
        run_id="clean-run",
        user_text="按已经确认的规则清洗数据并去掉空值",
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

    assert "dataset.transform" in result.capabilities


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
async def test_deterministic_path_rejects_present_but_unavailable_capability() -> None:
    contract = build_minimal_contract(
        run_id="unavailable-capability",
        user_text="检测销售额异常",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    catalog = _registry().capability_catalog()
    for item in catalog:
        if item["name"] == "stats.anomaly":
            item["allowed"] = False
            item["required_profile"] = "stats"
            item["unavailable_reason"] = "profile_not_enabled"

    with pytest.raises(ValueError, match="stats.anomaly"):
        await create_production_plan(
            user_text=contract.goal,
            contract=contract,
            datasets=[_dataset()],
            artifacts=[],
            registry=_registry(),
            gateway=None,
            blocking_clarification=None,
            capability_catalog=catalog,
        )


@pytest.mark.asyncio
async def test_prediction_request_fails_closed_on_governed_forecast_capability() -> None:
    contract = build_minimal_contract(
        run_id="forecast-unavailable",
        user_text="预测未来四周的销售额",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )

    with pytest.raises(ValueError, match="stats.forecast"):
        await create_production_plan(
            user_text=contract.goal,
            contract=contract,
            datasets=[_dataset()],
            artifacts=[],
            registry=_artifact_registry(),
            gateway=None,
            blocking_clarification=None,
        )


@pytest.mark.parametrize(
    ("user_text", "expected"),
    [
        ("分析地区对销售额的贡献占比", "stats.contribution"),
        ("比较不同地区的销售额是否有组间差异", "stats.group_compare"),
    ],
)
def test_advanced_stats_requests_use_governed_capabilities_only(
    user_text: str,
    expected: str,
) -> None:
    plan = build_deterministic_plan(
        user_text=user_text,
        context={"datasets": [], "artifacts": []},
        route="template",
        available_capabilities={expected, "data.aggregate"},
    )

    capabilities = [step["capability"] for step in plan["steps"]]
    assert capabilities == [expected]


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
    planning_request = (
        contract.goal
        + "\n\n这是基于父 TaskRun 的新分析分支。"
        + "\n- 需改进：COMPOSE_4D_FEEDBACK 请保留原始数据并重新核对字段"
    )

    result = await create_production_plan(
        user_text=planning_request,
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
    assert json.loads(request_payload)["planning_request"] == planning_request
