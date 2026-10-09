"""阶段 2C 后台 TaskRun 宿主的生命周期测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
from apps.api.routers.agent_runs import _prepend_event, _replay_and_subscribe
from apps.api.routers.chat import EventSourceResponse as ChatEventSourceResponse
from apps.orchestrator.run_manager import (
    ActiveConversationRunError,
    AgentRunManager,
    ManagedRunControl,
    SseItem,
)
from packages.session.task_models import TaskEvent, TaskRun
from packages.session.task_store import TaskStore
from sse_starlette.sse import AppStatus


@pytest.mark.asyncio
async def test_subscriber_disconnect_does_not_cancel_producer() -> None:
    manager = AgentRunManager()
    producer_continued = asyncio.Event()
    release = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        yield {"event": "done", "data": '{"run_status":"waiting_user"}'}
        producer_continued.set()
        await release.wait()
        yield {"event": "done", "data": '{"run_status":"completed"}'}

    first = manager.start("run-1", source)
    assert await anext(first) == {
        "event": "done",
        "data": '{"run_status":"waiting_user"}',
    }
    await first.aclose()
    await asyncio.wait_for(producer_continued.wait(), timeout=1)
    assert manager.control_for("run-1") is not None

    resumed = manager.resume("run-1")
    assert resumed is not None
    release.set()
    assert await asyncio.wait_for(anext(resumed), timeout=1) == {
        "event": "done",
        "data": '{"run_status":"completed"}',
    }
    await resumed.aclose()
    await asyncio.sleep(0)
    assert manager.control_for("run-1") is None

    assert "run-1" not in manager._runs


@pytest.mark.asyncio
async def test_close_before_first_read_is_idempotent_and_releases_subscription() -> None:
    manager = AgentRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await release.wait()
        if False:
            yield {"event": "done", "data": "{}"}

    subscription = manager.start("run-unread-close", source)
    await started.wait()
    entry = manager._runs["run-unread-close"]
    assert len(entry.subscribers) == 1

    await subscription.aclose()
    await subscription.aclose()

    assert not entry.subscribers
    assert manager.control_for("run-unread-close") is not None
    release.set()
    await asyncio.sleep(0)
    assert "run-unread-close" not in manager._runs


@pytest.mark.asyncio
async def test_cancelled_waiting_read_releases_subscription() -> None:
    manager = AgentRunManager()
    started = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await asyncio.Event().wait()
        if False:
            yield {"event": "done", "data": "{}"}

    subscription = manager.start("run-read-cancelled", source)
    await started.wait()
    entry = manager._runs["run-read-cancelled"]
    pending = asyncio.create_task(anext(subscription))
    await asyncio.sleep(0)

    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert not entry.subscribers
    await manager.shutdown()
    assert "run-read-cancelled" not in manager._runs


@pytest.mark.asyncio
async def test_prepend_wrapper_closes_unread_subscription() -> None:
    manager = AgentRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await release.wait()
        if False:
            yield {"event": "done", "data": "{}"}

    subscription = manager.start("run-prepend-close", source)
    await started.wait()
    entry = manager._runs["run-prepend-close"]
    wrapped = _prepend_event(
        {"event": "run.resumed", "data": "{}"},
        subscription,
    )
    try:
        assert (await anext(wrapped))["event"] == "run.resumed"
        await wrapped.aclose()
        assert not entry.subscribers
    finally:
        await subscription.aclose()
        release.set()
        await manager.shutdown()


@pytest.mark.asyncio
async def test_response_start_cancellation_closes_unstarted_wrapper_subscription() -> None:
    AppStatus.should_exit_event = None
    manager = AgentRunManager()
    source_started = asyncio.Event()
    finish_source = asyncio.Event()
    response_start = asyncio.Event()
    block_response_start = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        source_started.set()
        await finish_source.wait()
        if False:
            yield {"event": "done", "data": "{}"}

    subscription = manager.start("run-response-start-cancel", source)
    await source_started.wait()
    entry = manager._runs["run-response-start-cancel"]
    wrapped = _prepend_event(
        {"event": "run.resumed", "data": "{}"},
        subscription,
    )
    response = ChatEventSourceResponse(
        wrapped,
        cleanup=subscription,
        ping=3600,
    )

    async def receive() -> dict[str, str]:
        await asyncio.Event().wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.start":
            response_start.set()
            await block_response_start.wait()

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    response_task = asyncio.create_task(response(scope, receive, send))
    try:
        await asyncio.wait_for(response_start.wait(), timeout=1)
        finish_source.set()
        for _ in range(100):
            if entry.finished:
                break
            await asyncio.sleep(0)
        assert entry.finished
        assert len(entry.subscribers) == 1

        response_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await response_task

        assert not entry.subscribers
        assert "run-response-start-cancel" not in manager._runs
        await subscription.aclose()
        await subscription.aclose()
    finally:
        block_response_start.set()
        if not response_task.done():
            response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
        await manager.shutdown()
        AppStatus.should_exit_event = None


@pytest.mark.asyncio
async def test_disconnect_before_first_body_read_closes_actual_subscription() -> None:
    AppStatus.should_exit_event = None
    manager = AgentRunManager()
    source_started = asyncio.Event()
    finish_source = asyncio.Event()
    response_start_waiting = asyncio.Event()
    body_read_started = False

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        source_started.set()
        await finish_source.wait()
        if False:
            yield {"event": "done", "data": "{}"}

    subscription = manager.start("run-before-first-read", source)
    await source_started.wait()
    entry = manager._runs["run-before-first-read"]
    async def tracked_wrapper() -> AsyncIterator[SseItem]:
        nonlocal body_read_started
        body_read_started = True
        async for item in _prepend_event(
            {"event": "run.resumed", "data": "{}"},
            subscription,
        ):
            yield item

    response = ChatEventSourceResponse(
        tracked_wrapper(),
        cleanup=subscription,
        ping=3600,
    )

    async def receive() -> dict[str, str]:
        await response_start_waiting.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.start":
            response_start_waiting.set()
            await asyncio.Event().wait()

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    try:
        await asyncio.wait_for(response(scope, receive, send), timeout=1)
        assert not body_read_started
        assert not entry.subscribers
        finish_source.set()
        for _ in range(100):
            if "run-before-first-read" not in manager._runs:
                break
            await asyncio.sleep(0)
        assert "run-before-first-read" not in manager._runs
        await subscription.aclose()
        await subscription.aclose()
    finally:
        finish_source.set()
        await manager.shutdown()
        AppStatus.should_exit_event = None


@pytest.mark.parametrize("termination", ["disconnect", "cancel"])
@pytest.mark.asyncio
async def test_asgi_send_stage_termination_closes_subscription(
    termination: str,
) -> None:
    AppStatus.should_exit_event = None
    manager = AgentRunManager(subscriber_queue_size=4)
    source_started = asyncio.Event()
    finish_source = asyncio.Event()
    first_body_started = asyncio.Event()
    disconnect = asyncio.Event()
    send_block = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        source_started.set()
        yield {"event": "progress", "data": '{"n":1}'}
        await finish_source.wait()

    subscription = manager.start("run-asgi-disconnect", source)
    await source_started.wait()
    entry = manager._runs["run-asgi-disconnect"]
    response = ChatEventSourceResponse(subscription, ping=3600)

    async def receive() -> dict[str, str]:
        await disconnect.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        if message.get("type") == "http.response.body" and message.get("more_body"):
            first_body_started.set()
            await send_block.wait()

    scope: dict[str, object] = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/stream",
        "raw_path": b"/stream",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
    }
    response_task = asyncio.create_task(response(scope, receive, send))
    try:
        await asyncio.wait_for(first_body_started.wait(), timeout=1)
        finish_source.set()
        for _ in range(100):
            if entry.finished:
                break
            await asyncio.sleep(0)
        assert entry.finished
        queue = next(iter(entry.subscribers))
        assert queue.qsize() < queue.maxsize

        if termination == "disconnect":
            disconnect.set()
            await asyncio.wait_for(response_task, timeout=1)
        else:
            response_task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await response_task

        assert not entry.subscribers
        assert "run-asgi-disconnect" not in manager._runs
    finally:
        disconnect.set()
        if not response_task.done():
            response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
        await subscription.aclose()
        await manager.shutdown()
        AppStatus.should_exit_event = None


@pytest.mark.asyncio
async def test_slow_subscriber_is_bounded_and_fast_reconnect_keeps_order() -> None:
    manager = AgentRunManager(subscriber_queue_size=2)
    started = asyncio.Event()
    release = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await release.wait()
        yield {"event": "done", "data": '{"run_status":"completed"}'}

    slow = manager.start("run-bounded", source)
    await started.wait()
    entry = manager._runs["run-bounded"]
    slow_queue = next(iter(entry.subscribers))
    assert slow_queue.maxsize == 2

    manager.publish("run-bounded", {"event": "progress", "data": '{"n":1}'})
    manager.publish("run-bounded", {"event": "progress", "data": '{"n":2}'})
    manager.publish("run-bounded", {"event": "progress", "data": '{"n":3}'})

    assert slow_queue.qsize() <= 2
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(slow), timeout=1)
    assert slow_queue not in entry.subscribers
    assert manager.control_for("run-bounded") is not None

    reconnected = manager.subscribe("run-bounded")
    assert reconnected is not None
    manager.publish("run-bounded", {"event": "progress", "data": '{"n":4}'})
    manager.publish("run-bounded", {"event": "progress", "data": '{"n":5}'})
    assert (await asyncio.wait_for(anext(reconnected), timeout=1))["data"] == '{"n":4}'
    assert (await asyncio.wait_for(anext(reconnected), timeout=1))["data"] == '{"n":5}'

    release.set()
    assert (await asyncio.wait_for(anext(reconnected), timeout=1))["event"] == "done"
    await reconnected.aclose()
    await asyncio.sleep(0)
    assert manager.control_for("run-bounded") is None

    assert "run-bounded" not in manager._runs


@pytest.mark.asyncio
async def test_full_queue_at_terminal_replays_once_from_consumed_cursor() -> None:
    manager = AgentRunManager(subscriber_queue_size=1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await release.wait()
        yield {
            "event": "step.progress",
            "id": "run-terminal-full:2",
            "data": '{"sequence":2}',
        }
        yield {
            "event": "step.progress",
            "id": "run-terminal-full:3",
            "data": '{"sequence":3}',
        }

    subscription = manager.start("run-terminal-full", source)
    await started.wait()
    manager.publish(
        "run-terminal-full",
        {
            "event": "step.progress",
            "id": "run-terminal-full:1",
            "data": '{"sequence":1}',
        },
    )
    assert (await asyncio.wait_for(anext(subscription), timeout=1))["id"] == ("run-terminal-full:1")
    release.set()
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(subscription), timeout=1)
    await asyncio.sleep(0)
    assert manager.control_for("run-terminal-full") is None
    assert "run-terminal-full" not in manager._runs

    run = TaskRun(
        run_id="run-terminal-full",
        project_id="project-1",
        conversation_id="conversation-1",
        user_message_id="message-1",
        parent_run_id=None,
        goal="test",
        status="completed",
        state_version=2,
        plan_version=1,
        budget={},
        usage={},
        terminal_reason=None,
        created_at="2026-10-06T00:00:00Z",
        updated_at="2026-10-06T00:00:01Z",
        finished_at="2026-10-06T00:00:01Z",
    )
    events = [
        TaskEvent(
            event_id=f"event-{sequence}",
            run_id=run.run_id,
            sequence=sequence,
            event_type="step.progress",
            payload={"n": sequence},
            occurred_at="2026-10-06T00:00:00Z",
        )
        for sequence in (1, 2, 3)
    ]

    class ReplayStore:
        def list_events(
            self,
            run_id: str,
            *,
            after_sequence: int,
            limit: int,
        ) -> list[TaskEvent]:
            assert run_id == run.run_id
            return [event for event in events if event.sequence > after_sequence][:limit]

        def get_run(self, run_id: str) -> TaskRun | None:
            return run if run_id == run.run_id else None

    replay = _replay_and_subscribe(
        cast(TaskStore, ReplayStore()),
        run,
        1,
        None,
    )
    replayed = [item async for item in replay]
    assert [item.get("id") for item in replayed] == [
        f"{run.run_id}:2",
        f"{run.run_id}:3",
        None,
    ]
    assert replayed[-1]["event"] == "done"


@pytest.mark.parametrize("queue_size", [0, -1])
def test_subscriber_queue_size_must_be_positive(queue_size: int) -> None:
    with pytest.raises(ValueError, match="subscriber_queue_size"):
        AgentRunManager(subscriber_queue_size=queue_size)


@pytest.mark.asyncio
async def test_pause_resume_and_duplicate_answer_are_cooperative() -> None:
    manager = AgentRunManager()
    reached_boundary = asyncio.Event()
    question_ready = asyncio.Event()
    answers: list[object] = []

    async def source(control: ManagedRunControl) -> AsyncIterator[SseItem]:
        reached_boundary.set()
        assert await control.wait_until_runnable()
        question_ready.set()
        answer = await control.wait_for_answer("q1")
        answers.append(answer)
        yield {"event": "done", "data": '{"run_status":"completed"}'}

    first = manager.start("run-2", source)
    await reached_boundary.wait()
    assert manager.pause("run-2")
    await asyncio.sleep(0)
    resumed = manager.resume("run-2")
    assert resumed is not None
    await question_ready.wait()
    answered = manager.answer("run-2", question_id="q1", value="销售额")
    assert answered is not None
    duplicate = manager.answer("run-2", question_id="q1", value="订单量")
    assert duplicate is not None
    assert (await asyncio.wait_for(anext(answered), timeout=1))["event"] == "done"
    await answered.aclose()
    await duplicate.aclose()
    await first.aclose()
    await resumed.aclose()
    await asyncio.sleep(0)
    assert "run-2" not in manager._runs
    assert answers == ["销售额"]


@pytest.mark.asyncio
async def test_passive_subscription_does_not_resume_paused_producer() -> None:
    manager = AgentRunManager()
    paused = asyncio.Event()
    resumed = asyncio.Event()

    async def source(control: ManagedRunControl) -> AsyncIterator[SseItem]:
        control.pause()
        paused.set()
        assert await control.wait_until_runnable()
        resumed.set()
        yield {"event": "done", "data": '{"run_status":"completed"}'}

    initial = manager.start("run-passive", source)
    await paused.wait()
    passive = manager.subscribe("run-passive")
    assert passive is not None
    await asyncio.sleep(0)
    assert not resumed.is_set()

    active = manager.resume("run-passive")
    assert active is not None
    assert (await asyncio.wait_for(anext(passive), timeout=1))["event"] == "done"
    assert resumed.is_set()
    await passive.aclose()
    await active.aclose()
    await initial.aclose()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_forgets_background_producers() -> None:
    manager = AgentRunManager()
    started = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await asyncio.Event().wait()
        yield {"event": "done", "data": "{}"}

    subscription = manager.start("run-shutdown", source)
    await started.wait()

    await manager.shutdown()

    assert manager.control_for("run-shutdown") is None
    with pytest.raises(StopAsyncIteration):
        await anext(subscription)


@pytest.mark.asyncio
async def test_conversation_allows_only_one_live_execution_host() -> None:
    manager = AgentRunManager()
    started = asyncio.Event()
    release = asyncio.Event()

    async def source(_control: ManagedRunControl) -> AsyncIterator[SseItem]:
        started.set()
        await release.wait()
        yield {"event": "done", "data": '{"run_status":"completed"}'}

    first = manager.start("run-first", source, conversation_id="conversation-1")
    await started.wait()

    with pytest.raises(ActiveConversationRunError) as captured:
        manager.start("run-second", source, conversation_id="conversation-1")
    assert captured.value.run_id == "run-first"

    release.set()
    assert (await asyncio.wait_for(anext(first), timeout=1))["event"] == "done"
    await first.aclose()
    await asyncio.sleep(0)

    replacement = manager.start(
        "run-second",
        source,
        conversation_id="conversation-1",
    )
    assert (await asyncio.wait_for(anext(replacement), timeout=1))["event"] == "done"
    await replacement.aclose()
