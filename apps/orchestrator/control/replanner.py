"""阶段 2B 的 Observation 驱动 Replanner。

简单的同能力重试走确定性修订；需要换方法/降级时才调用受约束 LLM Planner。
所有输出仍使用统一 TaskPlan schema，并由 TaskStore 生成不可变新版本。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, cast

from packages.session.models import Artifact, Dataset, JsonObject
from packages.session.task_models import StepStatus, TaskStepRecord

from apps.orchestrator.agent_tools import AgentToolRegistry
from apps.orchestrator.control.contracts import TaskContract
from apps.orchestrator.control.planner_contract import PlanValidation, validate_task_plan
from apps.orchestrator.control.planner_prompt import PlannerGateway, generate_plan
from apps.orchestrator.control.production_planner import (
    build_planner_context,
    criterion_capabilities,
)

ReplanDisposition = Literal["revised", "blocked"]

_DETERMINISTIC_ACTIONS = {"retry", "correct_parameters"}
_MODEL_ACTIONS = {
    "use_alternative_capability",
    "degrade_method",
    "request_clarification",
}


@dataclass(frozen=True, slots=True)
class ReplanDecision:
    disposition: ReplanDisposition
    plan: JsonObject
    validation: PlanValidation
    reason: str
    audit: JsonObject
    step_status_overrides: dict[str, StepStatus]


async def create_replan(
    *,
    contract: TaskContract,
    current_plan: JsonObject,
    current_steps: list[TaskStepRecord],
    observation: JsonObject,
    datasets: list[Dataset],
    artifacts: list[Artifact],
    registry: AgentToolRegistry,
    gateway: PlannerGateway | None,
    temperature: float = 0.0,
    max_steps: int = 12,
) -> ReplanDecision:
    """依据一次失败 Observation 修订计划，保留已完成步骤。"""
    logical_step_id = str(observation.get("step_id", ""))
    failed_step = next(
        (step for step in current_steps if step.logical_id == logical_step_id),
        None,
    )
    if failed_step is None:
        raise ValueError("Observation 引用的步骤不属于当前计划")
    code = str(observation.get("code", "observation"))
    observation_id = str(observation.get("observation_id", "unknown"))
    reason = f"observation:{observation_id}:{code}"[:200]
    fallback_action = _fallback_action(failed_step)
    capabilities = {
        str(item["name"]) for item in registry.capability_catalog()
    }
    required = criterion_capabilities(contract)

    if fallback_action == "block":
        validation = validate_task_plan(
            current_plan,
            capabilities=capabilities,
            criterion_capabilities=required,
            max_steps=max_steps,
        )
        return ReplanDecision(
            disposition="blocked",
            plan=current_plan,
            validation=validation,
            reason=reason,
            audit=_deterministic_audit(
                current_plan,
                action="block",
                observation=observation,
            ),
            step_status_overrides={logical_step_id: "blocked"},
        )

    if fallback_action in _DETERMINISTIC_ACTIONS:
        validation = validate_task_plan(
            current_plan,
            capabilities=capabilities,
            criterion_capabilities=required,
            max_steps=max_steps,
        )
        if not validation.valid:
            raise ValueError("当前计划无法确定性修订: " + "; ".join(validation.issues))
        return ReplanDecision(
            disposition="revised",
            plan=current_plan,
            validation=validation,
            reason=reason,
            audit=_deterministic_audit(
                current_plan,
                action=fallback_action,
                observation=observation,
            ),
            step_status_overrides={},
        )

    if fallback_action not in _MODEL_ACTIONS or gateway is None:
        validation = validate_task_plan(
            current_plan,
            capabilities=capabilities,
            criterion_capabilities=required,
            max_steps=max_steps,
        )
        return ReplanDecision(
            disposition="blocked",
            plan=current_plan,
            validation=validation,
            reason=reason,
            audit=_deterministic_audit(
                current_plan,
                action="block:model_replanner_unavailable",
                observation=observation,
            ),
            step_status_overrides={logical_step_id: "blocked"},
        )

    context = build_planner_context(datasets=datasets, artifacts=artifacts)
    context.update(
        {
            "current_plan": current_plan,
            "step_statuses": {
                step.logical_id: step.status for step in current_steps
            },
        }
    )
    generated = await generate_plan(
        gateway,
        planning_request=contract.goal,
        contract=contract.to_dict(),
        context=context,
        capability_catalog=registry.capability_catalog(),
        observations=[_safe_observation(observation)],
        criterion_capabilities=required,
        temperature=temperature,
        max_steps=max_steps,
        additional_validator=_completed_step_validator(current_steps),
    )
    return ReplanDecision(
        disposition="revised",
        plan=generated.plan,
        validation=generated.validation,
        reason=reason,
        audit={
            "route": "llm",
            "phase": "replanner",
            "action": fallback_action,
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
            "observation_id": observation_id,
            "observation_code": code,
        },
        step_status_overrides={},
    )


def conditional_skip_after_success(
    *,
    completed_step: TaskStepRecord,
    tool_name: str,
    result: object,
    current_steps: list[TaskStepRecord],
) -> tuple[dict[str, StepStatus], str] | None:
    """在异常检测明确返回零异常时，跳过直接依赖的条件清洗步骤。"""
    if tool_name != "anomaly_detect" or not isinstance(result, dict):
        return None
    raw_count = result.get("n_anomalies")
    if isinstance(raw_count, bool):
        return None
    if isinstance(raw_count, int | float):
        if not float(raw_count).is_integer() or raw_count < 0:
            return None
        anomaly_count = int(raw_count)
        anomalies = result.get("anomalies")
        if isinstance(anomalies, list) and len(anomalies) != anomaly_count:
            return None
    else:
        anomalies = result.get("anomalies")
        if not isinstance(anomalies, list):
            return None
        anomaly_count = len(anomalies)
    if anomaly_count != 0:
        return None

    overrides: dict[str, StepStatus] = {}
    for step in current_steps:
        dependencies = {
            str(item)
            for item in cast(list[object], step.definition.get("dependencies", []))
        }
        if (
            step.status == "pending"
            and completed_step.logical_id in dependencies
            and step.definition.get("capability") == "dataset.transform"
        ):
            overrides[step.logical_id] = "skipped"
    if not overrides:
        return None
    return overrides, "observation:no_anomalies:skip_conditional_transform"


def should_replan_failure(observation: JsonObject) -> bool:
    """只对结果已知且可恢复的执行失败触发自动重规划。"""
    if observation.get("retryable") is not True:
        return False
    return str(observation.get("code", "")) in {
        "tool_execution_failed",
        "tool_postcondition_failed",
    }


def _fallback_action(step: TaskStepRecord) -> str:
    raw_fallbacks = step.definition.get("fallback")
    if not isinstance(raw_fallbacks, list):
        return "block"
    for item in raw_fallbacks:
        if isinstance(item, dict) and isinstance(item.get("action"), str):
            return str(item["action"])
    return "block"


def _completed_step_validator(
    current_steps: list[TaskStepRecord],
) -> Callable[[JsonObject], tuple[str, ...]]:
    immutable = {
        step.logical_id: step.definition
        for step in current_steps
        if step.status in {"completed", "skipped"}
    }

    def validate(plan: JsonObject) -> tuple[str, ...]:
        definitions = {
            str(item.get("step_id")): item
            for item in cast(list[JsonObject], plan.get("steps", []))
        }
        issues: list[str] = []
        for logical_id, definition in immutable.items():
            revised = definitions.get(logical_id)
            if revised is None:
                issues.append(f"replan:missing_completed_step={logical_id}")
            elif revised != definition:
                issues.append(f"replan:changed_completed_step={logical_id}")
        return tuple(issues)

    return validate


def _safe_observation(observation: JsonObject) -> JsonObject:
    """只传递结构化状态和有界摘要，不把原始工具结果送给 Replanner。"""
    return {
        "observation_id": observation.get("observation_id"),
        "step_id": observation.get("step_id"),
        "source": observation.get("source"),
        "status": observation.get("status"),
        "code": observation.get("code"),
        "summary": str(observation.get("summary", ""))[:1000],
        "retryable": observation.get("retryable") is True,
        "payload_ref": observation.get("payload_ref"),
    }


def _deterministic_audit(
    plan: JsonObject,
    *,
    action: str,
    observation: JsonObject,
) -> JsonObject:
    encoded = json.dumps(
        plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return {
        "route": "template",
        "phase": "replanner",
        "action": action,
        "response_hash": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        "model": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost": None,
        "observation_id": observation.get("observation_id"),
        "observation_code": observation.get("code"),
    }
