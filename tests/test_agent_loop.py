"""阶段3：Agent 循环测试。

覆盖：/chat/stream SSE 协议与持久化（吸收原阶段1 用例）、上下文装配（数据集
清单 + 分析登记表）、工具轮事件序列与工件落库、带错重试、同参熔断、调用数
上限、对话锁。
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.api.deps import model_gateway_dep, session_store_dep, settings_dep  # noqa: E402
from apps.api.main import app  # noqa: E402
from apps.orchestrator.agent_loop import (  # noqa: E402
    AgentLoopConfig,
    ConversationLockPool,
    stream_agent_chat,
)
from apps.orchestrator.agent_tools import AgentToolRegistry  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from packages.common.config import Settings  # noqa: E402
from packages.governance.schema_validator import SchemaValidationError  # noqa: E402
from packages.models.types import Message as ModelMessage  # noqa: E402
from packages.models.types import ModelResponse, Scenario, ToolCall  # noqa: E402
from packages.session.models import Conversation  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402
from sse_starlette.sse import AppStatus  # noqa: E402


class ScriptedGateway:
    """按脚本逐轮返回的假网关：记录每轮的消息与 tools。

    turns 每项：{deltas: [str], tool_calls: [ToolCall], error: Exception|None,
    fail_after_deltas: bool}；content 为 deltas 拼接。
    """

    def __init__(self, turns: list[dict[str, Any]] | None = None) -> None:
        self.turns = list(turns or [{"deltas": ["你好", "，有什么可以帮你？"]}])
        self.calls: list[dict[str, Any]] = []

    async def stream_turn(
        self,
        scenario: Scenario,
        messages: list[ModelMessage],
        *,
        tools: list[dict[str, Any]] | None = None,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str | ModelResponse]:
        del params
        self.calls.append(
            {"scenario": scenario, "messages": list(messages), "tools": tools}
        )
        turn = self.turns.pop(0)
        error = turn.get("error")
        if error is not None and not turn.get("fail_after_deltas"):
            raise error
        deltas: list[str] = turn.get("deltas", [])
        for piece in deltas:
            yield piece
        if error is not None:
            raise error
        yield ModelResponse(
            content="".join(deltas),
            model="scripted",
            tool_calls=list(turn.get("tool_calls", [])),
        )


class FakeRegistry:
    """确定性工具注册表替身：按工具名执行 handler。"""

    def __init__(self, handlers: dict[str, Any]) -> None:
        self._handlers = handlers
        self.executed: list[tuple[str, str]] = []

    def openai_tools(self) -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": name, "parameters": {}}}
            for name in self._handlers
        ]

    def execute(self, name: str, arguments_json: str) -> Any:
        self.executed.append((name, arguments_json))
        return self._handlers[name](json.loads(arguments_json or "{}"))


def _events(raw: list[dict[str, str]]) -> list[tuple[str, dict[str, Any]]]:
    return [(item["event"], json.loads(item["data"])) for item in raw]


async def _run_loop(
    store: SessionStore,
    conversation: Conversation,
    gateway: ScriptedGateway,
    registry: Any,
    user_text: str = "分析一下",
    config: AgentLoopConfig | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    raw = [
        item
        async for item in stream_agent_chat(
            conversation_id=conversation.id,
            project_id=conversation.project_id,
            user_text=user_text,
            store=store,
            gateway=cast(Any, gateway),
            registry=cast(AgentToolRegistry, registry),
            locks=ConversationLockPool(),
            config=config or AgentLoopConfig(tool_result_max_chars=500),
        )
    ]
    return _events(raw)


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "chatbi.db"))


@pytest.fixture
def conversation(store: SessionStore) -> Conversation:
    project = store.create_project("测试项目")
    return store.create_conversation(project.id)


def _register_dataset(store: SessionStore, conversation: Conversation, ref: str = "d1") -> None:
    store.register_dataset(
        ref=ref,
        project_id=conversation.project_id,
        filename="销售.xlsx",
        profile={
            "row_count": 3,
            "column_count": 2,
            "columns": [{"name": "月份"}, {"name": "销售额"}],
        },
    )


# ── 工具轮：事件序列、工件落库、结果回填 ──


@pytest.mark.asyncio
async def test_tool_round_emits_transparency_events_and_persists(
    store: SessionStore, conversation: Conversation
) -> None:
    _register_dataset(store, conversation)
    profile_result = {
        "profile": {"row_count": 3, "column_count": 2},
        "quality": {"duplicate_rows": 0},
    }
    registry = FakeRegistry({"get_data_profile": lambda args: profile_result})
    gateway = ScriptedGateway(
        [
            {
                "deltas": ["我先获取数据画像"],
                "tool_calls": [
                    ToolCall(id="c1", name="get_data_profile", arguments='{"dataset_ref":"d1"}')
                ],
            },
            {"deltas": ["结论：", "共 3 行。"]},
        ]
    )

    events = await _run_loop(store, conversation, gateway, registry)

    assert [name for name, _ in events] == [
        "meta",
        "text.delta",       # 工具轮开场白流式吐出
        "understanding",    # 轮末转为理解卡
        "plan",
        "tool_start",
        "artifact",
        "tool_end",
        "text.delta",       # 最终答复流式
        "text.delta",
        "done",
    ]
    by_name = dict(events)
    assert by_name["understanding"]["text"] == "我先获取数据画像"
    assert by_name["plan"]["steps"][0]["tool"] == "get_data_profile"
    assert by_name["tool_end"]["status"] == "ok"
    assert "3 行" in by_name["tool_end"]["summary"]
    assert by_name["done"]["tool_calls"] == 1

    # 工件：类型/来源/analysis_id/数据集归属
    artifacts = store.list_artifacts(conversation.id)
    assert len(artifacts) == 1
    artifact = artifacts[0]
    assert artifact.type == "profile"
    assert artifact.source_tool == "get_data_profile"
    assert artifact.dataset_ref == "d1"
    assert artifact.params is not None and artifact.params["analysis_id"]
    assert by_name["artifact"]["id"] == artifact.id

    # 消息：user + 工具轮 assistant（带 tool_calls）+ 最终 assistant；tool 结果不落消息表
    messages = store.list_messages(conversation.id)
    assert [m.role for m in messages] == ["user", "assistant", "assistant"]
    assert messages[1].tool_calls == [
        {"id": "c1", "name": "get_data_profile", "arguments": '{"dataset_ref":"d1"}'}
    ]
    assert messages[2].content == "结论：共 3 行。"

    # 第二轮模型请求里回填了 tool 结果
    second_call = gateway.calls[1]["messages"]
    tool_messages = [m for m in second_call if m.role == "tool"]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_call_id == "c1"
    assert '"duplicate_rows":0' in tool_messages[0].content


@pytest.mark.asyncio
async def test_tool_failure_feeds_error_back_for_retry(
    store: SessionStore, conversation: Conversation
) -> None:
    """校验/业务失败回传模型带错重试（14.5.1，复用 analyze 已验证模式）。"""
    attempts = {"n": 0}

    def flaky(args: dict[str, Any]) -> dict[str, Any]:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise SchemaValidationError("入参校验失败 @ value_col: 缺少必填字段")
        return {"rows": [], "group_total": 0, "agg": "sum"}

    registry = FakeRegistry({"aggregate_preview": flaky})
    call = {"tool_calls": [ToolCall(id="c1", name="aggregate_preview", arguments="{}")]}
    retry = {
        "tool_calls": [
            ToolCall(id="c2", name="aggregate_preview", arguments='{"value_col":"销售额"}')
        ]
    }
    gateway = ScriptedGateway([call, retry, {"deltas": ["修好了"]}])

    events = await _run_loop(store, conversation, gateway, registry)

    ends = [payload for name, payload in events if name == "tool_end"]
    assert [end["status"] for end in ends] == ["error", "ok"]
    assert "入参校验失败" in ends[0]["message"]
    assert dict(events)["done"]["tool_calls"] == 2
    # 失败结果以 tool 消息回传模型
    second_call = gateway.calls[1]["messages"]
    assert any(
        m.role == "tool" and "工具执行失败" in m.content for m in second_call
    )


@pytest.mark.asyncio
async def test_consecutive_same_tool_same_args_circuit_breaks(
    store: SessionStore, conversation: Conversation
) -> None:
    """连续两次同工具同参数 → 熔断，随后禁用 tools 强制作答（14.5.1）。"""
    registry = FakeRegistry({"kb_search": lambda args: {"is_empty": True, "hits": []}})
    same = {"tool_calls": [ToolCall(id="c1", name="kb_search", arguments='{"query":"口径"}')]}
    same2 = {"tool_calls": [ToolCall(id="c2", name="kb_search", arguments='{"query":"口径"}')]}
    gateway = ScriptedGateway([same, same2, {"deltas": ["我没能检索到更多结果"]}])

    events = await _run_loop(store, conversation, gateway, registry)

    ends = [payload for name, payload in events if name == "tool_end"]
    assert [end["status"] for end in ends] == ["ok", "error"]
    assert "熔断" in ends[1]["message"]
    assert len(registry.executed) == 1          # 第二次没有真正执行
    assert gateway.calls[2]["tools"] is None    # 熔断后禁用 tools 强制作答


@pytest.mark.asyncio
async def test_tool_call_budget_is_enforced(
    store: SessionStore, conversation: Conversation
) -> None:
    """单轮工具调用总数 ≤ max_tool_calls；超出的调用不执行并回传上限提示。"""
    registry = FakeRegistry({"kb_search": lambda args: {"is_empty": True, "hits": []}})
    burst = {
        "tool_calls": [
            ToolCall(id="c1", name="kb_search", arguments='{"query":"a"}'),
            ToolCall(id="c2", name="kb_search", arguments='{"query":"b"}'),
        ]
    }
    gateway = ScriptedGateway([burst, {"deltas": ["就查到这些"]}])
    config = AgentLoopConfig(max_tool_calls=1, tool_result_max_chars=500)

    events = await _run_loop(store, conversation, gateway, registry, config=config)

    ends = [payload for name, payload in events if name == "tool_end"]
    assert [end["status"] for end in ends] == ["ok", "error"]
    assert "上限" in ends[1]["message"]
    assert len(registry.executed) == 1
    assert gateway.calls[1]["tools"] is None
    assert dict(events)["done"]["tool_calls"] == 1


@pytest.mark.asyncio
async def test_system_context_lists_datasets_and_analysis_registry(
    store: SessionStore, conversation: Conversation
) -> None:
    """上下文装配：数据集清单（含血缘）+ 最新画像 + 分析登记表（14.5.2）。"""
    _register_dataset(store, conversation, "d1")
    store.register_dataset(
        ref="d2",
        project_id=conversation.project_id,
        filename="销售.xlsx（衍生）",
        profile={"row_count": 2, "column_count": 2, "columns": []},
        parent_ref="d1",
        transform={"drop_nulls": []},
    )
    seed = store.append_message(
        conversation_id=conversation.id, role="assistant", content="旧分析"
    )
    artifact = store.create_artifact(
        conversation_id=conversation.id,
        message_id=seed.id,
        type="stats",
        payload={"kind": "trend_analysis", "result": {"direction": "up"}},
        source_tool="trend_analysis",
        params={"analysis_id": "an-001", "time_col": "月份"},
        dataset_ref="d1",
    )
    del artifact
    gateway = ScriptedGateway([{"deltas": ["好的"]}])

    await _run_loop(store, conversation, gateway, FakeRegistry({}), user_text="继续")

    system = gateway.calls[0]["messages"][0]
    assert system.role == "system"
    assert "可用数据集" in system.content
    assert "d1" in system.content and "d2" in system.content
    assert "衍生自 d1" in system.content
    assert '"row_count":2' in system.content        # 最新数据集画像
    assert "分析登记表" in system.content
    assert "analysis_id=an-001" in system.content
    assert '"direction":"up"' in system.content     # 登记表摘要


# ── /chat/stream 端点：SSE 协议与持久化（吸收原阶段1 用例）──


@dataclass
class ChatHarness:
    client: TestClient
    store: SessionStore
    gateway: ScriptedGateway
    conversation: Conversation
    requests: list[list[ModelMessage]] = field(default_factory=list)


@pytest.fixture
def chat_harness(tmp_path: Path) -> Iterator[ChatHarness]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("聊天项目")
    conversation = store.create_conversation(project.id)
    gateway = ScriptedGateway()
    app.dependency_overrides[session_store_dep] = lambda: store
    app.dependency_overrides[model_gateway_dep] = lambda: gateway
    app.dependency_overrides[settings_dep] = lambda: Settings(
        chat_db_path=str(tmp_path / "chatbi.db"),
        chat_history_limit=3,
        chat_profile_max_chars=2_000,
    )
    # sse-starlette 的进程级退出 Event 会绑定首次 TestClient 的事件循环；测试隔离时重置。
    AppStatus.should_exit_event = None
    try:
        with TestClient(app) as client:
            yield ChatHarness(client, store, gateway, conversation)
    finally:
        app.dependency_overrides.clear()
        AppStatus.should_exit_event = None


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    normalized = text.replace("\r\n", "\n").strip()
    for block in normalized.split("\n\n"):
        lines = block.splitlines()
        event = next(
            (line.split(": ", 1)[1] for line in lines if line.startswith("event: ")),
            None,
        )
        data = next(
            (line.split(": ", 1)[1] for line in lines if line.startswith("data: ")),
            None,
        )
        if event is not None and data is not None:
            events.append((event, cast(dict[str, Any], json.loads(data))))
    return events


def test_stream_chat_emits_protocol_and_persists_complete_reply(
    chat_harness: ChatHarness,
) -> None:
    response = chat_harness.client.post(
        "/chat/stream",
        json={
            "conversation_id": chat_harness.conversation.id,
            "message": "  请介绍一下这个数据集  ",
        },
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert [name for name, _ in events] == ["meta", "text.delta", "text.delta", "done"]
    meta = events[0][1]
    done = events[-1][1]
    assert meta["conversation_id"] == chat_harness.conversation.id
    assert meta["message_id"] == done["message_id"]
    assert meta["title"] == "请介绍一下这个数据集"
    assert done["characters"] == len("你好，有什么可以帮你？")
    assert done["tool_calls"] == 0

    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "请介绍一下这个数据集"),
        ("assistant", "你好，有什么可以帮你？"),
    ]
    assert messages[1].id == meta["message_id"]
    call = chat_harness.gateway.calls[0]
    assert call["scenario"] == Scenario.AGENT
    assert call["tools"], "Agent 轮必须带工具定义"
    model_messages = call["messages"]
    assert model_messages[0].role == "system"
    assert "编造数字" in model_messages[0].content
    assert model_messages[-1].role == "user"
    assert model_messages[-1].content == "请介绍一下这个数据集"


def test_stream_context_contains_latest_profile_and_limited_history(
    chat_harness: ChatHarness,
) -> None:
    profile = {
        "row_count": 24,
        "column_count": 2,
        "columns": [{"name": "订单数"}, {"name": "销售额"}],
    }
    chat_harness.store.record_profile_upload(
        ref="sales-profile",
        project_id=chat_harness.conversation.project_id,
        conversation_id=chat_harness.conversation.id,
        filename="销售.xlsx",
        profile=profile,
        user_content="上传了文件：销售.xlsx",
        assistant_content="画像完成",
    )
    chat_harness.store.append_message(
        conversation_id=chat_harness.conversation.id,
        role="user",
        content="这条历史会被截掉",
    )
    chat_harness.store.append_message(
        conversation_id=chat_harness.conversation.id,
        role="assistant",
        content="旧回复",
    )

    response = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "当前问题"},
    )

    assert response.status_code == 200
    request = chat_harness.gateway.calls[0]["messages"]
    assert len(request) == 4  # system + 最近 3 条消息
    system = request[0].content
    assert "sales-profile" in system    # 数据集清单必须给出 dataset_ref 供模型调工具
    assert '"row_count":24' in system
    assert "订单数" in system
    assert "分析登记表" in system        # 上传画像工件已入登记表
    assert [message.content for message in request[1:]] == [
        "这条历史会被截掉",
        "旧回复",
        "当前问题",
    ]
    conversation = chat_harness.store.get_conversation(chat_harness.conversation.id)
    assert conversation is not None and conversation.title == "销售.xlsx"


def test_stream_model_failure_emits_error_and_does_not_persist_partial_assistant(
    chat_harness: ChatHarness,
) -> None:
    chat_harness.gateway.turns = [
        {"deltas": ["部分回复"], "error": RuntimeError("provider disconnected"),
         "fail_after_deltas": True},
    ]

    response = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "测试失败"},
    )

    events = _sse_events(response.text)
    assert [name for name, _ in events] == ["meta", "text.delta", "error"]
    assert events[-1][1] == {
        "code": "model_unavailable",
        "message": "模型暂时不可用，请稍后重试。",
        "retryable": True,
    }
    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert [(message.role, message.content) for message in messages] == [("user", "测试失败")]


def test_stream_empty_response_is_not_persisted(chat_harness: ChatHarness) -> None:
    chat_harness.gateway.turns = [{"deltas": []}]

    response = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "空响应"},
    )

    events = _sse_events(response.text)
    assert [name for name, _ in events] == ["meta", "error"]
    assert events[-1][1]["code"] == "empty_response"
    assert [message.role for message in chat_harness.store.list_messages(
        chat_harness.conversation.id
    )] == ["user"]


def test_stream_validates_request_before_model_call(chat_harness: ChatHarness) -> None:
    missing = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": "missing", "message": "你好"},
    )
    blank = chat_harness.client.post(
        "/chat/stream",
        json={"conversation_id": chat_harness.conversation.id, "message": "   "},
    )

    assert missing.status_code == 404
    assert missing.json()["detail"] == "对话不存在"
    assert blank.status_code == 422
    assert chat_harness.gateway.calls == []


@pytest.mark.asyncio
async def test_conversation_lock_pool_serializes_same_conversation() -> None:
    pool = ConversationLockPool()
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    order: list[str] = []

    async def first() -> None:
        async with pool.hold("same"):
            order.append("first-enter")
            first_entered.set()
            await release_first.wait()
            order.append("first-leave")

    async def second() -> None:
        await first_entered.wait()
        async with pool.hold("same"):
            order.append("second-enter")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_entered.wait()
    await asyncio.sleep(0)
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-leave", "second-enter"]
