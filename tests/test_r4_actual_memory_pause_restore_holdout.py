"""Independent current-protocol memory pause/recovery; no fabricated contract.

An actual Host first creates its contract/plan/snapshot.  Public TaskStore
control methods pause/resume it; a new Host restores that checkpoint and runs
real tools.  This covers backend recovery, not HTTP/browser pause controls.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from apps.orchestrator.agent_tools import AgentContext
from apps.orchestrator.run_manager import ManagedRunControl
from packages.session.memory_store import MemoryStore
from packages.session.task_store import TaskStore

from tests import test_r4_context_contract as support
from tests.test_agent_loop import _remember_agent_reference
from tests.test_agent_tools import _registry
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401


def _snapshots(store, run_id):
    connection = sqlite3.connect(Path(store.db_path).as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        return [
            dict(row)
            for row in connection.execute(
                "SELECT memory_snapshot_id, run_id, content_hash "
                "FROM memory_snapshots WHERE run_id = ?",
                (run_id,),
            )
        ]
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke", [False, True], ids=["fixed-proof-valid", "memory-revoked"])
async def test_actual_memory_checkpoint_restores_same_source_or_blocks(tmp_path, revoke):
    store, project, conversation = support.workspace(tmp_path)
    named = support.upload(
        store,
        project,
        conversation,
        body=(
            "月份,频道,状态,时长（分钟）\n"
            + "2026-03,B,已播出,900\n" * 6
            + "2026-04,B,已播出,901\n" * 6
        ).encode(),
        name="named-source.csv",
    )
    current = support.upload(
        store,
        project,
        conversation,
        body=(
            "月份,频道,状态,时长（分钟）\n"
            + "2026-01,A,已播出,10\n" * 6
            + "2026-02,A,已播出,20\n" * 6
        ).encode(),
        name="implicit-current.csv",
    )
    memory_id = _remember_agent_reference(
        store,
        conversation,
        alias="主数据",
        target_ref=named,
        key="independent-r4-actual-memory-recovery",
    )
    run_id = uuid.uuid4().hex
    goal = "使用主数据生成折线图"
    config = AgentLoopConfig(max_tool_calls=6, max_model_rounds=6, max_parallel_tools=1)
    initial_gateway = support.ScriptedModel([], end="blocked")
    initial_registry = _registry(AgentContext(store, project.id, conversation.id, run_id=run_id))
    control = ManagedRunControl()
    initial_stream = stream_agent_chat(
        conversation_id=conversation.id,
        project_id=project.id,
        store=store,
        user_text=goal,
        gateway=initial_gateway,
        registry=initial_registry,
        locks=ConversationLockPool(),
        config=config,
        principal=support.PRINCIPAL,
        run_id=run_id,
        control=control,
    )
    initial_events = []
    resumed_events = []
    resume_registry = None
    try:
        async for event in initial_stream:
            initial_events.append(event)
            if event["event"] == "run.started":
                break
        assert initial_gateway.calls == 0, initial_events
        tasks = TaskStore(store.db_path)
        running = tasks.get_run(run_id)
        assert running is not None and running.status == "running", initial_events
        fixed_contract = tasks.get_verified_contract(run_id)
        assert fixed_contract is not None
        assert fixed_contract["analysis_requirements"]["dataset_ref"] == named, fixed_contract
        fixed_snapshots = _snapshots(store, run_id)
        assert len(fixed_snapshots) == 1
        assert tasks.list_invocations(run_id) == []
        paused, _, changed = tasks.control_transition(
            run_id,
            expected_version=running.state_version,
            idempotency_key="independent-r4-pause-memory",
            command="pause",
            allowed_statuses={"running"},
            status="paused",
            event_type="run.paused",
            payload={"reason": "independent_memory_checkpoint"},
            require_idle=True,
            checkpoint_reason="independent_memory_checkpoint",
        )
        assert changed and paused.status == "paused"
        control.pause()
        if revoke:
            memories = MemoryStore(store)
            record = memories.get_record(memory_id, principal=support.PRINCIPAL)
            assert record is not None and record.status == "active"
            memories.soft_delete(
                memory_id,
                project_id=project.id,
                principal=support.PRINCIPAL,
                expected_version=record.version,
                idempotency_key="independent-r4-revoke-memory",
            )
        resumed, _, changed = tasks.control_transition(
            run_id,
            expected_version=paused.state_version,
            idempotency_key="independent-r4-resume-memory",
            command="resume",
            allowed_statuses={"paused"},
            status="running",
            event_type="run.resumed",
            payload={"reason": "independent_memory_checkpoint"},
            require_checkpoint=True,
        )
        assert changed and resumed.status == "running"
        gateway = support.ScriptedModel(
            [
                (
                    "gen_chart",
                    {
                        "dataset_ref": named,
                        "chart_type": "line",
                        "encoding": {"x": "月份", "y": support.METRIC, "agg": "sum"},
                    },
                )
            ],
            "已生成主数据的折线图。",
        )
        resume_registry = _registry(AgentContext(store, project.id, conversation.id, run_id=run_id))
        resumed_events = [
            event
            async for event in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                store=store,
                user_text=goal,
                gateway=gateway,
                registry=resume_registry,
                locks=ConversationLockPool(),
                config=config,
                principal=support.PRINCIPAL,
                run_id=run_id,
                resume_existing=True,
            )
        ]
        final_run = tasks.get_run(run_id)
        invocations = tasks.list_invocations(run_id)
        after_contract = tasks.get_verified_contract(run_id)
        after_snapshots = _snapshots(store, run_id)
        support.write_witness(
            tmp_path,
            "actual-memory-recovery",
            goal=goal,
            named=named,
            implicit_current=current,
            memory_id=memory_id,
            revoke=revoke,
            fixed_contract=fixed_contract,
            after_contract=after_contract,
            fixed_snapshots=fixed_snapshots,
            after_snapshots=after_snapshots,
            initial_events=initial_events,
            resumed_events=resumed_events,
            final_run=asdict(final_run) if final_run else None,
            invocations=[asdict(item) for item in invocations],
            resume_model_calls=gateway.calls,
            resume_model_messages=gateway.messages,
        )
        assert after_contract == fixed_contract
        assert after_snapshots == fixed_snapshots
        assert final_run is not None
        if revoke:
            assert final_run.status in {"failed", "blocked"}, resumed_events
            assert gateway.calls == 0, resumed_events
            assert not invocations, invocations
        else:
            assert final_run.status == "completed", resumed_events
            success = [item for item in invocations if item.status == "succeeded"]
            assert len(success) == 1 and success[0].args["dataset_ref"] == named, invocations
            meta = json.loads(next(e["data"] for e in resumed_events if e["event"] == "meta"))
            assert meta["memory_snapshot_id"] == fixed_snapshots[0]["memory_snapshot_id"]
    finally:
        await initial_stream.aclose()
        await initial_registry.aclose()
        if resume_registry is not None:
            await resume_registry.aclose()
