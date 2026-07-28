"""阶段 2B 依赖图调度器测试。"""

from __future__ import annotations

from apps.orchestrator.control.plan_executor import (
    match_ready_step,
    schedule_plan_steps,
)
from packages.session.task_models import StepStatus, TaskStepRecord


def _step(
    logical_id: str,
    *,
    capability: str,
    dependencies: list[str],
    status: StepStatus = "pending",
    position: int = 0,
) -> TaskStepRecord:
    return TaskStepRecord(
        step_id=f"db-{logical_id}",
        plan_id="plan",
        run_id="run",
        position=position,
        logical_id=logical_id,
        status=status,
        definition={
            "step_id": logical_id,
            "purpose": logical_id,
            "capability": capability,
            "dependencies": dependencies,
        },
        started_at=None,
        completed_at=None,
    )


class _Resolver:
    def capabilities_for_tool(self, tool_name: str) -> tuple[str, ...]:
        return {
            "profile": ("data.profile",),
            "trend": ("stats.trend",),
        }.get(tool_name, ())


def test_scheduler_only_releases_steps_whose_dependencies_are_satisfied() -> None:
    profile = _step(
        "profile",
        capability="data.profile",
        dependencies=[],
        position=0,
    )
    trend = _step(
        "trend",
        capability="stats.trend",
        dependencies=["profile"],
        position=1,
    )

    initial = schedule_plan_steps([profile, trend])
    after_profile = schedule_plan_steps(
        [
            _step(
                "profile",
                capability="data.profile",
                dependencies=[],
                status="completed",
                position=0,
            ),
            trend,
        ]
    )

    assert [item.logical_id for item in initial.ready] == ["profile"]
    assert [item.logical_id for item in initial.waiting] == ["trend"]
    assert [item.logical_id for item in after_profile.ready] == ["trend"]


def test_failed_dependency_never_unlocks_downstream_step() -> None:
    schedule = schedule_plan_steps(
        [
            _step(
                "profile",
                capability="data.profile",
                dependencies=[],
                status="failed",
            ),
            _step(
                "trend",
                capability="stats.trend",
                dependencies=["profile"],
                position=1,
            ),
        ]
    )

    assert schedule.ready == ()
    assert [item.logical_id for item in schedule.failed] == ["profile"]
    assert [item.logical_id for item in schedule.waiting] == ["trend"]
    assert schedule.deadlocked is True


def test_skipped_dependency_is_explicitly_satisfied() -> None:
    schedule = schedule_plan_steps(
        [
            _step(
                "optional_transform",
                capability="dataset.transform",
                dependencies=[],
                status="skipped",
            ),
            _step(
                "trend",
                capability="stats.trend",
                dependencies=["optional_transform"],
                position=1,
            ),
        ]
    )

    assert [item.logical_id for item in schedule.ready] == ["trend"]


def test_tool_can_only_bind_to_a_step_offered_at_start_of_round() -> None:
    profile = _step("profile", capability="data.profile", dependencies=[])
    schedule = schedule_plan_steps([profile])

    accepted = match_ready_step(
        tool_name="profile",
        schedule=schedule,
        resolver=_Resolver(),
        offered_step_ids={profile.step_id},
    )
    rejected = match_ready_step(
        tool_name="profile",
        schedule=schedule,
        resolver=_Resolver(),
        offered_step_ids=set(),
    )

    assert accepted == profile
    assert rejected is None
