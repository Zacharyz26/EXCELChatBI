"""为生产 Agent 生成轻量、确定性的任务提纲。

提纲只负责澄清和界面可观察性；实际工具选择由同一次 Agent function-calling
完成，不再在用户请求前增加一轮独立模型规划。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from mcp_servers.excel_parser.advisor import infer_data_roles_from_mapping
from packages.orchestration.contracts import TaskContract
from packages.orchestration.task_plan_contract import (
    PlanValidation,
    validate_task_plan,
)
from packages.session.models import Artifact, Dataset, JsonObject

from apps.orchestrator.agent_tools import AgentToolRegistry

OutlineRoute = Literal["fast", "template"]
PROMPT_VERSION = "deterministic-outline-v1"

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
_HYPOTHESIS_CAPABILITY_BY_KIND = {
    "trend": "stats.trend",
    "anomaly": "stats.anomaly",
    "segment_comparison": "stats.group_compare",
    "correlation": "stats.correlation",
}


@dataclass(frozen=True, slots=True)
class TaskOutline:
    """一次确定性任务提纲及其可持久审计元数据。"""

    route: OutlineRoute
    plan: JsonObject
    validation: PlanValidation
    audit: JsonObject

    @property
    def capabilities(self) -> set[str]:
        """返回计划步骤声明的能力集合。"""
        return {
            str(item["capability"]) for item in cast(list[JsonObject], self.plan.get("steps", []))
        }


def create_task_outline(
    *,
    user_text: str,
    contract: TaskContract,
    datasets: list[Dataset],
    artifacts: list[Artifact],
    registry: AgentToolRegistry,
    blocking_clarification: JsonObject | None,
    selected_hypothesis: JsonObject | None = None,
    max_steps: int = 12,
    capability_catalog: list[JsonObject] | None = None,
) -> TaskOutline:
    """生成并校验不调用模型的 fast/template TaskPlan。"""
    context = build_outline_context(datasets=datasets, artifacts=artifacts)
    effective_catalog = (
        registry.capability_catalog() if capability_catalog is None else capability_catalog
    )
    capabilities = {
        str(item["name"]) for item in effective_catalog if item.get("allowed") is not False
    }
    required_capabilities = criterion_capabilities(contract, artifacts=artifacts)

    if blocking_clarification is not None:
        plan = _clarification_plan(blocking_clarification)
        validation = validate_task_plan(
            plan,
            capabilities=capabilities,
            criterion_capabilities=required_capabilities,
            max_steps=max_steps,
        )
        return TaskOutline(
            route="fast",
            plan=plan,
            validation=validation,
            audit=_deterministic_audit("fast", plan),
        )

    selected_capabilities = (
        _selected_hypothesis_capabilities(selected_hypothesis, datasets, capabilities)
        if selected_hypothesis is not None
        else None
    )
    route = choose_outline_route(user_text, context)
    plan = build_deterministic_outline(
        user_text=user_text,
        context=context,
        route=route,
        available_capabilities=capabilities,
        require_available_capabilities=False,
        requested_capabilities=selected_capabilities,
    )
    validation = validate_task_plan(
        plan,
        capabilities=capabilities,
        criterion_capabilities=required_capabilities,
        max_steps=max_steps,
    )
    if not validation.valid:
        raise ValueError("确定性任务提纲生成了非法计划: " + "; ".join(validation.issues))
    return TaskOutline(
        route=route,
        plan=plan,
        validation=validation,
        audit=_deterministic_audit(route, plan),
    )


def build_outline_context(*, datasets: list[Dataset], artifacts: list[Artifact]) -> JsonObject:
    """构造不含原始行、文件路径和 Artifact 正文的任务提纲上下文。"""
    dataset_items: list[JsonObject] = []
    for dataset in datasets:
        raw_columns = dataset.profile.get("columns")
        columns: list[str] = []
        if isinstance(raw_columns, list):
            for item in raw_columns:
                value = item.get("name") if isinstance(item, dict) else item
                if isinstance(value, str) and value.strip():
                    columns.append(value.strip())
        resolved_roles: list[JsonObject] = []
        try:
            role_result = infer_data_roles_from_mapping(
                dataset.profile,
                dataset_ref=dataset.ref,
            )
        except ValueError:
            # Older/incomplete upload metadata cannot justify removing a real
            # profile prerequisite. Dependency inference therefore fails closed.
            pass
        else:
            for item in cast(list[JsonObject], role_result.get("columns") or []):
                column = item.get("column")
                role = item.get("primary_role")
                if isinstance(column, str) and isinstance(role, str):
                    resolved_roles.append(
                        {
                            "column": column,
                            "role": role,
                            "ambiguous": bool(item.get("ambiguous")),
                        }
                    )
        dataset_items.append(
            {
                "ref": dataset.ref,
                "filename": dataset.filename,
                "row_count": dataset.profile.get("row_count"),
                "column_count": dataset.profile.get("column_count"),
                "columns": columns,
                "resolved_roles": resolved_roles,
                "parent_ref": dataset.parent_ref,
            }
        )
    artifact_items: list[JsonObject] = []
    for artifact in artifacts[-20:]:
        params = artifact.params or {}
        safe_params = {
            key: params[key]
            for key in _SAFE_ARTIFACT_PARAM_KEYS
            if key in params and isinstance(params[key], str | int | float | bool)
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


def choose_outline_route(user_text: str, context: JsonObject) -> OutlineRoute:
    """按可观察请求复杂度选择路由，不读取评测标签。"""
    request = user_text.lower()
    datasets = cast(list[JsonObject], context.get("datasets") or [])
    columns = [
        str(column)
        for dataset in datasets
        for column in cast(list[object], dataset.get("columns") or [])
    ]
    if context.get("knowledge_conflicts"):
        return "template"
    if "深入分析" in request or "替代解释" in request:
        return "template"
    if (
        ("先" in request and ("最后" in request or "然后" in request))
        or ("排除" in request and ("重新" in request or "再" in request))
        or ("关系" in request and ("不同" in request or "比较" in request))
    ):
        return "template"
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
            "贡献",
            "占比",
            "分群比较",
            "组间差异",
            "join",
            "关联数据集",
            "跨表关联",
        )
    ):
        return "template"
    return "fast"


def build_deterministic_outline(
    *,
    user_text: str,
    context: JsonObject,
    route: OutlineRoute,
    available_capabilities: set[str],
    require_available_capabilities: bool = True,
    requested_capabilities: tuple[str, ...] | None = None,
) -> JsonObject:
    """为已知任务族构造最小、可验证的 fast/template 计划。"""
    requested = (
        list(requested_capabilities)
        if requested_capabilities is not None
        else _requested_capabilities(user_text, context)
    )
    unavailable = [item for item in requested if item not in available_capabilities]
    if unavailable and require_available_capabilities:
        raise ValueError("计划所需能力不可用: " + ", ".join(unavailable))
    selected = [item for item in requested if item in available_capabilities]
    steps: list[JsonObject] = []
    for index, capability in enumerate(selected, 1):
        logical_id = f"{capability.replace('.', '_').replace('-', '_')}_{index}"
        dependencies = _step_dependencies(
            capability=capability,
            prior_steps=steps,
            user_text=user_text,
            context=context,
        )
        step: JsonObject = {
            "step_id": logical_id,
            "purpose": _capability_purpose(capability),
            "capability": capability,
            "dependencies": dependencies,
            "expected_evidence": [f"绑定当前 run 与数据集版本的 {capability} Evidence"],
            "completion_conditions": [_capability_condition(capability)],
            "fallback": [
                {
                    "when": "能力调用失败或后置条件不成立",
                    "action": (
                        "correct_parameters" if capability.startswith("stats.") else "retry"
                    ),
                }
            ],
        }
        steps.append(step)
    assumptions = ["异常检测方法与阈值必须在结论中披露"] if "异常" in user_text else []
    return {
        "schema_version": 1,
        "summary": (
            "无需工具，直接生成受约束答复。" if not steps else "按已知任务族执行最小可验证步骤。"
        ),
        "steps": steps,
        "assumptions": assumptions,
        "clarifications": [],
    }


def _selected_hypothesis_capabilities(
    selection: JsonObject,
    datasets: list[Dataset],
    available_capabilities: set[str],
) -> tuple[str, ...]:
    """Bind a validated 6C selection to one executable statistical capability.

    Candidate screening already used governed upload metadata, so these read-only
    statistical tools consume the selected dataset directly and need no synthetic
    profile/tool dependency. TaskStore separately rejects stale plan/data versions.
    """
    kind = selection.get("kind")
    capability = selection.get("capability")
    dataset_ref = selection.get("dataset_ref")
    expected_capability = (
        _HYPOTHESIS_CAPABILITY_BY_KIND.get(kind) if isinstance(kind, str) else None
    )
    if (
        selection.get("schema") != "chatbi-hypothesis-selection-v1"
        or selection.get("schema_version") != 1
        or selection.get("question_id") != "analysis_goal"
        or not isinstance(capability, str)
        or capability != expected_capability
    ):
        raise ValueError("候选假设选择与受支持分析能力不匹配")
    if not isinstance(dataset_ref, str) or dataset_ref not in {item.ref for item in datasets}:
        raise ValueError("候选假设引用的数据集已失效")
    if capability not in available_capabilities:
        raise ValueError("候选假设所需分析能力不可用")
    return (capability,)


def _step_dependencies(
    *,
    capability: str,
    prior_steps: list[JsonObject],
    user_text: str,
    context: JsonObject,
) -> list[str]:
    """Return only dependencies supported by an actual data-flow requirement.

    Most existing multi-step intents retain their conservative sequential shape.
    The narrow exception is a trend read whose time and metric roles are already
    resolved from governed upload metadata: it does not consume the separately
    requested profile tool result and may share the same ready frontier.
    """
    if not prior_steps:
        return []
    prior_capabilities = {str(step.get("capability")) for step in prior_steps}
    if (
        capability == "stats.trend"
        and prior_capabilities == {"data.profile"}
        and _trend_inputs_resolved(user_text, context)
    ):
        return []
    if capability == "report.generate":
        return [str(step["step_id"]) for step in prior_steps]
    return [str(prior_steps[-1]["step_id"])]


def _trend_inputs_resolved(user_text: str, context: JsonObject) -> bool:
    datasets = cast(list[JsonObject], context.get("datasets") or [])
    explicitly_selected = [
        dataset
        for dataset in datasets
        if str(dataset.get("filename") or "") in user_text
        or str(dataset.get("ref") or "") in user_text
    ]
    candidates = explicitly_selected or (datasets if len(datasets) == 1 else [])
    for dataset in candidates:
        raw_roles = dataset.get("resolved_roles")
        if not isinstance(raw_roles, list):
            continue
        roles = [item for item in raw_roles if isinstance(item, dict)]
        time_columns = [
            str(item["column"])
            for item in roles
            if item.get("role") == "time"
            and item.get("ambiguous") is False
            and isinstance(item.get("column"), str)
        ]
        metric_columns = [
            str(item["column"])
            for item in roles
            if item.get("role") == "metric"
            and item.get("ambiguous") is False
            and isinstance(item.get("column"), str)
        ]
        if len(time_columns) != 1 or len(metric_columns) != 1:
            continue
        metric_is_bound = metric_columns[0] in user_text or len(metric_columns) == 1
        time_is_bound = time_columns[0] in user_text or any(
            token in user_text for token in ("时间", "趋势", "按月", "按周", "按季度")
        )
        if metric_is_bound and time_is_bound:
            return True
    return False


def _requested_capabilities(user_text: str, context: JsonObject) -> list[str]:
    request = user_text.lower()
    result: list[str] = []

    def add(capability: str) -> None:
        if capability not in result:
            result.append(capability)

    profile_requested = any(token in request for token in ("画像", "字段", "规模"))
    role_requested = any(
        token in request
        for token in (
            "数据角色",
            "字段角色",
            "时间列",
            "指标列",
            "度量列",
            "维度列",
            "id列",
            "id 列",
            "标识列",
        )
    )
    quality_requested = any(
        token in request for token in ("质量", "缺失", "重复", "常量列", "清洗建议")
    )
    # 三项能力由同一个受治理只读工具返回，一次计划只选择用户最具体的能力，
    # 避免为了画像、角色和质量重复读取同一个数据集。
    if role_requested:
        add("data.roles")
    elif quality_requested and not profile_requested:
        add("data.quality")
    elif profile_requested:
        add("data.profile")
    if any(token in request for token in ("定义", "口径", "公司规定")):
        add("knowledge.search")
    if "异常" in request:
        add("stats.anomaly")
    requests_transform = (
        "排除" in request or "过滤" in request or ("清洗" in request and "清洗建议" not in request)
    )
    if requests_transform:
        add("dataset.transform")
    dataset_mentions = sum(
        1
        for dataset in cast(list[JsonObject], context.get("datasets") or [])
        if str(dataset.get("filename", "")) in user_text or str(dataset.get("ref", "")) in user_text
    )
    if any(
        token in request
        for token in ("join", "关联数据集", "数据集关联", "跨表关联", "表连接", "合并两表")
    ) or (
        dataset_mentions >= 2
        and any(token in request for token in ("关联", "连接", "合并", "匹配"))
    ):
        add("dataset.join.preflight")
        mentions_preflight = any(
            token in request for token in ("预检", "风险评估", "评估风险", "可行性", "先看看能否")
        )
        explicitly_executes = any(
            token in request for token in ("执行", "生成关联", "创建关联", "完成关联", "合并数据集")
        )
        preflight_only = mentions_preflight and not explicitly_executes
        if not preflight_only:
            add("dataset.join.execute")
    contribution_requested = any(token in request for token in ("贡献", "占比", "构成"))
    group_compare_requested = any(
        token in request
        for token in (
            "分群比较",
            "组间差异",
            "群体差异",
            "分组比较",
            "群组比较",
            "比较不同",
        )
    )
    if contribution_requested:
        add("stats.contribution")
    elif group_compare_requested:
        add("stats.group_compare")
    elif "回归" in request:
        add("stats.regression")
    elif any(token in request for token in ("相关", "关系")):
        add("stats.correlation")
    if "预测" in request:
        add("stats.forecast")
    elif any(token in request for token in ("趋势", "随时间", "按月", "按周", "按季度")):
        add("stats.trend")
    if (
        not contribution_requested
        and not group_compare_requested
        and any(
            token in request
            for token in ("汇总", "合计", "平均", "各地区", "各产品", "多少", "转化率", "复购率")
        )
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
        str(item.get("type")) for item in artifacts if isinstance(item.get("type"), str)
    }
    reuses_artifacts = any(token in request for token in _ARTIFACT_REUSE_TOKENS)
    revises_chart = "chart" in artifact_types and any(
        token in request for token in _CHART_REVISION_TOKENS
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
        result = [item for item in result if item not in {"stats.trend", "data.aggregate"}]
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
    route: OutlineRoute,
    plan: JsonObject,
) -> JsonObject:
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {
        "route": route,
        "prompt_version": PROMPT_VERSION,
        "response_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _capability_purpose(capability: str) -> str:
    return {
        "data.profile": "取得数据规模与字段画像",
        "data.roles": "识别时间、指标、维度和标识字段并披露歧义",
        "data.quality": "检查缺失、重复和类型质量",
        "knowledge.search": "检索并引用业务口径来源",
        "data.aggregate": "按用户指定维度聚合指标",
        "dataset.transform": "依据已有 Evidence 创建衍生数据集",
        "dataset.join.preflight": "只读评估两个已确认数据集的 Join 可行性与膨胀风险",
        "dataset.join.execute": "在预检 Evidence 和数据版本校验后生成双父血缘关联数据集",
        "stats.anomaly": "识别异常并记录方法与阈值",
        "stats.trend": "计算指定范围和粒度的趋势",
        "stats.forecast": "生成预测并披露可靠性",
        "stats.correlation": "计算相关关系并避免因果表述",
        "stats.regression": "执行受约束回归并返回统计 Evidence",
        "stats.contribution": "计算受小群体保护的维度贡献与展示覆盖率",
        "stats.group_compare": "执行 Welch 分群比较与 Holm 成对校正",
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
    if capability == "dataset.join.preflight":
        return "已生成不含原始行且不修改数据的 Join 预检 Evidence"
    if capability == "dataset.join.execute":
        return "关联 dataset_ref 已登记完整双父血缘且执行参数与预检一致"
    return f"{capability} 调用成功并生成可追溯 Evidence"
