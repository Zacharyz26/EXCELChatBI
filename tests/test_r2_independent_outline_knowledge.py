"""Cross-review of compiled request consumers; no executing tool/model stand-ins."""

from __future__ import annotations

import pytest
from apps.orchestrator.control.task_outline import build_deterministic_outline
from packages.orchestration.analysis_requirements import (
    ColumnRoleBinding,
    explicit_analysis_requirements,
)
from packages.orchestration.knowledge_intent import knowledge_query_matches

from tests.test_r2_outline_knowledge import _outline

COLUMNS = ("月份", "频道", "播出时长（分钟）")
ROLES = tuple(
    ColumnRoleBinding(name, role)
    for name, role in zip(COLUMNS, ("time", "dimension", "metric"), strict=True)
)


def request(goal):
    return explicit_analysis_requirements(
        goal, columns=COLUMNS, dataset_refs=("d" * 32,), column_roles=ROLES
    )


def test_mixed_query_and_data_sequence_has_distinct_source_owned_steps():
    goal = (
        "查询《公司规定》；筛选频道=A；查询《公司规定的附录》；"
        "删除播出时长（分钟）的空值；按整行去重。"
    )
    required = request(goal)
    steps = _outline(goal, required).plan["steps"]
    assert [step["capability"] for step in steps] == [
        "knowledge.search",
        "dataset.transform",
        "knowledge.search",
        "dataset.transform",
        "dataset.transform",
    ]
    assert len({step["step_id"] for step in steps}) == 5
    assert [step["step_id"] for step in steps if step["capability"] == "dataset.transform"] == [
        op.operation_id for op in required.active_operations()
    ]
    for earlier, later in zip(steps, steps[1:], strict=False):
        assert earlier["step_id"] in later["dependencies"]
    assert "公司规定的附录" in "".join(steps[2]["completion_conditions"])
    assert "公司规定的附录" not in "".join(steps[0]["completion_conditions"])


def test_typed_required_work_cannot_be_omitted_by_selected_capability_hint():
    goal = "筛选频道=A；按整行去重。"
    required = request(goal)
    plan = build_deterministic_outline(
        user_text=goal,
        context={"datasets": [], "artifacts": []},
        route="template",
        available_capabilities={"data.profile", "dataset.transform"},
        requested_capabilities=("data.profile",),
        analysis_requirements=required,
    )
    assert [
        step["step_id"] for step in plan["steps"] if step["capability"] == "dataset.transform"
    ] == [operation.operation_id for operation in required.active_operations()]


@pytest.mark.parametrize("limit", [1, 2, 4])
def test_mixed_required_work_never_truncates_to_step_budget(limit):
    goal = (
        "查询《公司规定》；筛选频道=A；查询《设备规定》；删除播出时长（分钟）的空值；按整行去重。"
    )
    with pytest.raises(ValueError, match="必要步骤"):
        _outline(goal, request(goal), max_steps=limit)


def test_statistical_context_and_object_specific_knowledge_ban_stay_local():
    goal = "按整行口径去重；不要查询《公司规定》；查询《设备维护规定》。"
    required = request(goal)
    assert [query.path for query in required.knowledge_queries] == [("设备维护规定",)]
    assert [query.path for query in required.knowledge_prohibitions] == [("公司规定",)]
    steps = _outline(goal, required).plan["steps"]
    assert [step["capability"] for step in steps] == ["dataset.transform", "knowledge.search"]
    assert not knowledge_query_matches("公司规定", required.knowledge_queries[0])


@pytest.mark.parametrize("provider", ["data.aggregate", "stats.contribution"])
def test_pure_total_preserves_existing_provider_without_fictitious_group(provider):
    goal = "给出播出时长（分钟）的总计。"
    required = request(goal)
    assert required.group_col is None and not required.operations
    steps = _outline(goal, required, capabilities=(provider,)).plan["steps"]
    assert [step["capability"] for step in steps] == [provider]
    assert "播出时长（分钟）" in "".join(steps[0]["completion_conditions"])


def test_similar_query_name_withdrawal_preserves_other_required_step():
    goal = "查询《公司规定》；查询《公司规定的附录》；撤回查询《公司规定》的要求。"
    required = request(goal)
    assert [query.path for query in required.knowledge_queries] == [("公司规定的附录",)]
    steps = _outline(goal, required).plan["steps"]
    assert len(steps) == 1 and steps[0]["capability"] == "knowledge.search"
    assert "公司规定的附录" in "".join(steps[0]["completion_conditions"])
