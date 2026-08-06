"""v2.4 阶段 2A 的生产混合 Planner。

fast/template 路径确定性地产生与 LLM 路径相同的 TaskPlan；LLM 只看到经过
最小化的数据集结构、TaskContract、能力目录和 Artifact 摘要，不接触原始行。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from packages.session.models import Artifact, Dataset, JsonObject

from apps.orchestrator.agent_tools import AgentToolRegistry
from apps.orchestrator.control.contracts import TaskContract
from apps.orchestrator.control.planner_contract import (
    PlanValidation,
    validate_task_plan,
)
from apps.orchestrator.control.planner_prompt import (
    PROMPT_VERSION,
    PlannerGateway,
    generate_plan,
)

PlannerRoute = Literal["fast", "template", "llm"]

_ARTIFACT_CAPABILITY = {
    "profile": "data.profile",
    "citations": "knowledge.search",
    "table": "data.aggregate",
    "chart": "visualization.chart",
    "report": "report.generate",
}

_SAFE_ARTIFACT_PARAM_KEYS = {
    "analysis_id",
    "chart_type",
    "grain",
    "group_col",
    "time_col",
    "value_col",
}

_ARTIFACT_REUSE_TOKENS = (
    "刚才",
    "已有",
    "已完成",
    "上次",
    "这些",
    "上述",
    "前面",
    "之前",
    "第一张",
    "第二张",
    "上一张",
)

_CHART_REVISION_TOKENS = (
    "改成",
    "改为",
    "换成",
    "调整",
    "重新画",
    "重画",
    "再画",
    "新图",
)

_RECOMPUTE_TOKENS = ("重新分析", "再分析", "更新分析", "重算", "重新计算")


@dataclass(frozen=True, slots=True)
class ProductionPlan:
    """一次生产规划的计划、路由和可持久审计元数据。"""

    route: PlannerRoute
    requested_route: PlannerRoute
    plan: JsonObject
    validation: PlanValidation
    audit: JsonObject

    @property
    def capabilities(self) -> set[str]:
        """返回计划步骤声明的能力集合。"""
        return {
            str(item["capability"])
            for item in cast(list[JsonObject], self.plan.get("steps", []))
        }


async def create_production_plan(
    *,
    user_text: str,
    contract: TaskContract,
    datasets: list[Dataset],
    artifacts: list[Artifact],
    registry: AgentToolRegistry,
    gateway: PlannerGateway | None,
    blocking_clarification: JsonObject | None,
    temperature: float = 0.0,
    max_steps: int = 12,
    require_available_capabilities: bool = True,
    capability_catalog: list[JsonObject] | None = None,
) -> ProductionPlan:
    """选择 fast/template/LLM 路径，生成并确定性验证统一 TaskPlan。"""
    context = build_planner_context(datasets=datasets, artifacts=artifacts)
    effective_catalog = (
        registry.capability_catalog()
        if capability_catalog is None
        else capability_catalog
    )
    capabilities = {str(item["name"]) for item in effective_catalog}
    required_capabilities = criterion_capabilities(contract, artifacts=artifacts)

    if blocking_clarification is not None:
        plan = _clarification_plan(blocking_clarification)
        validation = validate_task_plan(
            plan,
            capabilities=capabilities,
            criterion_capabilities=required_capabilities,
            max_steps=max_steps,
        )
        return ProductionPlan(
            route="fast",
            requested_route="fast",
            plan=plan,
            validation=validation,
            audit=_deterministic_audit("fast", plan, reason="blocking_clarification"),
        )

    requested_route = choose_planner_route(user_text, context)
    if requested_route == "llm" and gateway is not None:
        generated = await generate_plan(
            gateway,
            planning_request=user_text,
            contract=contract.to_dict(),
            context=context,
            capability_catalog=effective_catalog,
            observations=[],
            criterion_capabilities=required_capabilities,
            temperature=temperature,
            max_steps=max_steps,
        )
        return ProductionPlan(
            route="llm",
            requested_route=requested_route,
            plan=generated.plan,
            validation=generated.validation,
            audit={
                "route": "llm",
                "requested_route": requested_route,
                "prompt_version": generated.prompt_version,
                "request_hash": generated.request_hash,
                "response_hash": generated.response_hash,
                "model": generated.model,
                "prompt_tokens": generated.prompt_tokens,
                "completion_tokens": generated.completion_tokens,
                "latency_ms": round(generated.latency_ms, 3),
                "cost": generated.cost,
                "cost_currency": generated.cost_currency,
                "pricing_effective_date": generated.pricing_effective_date,
                "repaired": generated.repaired,
            },
        )

    route: PlannerRoute = (
        "template" if requested_route == "llm" and gateway is None else requested_route
    )
    plan = build_deterministic_plan(
        user_text=user_text,
        context=context,
        route=route,
        available_capabilities=capabilities,
        require_available_capabilities=require_available_capabilities,
    )
    validation = validate_task_plan(
        plan,
        capabilities=capabilities,
        criterion_capabilities=required_capabilities,
        max_steps=max_steps,
    )
    if not validation.valid:
        raise ValueError(
            "确定性 Planner 生成了非法计划: " + "; ".join(validation.issues)
        )
    reason = "llm_gateway_unavailable" if requested_route == "llm" else "deterministic"
    return ProductionPlan(
        route=route,
        requested_route=requested_route,
        plan=plan,
        validation=validation,
        audit=_deterministic_audit(
            route,
            plan,
            reason=reason,
            requested_route=requested_route,
        ),
    )


def build_planner_context(
    *, datasets: list[Dataset], artifacts: list[Artifact]
) -> JsonObject:
    """构造不含原始行、文件路径和 Artifact 正文的 Planner 上下文。"""
    dataset_items: list[JsonObject] = []
    for dataset in datasets:
        raw_columns = dataset.profile.get("columns")
        columns: list[str] = []
        if isinstance(raw_columns, list):
            for item in raw_columns:
                value = item.get("name") if isinstance(item, dict) else item
                if isinstance(value, str) and value.strip():
                    columns.append(value.strip())
        dataset_items.append(
            {
                "ref": dataset.ref,
                "filename": dataset.filename,
                "row_count": dataset.profile.get("row_count"),
                "column_count": dataset.profile.get("column_count"),
                "columns": columns,
                "parent_ref": dataset.parent_ref,
            }
        )
    artifact_items: list[JsonObject] = []
    for artifact in artifacts[-20:]:
        params = artifact.params or {}
        safe_params = {
            key: params[key]
            for key in _SAFE_ARTIFACT_PARAM_KEYS
            if key in params
            and isinstance(params[key], str | int | float | bool)
        }
        artifact_items.append(
            {
                "artifact_id": artifact.id,
                "type": artifact.type,
                "source_tool": artifact.source_tool,
                "analysis_id": _artifact_analysis_id(artifact),
                "dataset_ref": artifact.dataset_ref,
                "params": safe_params,
                "file_available": (
                    bool(artifact.file_ref and Path(artifact.file_ref).is_file())
                    if artifact.type == "report"
                    else None
                ),
            }
        )
    return {
        "datasets": dataset_items,
        "artifacts": artifact_items,
        "knowledge_conflicts": False,
    }


def choose_planner_route(user_text: str, context: JsonObject) -> PlannerRoute:
    """按可观察请求复杂度选择路由，不读取评测标签。"""
    request = user_text.lower()
    datasets = cast(list[JsonObject], context.get("datasets") or [])
    columns = [
        str(column)
        for dataset in datasets
        for column in cast(list[object], dataset.get("columns") or [])
    ]
    if context.get("knowledge_conflicts"):
        return "llm"
    if "深入分析" in request or "替代解释" in request:
        return "llm"
    if (
        ("先" in request and ("最后" in request or "然后" in request))
        or ("排除" in request and ("重新" in request or "再" in request))
        or ("关系" in request and ("不同" in request or "比较" in request))
    ):
        return "llm"
    if context.get("observations") or context.get("artifacts"):
        return "template"
    if len([column for column in columns if "时间" in column or "日期" in column]) > 1:
        return "template"
    if any(
        token in request
        for token in (
            "图",
            "报告",
            "pdf",
            "趋势",
            "转化率",
            "异常",
            "预测",
            "相关",
            "回归",
        )
    ):
        return "template"
    return "fast"


def build_deterministic_plan(
    *,
    user_text: str,
    context: JsonObject,
    route: PlannerRoute,
    available_capabilities: set[str],
    require_available_capabilities: bool = True,
) -> JsonObject:
    """为已知任务族构造最小、可验证的 fast/template 计划。"""
    if route == "llm":
        raise ValueError("LLM 路径不能调用确定性计划构造器")
    requested = _requested_capabilities(user_text, context)
    unavailable = [item for item in requested if item not in available_capabilities]
    if unavailable and require_available_capabilities:
        raise ValueError("计划所需能力不可用: " + ", ".join(unavailable))
    selected = [item for item in requested if item in available_capabilities]
    steps: list[JsonObject] = []
    previous: str | None = None
    for index, capability in enumerate(selected, 1):
        logical_id = f"{capability.replace('.', '_').replace('-', '_')}_{index}"
        dependencies: list[str] = []
        if previous is not None:
            dependencies.append(previous)
        step: JsonObject = {
            "step_id": logical_id,
            "purpose": _capability_purpose(capability),
            "capability": capability,
            "dependencies": dependencies,
            "expected_evidence": [
                f"绑定当前 run 与数据集版本的 {capability} Evidence"
            ],
            "completion_conditions": [_capability_condition(capability)],
            "fallback": [
                {
                    "when": "能力调用失败或后置条件不成立",
                    "action": (
                        "correct_parameters"
                        if capability.startswith("stats.")
                        else "retry"
                    ),
                }
            ],
        }
        steps.append(step)
        previous = logical_id
    assumptions = (
        ["异常检测方法与阈值必须在结论中披露"]
        if "异常" in user_text
        else []
    )
    return {
        "schema_version": 1,
        "summary": (
            "无需工具，直接生成受约束答复。"
            if not steps
            else "按已知任务族执行最小可验证步骤。"
        ),
        "steps": steps,
        "assumptions": assumptions,
        "clarifications": [],
    }


def _requested_capabilities(user_text: str, context: JsonObject) -> list[str]:
    request = user_text.lower()
    result: list[str] = []

    def add(capability: str) -> None:
        if capability not in result:
            result.append(capability)

    if any(token in request for token in ("画像", "字段", "规模")):
        add("data.profile")
    if any(token in request for token in ("质量", "缺失", "重复")):
        add("data.profile")
    if any(token in request for token in ("定义", "口径", "公司规定")):
        add("knowledge.search")
    if "异常" in request:
        add("stats.anomaly")
    if "排除" in request or "过滤" in request or "清洗" in request:
        add("dataset.transform")
    if "回归" in request:
        add("stats.regression")
    elif any(token in request for token in ("相关", "关系")):
        add("stats.correlation")
    if "预测" in request:
        add("stats.trend")
    elif any(token in request for token in ("趋势", "随时间", "按月", "按周", "按季度")):
        add("stats.trend")
    if any(
        token in request
        for token in ("汇总", "合计", "平均", "各地区", "各产品", "多少", "转化率", "复购率")
    ):
        add("data.aggregate")
    if any(token in request for token in ("图", "可视化", "chart", "plot")):
        add("visualization.chart")
    if "报告" in request or "pdf" in request:
        if (
            not result
            and cast(list[JsonObject], context.get("datasets") or [])
            and not cast(list[JsonObject], context.get("artifacts") or [])
        ):
            add("data.profile")
        add("report.generate")

    artifacts = cast(list[JsonObject], context.get("artifacts") or [])
    artifact_types = {
        str(item.get("type"))
        for item in artifacts
        if isinstance(item.get("type"), str)
    }
    reuses_artifacts = any(token in request for token in _ARTIFACT_REUSE_TOKENS)
    revises_chart = (
        "chart" in artifact_types
        and any(token in request for token in _CHART_REVISION_TOKENS)
    )
    recomputes_analysis = any(token in request for token in _RECOMPUTE_TOKENS)

    if reuses_artifacts and not recomputes_analysis:
        if "profile" in artifact_types:
            result = [item for item in result if item != "data.profile"]
        if "stats" in artifact_types:
            result = [item for item in result if item != "stats.trend"]
        if "table" in artifact_types:
            result = [item for item in result if item != "data.aggregate"]
        if "chart" in artifact_types and not revises_chart:
            result = [item for item in result if item != "visualization.chart"]
    if revises_chart and not recomputes_analysis:
        # “把第二张图改成按月”描述的是已有图表的展示参数，不是重新做趋势分析。
        result = [
            item
            for item in result
            if item not in {"stats.trend", "data.aggregate"}
        ]
        if "visualization.chart" not in result:
            result.append("visualization.chart")

    if not result and cast(list[JsonObject], context.get("datasets") or []):
        if any(token in request for token in ("数据", "分析", "看看", "介绍")):
            add("data.profile")
    return result


def _artifact_analysis_id(artifact: Artifact) -> str:
    params = artifact.params or {}
    value = params.get("analysis_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    payload = artifact.payload or {}
    value = payload.get("analysis_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return artifact.id


def criterion_capabilities(
    contract: TaskContract, *, artifacts: list[Artifact] | None = None
) -> dict[str, set[str]]:
    mapping: dict[str, set[str]] = {}
    for criterion in contract.success_criteria:
        if artifacts and any(
            _artifact_satisfies_criterion(
                artifact,
                criterion.artifact_type,
                criterion.artifact_format,
            )
            for artifact in artifacts
        ):
            continue
        capability = (
            _ARTIFACT_CAPABILITY.get(criterion.artifact_type or "")
            if criterion.kind == "artifact"
            else None
        )
        if capability is not None:
            mapping[criterion.criterion_id] = {capability}
    return mapping


def _artifact_satisfies_criterion(
    artifact: Artifact,
    artifact_type: str | None,
    artifact_format: str | None,
) -> bool:
    if artifact_type is None or artifact.type != artifact_type:
        return False
    if artifact.type != "report":
        return True
    if not artifact.file_ref or not Path(artifact.file_ref).is_file():
        return False
    return artifact_format != "pdf" or artifact.file_ref.lower().endswith(".pdf")


def _clarification_plan(clarification: JsonObject) -> JsonObject:
    item: JsonObject = {
        "question_id": clarification["question_id"],
        "about": clarification["about"],
        "question": clarification["question"],
        "blocking": True,
    }
    return {
        "schema_version": 1,
        "summary": "等待用户确认阻塞歧义后再制定执行步骤。",
        "steps": [],
        "assumptions": [],
        "clarifications": [item],
    }


def _deterministic_audit(
    route: PlannerRoute,
    plan: JsonObject,
    *,
    reason: str,
    requested_route: PlannerRoute | None = None,
) -> JsonObject:
    encoded = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "route": route,
        "requested_route": requested_route or route,
        "prompt_version": PROMPT_VERSION,
        "response_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "model": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0.0,
        "cost": None,
        "cost_currency": None,
        "pricing_effective_date": None,
        "repaired": False,
        "fallback_reason": reason if reason != "deterministic" else None,
    }


def _capability_purpose(capability: str) -> str:
    return {
        "data.profile": "取得数据规模与字段画像",
        "data.quality": "检查缺失、重复和类型质量",
        "knowledge.search": "检索并引用业务口径来源",
        "data.aggregate": "按用户指定维度聚合指标",
        "dataset.transform": "依据已有 Evidence 创建衍生数据集",
        "stats.anomaly": "识别异常并记录方法与阈值",
        "stats.trend": "计算指定范围和粒度的趋势",
        "stats.forecast": "生成预测并披露可靠性",
        "stats.correlation": "计算相关关系并避免因果表述",
        "stats.regression": "执行受约束回归并返回统计 Evidence",
        "visualization.chart": "生成用户要求的真实图表工件",
        "report.generate": "生成可验证、可下载的报告工件",
    }.get(capability, f"执行 {capability} 能力")


def _capability_condition(capability: str) -> str:
    if capability == "visualization.chart":
        return "图表 Artifact 已持久化且可发送到前端"
    if capability == "report.generate":
        return "报告 Artifact 与所需文件均真实存在且可下载"
    if capability == "dataset.transform":
        return "衍生 dataset_ref 已登记血缘且属于当前项目"
    return f"{capability} 调用成功并生成可追溯 Evidence"
