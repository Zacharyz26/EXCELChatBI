"""R1 数量契约真实执行入口的离线回归，不调用业务模型。"""

from __future__ import annotations

import hashlib
import json
import socket
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from apps.api.run_host import agent_run_manager
from apps.api.schemas import ChatStreamRequest
from apps.orchestrator.agent_loop import (
    AgentLoopConfig,
    ConversationLockPool,
    _restore_task_contract,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import AgentContext, AgentToolError, _generate_report
from mcp_servers.agent_service.server import AgentServiceRuntime
from mcp_servers.common.contracts import MCPRequestContext
from mcp_servers.report.server import build_server as build_report_server
from packages.common.config import Settings
from packages.governance.permissions import Principal
from packages.models.types import ToolCall
from packages.orchestration.contracts import (
    QUANTITY_CONTRACT_VERSION,
    QuantityContractUnavailable,
    build_minimal_contract,
)
from packages.orchestration.state import InvalidStateTransition
from packages.session.models import Conversation
from packages.session.store import SessionStore
from packages.session.task_models import RunStatus, TaskRun
from packages.session.task_store import TaskStore

from tests.test_agent_loop import FakeRegistry, ScriptedGateway, _register_dataset, _run_loop


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """所有网络连接关闭；测试只能使用临时数据库和本地脚本响应。"""

    def deny(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("R1 plumbing tests must remain offline")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[SessionStore, Conversation, TaskStore]:
    store = SessionStore(str(tmp_path / "chat.db"))
    project = store.create_project("数量契约测试")
    conversation = store.create_conversation(project.id)
    return store, conversation, TaskStore(store.db_path)


def _create_run(
    workspace: tuple[SessionStore, Conversation, TaskStore], run_id: str = "quantity-run"
) -> None:
    store, conversation, tasks = workspace
    message = store.append_message(conversation_id=conversation.id, role="user", content="你好")
    contract = build_minimal_contract(
        run_id=run_id,
        user_text="你好",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    tasks.create_run(
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 3},
    )


def _replace_contract(
    store: SessionStore, run_id: str, version: object, *, remove: bool = False
) -> str:
    """仅在临时SQLite构造历史契约，并保留其正确的历史hash。"""
    tasks = TaskStore(store.db_path)
    payload = tasks.get_contract(run_id)
    assert payload is not None
    if remove:
        payload.pop("quantity_contract_version")
    else:
        payload["quantity_contract_version"] = version
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(store.db_path) as db:
        db.execute(
            "UPDATE task_contracts SET contract_json=?,contract_hash=? WHERE run_id=?",
            (text, hashlib.sha256(text.encode()).hexdigest(), run_id),
        )
    return text


def _move_to_active_state(tasks: TaskStore, run_id: str, state: str) -> TaskRun:
    """使用已有合法转移构造各活动恢复状态，不直接改运行状态表。"""
    run = tasks.get_run(run_id)
    assert run is not None
    if state == "planning":
        return run
    run, _ = tasks.transition(
        run_id,
        expected_version=run.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    if state != "running":
        run, _ = tasks.transition(
            run_id,
            expected_version=run.state_version,
            status=cast(RunStatus, state),
            event_type="fixture.state",
            payload={},
        )
    return run


def test_new_contract_version_is_frozen_in_hash() -> None:
    contract = build_minimal_contract(
        run_id="run",
        user_text="你好",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    payload = contract.to_dict()
    assert payload["quantity_contract_version"] == QUANTITY_CONTRACT_VERSION
    assert _restore_task_contract(payload, "run").content_hash == contract.content_hash
    del payload["quantity_contract_version"]
    with pytest.raises(QuantityContractUnavailable):
        _restore_task_contract(payload, "run")


@pytest.mark.parametrize("version", [None, "", "quantity-contract-v0", "quantity-contract-v99"])
def test_restore_refuses_unknown_or_null_version(version: object) -> None:
    contract = build_minimal_contract(
        run_id="run",
        user_text="你好",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    ).to_dict()
    contract["quantity_contract_version"] = version
    with pytest.raises(QuantityContractUnavailable):
        _restore_task_contract(contract, "run")


@pytest.mark.asyncio
@pytest.mark.parametrize("remove,version", [(True, None), (False, None), (False, "v99")])
@pytest.mark.parametrize("state", ["planning", "running", "waiting_user", "paused", "verifying"])
async def test_old_active_run_blocks_before_model_and_preserves_contract(
    workspace: tuple[SessionStore, Conversation, TaskStore],
    remove: bool,
    version: object,
    state: str,
) -> None:
    store, conversation, tasks = workspace
    _create_run(workspace)
    run = _move_to_active_state(tasks, "quantity-run", state)
    original = _replace_contract(store, run.run_id, version, remove=remove)
    gateway = ScriptedGateway([])
    registry = FakeRegistry({})
    events = [
        event
        async for event in stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text="你好",
            store=store,
            gateway=cast(Any, gateway),
            registry=cast(Any, registry),
            locks=ConversationLockPool(),
            config=AgentLoopConfig(),
            run_id=run.run_id,
            resume_existing=True,
        )
    ]
    final = tasks.get_run(run.run_id)
    assert final is not None and final.status == "blocked"
    assert final.terminal_reason == "quantity_contract_unavailable"
    assert gateway.calls == []
    assert registry.remote_catalog_validations == 0
    assert any("quantity_contract_unavailable" in e["data"] for e in events)
    with sqlite3.connect(store.db_path) as db:
        stored = db.execute(
            "SELECT contract_json FROM task_contracts WHERE run_id=?", (run.run_id,)
        ).fetchone()[0]
    assert stored == original


@pytest.mark.parametrize("state", ["planning", "running", "waiting_user", "paused", "verifying"])
def test_healthy_contract_cannot_forge_incompatibility_termination(
    workspace: tuple[SessionStore, Conversation, TaskStore], state: str
) -> None:
    _, _, tasks = workspace
    _create_run(workspace)
    run = _move_to_active_state(tasks, "quantity-run", state)
    before = tasks.get_verified_contract(run.run_id)
    with pytest.raises((ValueError, InvalidStateTransition)):
        tasks.terminate_active_run(
            run.run_id,
            status="blocked",
            reason="quantity_contract_unavailable",
            event_type="run.blocked",
        )
    after = tasks.get_run(run.run_id)
    assert after is not None and after.status == state and after.state_version == run.state_version
    assert tasks.get_verified_contract(run.run_id) == before


def test_exact_contract_hash_failure_is_read_only(
    workspace: tuple[SessionStore, Conversation, TaskStore],
) -> None:
    store, _, tasks = workspace
    _create_run(workspace)
    with sqlite3.connect(store.db_path) as db:
        db.execute("UPDATE task_contracts SET contract_hash='forged'")
    before = store.db_path.read_bytes()
    with pytest.raises(ValueError, match="hash"):
        tasks.get_verified_contract("quantity-run")
    assert store.db_path.read_bytes() == before


@pytest.mark.parametrize("missing", [True, False])
def test_report_refuses_missing_or_wrong_exact_run(
    workspace: tuple[SessionStore, Conversation, TaskStore], missing: bool
) -> None:
    store, conversation, _ = workspace
    _create_run(workspace)
    with pytest.raises(AgentToolError, match="TaskRun"):
        _generate_report(
            cast(Any, None),
            cast(Any, None),
            AgentContext(
                store,
                conversation.project_id,
                conversation.id,
                run_id=None if missing else "absent",
            ),
            {"title": "报告", "analysis_ids": []},
        )


def test_report_does_not_borrow_latest_run_version(
    workspace: tuple[SessionStore, Conversation, TaskStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, conversation, tasks = workspace
    _create_run(workspace)
    _replace_contract(store, "quantity-run", None, remove=True)

    def deny_latest(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("latest run is not an authority for report generation")

    monkeypatch.setattr(TaskStore, "get_latest_run_for_conversation", deny_latest)
    with pytest.raises(AgentToolError, match="数量契约"):
        _generate_report(
            cast(Any, None),
            cast(Any, None),
            AgentContext(store, conversation.project_id, conversation.id, run_id="quantity-run"),
            {"title": "报告", "analysis_ids": []},
        )
    stored = tasks.get_contract("quantity-run")
    assert stored is not None and stored.get("quantity_contract_version") is None


def test_report_publishes_verified_draft_text_not_wire_json(
    workspace: tuple[SessionStore, Conversation, TaskStore],
) -> None:
    store, conversation, _ = workspace
    _create_run(workspace)
    fixture = json.loads((Path(__file__).parent / "fixtures/r1_document_profile.json").read_text())[
        "profile"
    ]
    store.register_dataset(
        ref=fixture["dataset_ref"],
        project_id=conversation.project_id,
        filename="synthetic-broadcast.csv",
        profile=fixture["payload"]["profile"],
    )
    message = store.append_message(
        conversation_id=conversation.id, role="assistant", content="画像已登记"
    )
    artifact = store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="profile",
        payload=fixture["payload"],
        source_tool="get_data_profile",
        params={"analysis_id": "profile-for-report", "dataset_ref": fixture["dataset_ref"]},
        dataset_ref=fixture["dataset_ref"],
    )
    assert artifact.dataset_ref == fixture["dataset_ref"]

    def draft(text: str) -> str:
        return json.dumps(
            {
                "schema": "quantity-presentation-draft-v1",
                "blocks": [{"kind": "prose", "text": text}],
            },
            ensure_ascii=False,
        )

    result = _generate_report(
        build_report_server(),
        cast(Any, None),
        AgentContext(store, conversation.project_id, conversation.id, run_id="quantity-run"),
        {
            "title": draft("测试报告"),
            "insights": draft("以来源工件为准。"),
            "analysis_ids": ["profile-for-report"],
        },
    )
    assert "# 测试报告" in result["markdown"]
    assert "以来源工件为准。" in result["markdown"]
    assert "quantity-presentation-draft" not in result["markdown"]
    assert result["validation"]["prepared_sections"]["title"] == "测试报告"
    assert result["validation"]["quantity_contract_version"] == QUANTITY_CONTRACT_VERSION


@pytest.mark.asyncio
async def test_invalid_draft_never_falls_back_or_publishes(
    workspace: tuple[SessionStore, Conversation, TaskStore],
) -> None:
    store, conversation, tasks = workspace
    source = '{"schema":"quantity-presentation-draft-v1","blocks":['
    gateway = ScriptedGateway([{"deltas": [source]}])
    events = await _run_loop(store, conversation, gateway, FakeRegistry({}), user_text="你好")
    assert len(gateway.calls) == 1
    assert not any(
        name == "text.delta" and source in item.get("delta", "") for name, item in events
    )
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run is not None and run.status == "blocked"
    audit = tasks.list_events_by_type(run.run_id, "quantity.candidate")
    assert audit and audit[0].payload["source_text"] == source
    assert audit[0].payload["valid"] is False
    context = store.load_conversation_context(conversation.id)
    assert context is not None
    assert all(message.content != source for message in context.messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["八个异常数据点均已核实。", "已完成筛选并生成报告。"])
async def test_tool_preamble_unknown_fact_is_audited_but_not_published(
    workspace: tuple[SessionStore, Conversation, TaskStore],
    source: str,
) -> None:
    store, conversation, tasks = workspace
    _register_dataset(store, conversation)
    gateway = ScriptedGateway(
        [
            {
                "deltas": [source],
                "tool_calls": [
                    ToolCall(
                        id="profile-call",
                        name="get_data_profile",
                        arguments=json.dumps({"dataset_ref": "d" * 32}),
                    )
                ],
            },
            {"deltas": ["已查看画像。"]},
        ]
    )
    registry = FakeRegistry({"get_data_profile": lambda args: {"profile": {"row_count": 3}}})
    events = await _run_loop(store, conversation, gateway, registry, user_text="请查看画像")
    visible = "".join(
        str(item.get("delta", item.get("text", "")))
        for name, item in events
        if name in {"text.delta", "understanding"}
    )
    assert source not in visible
    assert "正在准备校验并执行：" in visible
    run = tasks.get_latest_run_for_conversation(conversation.id)
    assert run is not None
    audit = tasks.list_events_by_type(run.run_id, "quantity.candidate")
    initial = next(e for e in audit if e.payload["phase"] == "tool_preamble")
    assert initial.payload["source_text"] == source
    if source.startswith("八"):
        assert initial.payload["valid"] is False
    context = store.load_conversation_context(conversation.id)
    assert context is not None
    assert all(message.content != source for message in context.messages)
    assert len(registry.executed) == 1


@pytest.mark.asyncio
async def test_new_chat_passes_the_same_host_run_into_registry_and_runner(
    workspace: tuple[SessionStore, Conversation, TaskStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.api.routers import chat

    store, conversation, _ = workspace
    captured: dict[str, Any] = {}

    def registry(**kwargs: Any) -> Any:
        captured["context"] = kwargs["context"]
        return object()

    def start(run_id: str, factory: Any, **kwargs: Any) -> Any:
        captured["run_id"] = run_id
        return object()

    monkeypatch.setattr(chat, "build_registry", registry)
    monkeypatch.setattr(agent_run_manager, "start", start)
    monkeypatch.setattr(chat, "EventSourceResponse", lambda *args, **kwargs: kwargs)
    result = await chat.chat_stream(
        req=ChatStreamRequest(conversation_id=conversation.id, message="你好"),
        store=store,
        gateway=cast(Any, None),
        settings=Settings(chat_db_path=str(store.db_path)),
        excel=cast(Any, None),
        stats=cast(Any, None),
        chart=cast(Any, None),
        dataset_ops=cast(Any, None),
        report=cast(Any, None),
        retriever=cast(Any, None),
        principal=Principal(user_id="local-user"),
    )
    assert captured["context"].run_id == captured["run_id"]
    assert cast(Any, result)["headers"]["X-ChatBI-Run-ID"] == captured["run_id"]


def test_recovery_passes_exact_existing_run_to_registry(
    workspace: tuple[SessionStore, Conversation, TaskStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    from apps.api.routers import agent_runs

    store, _, tasks = workspace
    _create_run(workspace)
    run = tasks.get_run("quantity-run")
    assert run is not None
    captured: dict[str, Any] = {}

    def registry(**kwargs: Any) -> Any:
        captured["context"] = kwargs["context"]
        return object()

    def start(run_id: str, factory: Any, **kwargs: Any) -> Any:
        captured["run_id"] = run_id
        return object()

    monkeypatch.setattr(agent_runs, "build_registry", registry)
    monkeypatch.setattr(agent_run_manager, "start", start)
    execution = SimpleNamespace(
        excel=None,
        stats=None,
        chart=None,
        dataset_ops=None,
        report=None,
        retriever=None,
        gateway=None,
        settings=Settings(chat_db_path=str(store.db_path)),
    )
    agent_runs._start_recovered_run(
        run, store=store, principal=Principal(user_id="local-user"), execution=cast(Any, execution)
    )
    assert captured["context"].run_id == run.run_id == captured["run_id"]


def test_report_mcp_context_keeps_signed_run_identity(
    workspace: tuple[SessionStore, Conversation, TaskStore], monkeypatch: pytest.MonkeyPatch
) -> None:
    store, conversation, _ = workspace
    runtime = object.__new__(AgentServiceRuntime)
    runtime.store = store
    runtime.service_name = "report-tools"
    captured: dict[str, Any] = {}

    def registry(*, context: AgentContext) -> Any:
        captured["context"] = context
        return SimpleNamespace(execute=lambda name, arguments: {"observed": name})

    monkeypatch.setattr(runtime, "_authorize", lambda context, arguments: None)
    monkeypatch.setattr(runtime, "_registry", registry)
    context = MCPRequestContext(
        subject_id="local-user",
        project_id=conversation.project_id,
        conversation_id=conversation.id,
        run_id="signed-exact-run",
        plan_version=1,
        step_id="step",
        invocation_id="inv",
        idempotency_key="key",
        permission_snapshot_id="permission",
        memory_snapshot_id="memory",
        evidence_ledger_version=1,
        data_version_hash="version",
        cancellation_node_id="cancel",
        trace_id="trace",
        deadline_at="2099-01-01T00:00:00Z",
    )
    result = runtime.execute("generate_report", {}, context)
    assert result == {"observed": "generate_report"}
    assert captured["context"].run_id == context.run_id
