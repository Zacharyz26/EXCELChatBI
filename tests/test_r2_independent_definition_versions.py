"""Independent real versioned definitions: a model cannot select by output fit."""

import json
from dataclasses import asdict, replace

import pytest
from apps.api.deps import model_gateway_dep
from fastapi.testclient import TestClient
from packages.governance.permissions import Principal
from packages.knowledge.domain_store import DomainDefinitionStore
from packages.session.task_store import TaskStore

from tests.test_domain_definitions import _draft
from tests.test_r2_definition_contract import DefinitionCommands, no_network  # noqa: F401
from tests.test_r2_real_paths import METRIC, execute, upload


@pytest.mark.asyncio
async def test_two_real_versions_cannot_be_selected_by_matching_aggregate_arguments(tmp_path):
    await _exercise_definition_choice(tmp_path, single_historical=False)


@pytest.mark.asyncio
async def test_single_matching_historical_version_is_not_user_authorized(tmp_path):
    await _exercise_definition_choice(tmp_path, single_historical=True)


async def _exercise_definition_choice(tmp_path, *, single_historical):
    app, store, project, conversation, ref = upload(tmp_path)
    definitions = DomainDefinitionStore(store)
    principal = Principal("local-user")
    template = _draft()
    created = []
    for version, method, start, end in (
        (1, "sum" if single_historical else "mean", "2026-01-01T00:00:00Z", "2026-07-01T00:00:00Z"),
        (2, "mean" if single_historical else "sum", "2026-07-01T00:00:00Z", None),
    ):
        result = definitions.create_definition(
            project_id=project.id,
            principal=principal,
            draft=replace(
                template,
                version=version,
                effective_from=start,
                effective_to=end,
                formula={
                    **template.formula,
                    "arguments": {**template.formula["arguments"], "agg": method},
                },
            ),
            idempotency_key=f"independent-real-version-{version}",
        )
        assert result.outcome == "created"
        created.append(result.definition)
    for concept, field in (("dimension.bucket", "月份"), ("measure.value", METRIC)):
        definitions.register_field_mapping(
            project_id=project.id,
            dataset_ref=ref,
            concept_key=concept,
            field_name=field,
            source_ref="urn:independent:" + concept,
            principal=principal,
        )
    aggregate = {
        "dataset_ref": ref,
        "group_col": "月份",
        "value_col": METRIC,
        "agg": "sum",
        "sort": "group",
        "limit": 100,
    }
    commands = [
        (
            "domain_definition_lookup",
            {
                "semantic_key": template.semantic_key,
                "dataset_ref": ref,
                "as_of": "2026-06-01T00:00:00Z",
            },
        )
    ]
    if not single_historical:
        commands.append(
            (
                "domain_definition_lookup",
                {
                    "semantic_key": template.semantic_key,
                    "dataset_ref": ref,
                    "as_of": "2026-08-01T00:00:00Z",
                },
            ),
        )
    commands.append(("aggregate_preview", aggregate))
    gateway = DefinitionCommands(commands)
    initial = await execute(
        store, project, conversation, gateway, "按 metric.grouped_measure 汇总数据"
    )
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run.status == "waiting_user" and gateway.calls == 0, initial
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert waiting["about"] == "analysis_group_col", waiting
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            f'/agent/runs/{run.run_id}/clarifications/{waiting["question_id"]}/answer/stream',
            headers={"If-Match": str(run.state_version), "Idempotency-Key": "independent-versions"},
            json={"answer": "月份", "resume_token": waiting["resume_token"]},
        )
    final = tasks.get_run(run.run_id)
    calls = tasks.list_invocations(run.run_id)
    evidence = tasks.list_evidence(run.run_id)
    witness = {
        "definitions": [asdict(item) for item in created],
        "goal": tasks.get_verified_contract(run.run_id)["goal"],
        "run": asdict(final),
        "calls": [asdict(item) for item in calls],
        "evidence": [asdict(item) for item in evidence],
        "sse": response.text,
        "real_model_calls": 0,
    }
    (tmp_path / "two-real-versions-witness.json").write_text(
        json.dumps(witness, ensure_ascii=False, indent=2)
    )
    assert response.status_code == 200, witness
    lookups = [
        call
        for call in calls
        if call.tool_name == "domain_definition_lookup" and call.status == "succeeded"
    ]
    assert len(lookups) == (1 if single_historical else 2), witness
    resources = [
        item.source["definition_resource"]
        for item in evidence
        if "definition_resource" in item.source
    ]
    assert {item["definition_version"] for item in resources} == (
        {1} if single_historical else {1, 2}
    ), witness
    assert len({item["formula_hash"] for item in resources}) == (
        1 if single_historical else 2
    ), witness
    # Neither the user request nor its group-only answer chooses an effective version.
    assert not [
        call
        for call in calls
        if call.tool_name == "aggregate_preview" and call.status == "succeeded"
    ], witness
    assert final.status != "completed", witness
