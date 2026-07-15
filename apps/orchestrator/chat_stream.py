"""阶段 1 的纯 LLM 流式对话编排。

这里只做上下文装配、文本流和消息持久化；Agent tools、计划/执行事件与分析登记表
属于阶段 3，不在本模块提前实现。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Protocol

from fastapi.concurrency import run_in_threadpool
from openai import OpenAIError
from packages.common.logging import get_logger
from packages.models.types import Message as ModelMessage
from packages.models.types import Scenario
from packages.session.models import ConversationContext
from packages.session.store import SessionStore

_log = get_logger("orchestrator.chat_stream")

_SYSTEM_PROMPT = """你是 ChatBI 中文数据助手。请始终使用中文、表达清晰简洁。
你可以解释当前数据画像并回答通用问题，但不能假装已经执行统计、聚合、绘图或报告工具。
涉及数据列、行数、空值或其他数字时，只能引用下方数据画像中明确存在的信息；画像未提供的结论必须如实说明无法确认，禁止编造数字。
如果用户要求实际计算而当前上下文没有工具结果，应说明需要运行相应分析后才能给出结论。"""


class StreamingGateway(Protocol):
    """ModelGateway.stream 的最小结构化接口，便于编排层隔离与测试。"""

    def stream(
        self,
        scenario: Scenario,
        messages: list[ModelMessage],
        *,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str]: ...


@dataclass
class _LockEntry:
    lock: asyncio.Lock
    users: int = 0


class ConversationLockPool:
    """单进程内按 conversation_id 串行化流式轮次，避免消息交叉。"""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._entries: dict[str, _LockEntry] = {}

    @asynccontextmanager
    async def hold(self, conversation_id: str) -> AsyncIterator[None]:
        """持有一个对话锁；不同对话仍可并行。"""
        async with self._guard:
            entry = self._entries.get(conversation_id)
            if entry is None:
                entry = _LockEntry(lock=asyncio.Lock())
                self._entries[conversation_id] = entry
            entry.users += 1

        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            async with self._guard:
                entry.users -= 1
                if entry.users == 0:
                    self._entries.pop(conversation_id, None)


async def stream_plain_chat(
    *,
    conversation_id: str,
    user_text: str,
    store: SessionStore,
    gateway: StreamingGateway,
    locks: ConversationLockPool,
    history_limit: int,
    profile_max_chars: int,
) -> AsyncIterator[dict[str, str]]:
    """持久化用户消息、流式调用模型，并在完整成功后保存助手消息。"""
    async with locks.hold(conversation_id):
        assistant_message_id = uuid.uuid4().hex
        try:
            conversation, user_message = await run_in_threadpool(
                store.start_user_turn,
                conversation_id=conversation_id,
                content=user_text,
                suggested_title=_title_from_message(user_text),
            )
            context = await run_in_threadpool(store.load_conversation_context, conversation_id)
        except (sqlite3.Error, ValueError) as exc:
            _log.warning(
                "chat.persist_user_failed",
                conversation_id=conversation_id,
                error=str(exc),
            )
            yield _event(
                "error",
                {
                    "code": "conversation_unavailable",
                    "message": "对话状态已发生变化，请刷新后重试。",
                    "retryable": True,
                },
            )
            return

        if context is None:  # 防御性分支：正常情况下 start_user_turn 后一定存在
            yield _event(
                "error",
                {
                    "code": "conversation_unavailable",
                    "message": "对话不存在或已被删除。",
                    "retryable": False,
                },
            )
            return

        yield _event(
            "meta",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "user_message_id": user_message.id,
                "title": conversation.title,
            },
        )

        model_messages = build_chat_messages(
            context,
            history_limit=max(1, history_limit),
            profile_max_chars=max(1, profile_max_chars),
        )
        chunks: list[str] = []
        try:
            async for piece in gateway.stream(Scenario.AGENT, model_messages):
                if not piece:
                    continue
                chunks.append(piece)
                yield _event("text.delta", {"delta": piece})
        except (OpenAIError, RuntimeError, ValueError) as exc:
            _log.warning("chat.model_failed", conversation_id=conversation_id, error=str(exc))
            yield _event(
                "error",
                {
                    "code": "model_unavailable",
                    "message": "模型暂时不可用，请稍后重试。",
                    "retryable": True,
                },
            )
            return

        assistant_text = "".join(chunks)
        if not assistant_text.strip():
            yield _event(
                "error",
                {
                    "code": "empty_response",
                    "message": "模型没有返回有效内容，请重试。",
                    "retryable": True,
                },
            )
            return

        try:
            await run_in_threadpool(
                store.append_message,
                conversation_id=conversation_id,
                role="assistant",
                content=assistant_text,
                message_id=assistant_message_id,
            )
        except sqlite3.Error as exc:
            _log.error(
                "chat.persist_assistant_failed",
                conversation_id=conversation_id,
                message_id=assistant_message_id,
                error=str(exc),
            )
            yield _event(
                "error",
                {
                    "code": "persistence_failed",
                    "message": "回复已生成，但保存失败，请刷新后重试。",
                    "retryable": True,
                },
            )
            return

        yield _event(
            "done",
            {
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "characters": len(assistant_text),
            },
        )


def build_chat_messages(
    context: ConversationContext,
    *,
    history_limit: int,
    profile_max_chars: int,
) -> list[ModelMessage]:
    """装配纯聊天模型上下文：规则 + 最近画像 + 最近 N 条普通消息。"""
    profile_artifact = next(
        (
            artifact
            for artifact in reversed(context.artifacts)
            if artifact.type == "profile" and artifact.payload is not None
        ),
        None,
    )
    system_content = _SYSTEM_PROMPT
    if profile_artifact is not None and profile_artifact.payload is not None:
        profile_json = json.dumps(
            profile_artifact.payload,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        if len(profile_json) > profile_max_chars:
            profile_json = f"{profile_json[:profile_max_chars]}\n（数据画像已截断）"
        system_content = f"{system_content}\n\n当前数据画像：\n{profile_json}"
        _log.info(
            "chat.profile_context",
            conversation_id=context.conversation.id,
            dataset_ref=profile_artifact.dataset_ref,
            profile_chars=len(profile_json),
        )

    history = [
        ModelMessage(role=message.role, content=message.content)
        for message in context.messages
        if message.role in {"user", "assistant", "system"}
    ][-max(1, history_limit) :]
    return [ModelMessage(role="system", content=system_content), *history]


def _title_from_message(message: str) -> str:
    compact = " ".join(message.split())
    return compact[:30] or "新对话"


def _event(name: str, payload: dict[str, object]) -> dict[str, str]:
    return {
        "event": name,
        "data": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }
