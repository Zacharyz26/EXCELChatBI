"""v2.4 TaskRun 查询与阶段 2C 控制接口。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from mcp_servers.common.base_server import MCPServer
from packages.common.config import Settings
from packages.governance.permissions import Principal
from packages.models.gateway import ModelGateway
from packages.rag.retriever import HybridRetriever
from packages.session.store import SessionStore
from packages.session.task_models import TaskEvent, TaskRun
from packages.session.task_store import (
    ControlConflict,
    IdempotencyConflict,
    StateVersionConflict,
    TaskStore,
)
from sse_starlette.sse import EventSourceResponse

from apps.api.auth import current_principal_dep
from apps.api.authz import require_run_access
from apps.api.deps import (
    chart_tools_dep,
    dataset_ops_tools_dep,
    excel_tools_dep,
    model_gateway_dep,
    report_tools_dep,
    retriever_dep,
    session_store_dep,
    settings_dep,
    stats_tools_dep,
)
from apps.api.run_host import agent_run_manager, conversation_locks
from apps.api.schemas import ClarificationAnswerRequest
from apps.orchestrator.agent_loop import AgentLoopConfig, stream_agent_chat
from apps.orchestrator.agent_tools import (
    AgentContext,
    build_registry,
    mcp_client_config_from_settings,
)

router = APIRouter(prefix="/agent/runs", tags=["agent-runs"])


@dataclass(frozen=True, slots=True)
class _RunExecutionServices:
    gateway: ModelGateway
    settings: Settings
    excel: MCPServer
    stats: MCPServer
    chart: MCPServer
    dataset_ops: MCPServer
    report: MCPServer
    retriever: HybridRetriever


def _run_execution_services_dep(
    gateway: ModelGateway = Depends(model_gateway_dep),
    settings: Settings = Depends(settings_dep),
    excel: MCPServer = Depends(excel_tools_dep),
    stats: MCPServer = Depends(stats_tools_dep),
    chart: MCPServer = Depends(chart_tools_dep),
    dataset_ops: MCPServer = Depends(dataset_ops_tools_dep),
    report: MCPServer = Depends(report_tools_dep),
    retriever: HybridRetriever = Depends(retriever_dep),
) -> _RunExecutionServices:
    return _RunExecutionServices(
        gateway=gateway,
        settings=settings,
        excel=excel,
        stats=stats,
        chart=chart,
        dataset_ops=dataset_ops,
        report=report,
        retriever=retriever,
    )


@router.post("/{run_id}/pause")
async def pause_agent_run(
    run_id: str,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    tasks, run = await _writable_run(store, run_id, principal)
    control = agent_run_manager.control_for(run_id)
    if control is None:
        raise HTTPException(status_code=409, detail="任务没有活动执行宿主")
    control.pause()
    try:
        updated, event, created = await run_in_threadpool(
            tasks.control_transition,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            command="pause",
            allowed_statuses={"running"},
            status="paused",
            event_type="run.paused",
            payload={"reason": "user_requested"},
            require_idle=True,
            checkpoint_reason="user_pause",
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        if run.status == "paused":
            control.pause()
        else:
            control.resume()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not created and updated.status != "paused":
        control.resume()
    if created:
        item = _sse_task_event(event, updated.conversation_id)
        agent_run_manager.publish(run_id, item)
        agent_run_manager.publish(
            run_id,
            _sse_done(updated, event.sequence),
        )
    return _control_payload(updated, event, replayed=not created)


@router.post("/{run_id}/resume/stream", response_class=EventSourceResponse)
async def resume_agent_run(
    run_id: str,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
    execution: _RunExecutionServices = Depends(_run_execution_services_dep),
) -> EventSourceResponse:
    tasks, run = await _writable_run(store, run_id, principal)
    has_active_host = agent_run_manager.control_for(run_id) is not None
    try:
        updated, event, created = await run_in_threadpool(
            tasks.control_transition,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            command="resume",
            allowed_statuses={"paused"},
            status="running",
            event_type="run.resumed",
            payload={"reason": "user_requested"},
            require_checkpoint=True,
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    subscription: AsyncIterator[dict[str, str]] | None
    if not created and updated.status != "running":
        subscription = _single_event(_sse_done(updated, event.sequence))
    elif has_active_host:
        subscription = agent_run_manager.resume(run_id)
    else:
        subscription = _start_recovered_run(
            updated,
            store=store,
            principal=principal,
            execution=execution,
        )
    if subscription is None:
        raise HTTPException(status_code=409, detail="任务执行宿主恢复失败")
    return EventSourceResponse(
        _prepend_event(
            _sse_task_event(event, updated.conversation_id),
            subscription,
        ),
        ping=15,
    )


@router.post(
    "/{run_id}/clarifications/{question_id}/answer/stream",
    response_class=EventSourceResponse,
)
async def answer_agent_clarification(
    run_id: str,
    question_id: str,
    req: ClarificationAnswerRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
    execution: _RunExecutionServices = Depends(_run_execution_services_dep),
) -> EventSourceResponse:
    tasks, run = await _writable_run(store, run_id, principal)
    has_active_host = agent_run_manager.control_for(run_id) is not None
    try:
        updated, event, created = await run_in_threadpool(
            tasks.answer_clarification,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            question_id=question_id,
            resume_token=req.resume_token,
            answer=req.answer,
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    store.invalidate_conversation(updated.conversation_id)
    subscription: AsyncIterator[dict[str, str]] | None
    if not created and updated.status not in {"planning", "running"}:
        subscription = _single_event(_sse_done(updated, event.sequence))
    elif has_active_host:
        subscription = agent_run_manager.answer(
            run_id,
            question_id=question_id,
            value=req.answer,
        )
    else:
        subscription = _start_recovered_run(
            updated,
            store=store,
            principal=principal,
            execution=execution,
            clarification_question_id=question_id,
            clarification_answer=req.answer,
        )
    if subscription is None:
        raise HTTPException(status_code=409, detail="任务执行宿主恢复失败")
    return EventSourceResponse(
        _prepend_event(
            _sse_task_event(event, updated.conversation_id),
            subscription,
        ),
        ping=15,
    )


@router.post("/{run_id}/cancel")
async def cancel_agent_run(
    run_id: str,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    tasks, run = await _writable_run(store, run_id, principal)
    try:
        updated, event, created = await run_in_threadpool(
            tasks.control_transition,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            command="cancel",
            allowed_statuses={
                "planning",
                "waiting_user",
                "running",
                "verifying",
                "paused",
            },
            status="cancelled",
            event_type="run.cancelled",
            payload={"reason": "user_requested"},
            terminal_reason="user_cancelled",
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        agent_run_manager.cancel(run_id)
        item = _sse_task_event(event, updated.conversation_id)
        agent_run_manager.publish(run_id, item)
        agent_run_manager.publish(run_id, _sse_done(updated, event.sequence))
    return _control_payload(updated, event, replayed=not created)


@router.post(
    "/{run_id}/steps/{step_id}/retry/stream",
    response_class=EventSourceResponse,
)
async def retry_agent_step(
    run_id: str,
    step_id: str,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
    execution: _RunExecutionServices = Depends(_run_execution_services_dep),
) -> EventSourceResponse:
    tasks, run = await _writable_run(store, run_id, principal)
    has_active_host = agent_run_manager.control_for(run_id) is not None
    try:
        updated, _plan, _steps, event, created = await run_in_threadpool(
            tasks.retry_step,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            step_id=step_id,
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    subscription: AsyncIterator[dict[str, str]] | None
    if not created and updated.status != "running":
        subscription = _single_event(_sse_done(updated, event.sequence))
    elif has_active_host:
        subscription = agent_run_manager.resume(run_id)
    else:
        subscription = _start_recovered_run(
            updated,
            store=store,
            principal=principal,
            execution=execution,
        )
    if subscription is None:
        raise HTTPException(status_code=409, detail="任务执行宿主恢复失败")
    return EventSourceResponse(
        _prepend_event(
            _sse_task_event(event, updated.conversation_id),
            subscription,
        ),
        ping=15,
    )


@router.get("/{run_id}")
async def get_agent_run(
    run_id: str,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    require_run_access(store, run, principal)
    contract = await run_in_threadpool(tasks.get_contract, run_id)
    snapshot = await run_in_threadpool(tasks.get_snapshot, run_id)
    plan = await run_in_threadpool(tasks.get_active_plan, run_id)
    steps = await run_in_threadpool(tasks.list_plan_steps, run_id)
    return {
        "run": _run_payload(run),
        "contract": contract,
        "plan": (
            {
                "plan_id": plan.plan_id,
                "version": plan.version,
                "reason": plan.reason,
                "definition": plan.plan,
                "created_at": plan.created_at,
            }
            if plan is not None
            else None
        ),
        "steps": [
            {
                "step_id": step.logical_id,
                "persisted_step_id": step.step_id,
                "position": step.position,
                "status": step.status,
                "definition": step.definition,
                "started_at": step.started_at,
                "completed_at": step.completed_at,
            }
            for step in steps
        ],
        "state": snapshot,
    }


@router.get("/{run_id}/events")
async def get_agent_run_events(
    run_id: str,
    after_sequence: int = Query(default=0, ge=0),
    limit: int = Query(default=200, ge=1, le=1000),
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    require_run_access(store, run, principal)
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


async def _writable_run(
    store: SessionStore,
    run_id: str,
    principal: Principal,
) -> tuple[TaskStore, TaskRun]:
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    require_run_access(store, run, principal, write=True)
    return tasks, run


def _start_recovered_run(
    run: TaskRun,
    *,
    store: SessionStore,
    principal: Principal,
    execution: _RunExecutionServices,
    clarification_question_id: str | None = None,
    clarification_answer: object | None = None,
) -> AsyncIterator[dict[str, str]]:
    registry = build_registry(
        excel=execution.excel,
        stats=execution.stats,
        chart=execution.chart,
        dataset_ops=execution.dataset_ops,
        report=execution.report,
        retriever=execution.retriever,
        context=AgentContext(
            store=store,
            project_id=run.project_id,
            conversation_id=run.conversation_id,
        ),
        mcp_config=mcp_client_config_from_settings(execution.settings),
    )
    settings = execution.settings
    stored_max_tool_calls = run.budget.get("max_tool_calls")
    max_tool_calls = settings.agent_max_tool_calls
    if (
        isinstance(stored_max_tool_calls, int)
        and not isinstance(stored_max_tool_calls, bool)
        and stored_max_tool_calls > 0
    ):
        max_tool_calls = min(max_tool_calls, stored_max_tool_calls)
    config = AgentLoopConfig(
        history_limit=settings.chat_history_limit,
        profile_max_chars=settings.chat_profile_max_chars,
        compaction_trigger_chars=settings.chat_compaction_trigger_chars,
        compaction_keep_recent=settings.chat_compaction_keep_recent,
        compaction_summary_max_chars=settings.chat_compaction_summary_max_chars,
        compaction_message_max_chars=settings.chat_compaction_message_max_chars,
        max_tool_calls=max_tool_calls,
        tool_result_max_chars=settings.agent_tool_result_max_chars,
        registry_max_entries=settings.agent_registry_max_entries,
        run_timeout_seconds=settings.agent_run_timeout_seconds,
        model_timeout_seconds=settings.agent_model_timeout_seconds,
        tool_timeout_seconds=settings.agent_tool_timeout_seconds,
    )
    return agent_run_manager.start(
        run.run_id,
        lambda control: stream_agent_chat(
            conversation_id=run.conversation_id,
            project_id=run.project_id,
            user_text=run.goal,
            store=store,
            gateway=execution.gateway,
            registry=registry,
            locks=conversation_locks,
            config=config,
            planner_gateway=execution.gateway,
            principal=principal,
            run_id=run.run_id,
            control=control,
            resume_existing=True,
            clarification_question_id=clarification_question_id,
            clarification_answer=clarification_answer,
        ),
    )


def _state_version(value: str) -> int:
    clean = value.strip().strip('"')
    try:
        parsed = int(clean)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="If-Match 必须是整数 state_version",
        ) from exc
    if parsed < 1:
        raise HTTPException(
            status_code=400,
            detail="If-Match 必须是正整数 state_version",
        )
    return parsed


def _control_payload(
    run: TaskRun,
    event: TaskEvent,
    *,
    replayed: bool,
) -> dict[str, object]:
    return {
        "run": _run_payload(run),
        "event": _event_payload(event),
        "replayed": replayed,
    }


def _sse_task_event(
    event: TaskEvent,
    conversation_id: str,
) -> dict[str, str]:
    return {
        "id": f"{event.run_id}:{event.sequence}",
        "event": event.event_type,
        "data": json.dumps(
            {
                "schema_version": "2.0",
                "event_id": event.event_id,
                "run_id": event.run_id,
                "conversation_id": conversation_id,
                "sequence": event.sequence,
                "occurred_at": event.occurred_at,
                "payload": event.payload,
            },
            ensure_ascii=False,
        ),
    }


def _sse_done(run: TaskRun, last_sequence: int) -> dict[str, str]:
    return {
        "event": "done",
        "data": json.dumps(
            {
                "conversation_id": run.conversation_id,
                "run_id": run.run_id,
                "run_status": run.status,
                "last_sequence": last_sequence,
                "characters": 0,
                "tool_calls": run.usage.get("tool_calls", 0),
            },
            ensure_ascii=False,
        ),
    }


async def _prepend_event(
    first: dict[str, str],
    rest: AsyncIterator[dict[str, str]],
) -> AsyncIterator[dict[str, str]]:
    yield first
    async for item in rest:
        yield item


async def _single_event(item: dict[str, str]) -> AsyncIterator[dict[str, str]]:
    yield item
