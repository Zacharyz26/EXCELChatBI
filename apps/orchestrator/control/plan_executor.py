"""阶段 2B 的确定性依赖图调度器。

Planner 负责描述步骤和依赖；本模块只根据持久化状态计算当前可执行集合，
不调用模型、不猜测依赖，也不把失败步骤静默视为完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from packages.session.task_models import TaskStepRecord


class CapabilityResolver(Protocol):
    """Executor 所需的最小工具能力解析接口。"""

    def capabilities_for_tool(self, tool_name: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class PlanSchedule:
    """一个计划版本在当前持久化状态下的调度快照。"""

    ready: tuple[TaskStepRecord, ...]
    waiting: tuple[TaskStepRecord, ...]
    running: tuple[TaskStepRecord, ...]
    completed: tuple[TaskStepRecord, ...]
    failed: tuple[TaskStepRecord, ...]
    blocked: tuple[TaskStepRecord, ...]

    @property
    def ready_capabilities(self) -> set[str]:
        return {
            str(step.definition["capability"])
            for step in self.ready
        }

    @property
    def all_finished(self) -> bool:
        return not (
            self.ready
            or self.waiting
            or self.running
            or self.failed
            or self.blocked
        )

    @property
    def deadlocked(self) -> bool:
        """没有运行/就绪步骤但仍有未完成步骤时，必须重规划或阻塞。"""
        return not self.ready and not self.running and bool(
            self.waiting or self.failed or self.blocked
        )


def schedule_plan_steps(steps: list[TaskStepRecord]) -> PlanSchedule:
    """按依赖和持久状态计算就绪步骤，保持计划中的原始顺序。"""
    known = {step.logical_id for step in steps}
    satisfied = {
        step.logical_id
        for step in steps
        if step.status in {"completed", "skipped"}
    }
    ready: list[TaskStepRecord] = []
    waiting: list[TaskStepRecord] = []
    running: list[TaskStepRecord] = []
    completed: list[TaskStepRecord] = []
    failed: list[TaskStepRecord] = []
    blocked: list[TaskStepRecord] = []

    for step in steps:
        dependencies = {
            str(item)
            for item in step.definition.get("dependencies", [])
            if isinstance(item, str)
        }
        if not dependencies.issubset(known):
            # TaskPlan 在入库前已经校验；若持久数据仍出现未知依赖，必须 fail-closed。
            blocked.append(step)
        elif step.status in {"completed", "skipped"}:
            completed.append(step)
        elif step.status == "running":
            running.append(step)
        elif step.status == "failed":
            failed.append(step)
        elif step.status == "blocked":
            blocked.append(step)
        elif dependencies.issubset(satisfied):
            ready.append(step)
        else:
            waiting.append(step)

    return PlanSchedule(
        ready=tuple(ready),
        waiting=tuple(waiting),
        running=tuple(running),
        completed=tuple(completed),
        failed=tuple(failed),
        blocked=tuple(blocked),
    )


def match_ready_step(
    *,
    tool_name: str,
    schedule: PlanSchedule,
    resolver: CapabilityResolver,
    offered_step_ids: set[str],
) -> TaskStepRecord | None:
    """把调用绑定到本轮已显式开放、且当前仍就绪的第一个匹配步骤。"""
    tool_capabilities = set(resolver.capabilities_for_tool(tool_name))
    return next(
        (
            step
            for step in schedule.ready
            if step.step_id in offered_step_ids
            and str(step.definition.get("capability")) in tool_capabilities
        ),
        None,
    )


def schedule_payload(schedule: PlanSchedule) -> dict[str, list[str]]:
    """生成可写入事件/模型指令的有界逻辑步骤摘要。"""
    return {
        "ready": [step.logical_id for step in schedule.ready],
        "waiting": [step.logical_id for step in schedule.waiting],
        "running": [step.logical_id for step in schedule.running],
        "completed": [step.logical_id for step in schedule.completed],
        "failed": [step.logical_id for step in schedule.failed],
        "blocked": [step.logical_id for step in schedule.blocked],
    }
