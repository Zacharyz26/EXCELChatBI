"""阶段 1 第三步：纯 LLM SSE 对话、上下文和持久化测试。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from apps.api.deps import model_gateway_dep, session_store_dep, settings_dep
from apps.api.main import app
from apps.orchestrator.chat_stream import ConversationLockPool
from fastapi.testclient import TestClient
from packages.common.config import Settings
from packages.models.types import Message as ModelMessage
from packages.models.types import Scenario
from packages.session.models import Conversation
from packages.session.store import SessionStore
from sse_starlette.sse import AppStatus


class RecordingGateway:
    """记录模型入参并按脚本流式返回。"""

    def __init__(self, chunks: list[str] | None = None) -> None:
        self.chunks = chunks if chunks is not None else ["你好", "，有什么可以帮你？"]
        self.error: Exception | None = None
        self.fail_after_chunks = False
        self.scenarios: list[Scenario] = []
        self.requests: list[list[ModelMessage]] = []

    async def stream(
        self,
        scenario: Scenario,
        messages: list[ModelMessage],
        *,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str]:
        del params
        self.scenarios.append(scenario)
        self.requests.append(messages)
        if self.error is not None and not self.fail_after_chunks:
            raise self.error
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


@dataclass
class ChatHarness:
    client: TestClient
    store: SessionStore
    gateway: RecordingGateway
    conversation: Conversation


@pytest.fixture
def chat_harness(tmp_path: Path) -> Iterator[ChatHarness]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project("聊天项目")
    conversation = store.create_conversation(project.id)
    gateway = RecordingGateway()
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


def test_stream_chat_emits_protocol_and_persists_complete_reply(chat_harness: ChatHarness) -> None:
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

    messages = chat_harness.store.list_messages(chat_harness.conversation.id)
    assert [(message.role, message.content) for message in messages] == [
        ("user", "请介绍一下这个数据集"),
        ("assistant", "你好，有什么可以帮你？"),
    ]
    assert messages[1].id == meta["message_id"]
    assert chat_harness.gateway.scenarios == [Scenario.AGENT]
    model_messages = chat_harness.gateway.requests[0]
    assert model_messages[0].role == "system"
    assert "禁止编造数字" in model_messages[0].content
    assert model_messages[-1] == ModelMessage(role="user", content="请介绍一下这个数据集")


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
    request = chat_harness.gateway.requests[0]
    assert len(request) == 4  # system + 最近 3 条消息
    assert "sales-profile" not in request[0].content  # 画像载荷不伪造额外引用字段
    assert '"row_count":24' in request[0].content
    assert "订单数" in request[0].content
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
    chat_harness.gateway.chunks = ["部分回复"]
    chat_harness.gateway.error = RuntimeError("provider disconnected")
    chat_harness.gateway.fail_after_chunks = True

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
    chat_harness.gateway.chunks = []

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
    assert chat_harness.gateway.requests == []


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
    assert order == ["first-enter"]
    release_first.set()
    await asyncio.gather(first_task, second_task)
    assert order == ["first-enter", "first-leave", "second-enter"]
