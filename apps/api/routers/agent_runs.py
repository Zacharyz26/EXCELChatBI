"""Read-only v2.4 TaskRun and lifecycle event recovery endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from packages.session.store import SessionStore
from packages.session.task_models import TaskEvent, TaskRun
from packages.session.task_store import TaskStore

from apps.api.deps import session_store_dep

router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])


@router.get("/{run_id}")
async def get_agent_run(
    run_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> dict[str, object]:
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    contract = await run_in_threadpool(tasks.get_contract, run_id)
    snapshot = await run_in_threadpool(tasks.get_snapshot, run_id)
    return {
        "run": _run_payload(run),
        "contract": contract,
        "state": snapshot,
    }


@router.get("/{run_id}/events")
async def get_agent_run_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    store: SessionStore = Depends(session_store_dep),
) -> dict[str, object]:
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    events = await run_in_threadpool(
        tasks.list_events,
        run_id,
        after_sequence=after_sequence,
        limit=limit,
    )
    return {
        "run_id": run_id,
        "events": [_event_payload(event) for event in events],
        "last_sequence": events[-1].sequence if events else after_sequence,
    }


def _run_payload(run: TaskRun) -> dict[str, object]:
    return {
        "run_id": run.run_id,
        "project_id": run.project_id,
        "conversation_id": run.conversation_id,
        "user_message_id": run.user_message_id,
        "parent_run_id": run.parent_run_id,
        "goal": run.goal,
        "status": run.status,
        "state_version": run.state_version,
        "plan_version": run.plan_version,
        "budget": run.budget,
        "usage": run.usage,
        "terminal_reason": run.terminal_reason,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "finished_at": run.finished_at,
    }


def _event_payload(event: TaskEvent) -> dict[str, object]:
    return {
        "schema_version": "2.0",
        "event_id": event.event_id,
        "run_id": event.run_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "payload": event.payload,
        "occurred_at": event.occurred_at,
    }
