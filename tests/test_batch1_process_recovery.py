"""真实子进程强制退出：提交后复用；执行后未提交保留 unknown，不承诺远端 exactly-once。"""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import os
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import AgentLoopConfig, ConversationLockPool, stream_agent_chat
from packages.models.types import ToolCall
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests.test_agent_loop import FakeRegistry, ScriptedGateway


def _crash_worker(
    database: str, project_id: str, conversation_id: str, marker: str, after_commit: bool
) -> None:
    store = SessionStore(database)

    def execute(_args):
        with Path(marker).open("a", encoding="utf-8") as output:
            output.write("effect\n")
            output.flush()
            os.fsync(output.fileno())
        if not after_commit:
            os._exit(77)
        return {"profile": {"row_count": 3, "column_count": 1}}

    if after_commit:
        original = TaskStore.commit_tool_success

        def crash_after_commit(self, *args, **kwargs):
            original(self, *args, **kwargs)
            os._exit(77)

        TaskStore.commit_tool_success = crash_after_commit
    registry = FakeRegistry({"get_data_profile": execute})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="before-crash",
                        name="get_data_profile",
                        arguments=json.dumps({"dataset_ref": "d" * 32}),
                    )
                ]
            }
        ]
    )

    async def run():
        async for _ in stream_agent_chat(
            conversation_id=conversation_id,
            project_id=project_id,
            user_text="读取数据画像",
            store=store,
            registry=registry,
            gateway=gateway,
            locks=ConversationLockPool(),
            config=AgentLoopConfig(max_model_rounds=3),
        ):
            pass

    asyncio.run(run())


@pytest.mark.skipif(
    "fork" not in multiprocessing.get_all_start_methods(), reason="需要 Linux fork 故障注入"
)
@pytest.mark.parametrize("after_commit", [False, True])
def test_process_exit_at_execution_commit_boundary(tmp_path, after_commit):
    store = SessionStore(str(tmp_path / "isolated.db"))
    project = store.create_project("process fault synthetic")
    conversation = store.create_conversation(project.id)
    store.register_dataset(
        ref="d" * 32,
        project_id=project.id,
        filename="synthetic.csv",
        profile={
            "row_count": 3,
            "column_count": 1,
            "columns": [
                {"name": "value", "dtype": "float", "null_ratio": 0.0, "distinct_count": 3}
            ],
        },
    )
    marker = tmp_path / "effects.txt"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_worker,
        args=(str(store.db_path), project.id, conversation.id, str(marker), after_commit),
    )
    process.start()
    process.join(timeout=30)
    if process.is_alive():
        process.terminate()
        process.join(timeout=5)
        pytest.fail("故障注入进程未在时限内退出")
    assert process.exitcode == 77
    assert marker.read_text().splitlines() == ["effect"]
    tasks = TaskStore(store.db_path)
    tasks.recover_stale_runs(stale_after_seconds=0)
    run = tasks.list_runs_for_conversation(conversation.id)[0]
    prior = tasks.list_invocations(run.run_id)
    if not after_commit:
        assert run.status == "failed"
        assert prior[0].status == "unknown"
        assert store.list_artifacts(conversation.id) == []
        return
    assert run.status == "paused"
    original_artifacts = [item.id for item in store.list_artifacts(conversation.id)]
    original_evidence = [item.evidence_id for item in tasks.list_evidence(run.run_id)]
    assert len(original_artifacts) == len(original_evidence) == 1
    run, _, _ = tasks.control_transition(
        run.run_id,
        expected_version=run.state_version,
        idempotency_key="user-resume",
        command="resume",
        allowed_statuses={"paused"},
        status="running",
        event_type="run.resumed",
        payload={},
        require_checkpoint=True,
    )
    registry = FakeRegistry({"get_data_profile": lambda _args: pytest.fail("已提交调用不得重执行")})
    gateway = ScriptedGateway(
        [
            {
                "tool_calls": [
                    ToolCall(
                        id="changed-call-id",
                        name="get_data_profile",
                        arguments=json.dumps({"dataset_ref": "d" * 32}),
                    )
                ]
            },
            {"deltas": ["画像已完成。"]},
        ]
    )

    async def resume():
        return [
            item
            async for item in stream_agent_chat(
                conversation_id=conversation.id,
                project_id=project.id,
                user_text="读取数据画像",
                store=store,
                registry=registry,
                gateway=gateway,
                locks=ConversationLockPool(),
                config=AgentLoopConfig(max_model_rounds=3),
                run_id=run.run_id,
                resume_existing=True,
            )
        ]

    events = asyncio.run(resume())
    assert any(
        json.loads(item["data"]).get("reused") for item in events if item["event"] == "tool_end"
    )
    assert [item.id for item in store.list_artifacts(conversation.id)] == original_artifacts
    assert [item.evidence_id for item in tasks.list_evidence(run.run_id)] == original_evidence
    assert len(tasks.list_invocations(run.run_id)) == 1
    assert marker.read_text().splitlines() == ["effect"]
    assert tasks.get_run(run.run_id).status == "completed"
