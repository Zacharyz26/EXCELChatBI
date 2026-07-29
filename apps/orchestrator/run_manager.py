"""单进程 TaskRun 执行宿主与 SSE 订阅管理。

生产请求由后台 producer 持有 Agent generator；浏览器连接只是订阅者，断开不会
关闭 producer。阶段 2C 先支持同进程 pause/resume/clarification，持久化 Checkpoint
重建由后续恢复器在同一接口上补齐。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Protocol, cast

from packages.session.models import JsonObject

SseItem = dict[str, str]
_END = object()


class RunControl(Protocol):
    """Agent 循环依赖的最小控制信号接口。"""

    async def wait_until_runnable(self) -> bool: ...

    async def wait_for_answer(self, question_id: str) -> object | None: ...

    def active_elapsed_seconds(self) -> float: ...


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
    subscribers: set[asyncio.Queue[object]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    finished: bool = False


class AgentRunManager:
    """持有后台 Agent producer，并向一个或多个 SSE 客户端广播事件。"""

    def __init__(self) -> None:
        self._runs: dict[str, _ManagedRun] = {}

    def start(
        self,
        run_id: str,
        source_factory: Callable[[ManagedRunControl], AsyncIterator[SseItem]],
    ) -> AsyncGenerator[SseItem, None]:
        if run_id in self._runs:
            raise RuntimeError(f"TaskRun 已有活动执行宿主: {run_id}")
        entry = _ManagedRun(control=ManagedRunControl())
        queue: asyncio.Queue[object] = asyncio.Queue()
        entry.subscribers.add(queue)
        self._runs[run_id] = entry
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

    def resume(self, run_id: str) -> AsyncGenerator[SseItem, None] | None:
        entry = self._runs.get(run_id)
        if entry is None or entry.finished:
            return None
        queue: asyncio.Queue[object] = asyncio.Queue()
        entry.subscribers.add(queue)
        entry.control.resume()
        return self._subscription(run_id, entry, queue)

    def answer(
        self,
        run_id: str,
        *,
        question_id: str,
        value: object,
    ) -> AsyncGenerator[SseItem, None] | None:
        entry = self._runs.get(run_id)
        if entry is None or entry.finished:
            return None
        queue: asyncio.Queue[object] = asyncio.Queue()
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
        for queue in tuple(entry.subscribers):
            queue.put_nowait(item)

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

    async def _produce(
        self,
        run_id: str,
        entry: _ManagedRun,
        source: AsyncIterator[SseItem],
    ) -> None:
        try:
            async for item in source:
                for queue in tuple(entry.subscribers):
                    queue.put_nowait(item)
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
            for queue in tuple(entry.subscribers):
                queue.put_nowait(item)
        finally:
            entry.finished = True
            for queue in tuple(entry.subscribers):
                queue.put_nowait(_END)
            if not entry.subscribers:
                self._runs.pop(run_id, None)

    async def _subscription(
        self,
        run_id: str,
        entry: _ManagedRun,
        queue: asyncio.Queue[object],
    ) -> AsyncGenerator[SseItem, None]:
        try:
            while True:
                item = await queue.get()
                if item is _END:
                    return
                assert isinstance(item, dict)
                typed_item = cast(SseItem, item)
                yield typed_item
                # done 只结束当前 SSE 订阅；waiting_user/paused 的 producer 继续存活。
                if typed_item.get("event") == "done":
                    return
        finally:
            entry.subscribers.discard(queue)
            if entry.finished and not entry.subscribers:
                self._runs.pop(run_id, None)
