"""阶段 2C 后台 TaskRun 宿主的生命周期测试。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from apps.orchestrator.run_manager import AgentRunManager, ManagedRunControl, SseItem


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
