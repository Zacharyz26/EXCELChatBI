"""R2 真实上传、工具、Host、SQLite，只有模型离线脚本化。"""

import csv
import io
import json
import socket
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.api.deps import model_gateway_dep, session_store_dep
from apps.api.main import create_app
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from fastapi.testclient import TestClient
from packages.common.dataset_store import load_dataframe
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore
from sse_starlette.sse import AppStatus

from tests.test_agent_tools import _registry

ROOT = Path(__file__).parents[1]
METRIC = "播出时长（分钟）"
FILTERS = [
    {"column": "频道", "op": "==", "value": "A"},
    {"column": "状态", "op": "==", "value": "已播出"},
]
CHAIN_GOAL = (
    "筛选条件：频道=A，状态=已播出。然后删除播出时长（分钟）的空值，"
    "然后按整行口径去重，最后按月份汇总播出时长（分钟）并给出总计。"
)
NATURAL_FILTER_GOAL = "筛选频道=A且状态=已播出的记录，说明筛选前和筛选后的行数。"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*_a, **_kw):
        raise AssertionError("R2 real paths never call external services")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)


@pytest.fixture(autouse=True)
def isolated_sse_lifecycle():
    # 与 test_agent_loop.chat_harness 相同：TestClient 的独立事件循环不能
    # 复用上一用例由 sse-starlette 创建的进程级退出 Event。
    AppStatus.should_exit_event = None
    try:
        yield
    finally:
        AppStatus.should_exit_event = None


def upload(tmp_path):
    body = (ROOT / "examples/handoff/broadcast.csv").read_bytes()
    raw = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    selected = [r for r in raw if r["频道"] == "A" and r["状态"] == "已播出"]
    nonnull = [r for r in selected if r[METRIC]]
    unique = list({tuple(r.items()): r for r in nonnull}.values())
    assert (len(raw), len(selected), len(nonnull), len(unique)) == (75, 38, 37, 36)
    assert sum(float(r[METRIC]) for r in unique) == 1350
    store = SessionStore(str(tmp_path / "host.sqlite3"))
    project = store.create_project("R2 offline model, real synthetic tools")
    conversation = store.create_conversation(project.id)
    app = create_app()
    app.dependency_overrides[session_store_dep] = lambda: store
    with TestClient(app) as client:
        response = client.post(
            "/upload/excel",
            data={"project_id": project.id, "conversation_id": conversation.id},
            files={"file": ("broadcast.csv", body, "text/csv")},
        )
    assert response.status_code == 200
    return app, store, project, conversation, response.json()["dataset_ref"]


class OfflineOperations:
    def __init__(self, store, conversation, source, operations, answer):
        self.store, self.conversation, self.source = store, conversation, source
        self.operations, self.answer, self.calls = operations, answer, 0

    async def stream_turn(self, *_a, **_kw):
        self.calls += 1
        if self.calls > len(self.operations) + 2:
            raise ModelBudgetExceeded("Offline fixture finished; no provider was contacted")
        if self.calls <= len(self.operations):
            tool, parameters, use_source = self.operations[self.calls - 1]
            tasks = TaskStore(self.store.db_path)
            run = tasks.get_latest_run_for_conversation(self.conversation.id)
            evidence = tasks.list_evidence(run.run_id)
            outputs = [
                e.summary.get("fact_projection", {}).get("output_ref")
                for e in evidence
                if e.source.get("tool") == "transform_dataset"
            ]
            current = next((ref for ref in reversed(outputs) if ref), self.source)
            yield ModelResponse(
                content="",
                model="offline-r2",
                tool_calls=[
                    ToolCall(
                        f"local-{self.calls}",
                        tool,
                        json.dumps(
                            {"dataset_ref": self.source if use_source else current, **parameters}
                        ),
                    )
                ],
            )
        else:
            yield ModelResponse(content=self.answer, model="offline-r2")


async def execute(store, project, conversation, gateway, goal):
    registry = _registry(AgentContext(store, project.id, conversation.id))
    try:
        return [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=goal,
                gateway=gateway,
                registry=registry,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=12, max_parallel_tools=1, max_model_rounds=8),
            )
        ]
    finally:
        await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    [
        "correct",
        "wrong-order",
        "wrong-column",
        "skip-dedup",
        "original-version",
        "merged",
        "wrong-group",
        "missing-total",
    ],
)
async def test_real_ordered_operations_and_full_completion(tmp_path, variant):
    _, store, project, conversation, ref = upload(tmp_path)
    operations = [
        ("transform_dataset", {"filters": FILTERS}, False),
        ("transform_dataset", {"drop_nulls": [METRIC]}, False),
        ("transform_dataset", {"drop_duplicates": []}, False),
        ("aggregate_preview", {"group_col": "月份", "value_col": METRIC, "agg": "sum"}, False),
    ]
    if variant == "wrong-order":
        operations[1], operations[2] = operations[2], operations[1]
    if variant == "wrong-column":
        operations[1] = ("transform_dataset", {"drop_nulls": ["频道"]}, False)
    if variant == "skip-dedup":
        operations.pop(2)
    if variant == "original-version":
        operations[1] = (*operations[1][:2], True)
    if variant == "merged":
        operations[0] = ("transform_dataset", {"filters": FILTERS, "drop_nulls": [METRIC]}, False)
    if variant == "wrong-group":
        operations[-1] = (
            "aggregate_preview",
            {"group_col": "频道", "value_col": METRIC, "agg": "sum"},
            False,
        )
    answer = "操作已完成。" if variant == "missing-total" else "播出时长（分钟）总计为1350分钟。"
    gateway = OfflineOperations(store, conversation, ref, operations, answer)
    events = await execute(store, project, conversation, gateway, CHAIN_GOAL)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    invocations = tasks.list_invocations(run.run_id)
    evidence = tasks.list_evidence(run.run_id)
    witness = {
        "variant": variant,
        "goal": CHAIN_GOAL,
        "run": asdict(run),
        "events": events,
        "contract": tasks.get_contract(run.run_id),
        "invocations": [asdict(x) for x in invocations],
        "evidence": [asdict(x) for x in evidence],
        "plan": [asdict(x) for x in tasks.list_plan_steps(run.run_id)],
        "paid_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert (run.status == "completed") is (variant == "correct"), witness
    if variant == "correct":
        transforms = [e for e in evidence if e.source.get("tool") == "transform_dataset"]
        assert len(transforms) == 3
        children = [e.summary["fact_projection"]["output_ref"] for e in transforms]
        assert [len(load_dataframe(child)) for child in children] == [38, 37, 36]
        assert sum(load_dataframe(children[-1])[METRIC]) == 1350
        assert len([i for i in invocations if i.status == "succeeded"]) == 4
        assert all(s.status == "completed" for s in tasks.list_plan_steps(run.run_id))
    else:
        assert any(
            e.get("event") == "verification" or e.get("event") == "tool_end" for e in events
        ), witness


@pytest.mark.asyncio
async def test_original_natural_filter_waits_then_real_api_answer_preserves_full_conditions(
    tmp_path,
):
    app, store, project, conversation, ref = upload(tmp_path)
    gateway = OfflineOperations(
        store,
        conversation,
        ref,
        [("transform_dataset", {"filters": FILTERS}, False)],
        "筛选前75行，筛选后38行。",
    )
    events = await execute(store, project, conversation, gateway, NATURAL_FILTER_GOAL)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run.status == "waiting_user" and gateway.calls == 0, events
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert "enum" not in waiting["answer_schema"], waiting
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    with TestClient(app) as client:
        response = client.post(
            f"/agent/runs/{run.run_id}/clarifications/{waiting['question_id']}/answer/stream",
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "r2-original-filter-confirmation",
            },
            json={"answer": "频道=A AND 状态=已播出", "resume_token": waiting["resume_token"]},
        )
    result = TaskStore(store.db_path).get_run(run.run_id)
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {
                "initial": events,
                "waiting": waiting,
                "api_status": response.status_code,
                "api_sse": response.text,
                "final_run": asdict(result),
                "contract": tasks.get_verified_contract(run.run_id),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    assert response.status_code == 200 and result.status == "completed", response.text
    assert tasks.get_verified_contract(run.run_id)["analysis_requirements"]["filters"] == FILTERS
    assert tasks.get_verified_contract(run.run_id)["analysis_requirements"]["revision"] == 1


@pytest.mark.asyncio
async def test_month_enum_real_api_resume_aggregate_and_total(tmp_path):
    app, store, project, conversation, ref = upload(tmp_path)
    rows = list(csv.DictReader(io.StringIO((ROOT / "examples/handoff/broadcast.csv").read_text())))
    total = sum(float(row[METRIC]) for row in rows if row[METRIC])
    goal = "按字段分组汇总播出时长（分钟），并给出总计。"
    gateway = OfflineOperations(
        store,
        conversation,
        ref,
        [("aggregate_preview", {"group_col": "月份", "value_col": METRIC, "agg": "sum"}, False)],
        f"播出时长（分钟）总计为{total:g}分钟。",
    )
    initial = await execute(store, project, conversation, gateway, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run.status == "waiting_user" and gateway.calls == 0, initial
    waiting = tasks.list_events_by_type(run.run_id, "waiting_user")[-1].payload
    assert "月份" in waiting["answer_schema"]["enum"], waiting
    assert "记录ID" not in waiting["answer_schema"]["enum"], waiting
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    url = f"/agent/runs/{run.run_id}/clarifications/{waiting['question_id']}/answer/stream"
    with TestClient(app) as client:
        rejected = client.post(
            url,
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "r2-month-invalid-choice",
            },
            json={"answer": "记录ID", "resume_token": waiting["resume_token"]},
        )
        assert rejected.status_code == 409 and gateway.calls == 0, rejected.text
        response = client.post(
            url,
            headers={
                "If-Match": str(run.state_version),
                "Idempotency-Key": "r2-month-valid-choice",
            },
            json={"answer": "月份", "resume_token": waiting["resume_token"]},
        )
    reopened = TaskStore(store.db_path)
    final = reopened.get_run(run.run_id)
    invocations = reopened.list_invocations(run.run_id)
    contract = reopened.get_verified_contract(run.run_id)
    witness = {
        "goal": goal,
        "independent_total": total,
        "initial": initial,
        "waiting": waiting,
        "rejected_api_status": rejected.status_code,
        "api_status": response.status_code,
        "api_sse": response.text,
        "run": asdict(final),
        "contract": contract,
        "invocations": [asdict(x) for x in invocations],
        "evidence": [asdict(x) for x in reopened.list_evidence(run.run_id)],
        "paid_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert response.status_code == 200 and final.status == "completed", witness
    assert len(invocations) == 1 and invocations[0].status == "succeeded", witness
    assert invocations[0].args == {
        "dataset_ref": ref,
        "group_col": "月份",
        "value_col": METRIC,
        "agg": "sum",
    }
    assert contract["analysis_requirements"]["group_col"] == "月份"
    assert contract["analysis_requirements"]["total"] is True
    assert contract["analysis_requirements"]["revision"] == 1
    assert str(int(total)) in response.text
