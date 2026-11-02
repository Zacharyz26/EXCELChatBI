"""R4 independent context contract, frozen before implementation.

Real upload, SessionStore/TaskStore, data tools and complete Host paths. Only
model choices are scripted; no network, model provider or browser is involved.
No successful invocation, Evidence or completed state is manually fabricated.
"""

from __future__ import annotations

import csv
import io
import json
import socket
import uuid
from dataclasses import asdict

import pytest
from apps.api.deps import session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from fastapi.testclient import TestClient
from packages.common.config import get_settings
from packages.common.dataset_store import load_dataframe
from packages.governance.permissions import Principal
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.orchestration.contracts import build_minimal_contract
from packages.session.coref import ReferenceResolver
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from sse_starlette.sse import AppStatus

from tests.test_agent_tools import _registry

METRIC = "时长（分钟）"
CSV = (
    "月份,频道,状态,时长（分钟）\n"
    "2026-01,A,已播出,10\n"
    "2026-01,A,已播出,20\n"
    "2026-02,A,待播,30\n"
    "2026-02,B,已播出,40\n"
    "2026-02,A,已播出,\n"
    "2026-02,A,已播出,50\n"
).encode()
OTHER = ("月份,频道,状态,时长（分钟）\n" "2026-03,B,已播出,900\n" "2026-03,B,已播出,900\n").encode()
FILTERS = [
    {"column": "频道", "op": "==", "value": "A"},
    {"column": "状态", "op": "==", "value": "已播出"},
]
PRINCIPAL = Principal(user_id="local-user")


@pytest.fixture(autouse=True)
def offline_and_isolated(tmp_path, monkeypatch):
    def denied(*_args, **_kwargs):
        raise AssertionError("R4 frozen context tests never contact external services")

    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket.socket, "connect_ex", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    for key, dirname in (
        ("DATASET_DIR", "datasets"),
        ("UPLOAD_DIR", "uploads"),
        ("REPORT_DIR", "reports"),
        ("CHAT_DB_PATH", "unused-default.sqlite3"),
    ):
        monkeypatch.setenv(key, str(tmp_path / dirname))
    get_settings.cache_clear()
    AppStatus.should_exit_event = None
    try:
        yield
    finally:
        AppStatus.should_exit_event = None
        get_settings.cache_clear()


def workspace(tmp_path):
    rows = list(csv.DictReader(io.StringIO(CSV.decode())))
    selected = [row for row in rows if row["频道"] == "A" and row["状态"] == "已播出"]
    assert len(rows) == 6 and len(selected) == 4
    assert sum(int(row[METRIC]) for row in selected if row[METRIC]) == 80
    store = SessionStore(str(tmp_path / "context.sqlite3"))
    project = store.create_project("R4 synthetic context identity")
    conversation = store.create_conversation(project.id)
    return store, project, conversation


def application(store):
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    return app


def upload(store, project, conversation, *, body=CSV, name="source.csv"):
    with TestClient(application(store)) as client:
        response = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conversation.id},
            files={"file": (name, body, "text/csv")},
        )
    assert response.status_code == 200, response.text
    return response.json()["dataset_ref"]


def resolve(store, project, conversation, query):
    return ReferenceResolver(store, audit_recorder=lambda _event: None).resolve(
        query,
        project_id=project.id,
        conversation_id=conversation.id,
        principal=PRINCIPAL,
    )


def ref_set(resolution):
    return {target.dataset_ref for target in resolution.targets if target.dataset_ref is not None}


def write_witness(tmp_path, label, **payload):
    (tmp_path / f"{label}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    )


class ScriptedModel:
    def __init__(self, calls, answer="已完成。", *, end="answer"):
        self.tools = calls
        self.answer = answer
        self.end = end
        self.calls = 0
        self.messages = []

    async def stream_turn(self, _scenario, messages, **_kwargs):
        self.calls += 1
        self.messages.append([asdict(item) for item in messages])
        if self.calls <= len(self.tools):
            name, arguments = self.tools[self.calls - 1]
            yield ModelResponse(
                content="",
                model="offline-r4-context",
                tool_calls=[
                    ToolCall(
                        f"context-call-{self.calls}",
                        name,
                        json.dumps(arguments, ensure_ascii=False),
                    )
                ],
            )
        elif self.end == "timeout":
            raise TimeoutError("Local scripted failure after a real successful tool")
        elif self.end == "blocked" or self.calls > len(self.tools) + 3:
            raise ModelBudgetExceeded("Offline fixture exhausted; no provider was contacted")
        else:
            yield ModelResponse(content=self.answer, model="offline-r4-context")


async def execute(store, project, conversation, model, goal):
    run_id = uuid.uuid4().hex
    registry = _registry(
        AgentContext(
            store,
            project.id,
            conversation.id,
            run_id=run_id,
        )
    )
    try:
        events = [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=goal,
                gateway=model,
                registry=registry,
                locks=ConversationLockPool(),
                run_id=run_id,
                config=AgentLoopConfig(max_tool_calls=6, max_model_rounds=6, max_parallel_tools=1),
            )
        ]
    finally:
        await registry.aclose()
    tasks = TaskStore(store.db_path)
    run = tasks.get_run(run_id)
    assert run is not None, events
    return {
        "run": asdict(run),
        "events": events,
        "model_calls": model.calls,
        "invocations": [asdict(item) for item in tasks.list_invocations(run_id)],
        "evidence": [asdict(item) for item in tasks.list_evidence(run_id)],
        "bindings": [asdict(item) for item in tasks.list_dataset_bindings(run_id)],
        "contract": tasks.get_verified_contract(run_id),
        "data_version_hash": tasks.data_version_hash(run_id),
        "layer": "real local Host/tools/SQLite; scripted model; no browser/provider",
    }


async def filtered_source(store, project, conversation, *, end="answer"):
    original = upload(store, project, conversation)
    model = ScriptedModel(
        [("transform_dataset", {"dataset_ref": original, "filters": FILTERS})],
        "筛选前6行，筛选后4行。",
        end=end,
    )
    result = await execute(
        store,
        project,
        conversation,
        model,
        f"dataset_ref={original}；筛选条件：频道=A，状态=已播出。说明筛选前后的行数。",
    )
    successful = [row for row in result["invocations"] if row["status"] == "succeeded"]
    assert len(successful) == 1 and successful[0]["tool_name"] == "transform_dataset", result
    transforms = [
        row for row in result["evidence"] if row["source"].get("tool") == "transform_dataset"
    ]
    assert len(transforms) == 1, result
    projection = transforms[0]["summary"]["fact_projection"]
    child = projection["output_ref"]
    assert projection["input_ref"] == original
    assert projection["arguments"]["filters"] == FILTERS
    frame = load_dataframe(child)
    assert len(frame) == 4 and frame[METRIC].sum() == 80
    assert store.get_dataset(child).parent_ref == original
    expected_status = (
        "completed" if end == "answer" else "failed" if end == "timeout" else "blocked"
    )
    assert result["run"]["status"] == expected_status, result
    return original, child, result


def conversation_detail(store, conversation):
    with TestClient(application(store)) as client:
        response = client.get(f"/conversations/{conversation.id}")
    assert response.status_code == 200, response.text
    return response.json()


def execution_projection(detail):
    projection = detail.get("execution_context")
    assert isinstance(projection, dict), {
        "interface_status": "R4_INTERFACE_NOT_IMPLEMENTED_OR_INVALID",
        "expected": "GET conversation.execution_context read-only projection",
        "detail": detail,
    }
    return projection


@pytest.mark.parametrize("reopen", [False, True])
def test_current_dataset_is_conversation_owned_not_project_latest(tmp_path, reopen):
    store, project, first = workspace(tmp_path)
    first_ref = upload(store, project, first)
    second = store.create_conversation(project.id)
    second_ref = upload(store, project, second, body=OTHER, name="other.csv")
    if reopen:
        store = SessionStore(store.db_path)
    one = resolve(store, project, first, "查看当前数据集。")
    two = resolve(store, project, second, "查看当前数据集。")
    write_witness(tmp_path, "resolver", first=asdict(one), second=asdict(two))
    assert one.status == "resolved" and ref_set(one) == {first_ref}
    assert two.status == "resolved" and ref_set(two) == {second_ref}


def test_empty_conversation_does_not_inherit_project_only_dataset(tmp_path):
    store, project, first = workspace(tmp_path)
    uploaded = upload(store, project, first)
    empty = store.create_conversation(project.id)
    result = resolve(store, project, empty, "查看当前数据集。")
    write_witness(tmp_path, "empty-conversation", result=asdict(result), unrelated=uploaded)
    assert result.status in {"unresolved", "ambiguous"}
    assert ref_set(result) == set()


def test_explicit_project_dataset_reference_is_deliberate_switch(tmp_path):
    store, project, first = workspace(tmp_path)
    old = upload(store, project, first)
    second = store.create_conversation(project.id)
    latest = upload(store, project, second, body=OTHER, name="other.csv")
    result = resolve(store, project, first, f"接下来的分析请使用数据集（dataset_ref: {latest}）。")
    write_witness(tmp_path, "explicit-switch", result=asdict(result), prior=old)
    assert result.status == "resolved" and ref_set(result) == {latest}


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_old", [False, True])
async def test_real_next_turn_keeps_child_scope_unless_explicitly_switched(tmp_path, explicit_old):
    store, project, conversation = workspace(tmp_path)
    original, child, first = await filtered_source(store, project, conversation)
    another = store.create_conversation(project.id)
    upload(store, project, another, body=OTHER, name="unrelated-later.csv")
    reopened = SessionStore(store.db_path)
    expected = original if explicit_old else child
    goal = f"dataset_ref={original}；查看数据画像。" if explicit_old else "查看当前数据集的画像。"
    second = await execute(
        reopened,
        project,
        conversation,
        ScriptedModel([("get_data_profile", {"dataset_ref": expected})], "已完成画像。"),
        goal,
    )
    write_witness(tmp_path, "two-turns", first=first, second=second, child=child, original=original)
    assert second["run"]["status"] == "completed", second
    assert second["contract"]["analysis_requirements"]["dataset_ref"] == expected, second
    successful = [row for row in second["invocations"] if row["status"] == "succeeded"]
    assert len(successful) == 1 and successful[0]["args"]["dataset_ref"] == expected, second
    assert (
        TaskStore(reopened.db_path).data_version_hash(first["run"]["run_id"])
        == first["data_version_hash"]
    )


@pytest.mark.asyncio
async def test_implicit_current_child_cannot_be_replaced_by_old_parent_tool(tmp_path):
    store, project, conversation = workspace(tmp_path)
    original, child, first = await filtered_source(store, project, conversation)
    second = await execute(
        store,
        project,
        conversation,
        ScriptedModel([("get_data_profile", {"dataset_ref": original})], "已完成画像。"),
        "查看当前数据集的画像。",
    )
    write_witness(tmp_path, "wrong-version", first=first, second=second, child=child)
    assert second["run"]["status"] != "completed", second
    assert not any(row["status"] == "succeeded" for row in second["invocations"]), second


@pytest.mark.asyncio
async def test_explicit_selection_persists_over_older_child_on_next_natural_turn(tmp_path):
    store, project, conversation = workspace(tmp_path)
    original, child, first = await filtered_source(store, project, conversation)
    selection = await execute(
        store,
        project,
        conversation,
        ScriptedModel([], "已确认后续分析使用指定数据集。"),
        f"接下来的分析请使用数据集（dataset_ref: {original}）。",
    )
    assert selection["run"]["status"] == "completed", selection
    assert selection["invocations"] == [] and selection["evidence"] == [], selection
    assert selection["contract"]["analysis_requirements"]["dataset_ref"] == original, selection
    another = store.create_conversation(project.id)
    upload(store, project, another, body=OTHER, name="unrelated-after-selection.csv")
    reopened = SessionStore(store.db_path)
    resolved = resolve(reopened, project, conversation, "查看当前数据集。")
    next_turn = await execute(
        reopened,
        project,
        conversation,
        ScriptedModel([("get_data_profile", {"dataset_ref": original})], "已完成画像。"),
        "请提供画像。",
    )
    write_witness(
        tmp_path,
        "selection-followup",
        first=first,
        selection=selection,
        next_turn=next_turn,
        resolved=asdict(resolved),
        old_child=child,
    )
    assert resolved.status == "resolved" and ref_set(resolved) == {original}
    assert next_turn["run"]["status"] == "completed", next_turn
    assert next_turn["contract"]["analysis_requirements"]["dataset_ref"] == original, next_turn
    successful = [row for row in next_turn["invocations"] if row["status"] == "succeeded"]
    assert len(successful) == 1 and successful[0]["args"]["dataset_ref"] == original, next_turn


@pytest.mark.asyncio
async def test_unproven_registered_child_cannot_replace_verified_context(tmp_path):
    store, project, conversation = workspace(tmp_path)
    original, child, first = await filtered_source(store, project, conversation)
    # Actual tool call outside a TaskRun intentionally has no successful run
    # invocation, Evidence or derived binding. Its output is real, not forged.
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        unbound = registry.execute(
            "transform_dataset", json.dumps({"dataset_ref": original, "drop_nulls": [METRIC]})
        )
    finally:
        await registry.aclose()
    candidates = [
        item for item in store.list_datasets(project.id) if item.ref not in {original, child}
    ]
    assert len(candidates) == 1 and len(load_dataframe(candidates[0].ref)) == 5
    result = resolve(SessionStore(store.db_path), project, conversation, "查看当前数据集。")
    write_witness(
        tmp_path,
        "unbound-child",
        first=first,
        result=asdict(result),
        unbound=str(unbound),
        candidate=candidates[0].ref,
    )
    assert result.status == "resolved" and ref_set(result) == {child}, result


@pytest.mark.asyncio
async def test_same_row_filter_still_advances_version_and_retains_lineage(tmp_path):
    store, project, conversation = workspace(tmp_path)
    _, first_child, first = await filtered_source(store, project, conversation)
    second_filter = [{"column": "频道", "op": "==", "value": "A"}]
    second = await execute(
        store,
        project,
        conversation,
        ScriptedModel(
            [("transform_dataset", {"dataset_ref": first_child, "filters": second_filter})],
            "筛选前4行，筛选后4行。",
        ),
        f"dataset_ref={first_child}；筛选条件：频道=A。说明筛选前后的行数。",
    )
    assert second["run"]["status"] == "completed", second
    evidence = [
        row for row in second["evidence"] if row["source"].get("tool") == "transform_dataset"
    ]
    assert len(evidence) == 1, second
    projection = evidence[0]["summary"]["fact_projection"]
    final_child = projection["output_ref"]
    assert final_child != first_child and projection["input_ref"] == first_child
    assert projection["arguments"]["filters"] == second_filter
    assert len(load_dataframe(final_child)) == len(load_dataframe(first_child)) == 4
    assert store.get_dataset(final_child).parent_ref == first_child
    another = store.create_conversation(project.id)
    upload(store, project, another, body=OTHER, name="unrelated-after-noop.csv")
    resolved = resolve(SessionStore(store.db_path), project, conversation, "查看当前数据集。")
    write_witness(
        tmp_path, "same-rows-new-version", first=first, second=second, resolved=asdict(resolved)
    )
    assert resolved.status == "resolved" and ref_set(resolved) == {final_child}


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["answer", "timeout", "blocked"])
async def test_projection_preserves_actual_success_child_and_exact_run_status(tmp_path, end):
    store, project, conversation = workspace(tmp_path)
    original, child, first = await filtered_source(store, project, conversation, end=end)
    another = store.create_conversation(project.id)
    upload(store, project, another, body=OTHER, name="project-last.csv")
    reopened = SessionStore(store.db_path)
    detail = conversation_detail(reopened, conversation)
    write_witness(tmp_path, "projection-before-assertion", first=first, detail=detail)
    projection = execution_projection(detail)
    assert projection["dataset_ref"] == child, projection
    assert projection["run_status"] == first["run"]["status"], projection
    assert (
        TaskStore(reopened.db_path).get_run(first["run"]["run_id"]).status == first["run"]["status"]
    )
    persisted = TaskStore(reopened.db_path)
    assert persisted.get_run(first["run"]["run_id"]).state_version == first["run"]["state_version"]
    assert [asdict(item) for item in persisted.list_invocations(first["run"]["run_id"])] == first[
        "invocations"
    ]
    assert reopened.get_dataset(child).parent_ref == original


@pytest.mark.asyncio
async def test_paused_task_reopen_does_not_relabel_success_or_advance_dataset(tmp_path):
    store, project, conversation = workspace(tmp_path)
    _, child, first = await filtered_source(store, project, conversation)
    tasks = TaskStore(store.db_path)
    run_id = uuid.uuid4().hex
    contract = build_minimal_contract(
        run_id=run_id,
        user_text=f"dataset_ref={child}；查看数据画像。",
        dataset_refs=(child,),
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    _, _, run, _ = tasks.start_run_with_user_turn(
        project_id=project.id,
        conversation_id=conversation.id,
        content=contract.goal,
        suggested_title="paused context fixture",
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    paused, _, changed = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="r4-fixture-pause",
        command="pause",
        allowed_statuses={"planning"},
        status="paused",
        event_type="run.paused",
        payload={"source": "independent_context_test"},
        require_idle=True,
    )
    assert changed and paused.status == "paused"
    reopened = SessionStore(store.db_path)
    detail = conversation_detail(reopened, conversation)
    write_witness(tmp_path, "paused", first=first, paused=asdict(paused), detail=detail)
    projection = execution_projection(detail)
    assert projection["dataset_ref"] == child, projection
    assert projection["run_status"] == "paused", projection
    assert TaskStore(reopened.db_path).get_run(run_id).status == "paused"
    assert tasks.list_invocations(run_id) == []


@pytest.mark.asyncio
async def test_deleted_current_source_is_unavailable_not_silently_parent_or_latest(tmp_path):
    store, project, conversation = workspace(tmp_path)
    original, child, first = await filtered_source(store, project, conversation)
    another = store.create_conversation(project.id)
    latest = upload(store, project, another, body=OTHER, name="not-a-replacement.csv")
    assert store.delete_dataset(child)
    reopened = SessionStore(store.db_path)
    result = resolve(reopened, project, conversation, "查看当前数据集。")
    detail = conversation_detail(reopened, conversation)
    write_witness(
        tmp_path,
        "deleted-source",
        first=first,
        result=asdict(result),
        detail=detail,
        original=original,
        unrelated=latest,
    )
    assert result.status in {"unresolved", "ambiguous"} and not ref_set(result), result
    projection = execution_projection(detail)
    assert projection["dataset_ref"] is None, projection
    assert TaskStore(reopened.db_path).get_run(first["run"]["run_id"]).status == "completed"
