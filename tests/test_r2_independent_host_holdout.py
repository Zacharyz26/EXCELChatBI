"""Independent Host / answer-transaction checks; local scripted model only."""

from __future__ import annotations

import json
import socket
import sqlite3
from dataclasses import asdict

import pytest
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.orchestration.analysis_requirements import AnalysisRequirements
from packages.orchestration.knowledge_intent import knowledge_query_step_id
from packages.session.task_store import ControlConflict, IdempotencyConflict, TaskStore

from tests.test_r2_analysis_clarification import answer, waiting_fixture
from tests.test_r2_real_paths import execute, upload


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Independent R2 review forbids network and paid models")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


def database_dump(store):
    with sqlite3.connect(store.db_path) as connection:
        return tuple(connection.iterdump())


def test_replay_with_changed_answer_never_mutates_contract_or_any_row(tmp_path):
    store, tasks, run, payload, _, _ = waiting_fixture(tmp_path)
    answer(tasks, run, payload, value="月份", key="same-key")
    before = database_dump(store)
    with pytest.raises(IdempotencyConflict):
        answer(tasks, run, payload, value="频道", key="same-key")
    assert database_dump(store) == before


def test_old_answer_cannot_be_reapplied_with_fresh_idempotency_key(tmp_path):
    store, tasks, run, payload, _, _ = waiting_fixture(tmp_path)
    updated, _, _ = answer(tasks, run, payload, key="first-answer")
    before = database_dump(store)
    with pytest.raises(ControlConflict):
        tasks.answer_clarification(
            run.run_id,
            expected_version=updated.state_version,
            idempotency_key="new-key",
            question_id=payload["question_id"],
            resume_token=payload["resume_token"],
            answer="月份",
        )
    assert database_dump(store) == before


def test_error_after_contract_update_rolls_back_the_entire_answer(tmp_path, monkeypatch):
    import packages.session.task_store as module

    store, tasks, run, payload, _, _ = waiting_fixture(tmp_path)
    before = database_dump(store)

    def reject_later_role_gate(*args, **kwargs):
        raise ControlConflict("independent injected later validator rejection")

    monkeypatch.setattr(module, "_validated_data_role_confirmation", reject_later_role_gate)
    with pytest.raises(ControlConflict, match="independent injected"):
        answer(tasks, run, payload)
    assert database_dump(store) == before


def test_contract_corruption_is_rejected_without_additional_state_change(tmp_path):
    store, tasks, run, payload, _, _ = waiting_fixture(tmp_path)
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            "UPDATE task_contracts SET contract_hash=? WHERE run_id=?", ("0" * 64, run.run_id)
        )
    before = database_dump(store)
    with pytest.raises(ControlConflict):
        answer(tasks, run, payload)
    assert database_dump(store) == before


class OrderedLocalQueries:
    """Only model response is scripted; domain lookup executes its real local store."""

    def __init__(self, tool, reversed_order):
        self.tool = tool
        self.queries = ["policy_a", "policy_b"]
        if reversed_order:
            self.queries.reverse()
        self.calls = 0

    async def stream_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 4:
            raise ModelBudgetExceeded("Offline independent query fixture exhausted")
        if self.calls <= 2:
            key = "query" if self.tool == "kb_search" else "semantic_key"
            yield ModelResponse(
                content="",
                model="independent-offline",
                tool_calls=[
                    ToolCall(
                        f"independent-query-{self.calls}",
                        self.tool,
                        json.dumps({key: self.queries[self.calls - 1]}, ensure_ascii=False),
                    )
                ],
            )
        else:
            yield ModelResponse(
                content="未找到匹配资料，不能据此回答规定内容。", model="independent-offline"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["kb_search", "domain_definition_lookup"])
@pytest.mark.parametrize("reversed_order", [False, True])
async def test_every_knowledge_provider_obeys_exact_ready_object(tmp_path, tool, reversed_order):
    _, store, project, conversation, _ = upload(tmp_path)
    gateway = OrderedLocalQueries(tool, reversed_order)
    events = await execute(
        store,
        project,
        conversation,
        gateway,
        "先查询《policy_a》，再查询《policy_b》。",
    )
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    invocations = tasks.list_invocations(run.run_id)
    succeeded = [i for i in invocations if i.status == "succeeded"]
    key = "query" if tool == "kb_search" else "semantic_key"
    successful_queries = [i.args.get(key) for i in succeeded if i.tool_name == tool]
    (tmp_path / "independent-knowledge-witness.json").write_text(
        json.dumps(
            {
                "tool": tool,
                "reversed": reversed_order,
                "run": asdict(run),
                "events": events,
                "invocations": [asdict(i) for i in invocations],
                "steps": [asdict(s) for s in tasks.list_plan_steps(run.run_id)],
                "paid_model_calls": 0,
                "kb_search_retriever": "labeled local empty fixture",
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    if reversed_order:
        assert "policy_b" not in successful_queries
        assert run.status != "completed"
    else:
        assert successful_queries == ["policy_a", "policy_b"]
        assert run.status == "completed"


class ExtraLocalQueryThenRequired:
    def __init__(self):
        self.calls = 0

    async def stream_turn(self, *args, **kwargs):
        self.calls += 1
        commands = [
            ("domain_definition_lookup", {"semantic_key": "policy_extra"}),
            ("kb_search", {"query": "policy_a"}),
            ("kb_search", {"query": "policy_b"}),
        ]
        if self.calls > 5:
            raise ModelBudgetExceeded("Independent optional-query fixture exhausted")
        if self.calls <= len(commands):
            tool, arguments = commands[self.calls - 1]
            yield ModelResponse(
                content="",
                model="independent-offline",
                tool_calls=[ToolCall(f"mixed-query-{self.calls}", tool, json.dumps(arguments))],
            )
        else:
            yield ModelResponse(
                content="未找到匹配资料，不能据此回答规定内容。", model="independent-offline"
            )


@pytest.mark.asyncio
async def test_unrelated_domain_query_cannot_consume_a_required_knowledge_step(tmp_path):
    _, store, project, conversation, _ = upload(tmp_path)
    events = await execute(
        store,
        project,
        conversation,
        ExtraLocalQueryThenRequired(),
        "先查询《policy_a》，再查询《policy_b》。",
    )
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    calls = tasks.list_invocations(run.run_id)
    steps = tasks.list_plan_steps(run.run_id)
    required = AnalysisRequirements.from_dict(
        tasks.get_verified_contract(run.run_id)["analysis_requirements"]
    )
    ids = {
        q.path[0]: knowledge_query_step_id(q, required.source_hash)
        for q in required.knowledge_queries
    }
    # Physical TaskPlanStep identity, rather than capability name, owns a proof.
    step_by_id = {s.step_id: s.logical_id for s in steps}
    (tmp_path / "optional-query-witness.json").write_text(
        json.dumps(
            {
                "events": events,
                "run": asdict(run),
                "calls": [asdict(i) for i in calls],
                "steps": [asdict(s) for s in steps],
                "expected_query_ids": ids,
                "paid_model_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    for invocation in calls:
        if invocation.args.get("semantic_key") == "policy_extra":
            assert step_by_id.get(invocation.step_id) not in set(ids.values())
    for name in ("policy_a", "policy_b"):
        successes = [i for i in calls if i.status == "succeeded" and i.args.get("query") == name]
        assert len(successes) == 1
        assert step_by_id.get(successes[0].step_id) == ids[name]
    assert run.status == "completed"


class DefinitionFallbackQueries:
    def __init__(self, include_second_object):
        self.commands = [
            ("domain_definition_lookup", {"semantic_key": "policy_a"}),
            ("kb_search", {"query": "policy_a"}),
        ]
        if include_second_object:
            self.commands.append(("kb_search", {"query": "policy_b"}))
        self.calls = 0

    async def stream_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls > len(self.commands) + 2:
            raise ModelBudgetExceeded("Independent definition fallback fixture exhausted")
        if self.calls <= len(self.commands):
            tool, arguments = self.commands[self.calls - 1]
            yield ModelResponse(
                content="",
                model="independent-offline",
                tool_calls=[ToolCall(f"fallback-{self.calls}", tool, json.dumps(arguments))],
            )
        else:
            yield ModelResponse(
                content="未找到匹配资料，不能据此回答规定内容。", model="independent-offline"
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("include_second_object", [False, True])
async def test_existing_definition_to_search_fallback_preserves_other_obligations(
    tmp_path, include_second_object
):
    # Existing Agent system contract: missing governed definition -> KB lookup.
    _, store, project, conversation, _ = upload(tmp_path)
    events = await execute(
        store,
        project,
        conversation,
        DefinitionFallbackQueries(include_second_object),
        "先查询《policy_a》，再查询《policy_b》。",
    )
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    calls = tasks.list_invocations(run.run_id)
    steps = tasks.list_plan_steps(run.run_id)
    required = AnalysisRequirements.from_dict(
        tasks.get_verified_contract(run.run_id)["analysis_requirements"]
    )
    expected_ids = {
        q.path[0]: knowledge_query_step_id(q, required.source_hash)
        for q in required.knowledge_queries
    }
    step_by_id = {s.step_id: s.logical_id for s in steps}
    (tmp_path / "fallback-query-witness.json").write_text(
        json.dumps(
            {
                "run": asdict(run),
                "calls": [asdict(i) for i in calls],
                "events": events,
                "steps": [asdict(s) for s in steps],
                "include_second_object": include_second_object,
                "paid_model_calls": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    first = [
        i
        for i in calls
        if i.status == "succeeded"
        and i.tool_name == "domain_definition_lookup"
        and i.args.get("semantic_key") == "policy_a"
    ]
    fallback = [
        i
        for i in calls
        if i.status == "succeeded"
        and i.tool_name == "kb_search"
        and i.args.get("query") == "policy_a"
    ]
    assert len(first) == len(fallback) == 1
    assert step_by_id.get(first[0].step_id) == expected_ids["policy_a"]
    assert step_by_id.get(fallback[0].step_id) != expected_ids["policy_b"]
    assert (run.status == "completed") is include_second_object
    if include_second_object:
        second = [i for i in calls if i.status == "succeeded" and i.args.get("query") == "policy_b"]
        assert len(second) == 1
        assert step_by_id.get(second[0].step_id) == expected_ids["policy_b"]
