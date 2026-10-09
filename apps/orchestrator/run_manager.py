"""单进程 TaskRun 执行宿主与 SSE 订阅管理。

生产请求由后台 producer 持有 Agent generator；浏览器连接只是订阅者，断开不会
关闭 producer。阶段 2C 先支持同进程 pause/resume/clarification，持久化 Checkpoint
重建由后续恢复器在同一接口上补齐。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

from packages.session.models import JsonObject

SseItem = dict[str, str]
_DEFAULT_SUBSCRIBER_QUEUE_SIZE = 256
_END = object()
_OVERFLOW = object()


class RunControl(Protocol):
    """Agent 循环依赖的最小控制信号接口。"""

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    async def wait_until_runnable(self) -> bool: ...

    async def wait_for_answer(self, question_id: str) -> object | None: ...

    def active_elapsed_seconds(self) -> float: ...


class ActiveConversationRunError(RuntimeError):
    """A conversation already has a live execution host in this process."""

    def __init__(self, conversation_id: str, run_id: str) -> None:
        self.conversation_id = conversation_id
        self.run_id = run_id
        super().__init__(f"对话已有活动执行宿主: {conversation_id} ({run_id})")


@dataclass(slots=True)
class ManagedRunControl:
    """一个活动 run 的协作式暂停、取消和澄清信号。"""

    _runnable: asyncio.Event = field(default_factory=asyncio.Event)
    _answer_available: asyncio.Event = field(default_factory=asyncio.Event)
    _answers: dict[str, object] = field(default_factory=dict)
    _answered_questions: set[str] = field(default_factory=set)
    _cancelled: bool = False
    _created_at: float = field(default_factory=time.monotonic)
    _suspended_at: float | None = None
    _suspended_seconds: float = 0.0

    def __post_init__(self) -> None:
        self._runnable.set()

    def pause(self) -> None:
        self._begin_suspension()
        self._runnable.clear()

    def resume(self) -> None:
        self._end_suspension()
        self._runnable.set()

    def cancel(self) -> None:
        self._cancelled = True
        self._runnable.set()
        self._answer_available.set()

    def answer(self, question_id: str, value: object) -> None:
        if question_id in self._answered_questions:
            return
        self._answered_questions.add(question_id)
        self._answers[question_id] = value
        self._answer_available.set()

    async def wait_until_runnable(self) -> bool:
        await self._runnable.wait()
        return not self._cancelled

    async def wait_for_answer(self, question_id: str) -> object | None:
        self._begin_suspension()
        try:
            while not self._cancelled:
                if question_id in self._answers:
                    answer = self._answers.pop(question_id)
                    if not self._answers:
                        self._answer_available.clear()
                    return answer
                await self._answer_available.wait()
                if question_id not in self._answers:
                    self._answer_available.clear()
            return None
        finally:
            self._end_suspension()

    def active_elapsed_seconds(self) -> float:
        suspended = self._suspended_seconds
        if self._suspended_at is not None:
            suspended += time.monotonic() - self._suspended_at
        return max(0.0, time.monotonic() - self._created_at - suspended)

    def _begin_suspension(self) -> None:
        if self._suspended_at is None:
            self._suspended_at = time.monotonic()

    def _end_suspension(self) -> None:
        if self._suspended_at is None:
            return
        self._suspended_seconds += time.monotonic() - self._suspended_at
        self._suspended_at = None


@dataclass(slots=True)
class _ManagedRun:
    control: ManagedRunControl
    conversation_id: str | None = None
    subscribers: set[asyncio.Queue[object]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    finished: bool = False


class RunSubscription(Protocol):
    """可确定关闭的 run 事件订阅。"""

    def __aiter__(self) -> AsyncIterator[SseItem]: ...

    async def __anext__(self) -> SseItem: ...

    async def aclose(self) -> None: ...


class _QueueSubscription:
    """Queue-backed subscription whose cleanup does not depend on first iteration."""

    def __init__(
        self,
        manager: AgentRunManager,
        run_id: str,
        entry: _ManagedRun,
        queue: asyncio.Queue[object],
    ) -> None:
        self._manager = manager
        self._run_id = run_id
        self._entry = entry
        self._queue = queue
        self._closed = False

    def __aiter__(self) -> _QueueSubscription:
        return self

    async def __anext__(self) -> SseItem:
        if self._closed:
            raise StopAsyncIteration
        try:
            item = await self._queue.get()
        except asyncio.CancelledError:
            self._close()
            raise
        if item is _END or item is _OVERFLOW:
            self._close()
            raise StopAsyncIteration
        assert isinstance(item, dict)
        typed_item = cast(SseItem, item)
        # done 只结束当前 SSE 订阅；waiting_user/paused 的 producer 继续存活。
        if typed_item.get("event") == "done":
            self._close()
        return typed_item

    async def aclose(self) -> None:
        self._close()

    def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._entry.subscribers.discard(self._queue)
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        if self._entry.finished and not self._entry.subscribers:
            self._manager._runs.pop(self._run_id, None)


class AgentRunManager:
    """持有后台 Agent producer，并向一个或多个 SSE 客户端广播事件。"""

    def __init__(self, *, subscriber_queue_size: int = _DEFAULT_SUBSCRIBER_QUEUE_SIZE) -> None:
        if subscriber_queue_size <= 0:
            raise ValueError("subscriber_queue_size 必须为正整数")
        self._runs: dict[str, _ManagedRun] = {}
        self._conversation_runs: dict[str, str] = {}
        self._subscriber_queue_size = subscriber_queue_size

    def start(
        self,
        run_id: str,
        source_factory: Callable[[ManagedRunControl], AsyncIterator[SseItem]],
        *,
        conversation_id: str | None = None,
    ) -> RunSubscription:
        if run_id in self._runs:
            raise RuntimeError(f"TaskRun 已有活动执行宿主: {run_id}")
        if conversation_id is not None:
            active_run_id = self._conversation_runs.get(conversation_id)
            if active_run_id is not None:
                active_entry = self._runs.get(active_run_id)
                if active_entry is not None and not active_entry.finished:
                    raise ActiveConversationRunError(conversation_id, active_run_id)
                self._conversation_runs.pop(conversation_id, None)
        entry = _ManagedRun(
            control=ManagedRunControl(),
            conversation_id=conversation_id,
        )
        queue = self._new_subscriber_queue()
        entry.subscribers.add(queue)
        self._runs[run_id] = entry
        if conversation_id is not None:
            self._conversation_runs[conversation_id] = run_id
        source = source_factory(entry.control)
        entry.task = asyncio.create_task(
            self._produce(run_id, entry, source),
            name=f"chatbi-run-{run_id}",
        )
        return self._subscription(run_id, entry, queue)

    def control_for(self, run_id: str) -> ManagedRunControl | None:
        entry = self._runs.get(run_id)
        return entry.control if entry is not None and not entry.finished else None

    def pause(self, run_id: str) -> bool:
        control = self.control_for(run_id)
        if control is None:
            return False
        control.pause()
        return True

    def resume(self, run_id: str) -> RunSubscription | None:
        subscription = self.subscribe(run_id)
        if subscription is None:
            return None
        entry = self._runs[run_id]
        entry.control.resume()
        return subscription

    def subscribe(self, run_id: str) -> RunSubscription | None:
        """只订阅活动 producer，不改变暂停、澄清或取消控制状态。"""
        entry = self._runs.get(run_id)
        if entry is None or entry.finished:
            return None
        queue = self._new_subscriber_queue()
        entry.subscribers.add(queue)
        return self._subscription(run_id, entry, queue)

    def answer(
        self,
        run_id: str,
        *,
        question_id: str,
        value: object,
    ) -> RunSubscription | None:
        entry = self._runs.get(run_id)
        if entry is None or entry.finished:
            return None
        queue = self._new_subscriber_queue()
        entry.subscribers.add(queue)
        entry.control.answer(question_id, value)
        return self._subscription(run_id, entry, queue)

    def cancel(self, run_id: str) -> bool:
        control = self.control_for(run_id)
        if control is None:
            return False
        control.cancel()
        return True

    def publish(self, run_id: str, item: SseItem) -> None:
        entry = self._runs.get(run_id)
        if entry is None:
            return
        self._broadcast(entry, item)

    async def shutdown(self) -> None:
        """停止本进程 producer；调用方应先把可恢复运行态持久化为 paused。"""
        tasks = [
            entry.task
            for entry in self._runs.values()
            if entry.task is not None and not entry.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._runs.clear()
        self._conversation_runs.clear()

    async def _produce(
        self,
        run_id: str,
        entry: _ManagedRun,
        source: AsyncIterator[SseItem],
    ) -> None:
        try:
            async for item in source:
                self._broadcast(entry, item)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            payload: JsonObject = {
                "code": "run_host_failed",
                "message": "任务执行宿主异常终止，请刷新任务状态。",
                "retryable": True,
                "detail": type(exc).__name__,
            }
            item = {
                "event": "error",
                "data": json.dumps(payload, ensure_ascii=False),
            }
            self._broadcast(entry, item)
        finally:
            entry.finished = True
            if (
                entry.conversation_id is not None
                and self._conversation_runs.get(entry.conversation_id) == run_id
            ):
                self._conversation_runs.pop(entry.conversation_id, None)
            for queue in tuple(entry.subscribers):
                try:
                    queue.put_nowait(_END)
                except asyncio.QueueFull:
                    self._evict_lagged_subscriber(entry, queue)
            if not entry.subscribers:
                self._runs.pop(run_id, None)

    def _new_subscriber_queue(self) -> asyncio.Queue[object]:
        return asyncio.Queue(maxsize=self._subscriber_queue_size)

    def _broadcast(self, entry: _ManagedRun, item: SseItem) -> None:
        """非阻塞广播；单个慢客户端不能给 producer 施加无界背压。"""
        for queue in tuple(entry.subscribers):
            try:
                queue.put_nowait(item)
            except asyncio.QueueFull:
                self._evict_lagged_subscriber(entry, queue)

    @staticmethod
    def _evict_lagged_subscriber(
        entry: _ManagedRun,
        queue: asyncio.Queue[object],
    ) -> None:
        """结束落后订阅；客户端使用已确认 SSE 游标从持久事件日志重连。"""
        entry.subscribers.discard(queue)
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                break
        # 队列上限已校验为正；清空后 sentinel 必达，不会留下悬挂消费者。
        queue.put_nowait(_OVERFLOW)

    def _subscription(
        self,
        run_id: str,
        entry: _ManagedRun,
        queue: asyncio.Queue[object],
    ) -> RunSubscription:
        return _QueueSubscription(self, run_id, entry, queue)
