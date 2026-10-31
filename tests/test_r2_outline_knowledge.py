"""Independent R2 consumer contracts; no network or model fixtures."""

from __future__ import annotations

import hashlib
import importlib
from dataclasses import replace

import pytest
from apps.orchestrator.agent_tools import AgentToolRegistry, AgentToolSpec
from apps.orchestrator.control.task_outline import create_task_outline
from mcp_servers.common.contracts import ToolCapabilityMetadata
from packages.orchestration.contracts import SuccessCriterion, TaskContract
from packages.session.models import Artifact, Dataset


def knowledge():
    return importlib.import_module("packages.orchestration.knowledge_intent")


@pytest.mark.parametrize(
    "goal,active,prohibited",
    [
        ("请查询《公司规定》", {("公司规定",)}, set()),
        ("不要查询《公司规定》", set(), {("公司规定",)}),
        ("查询《公司规定的附录》；撤回查询《公司规定》的请求。", {("公司规定的附录",)}, set()),
        ("查询《公司规定》的附录；不要查询《公司规定》。", set(), {("公司规定",)}),
        ("不要查询《公司规定》；改为查询《公司规定》。", {("公司规定",)}, {("公司规定",)}),
        ("不要查询《公司规定》；解除不查询《公司规定》的限制。", set(), set()),
        ("只解释统计计数口径，不查询知识库。", set(), {("知识库",)}),
    ],
)
def test_final_knowledge_targets(goal, active, prohibited):
    result = knowledge().knowledge_query_requirements(goal)
    assert {q.path for q in result.active} == active
    assert {q.path for q in result.prohibited} == prohibited
    assert all(
        goal[a:b] and 0 <= a < b <= len(goal)
        for q in (*result.active, *result.prohibited)
        for a, b in q.source_spans
    )


def test_local_operation_scope_does_not_swallow_adjacent_query():
    text = "按整行口径去重；然后查询《公司规定》。"
    result = knowledge().knowledge_query_requirements(text, data_operation_spans=((0, 7),))
    assert {q.path for q in result.active} == {("公司规定",)}
    assert result.active[0].source_spans == ((8, len(text) - 1),)


def test_explicit_search_inside_operation_context_is_not_exempt():
    text = "查询去重口径的公司规定。"
    result = knowledge().knowledge_query_requirements(text, data_operation_spans=((0, len(text)),))
    assert len(result.active) == 1


def test_query_source_spans_survive_ignored_blocks():
    prefix = (
        "以下只是示例：\n请查询《被忽略的公司规定》。\n以上不是本次请求。\n"
        "```text\n不要查询《公司规定》\n```\n"
    )
    suffix = "请查询《公司规定》。"
    result = knowledge().knowledge_query_requirements(prefix + suffix)
    assert len(result.active) == 1
    assert result.active[0].path == ("公司规定",)
    assert result.active[0].source_spans == ((len(prefix), len(prefix + suffix) - 1),)


@pytest.mark.parametrize(
    "query,expected",
    [
        ("《公司规定》的附录", False),
        ("公司规定的附录", False),
        ("《公司规定的附录》", False),
        ("公司规定修订版", False),
        ("", False),
    ],
)
def test_literal_scope_matching_has_no_substring_authority(query, expected):
    k = knowledge()
    target = k.KnowledgeQuery(("公司规定",), False, ((0, 4),))
    assert k.knowledge_query_matches(query, target) is expected


def _requirements(goal, *, kinds=("filter", "drop_nulls", "drop_duplicates"), total=False):
    a = importlib.import_module("packages.orchestration.analysis_requirements")
    ops = []
    for index, kind in enumerate(kinds):
        params = (
            a.OperationParameters(filters=(a.FilterCondition("频道", "==", "A"),))
            if kind == "filter"
            else a.OperationParameters(columns=("播出时长（分钟）",))
            if kind == "drop_nulls"
            else a.OperationParameters(whole_row=True)
            if kind == "drop_duplicates"
            else a.OperationParameters()
        )
        ops.append(
            a.OperationRequirement(
                "op_" + str(index + 1) * 32,
                kind,
                "current",
                params,
                (index, index + 1),
                (0, len(goal)),
                index,
            )
        )
    filters = tuple(
        {"column": "频道", "op": "==", "value": "A"} for kind in kinds if kind == "filter"
    )
    return a.AnalysisRequirements(
        filters=filters,
        total=total,
        total_column="播出时长（分钟）" if total else None,
        dataset_ref="d" * 32,
        schema_version=2,
        source_hash=hashlib.sha256(goal.encode()).hexdigest(),
        source_length=len(goal),
        operations=tuple(ops),
        group_col="月份" if "aggregate" in kinds else None,
        value_col="播出时长（分钟）" if total or "aggregate" in kinds else None,
        aggregation="sum" if total or "aggregate" in kinds else None,
    )


def _outline(goal, requirements, *, capabilities=None, artifacts=(), max_steps=12):
    capabilities = capabilities or (
        "data.profile",
        "dataset.transform",
        "data.aggregate",
        "knowledge.search",
        "stats.trend",
    )
    registry = AgentToolRegistry(
        [
            AgentToolSpec(
                name="tool_" + str(i),
                description="test governed capability",
                parameters={"type": "object"},
                runner=lambda _: {},
                metadata=ToolCapabilityMetadata(capabilities=(cap,)),
            )
            for i, cap in enumerate(capabilities)
        ]
    )
    contract = TaskContract(
        "run",
        goal,
        (SuccessCriterion("answer", "response", "正确回答"),),
        (),
        analysis_requirements=requirements,
    )
    dataset = Dataset(
        ref="d" * 32,
        project_id="p",
        filename="broadcast.csv",
        profile={
            "row_count": 75,
            "columns": [{"name": "频道"}, {"name": "播出时长（分钟）"}, {"name": "月份"}],
        },
        parent_ref=None,
        transform=None,
        created_at="now",
    )
    return create_task_outline(
        user_text=goal,
        contract=contract,
        datasets=[dataset],
        artifacts=list(artifacts),
        registry=registry,
        blocking_clarification=None,
        max_steps=max_steps,
    )


def test_each_typed_operation_has_own_ordered_step():
    goal = "处理已确认的三个操作。"
    requirements = _requirements(goal)
    steps = _outline(goal, requirements).plan["steps"]
    assert [s["step_id"] for s in steps] == [op.operation_id for op in requirements.operations]
    assert [s["capability"] for s in steps] == ["dataset.transform"] * 3
    assert [s["dependencies"] for s in steps] == [[], [steps[0]["step_id"]], [steps[1]["step_id"]]]


def test_current_contract_is_not_reparsed_as_described_text():
    goal = "这些是清洗建议，稍后按月汇总。"
    requirements = _requirements(goal, kinds=())
    capabilities = _outline(goal, requirements).capabilities
    assert "dataset.transform" not in capabilities
    assert "data.aggregate" not in capabilities
    assert "stats.trend" not in capabilities


def test_grouped_aggregate_does_not_add_unrequested_trend():
    goal = "按月份汇总播出时长（分钟），并交付总计。"
    requirements = _requirements(goal, kinds=("aggregate",), total=True)
    steps = _outline(goal, requirements).plan["steps"]
    assert [(s["step_id"], s["capability"]) for s in steps] == [
        (requirements.operations[0].operation_id, "data.aggregate")
    ]
    assert "月份" in "".join(steps[0]["completion_conditions"])


def test_pure_total_has_tool_plan_without_group_obligation():
    goal = "给出播出时长（分钟）总计。"
    requirements = _requirements(goal, kinds=(), total=True)
    steps = _outline(goal, requirements).plan["steps"]
    assert requirements.group_col is None
    assert any(s["capability"] == "data.aggregate" for s in steps)
    assert any("总计" in "".join(s["completion_conditions"]) for s in steps)


def test_existing_table_cannot_delete_current_aggregate():
    goal = "把之前的结果重新按月份汇总。"
    requirements = _requirements(goal, kinds=("aggregate",))
    table = Artifact(
        id="old",
        conversation_id="c",
        message_id="m",
        type="table",
        payload={},
        file_ref=None,
        source_tool="aggregate_preview",
        params={},
        dataset_ref="x" * 32,
        created_at="now",
    )
    steps = _outline(goal, requirements, artifacts=(table,)).plan["steps"]
    assert [s["step_id"] for s in steps] == [requirements.operations[0].operation_id]


def test_required_operation_cannot_be_silently_omitted():
    goal = "执行已确认操作。"
    requirements = _requirements(goal, kinds=("drop_duplicates",))
    with pytest.raises(ValueError):
        _outline(goal, requirements, capabilities=("data.profile",))


def test_required_operation_count_cannot_be_truncated():
    goal = "依序执行已经确认的操作。"
    with pytest.raises(ValueError):
        _outline(goal, _requirements(goal), max_steps=2)


def test_contract_source_mismatch_never_falls_back():
    goal = "执行当前操作。"
    requirements = _requirements(goal)
    with pytest.raises(ValueError):
        _outline(goal, replace(requirements, source_hash="0" * 64))


@pytest.mark.parametrize(
    "active_path,ban_path,query,expected",
    [
        (("知识库",), ("公司规定",), "公司规定", False),
        (("公司规定",), ("知识库",), "公司规定", True),
        (("公司规定",), ("公司规定",), "公司规定", True),
        (("公司规定",), ("公司规定", "附录"), "《公司规定》的附录", False),
        (("公司规定", "附录"), ("公司规定",), "《公司规定》的附录", True),
    ],
)
def test_active_revision_must_be_as_specific_as_prohibition(active_path, ban_path, query, expected):
    k = knowledge()
    active = k.KnowledgeQuery(active_path, active_path == ("知识库",), ())
    banned = k.KnowledgeQuery(ban_path, ban_path == ("知识库",), ())
    assert k.knowledge_query_allowed(query, (active,), (banned,)) is expected


def test_withdrawn_request_span_does_not_support_later_reissued_request():
    prefix = "查询《公司规定》；撤回查询《公司规定》的要求；"
    final = "查询《公司规定》"
    result = knowledge().knowledge_query_requirements(prefix + final)
    assert len(result.active) == 1
    assert result.active[0].source_spans == ((len(prefix), len(prefix + final)),)


def test_pure_total_keeps_existing_contribution_provider():
    goal = "给出播出时长（分钟）总计。"
    requirements = _requirements(goal, kinds=(), total=True)
    steps = _outline(goal, requirements, capabilities=("stats.contribution",)).plan["steps"]
    assert [s["capability"] for s in steps] == ["stats.contribution"]
    assert requirements.group_col is None
    assert "总计" in "".join(steps[0]["completion_conditions"])


def test_each_required_knowledge_object_has_a_distinct_step():
    goal = "查询《公司规定》；查询《公司规定的附录》。"
    k = knowledge()
    intent = k.knowledge_query_requirements(goal)
    requirements = replace(_requirements(goal, kinds=()), knowledge_queries=intent.active)
    steps = _outline(goal, requirements).plan["steps"]
    ordered = sorted(intent.active, key=lambda query: min(query.source_spans))
    assert [s["step_id"] for s in steps] == [
        k.knowledge_query_step_id(q, requirements.source_hash) for q in ordered
    ]
    assert [s["capability"] for s in steps] == ["knowledge.search", "knowledge.search"]
    assert steps[1]["dependencies"] == [steps[0]["step_id"]]
    assert "公司规定的附录" in "".join(steps[1]["completion_conditions"])
