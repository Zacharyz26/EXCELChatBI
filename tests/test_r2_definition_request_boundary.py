"""Frozen request grammar and definition-plan tests; no business model calls."""

from dataclasses import replace

import pytest
from packages.orchestration.knowledge_intent import (
    KnowledgeQuery,
    knowledge_query_matches,
    knowledge_query_requirements,
    knowledge_query_step_id,
)

from tests.test_r2_outline_knowledge import _outline, _requirements


@pytest.mark.parametrize("suffix", [" 怎么定义", "如何定义", "的定义是什么", "定义是什么"])
def test_request_predicate_is_not_part_of_semantic_key(suffix):
    goal = "metric.grouped_measure" + suffix + "？"
    queries = knowledge_query_requirements(goal).active
    assert len(queries) == 1
    assert queries[0].path == ("metric.grouped_measure",)
    assert queries[0].source_spans == ((0, len(goal) - 1),)
    assert knowledge_query_matches("metric.grouped_measure", queries[0])
    assert not knowledge_query_matches("metric.grouped_measure_v2", queries[0])
    # Actual provider arguments retain literal identity: no request grammar there.
    assert not knowledge_query_matches(goal[:-1], queries[0])


@pytest.mark.parametrize("opening,closing", [("《", "》"), ('"', '"')])
def test_quoted_literal_predicate_words_are_not_removed(opening, closing):
    name = "metric.grouped_measure 怎么定义"
    queries = knowledge_query_requirements(f"查询{opening}{name}{closing}。").active
    assert [query.path for query in queries] == [(name,)]
    assert not knowledge_query_matches("metric.grouped_measure", queries[0])


def test_external_predicate_can_follow_a_complete_quoted_literal():
    goal = "《metric.grouped_measure 怎么定义》的定义是什么？"
    queries = knowledge_query_requirements(goal).active
    assert [query.path for query in queries] == [("metric.grouped_measure 怎么定义",)]


def test_request_revision_preserves_exact_object_identity():
    intent = knowledge_query_requirements(
        "不要查询metric.grouped_measure怎么定义；" "改为查询metric.grouped_measure_v2怎么定义。"
    )
    assert [query.path for query in intent.active] == [("metric.grouped_measure_v2",)]
    assert [query.path for query in intent.prohibited] == [("metric.grouped_measure",)]


def test_non_request_literal_title_is_not_globally_stripped():
    required = KnowledgeQuery(("季度定义",), False, ((0, 4),))
    assert knowledge_query_matches("季度定义", required)
    assert not knowledge_query_matches("季度", required)


def _definition_requirements():
    goal = "按metric.grouped_measure汇总数据。"
    required = _requirements(goal, kinds=("aggregate",))
    verb = goal.index("汇总")
    operation = replace(required.operations[0], span=(verb, verb + 2))
    query = KnowledgeQuery(("metric.grouped_measure",), False, ((1, verb),))
    return goal, replace(
        required,
        operations=(
            replace(
                operation,
                parameters=replace(operation.parameters, definition_key="metric.grouped_measure"),
            ),
        ),
        knowledge_queries=(query,),
    )


def test_definition_driven_plan_requires_existing_resolve_capability_first():
    goal, required = _definition_requirements()
    steps = _outline(
        goal, required, capabilities=("knowledge.definition.resolve", "data.aggregate")
    ).plan["steps"]
    assert [step["capability"] for step in steps] == [
        "knowledge.definition.resolve",
        "data.aggregate",
    ]
    assert steps[0]["step_id"] == knowledge_query_step_id(
        required.knowledge_queries[0], required.source_hash
    )
    assert steps[1]["dependencies"] == [steps[0]["step_id"]]


def test_ordinary_search_provider_cannot_replace_definition_resolution():
    goal, required = _definition_requirements()
    with pytest.raises(ValueError, match="能力不可用"):
        _outline(goal, required, capabilities=("knowledge.search", "data.aggregate"))


def test_definition_and_unrelated_knowledge_have_distinct_capabilities():
    goal, required = _definition_requirements()
    required = replace(
        required,
        knowledge_queries=(
            *required.knowledge_queries,
            KnowledgeQuery(("公司规定",), False, ((len(goal) - 2, len(goal) - 1),)),
        ),
    )
    steps = _outline(
        goal,
        required,
        capabilities=("knowledge.search", "knowledge.definition.resolve", "data.aggregate"),
    ).plan["steps"]
    assert [step["capability"] for step in steps] == [
        "knowledge.definition.resolve",
        "data.aggregate",
        "knowledge.search",
    ]
