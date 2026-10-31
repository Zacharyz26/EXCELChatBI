"""Independent R2 opaque-reference/report boundary; only the model is scripted.

Oracle: a visible persisted Artifact may be selected, not turned into a data
predicate; opaque-reference syntax never authorizes other business comparisons.
"""

from __future__ import annotations

import hashlib
import json
import socket
import uuid
from dataclasses import asdict

import pytest
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from packages.common.config import get_settings
from packages.models.types import ModelBudgetExceeded, ModelResponse, ToolCall
from packages.orchestration.analysis_requirements import explicit_analysis_requirements
from packages.session.task_store import TaskStore
from sse_starlette.sse import AppStatus

from tests.test_agent_tools import _registry
from tests.test_r2_real_paths import OfflineOperations, execute, upload

TOKEN = "93e1b76b0a2e4d0cbf95a90841a93d5f"
COLUMNS = ("月份", "频道", "状态", "播出时长（分钟）")


@pytest.fixture(autouse=True)
def offline_runtime(monkeypatch, tmp_path):
    def deny(*_args, **_kwargs):
        raise AssertionError("reference holdout cannot contact external services")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setenv("REPORT_DIR", str(tmp_path / "reports"))
    get_settings.cache_clear()
    AppStatus.should_exit_event = None
    try:
        yield
    finally:
        AppStatus.should_exit_event = None
        get_settings.cache_clear()


class ReportResponses:
    def __init__(self, dataset_ref):
        self.dataset_ref = dataset_ref
        self.calls = 0

    async def stream_turn(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls > 5:
            raise ModelBudgetExceeded("offline fixture exhausted; no external model")
        if self.calls == 1:
            call = ToolCall(
                "current-profile",
                "get_data_profile",
                json.dumps({"dataset_ref": self.dataset_ref}),
            )
        elif self.calls == 2:
            call = ToolCall(
                "referenced-report",
                "generate_report",
                json.dumps({"title": "指定画像报告", "analysis_ids": [], "include_pdf": False}),
            )
        else:
            yield ModelResponse(content="指定分析报告已生成。", model="offline-reference")
            return
        yield ModelResponse(content="", model="offline-reference", tool_calls=[call])


async def execute_bound_report(store, project, conversation, gateway, goal):
    # Same exact-run contract as apps/api/routers/chat.py: generate once, pass
    # to both the real tool context and Host. No database state is fabricated.
    run_id = uuid.uuid4().hex
    registry = _registry(AgentContext(store, project.id, conversation.id, run_id=run_id))
    try:
        return [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                user_text=goal,
                store=store,
                gateway=gateway,
                registry=registry,
                run_id=run_id,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_tool_calls=12, max_parallel_tools=1, max_model_rounds=8),
            )
        ]
    finally:
        await registry.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["visible", "missing", "foreign"])
async def test_real_artifact_reference_and_report_admission(tmp_path, reference_kind):
    _, store, project, conversation, source = upload(tmp_path)
    seed = OfflineOperations(
        store, conversation, source, [("get_data_profile", {}, True)], "画像已生成。"
    )
    seed_events = await execute(
        store, project, conversation, seed, "请查看当前数据画像，暂不筛选或清洗。"
    )
    old = next(
        artifact
        for artifact in store.list_artifacts(conversation.id)
        if artifact.source_tool == "get_data_profile"
    )
    seed_run = TaskStore(store.db_path).get_latest_run_for_conversation(conversation.id)
    assert any(
        call.tool_name == "get_data_profile" and call.status == "succeeded"
        for call in TaskStore(store.db_path).list_invocations(seed_run.run_id)
    ), seed_events
    selected_id = old.id
    if reference_kind == "missing":
        selected_id = TOKEN
        assert all(a.id != selected_id for a in store.list_artifacts(conversation.id))
    elif reference_kind == "foreign":
        foreign_project = store.create_project("private reference fixture")
        foreign_conversation = store.create_conversation(foreign_project.id)
        foreign_message = store.append_message(
            conversation_id=foreign_conversation.id, role="assistant", content="private fixture"
        )
        foreign = store.create_artifact(
            conversation_id=foreign_conversation.id,
            message_id=foreign_message.id,
            type=old.type,
            payload=old.payload,
            params=old.params,
            source_tool=old.source_tool,
            dataset_ref=None,
        )
        selected_id = foreign.id
    goal = f"请先查看当前数据画像，再使用 analysis_id={selected_id} 生成报告"
    gateway = ReportResponses(source)
    events = await execute_bound_report(store, project, conversation, gateway, goal)
    tasks = TaskStore(store.db_path)
    run = tasks.get_latest_run_for_conversation(conversation.id)
    invocations = tasks.list_invocations(run.run_id)
    records = tasks.list_evidence(run.run_id)
    witness = {
        "goal": goal,
        "reference_kind": reference_kind,
        "selected_artifact_id": selected_id,
        "selected_analysis_id": old.params["analysis_id"],
        "old_artifact": asdict(old),
        "run": asdict(run),
        "contract": tasks.get_contract(run.run_id),
        "invocations": [asdict(i) for i in invocations],
        "evidence": [asdict(e) for e in records],
        "events": events,
        "offline_model_turns": gateway.calls,
        "real_model_calls": 0,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    if reference_kind != "visible":
        assert run.status == "waiting_user", witness
        assert gateway.calls == 0 and not invocations, witness
        assert not list((tmp_path / "reports").glob("*.md")), witness
        return
    assert run.status == "completed", witness
    reports = [i for i in invocations if i.tool_name == "generate_report"]
    assert len(reports) == 1 and reports[0].status == "succeeded", witness
    assert reports[0].args["analysis_ids"] == [old.params["analysis_id"]], witness
    profile = [i for i in invocations if i.tool_name == "get_data_profile"]
    assert len(profile) == 1 and profile[0].status == "succeeded", witness
    generated = list((tmp_path / "reports").glob("*.md"))
    assert len(generated) == 1, witness
    content = generated[0].read_bytes()
    assert "指定画像报告" in content.decode("utf-8"), witness
    report_record = next(e for e in records if e.source.get("tool") == "generate_report")
    report_artifact = next(
        a for a in store.list_artifacts(conversation.id) if a.source_tool == "generate_report"
    )
    assert (
        report_artifact.payload["validation"]["content_sha256"]
        == hashlib.sha256(content).hexdigest()
    )
    assert report_record.summary.get("report_validation_hash"), witness
    (tmp_path / "report.md").write_bytes(content)


@pytest.mark.parametrize("width", [12, 32])
def test_opaque_reference_is_not_a_business_filter_without_schema_collision(width):
    req = explicit_analysis_requirements(
        f"使用 analysis_id={TOKEN[:width]} 生成报告", columns=COLUMNS
    )
    assert not req.needs_clarification(), req.to_dict()
    assert not req.filters and not req.active_operations(), req.to_dict()


@pytest.mark.parametrize(
    "text,columns",
    [
        (f"使用 analysis_id={TOKEN} 生成报告，频道=A。", COLUMNS),
        (f"频道=A；使用 analysis_id={TOKEN} 生成报告。", COLUMNS),
        (f"使用 analysis_id={TOKEN} 生成报告\n状态=已播出。", COLUMNS),
        (f"使用 fake_analysis_id={TOKEN} 生成报告。", COLUMNS),
        (f"使用 analysis_id_extra={TOKEN} 生成报告。", COLUMNS),
        (f"使用 analysis_id={TOKEN} 生成报告。", (*COLUMNS, "analysis_id")),
    ],
)
def test_reference_syntax_cannot_discard_unowned_business_comparisons(text, columns):
    req = explicit_analysis_requirements(text, columns=columns)
    assert req.needs_clarification(), req.to_dict()


def test_same_named_column_remains_a_real_filter():
    req = explicit_analysis_requirements(
        f"筛选 analysis_id={TOKEN}。",
        columns=(*COLUMNS, "analysis_id"),
        dataset_refs=("e" * 32,),
    )
    assert not req.needs_clarification(), req.to_dict()
    assert list(req.filters) == [{"column": "analysis_id", "op": "==", "value": TOKEN}]
    assert [op.kind for op in req.active_operations()] == ["filter"]


@pytest.mark.parametrize("partial_name", ["id", "analysis"])
def test_partial_schema_field_name_does_not_own_the_complete_reference_header(partial_name):
    req = explicit_analysis_requirements(
        f"使用 analysis_id={TOKEN} 生成报告。", columns=(*COLUMNS, partial_name)
    )
    assert not req.needs_clarification(), req.to_dict()
    assert not req.filters and not req.active_operations(), req.to_dict()


@pytest.mark.parametrize("field", ["审计analysis_id", "foo.analysis_id"])
def test_complete_prefixed_schema_field_cannot_be_partly_erased_as_a_reference(field):
    req = explicit_analysis_requirements(
        f"{field}={TOKEN}。请生成报告。", columns=(*COLUMNS, field)
    )
    assert req.needs_clarification(), req.to_dict()
