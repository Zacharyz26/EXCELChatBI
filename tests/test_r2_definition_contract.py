"""Frozen diagnosis: real uploaded dataset, DomainDefinitionStore and Host.

Only the model is scripted; no response/Evidence/state is injected into Host.
"""

import copy
import json
import socket
from dataclasses import asdict, replace

import pytest
from apps.api.deps import model_gateway_dep
from apps.orchestrator.agent_tools import AgentContext
from fastapi.testclient import TestClient
from packages.governance.permissions import Principal
from packages.knowledge.domain_store import DomainDefinitionStore
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.orchestration.analysis_requirements import AnalysisRequirements
from packages.orchestration.dataset_scope import operation_admission_error, operation_progress
from packages.session.task_store import TaskStore
from sse_starlette.sse import AppStatus

from tests.test_agent_tools import _registry
from tests.test_domain_definitions import _draft
from tests.test_r2_real_paths import METRIC, execute, upload

KEY = "metric.grouped_measure"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("offline definition probe prohibits external services")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    AppStatus.should_exit_event = None
    yield
    AppStatus.should_exit_event = None


def workspace(tmp_path, aggregation="sum"):
    app, store, project, conversation, ref = upload(tmp_path)
    definitions = DomainDefinitionStore(store)
    principal = Principal("local-user")
    draft = _draft()
    draft = replace(
        draft,
        formula={**draft.formula, "arguments": {**draft.formula["arguments"], "agg": aggregation}},
    )
    definition = definitions.create_definition(
        project_id=project.id,
        principal=principal,
        draft=draft,
        idempotency_key="real-definition-v1",
    ).definition
    for concept, field in (("dimension.bucket", "月份"), ("measure.value", METRIC)):
        definitions.register_field_mapping(
            project_id=project.id,
            dataset_ref=ref,
            concept_key=concept,
            field_name=field,
            source_ref="urn:probe:" + concept,
            principal=principal,
        )
    return app, store, project, conversation, ref, definition


class DefinitionThenCompute:
    def __init__(self, ref, aggregate):
        self.ref, self.aggregate, self.calls = ref, aggregate, 0

    async def stream_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 4:
            raise ModelBudgetExceeded("offline definition probe exhausted")
        if self.calls == 1:
            yield ModelResponse(
                content="",
                model="offline-real-domain",
                tool_calls=[
                    ToolCall(
                        "lookup",
                        "domain_definition_lookup",
                        json.dumps({"semantic_key": KEY, "dataset_ref": self.ref}),
                    )
                ],
            )
        elif self.calls == 2 and self.aggregate:
            yield ModelResponse(
                content="",
                model="offline-real-domain",
                tool_calls=[
                    ToolCall(
                        "aggregate",
                        "aggregate_preview",
                        json.dumps(
                            {
                                "dataset_ref": self.ref,
                                "group_col": "月份",
                                "value_col": METRIC,
                                "agg": "sum",
                                "sort": "group",
                                "limit": 100,
                            }
                        ),
                    )
                ],
            )
        else:
            yield ModelResponse(
                content="已查询项目内定义；来源：urn:domain-definition:grouped-measure:v1。",
                model="offline-real-domain",
            )


@pytest.mark.asyncio
async def test_real_definition_fixture_compiles_exact_existing_tool_contract(tmp_path):
    _, store, project, conversation, ref, definition = workspace(tmp_path)
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        result = registry.execute(
            "domain_definition_lookup", json.dumps({"semantic_key": KEY, "dataset_ref": ref})
        )
    finally:
        await registry.aclose()
    (tmp_path / "control-tool.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert result["status"] == "resolved" and not result["requires_clarification"]
    assert result["compiled_invocation"]["definition_id"] == definition.definition_id
    assert result["compiled_invocation"]["tool_name"] == "aggregate_preview"
    assert result["compiled_invocation"]["arguments"] == {
        "dataset_ref": ref,
        "group_col": "月份",
        "value_col": METRIC,
        "agg": "sum",
        "sort": "group",
        "limit": 100,
    }


@pytest.mark.asyncio
async def test_original_definition_question_binds_the_exact_subject(tmp_path):
    goal, aggregate = "metric.grouped_measure 怎么定义？", False
    _, store, project, conversation, ref, _ = workspace(tmp_path)
    gateway = DefinitionThenCompute(ref, aggregate)
    events = await execute(store, project, conversation, gateway, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    calls = tasks.list_invocations(run.run_id)
    payload = tasks.get_verified_contract(run.run_id)
    requirements = AnalysisRequirements.from_dict(payload["analysis_requirements"])
    witness = {
        "goal": goal,
        "events": events,
        "run": asdict(run),
        "contract": payload,
        "calls": [asdict(c) for c in calls],
        "evidence": [asdict(e) for e in tasks.list_evidence(run.run_id)],
        "model": "offline scripted",
        "paid_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    # The existing authenticated definition resolution must be reachable first.
    assert any(
        c.tool_name == "domain_definition_lookup" and c.status == "succeeded" for c in calls
    ), witness
    assert any(q.path == (KEY,) for q in requirements.knowledge_queries), witness
    assert run.status == "completed", witness


class WrongSumAfterClarification:
    def __init__(self, ref, skip_lookup):
        self.ref, self.skip_lookup, self.calls = ref, skip_lookup, 0

    async def stream_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls > 4:
            raise ModelBudgetExceeded("offline definition safety probe exhausted")
        if self.calls == 1 and not self.skip_lookup:
            tool, params = (
                "domain_definition_lookup",
                {"semantic_key": KEY, "dataset_ref": self.ref},
            )
        elif self.calls == (1 if self.skip_lookup else 2):
            tool, params = (
                "aggregate_preview",
                {
                    "dataset_ref": self.ref,
                    "group_col": "月份",
                    "value_col": METRIC,
                    "agg": "sum",
                    "sort": "group",
                    "limit": 100,
                },
            )
        else:
            yield ModelResponse(content="已完成分组汇总。", model="offline-definition-safety")
            return
        yield ModelResponse(
            content="",
            model="offline-definition-safety",
            tool_calls=[ToolCall("safety-" + str(self.calls), tool, json.dumps(params))],
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("skip_lookup", [False, True])
async def test_group_answer_does_not_authorize_ignoring_registered_definition(
    tmp_path, skip_lookup
):
    app, store, project, conversation, ref, definition = workspace(tmp_path, aggregation="mean")
    gateway = WrongSumAfterClarification(ref, skip_lookup)
    goal = "按 metric.grouped_measure 汇总数据"
    initial = await execute(store, project, conversation, gateway, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run.status == "waiting_user" and gateway.calls == 0, initial
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert waiting["about"] == "analysis_group_col", waiting
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            f'/agent/runs/{run.run_id}/clarifications/{waiting["question_id"]}/answer/stream',
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "definition-group-answer",
            },
            json={"answer": "月份", "resume_token": waiting["resume_token"]},
        )
    final = tasks.get_run(run.run_id)
    calls = tasks.list_invocations(run.run_id)
    witness = {
        "goal": goal,
        "registered_definition": asdict(definition),
        "skip_lookup": skip_lookup,
        "initial": initial,
        "waiting": waiting,
        "api_status": response.status_code,
        "api_sse": response.text,
        "run": asdict(final),
        "contract": tasks.get_verified_contract(run.run_id),
        "calls": [asdict(c) for c in calls],
        "evidence": [asdict(e) for e in tasks.list_evidence(run.run_id)],
        "paid_calls": 0,
    }
    (tmp_path / "safety-witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert response.status_code == 200, witness
    assert final.status != "completed", witness
    assert not any(
        c.tool_name == "aggregate_preview" and c.status == "succeeded" for c in calls
    ), witness


class DefinitionCommands:
    def __init__(self, commands):
        self.commands, self.calls = commands, 0

    async def stream_turn(self, *args, **kwargs):
        self.calls += 1
        if self.calls > len(self.commands) + 2:
            raise ModelBudgetExceeded("offline typed definition fixture exhausted")
        if self.calls <= len(self.commands):
            tool, parameters = self.commands[self.calls - 1]
            yield ModelResponse(
                content="",
                model="offline-definition-contract",
                tool_calls=[ToolCall("def-" + str(self.calls), tool, json.dumps(parameters))],
            )
        else:
            yield ModelResponse(content="已完成分组汇总。", model="offline-definition-contract")


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["sum_success", "wrong_key", "kb_only", "no_compilation"])
async def test_definition_proof_required_after_real_group_answer(tmp_path, variant):
    app, store, project, conversation, ref, _ = workspace(tmp_path)
    lookup = ("domain_definition_lookup", {"semantic_key": KEY, "dataset_ref": ref})
    if variant == "wrong_key":
        lookup = (
            "domain_definition_lookup",
            {"semantic_key": "metric.other_measure", "dataset_ref": ref},
        )
    elif variant == "kb_only":
        lookup = ("kb_search", {"query": KEY})
    elif variant == "no_compilation":
        lookup = ("domain_definition_lookup", {"semantic_key": KEY})
    aggregate = {
        "dataset_ref": ref,
        "group_col": "月份",
        "value_col": METRIC,
        "agg": "sum",
        "sort": "group",
        "limit": 100,
    }
    gateway = DefinitionCommands([lookup, ("aggregate_preview", aggregate)])
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
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "definition-proof-answer",
            },
            json={"answer": "月份", "resume_token": waiting["resume_token"]},
        )
    final = tasks.get_run(run.run_id)
    calls = tasks.list_invocations(run.run_id)
    records = tasks.list_evidence(run.run_id)
    payload = tasks.get_verified_contract(run.run_id)
    witness = {
        "variant": variant,
        "waiting": waiting,
        "api_sse": response.text,
        "run": asdict(final),
        "contract": payload,
        "calls": [asdict(c) for c in calls],
        "evidence": [asdict(e) for e in records],
        "paid_calls": 0,
    }
    (tmp_path / "definition-proof-witness.json").write_text(
        json.dumps(witness, ensure_ascii=False, indent=2)
    )
    assert response.status_code == 200, witness
    accepted = variant == "sum_success"
    assert (final.status == "completed") is accepted, witness
    data = [c for c in calls if c.tool_name == "aggregate_preview" and c.status == "succeeded"]
    assert bool(data) is accepted, witness
    if accepted:
        requirements = AnalysisRequirements.from_dict(payload["analysis_requirements"])
        definition_calls = [c for c in calls if c.tool_name == "domain_definition_lookup"]
        definition_records = [
            e for e in records if e.source.get("tool") == "domain_definition_lookup"
        ]
        assert len(definition_calls) == len(definition_records) == 1, witness
        base = definition_records[0]
        bindings = tasks.list_dataset_bindings(run.run_id)

        def check(record):
            return operation_admission_error(
                requirements,
                run_id=run.run_id,
                tool_name="aggregate_preview",
                arguments=aggregate,
                invocations=definition_calls,
                evidence=[record],
                bindings=bindings,
            )

        assert check(base) is None
        # Mutate independent copies only: the real database remains immutable.
        mutations = {}
        for label in ("wrong_key", "wrong_dataset", "wrong_version", "wrong_formula", "old_run"):
            source = copy.deepcopy(base.source)
            if label == "wrong_key":
                source["definition_resource"]["semantic_key"] = "metric.other_measure"
            elif label == "wrong_dataset":
                source["dataset_ref"] = "f" * 32
            elif label == "wrong_version":
                source["definition_resource"]["definition_version"] += 1
            elif label == "wrong_formula":
                source["definition_resource"]["formula_hash"] = "e" * 64
            record = replace(
                base, source=source, run_id="old-run" if label == "old_run" else base.run_id
            )
            error = check(record)
            mutations[label] = error
            assert error is not None, (label, asdict(record))
        (tmp_path / "projection-mutations.json").write_text(
            json.dumps(mutations, ensure_ascii=False, indent=2)
        )
        alternative_source = copy.deepcopy(base.source)
        alternative_id = "c" * 32
        alternative_source["definition_resource"].update(
            definition_id=alternative_id,
            definition_version=2,
            resource_uri=f"chatbi://domain-definitions/{alternative_id}",
        )
        alternative_source["compiled_invocation"].update(
            definition_id=alternative_id, definition_version=2
        )
        alternate_call = replace(definition_calls[0], invocation_id="other-version")
        alternate_record = replace(
            base,
            evidence_id="other-version",
            invocation_id="other-version",
            source=alternative_source,
        )
        assert (
            operation_admission_error(
                requirements,
                run_id=run.run_id,
                tool_name="aggregate_preview",
                arguments=aggregate,
                invocations=[*definition_calls, alternate_call],
                evidence=[base, alternate_record],
                bindings=bindings,
            )
            is not None
        )
        late_progress = operation_progress(
            requirements, run.run_id, [data[0], *definition_calls], records, bindings
        )
        assert late_progress.issues and not late_progress.complete
