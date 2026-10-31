"""Independent Host capability ownership: search is not definition resolution."""

import json
from dataclasses import asdict

import pytest
from apps.api.deps import model_gateway_dep
from fastapi.testclient import TestClient
from packages.session.task_store import TaskStore

from tests.test_r2_definition_contract import (
    KEY,
    METRIC,
    DefinitionCommands,
    execute,
    no_network,  # noqa: F401
    workspace,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("resolve_definition", [False, True])
async def test_same_object_search_cannot_consume_the_required_definition_step(
    tmp_path, resolve_definition
):
    app, store, project, conversation, ref, _ = workspace(tmp_path)
    commands = [("kb_search", {"query": KEY})]
    if resolve_definition:
        commands.append(("domain_definition_lookup", {"semantic_key": KEY, "dataset_ref": ref}))
    commands.append(
        (
            "aggregate_preview",
            {
                "dataset_ref": ref,
                "group_col": "月份",
                "value_col": METRIC,
                "agg": "sum",
                "sort": "group",
                "limit": 100,
            },
        )
    )
    gateway = DefinitionCommands(commands)
    goal = "按 metric.grouped_measure 汇总数据"
    await execute(store, project, conversation, gateway, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run.status == "waiting_user" and gateway.calls == 0
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            f'/agent/runs/{run.run_id}/clarifications/{waiting["question_id"]}/answer/stream',
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "independent-provider-answer",
            },
            json={"answer": "月份", "resume_token": waiting["resume_token"]},
        )
    final = tasks.get_run(run.run_id)
    calls = tasks.list_invocations(run.run_id)
    steps = tasks.list_plan_steps(run.run_id)
    witness = {
        "goal": goal,
        "resolve_definition": resolve_definition,
        "run": asdict(final),
        "calls": [asdict(call) for call in calls],
        "steps": [asdict(step) for step in steps],
        "contract": tasks.get_verified_contract(run.run_id),
        "evidence": [asdict(record) for record in tasks.list_evidence(run.run_id)],
        "api_status": response.status_code,
        "api_sse": response.text,
        "paid_calls": 0,
    }
    (tmp_path / "independent-definition-provider.json").write_text(
        json.dumps(witness, ensure_ascii=False, indent=2)
    )
    assert response.status_code == 200, witness
    search = [call for call in calls if call.tool_name == "kb_search"]
    assert search and search[0].status == "succeeded" and search[0].step_id is None, witness
    required = [
        step
        for step in steps
        if step.definition.get("capability") == "knowledge.definition.resolve"
    ]
    assert len(required) == 1, witness
    assert (required[0].status == "completed") is resolve_definition, witness
    assert (final.status == "completed") is resolve_definition, witness
    definition = [call for call in calls if call.tool_name == "domain_definition_lookup"]
    if resolve_definition:
        assert len(definition) == 1 and definition[0].step_id == required[0].step_id, witness
    assert (
        any(call.tool_name == "aggregate_preview" and call.status == "succeeded" for call in calls)
        is resolve_definition
    ), witness
