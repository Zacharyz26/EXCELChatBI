"""v2.4 TaskRun 查询与阶段 2C 控制接口。"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterator
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
from packages.session.task_models import (
    ApprovalRecord,
    CancellationNode,
    EvidenceLedgerEntry,
    EvidenceRecord,
    TaskDatasetBinding,
    TaskEvent,
    TaskExecutionScope,
    TaskPlanRecord,
    TaskRun,
    TaskStepRecord,
    ToolInvocation,
)
from packages.session.task_store import (
    ControlConflict,
    IdempotencyConflict,
    StateVersionConflict,
    TaskStore,
)
from sse_starlette.sse import EventSourceResponse

from apps.api.auth import current_principal_dep
from apps.api.authz import require_conversation_access, require_run_access
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
from apps.api.schemas import (
    ApprovalDecisionRequest,
    ApprovalResponse,
    ClarificationAnswerRequest,
    PlanRevisionRequest,
    RunFeedbackRequest,
)
from apps.orchestrator.agent_loop import AgentLoopConfig, stream_agent_chat
from apps.orchestrator.agent_tools import (
    AgentContext,
    build_registry,
    enabled_capability_profiles_from_settings,
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
    if await run_in_threadpool(tasks.has_valid_pending_approval, run_id):
        raise HTTPException(
            status_code=409,
            detail="任务仍有待决定的高风险授权，不能恢复执行",
        )
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


@router.post("/{run_id}/plan/revisions")
async def revise_agent_plan(
    run_id: str,
    req: PlanRevisionRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    """在 paused 安全边界创建不可变用户计划修订；不会自动恢复执行。"""
    tasks, _run = await _writable_run(store, run_id, principal)
    try:
        updated, plan, steps, event, created = await run_in_threadpool(
            tasks.revise_plan_by_user,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            plan=req.plan,
            reason=req.reason,
            skipped_step_ids=set(req.skipped_step_ids),
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if created:
        agent_run_manager.publish(
            run_id,
            _sse_task_event(event, updated.conversation_id),
        )
    return {
        "run": _run_payload(updated),
        "plan": _plan_payload(plan),
        "steps": [_step_payload(step) for step in steps],
        "event": _event_payload(event),
        "replayed": not created,
    }


@router.get(
    "/{run_id}/approvals",
    response_model=list[ApprovalResponse],
)
async def list_agent_approvals(
    run_id: str,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> list[ApprovalResponse]:
    """返回当前认证主体在该 TaskRun 中可见的 ApprovalRecord。"""
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    require_run_access(store, run, principal)
    records = await run_in_threadpool(
        tasks.list_approvals,
        run_id,
        tenant_id=principal.tenant_scope,
        subject_user_id=principal.user_id,
    )
    return [ApprovalResponse.model_validate(_approval_payload(record)) for record in records]


@router.post(
    "/{run_id}/approvals/{approval_id}/decision",
)
async def decide_agent_approval(
    run_id: str,
    approval_id: str,
    req: ApprovalDecisionRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    """批准或拒绝固定版本授权；决定后任务仍保持 paused，需显式恢复。"""
    tasks, _run = await _writable_run(store, run_id, principal)
    approval = await run_in_threadpool(tasks.get_approval, approval_id)
    if approval is None or approval.run_id != run_id:
        raise HTTPException(status_code=404, detail="授权请求不存在")
    try:
        updated, decided, event, created = await run_in_threadpool(
            tasks.decide_approval,
            approval_id,
            expected_run_version=_state_version(if_match),
            expected_approval_version=req.expected_version,
            idempotency_key=idempotency_key,
            tenant_id=principal.tenant_scope,
            actor_user_id=principal.user_id,
            decision=req.decision,
            reason=req.reason,
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if created:
        agent_run_manager.publish(
            run_id,
            _sse_task_event(event, updated.conversation_id),
        )
    return {
        "run": _run_payload(updated),
        "approval": _approval_payload(decided),
        "event": _event_payload(event),
        "replayed": not created,
    }


@router.get("/by-conversation/{conversation_id}/latest")
async def get_latest_conversation_run(
    conversation_id: str,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    """返回当前主体可见对话的最近 TaskRun，供刷新和新浏览器会话恢复。"""
    await run_in_threadpool(
        require_conversation_access,
        store,
        conversation_id,
        principal,
    )
    run = await run_in_threadpool(
        TaskStore(store.db_path).get_latest_run_for_conversation,
        conversation_id,
    )
    return {"run": _run_payload(run) if run is not None else None}


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
    invocations = await run_in_threadpool(tasks.list_invocations, run_id)
    evidence = await run_in_threadpool(tasks.list_evidence, run_id)
    execution_scope = await run_in_threadpool(tasks.get_execution_scope, run_id)
    dataset_bindings = await run_in_threadpool(tasks.list_dataset_bindings, run_id)
    cancellation_nodes = await run_in_threadpool(tasks.list_cancellation_nodes, run_id)
    evidence_ledger = await run_in_threadpool(tasks.list_evidence_ledger, run_id)
    data_version_hash = (
        await run_in_threadpool(tasks.data_version_hash, run_id)
        if execution_scope is not None
        else None
    )
    started_events = await run_in_threadpool(
        tasks.list_events_by_type,
        run_id,
        "step.started",
    )
    related_runs = await run_in_threadpool(
        tasks.list_runs_for_conversation,
        run.conversation_id,
        limit=50,
    )
    feedback_events = await run_in_threadpool(
        tasks.list_recent_events_by_type,
        run_id,
        "user.feedback",
        limit=100,
    )
    return {
        "run": _run_payload(run),
        "contract": contract,
        "plan": (_plan_payload(plan) if plan is not None else None),
        "steps": [_step_payload(step) for step in steps],
        "tool_audits": _tool_audit_payloads(
            invocations,
            evidence,
            started_events,
            cancellation_nodes,
            evidence_ledger,
        ),
        "execution_control": _execution_control_payload(
            execution_scope,
            dataset_bindings,
            cancellation_nodes,
            evidence_ledger,
            data_version_hash,
        ),
        "related_runs": [_run_payload(item) for item in related_runs],
        "feedback": [_feedback_payload(event) for event in feedback_events],
        "hypothesis_screening": (
            snapshot.get("hypothesis_screening") if snapshot is not None else None
        ),
        "hypothesis_execution": (
            snapshot.get("hypothesis_execution") if snapshot is not None else None
        ),
        "hypothesis_followup": (
            snapshot.get("hypothesis_followup") if snapshot is not None else None
        ),
        "state": snapshot,
    }


@router.post("/{run_id}/feedback")
async def record_agent_run_feedback(
    run_id: str,
    req: RunFeedbackRequest,
    idempotency_key: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=8, max_length=200),
    ],
    if_match: Annotated[str, Header(alias="If-Match")],
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> dict[str, object]:
    tasks, _run = await _writable_run(store, run_id, principal)
    try:
        updated, event, created = await run_in_threadpool(
            tasks.record_user_feedback,
            run_id,
            expected_version=_state_version(if_match),
            idempotency_key=idempotency_key,
            subject_user_id=principal.user_id,
            rating=req.rating,
            comment=req.comment,
            evidence_ids=tuple(req.evidence_ids),
            artifact_ids=tuple(req.artifact_ids),
        )
    except (ControlConflict, IdempotencyConflict, StateVersionConflict) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if created:
        agent_run_manager.publish(
            run_id,
            _sse_task_event(event, updated.conversation_id),
        )
    return {
        "run": _run_payload(updated),
        "event": _event_payload(event),
        "feedback": _feedback_payload(event),
        "replayed": not created,
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


@router.get("/{run_id}/stream", response_class=EventSourceResponse)
async def reconnect_agent_run_stream(
    run_id: str,
    after_sequence: int | None = Query(default=None, ge=0),
    last_event_id: Annotated[
        str | None,
        Header(alias="Last-Event-ID", max_length=200),
    ] = None,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> EventSourceResponse:
    """回放游标后的持久事件，再无控制副作用地接续当前 producer。"""
    tasks = TaskStore(store.db_path)
    run = await run_in_threadpool(tasks.get_run, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    require_run_access(store, run, principal)
    cursor = _reconnect_cursor(run_id, after_sequence, last_event_id)
    # 先订阅再查询，查询窗口内新提交的事件会同时出现在队列与数据库，生成器按 sequence 去重。
    subscription = agent_run_manager.subscribe(run_id)
    return EventSourceResponse(
        _replay_and_subscribe(tasks, run, cursor, subscription),
        ping=15,
        headers={"X-ChatBI-Run-ID": run_id},
    )


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
        "autonomy_mode": run.autonomy_mode,
    }


def _reconnect_cursor(
    run_id: str,
    after_sequence: int | None,
    last_event_id: str | None,
) -> int:
    header_cursor: int | None = None
    if last_event_id:
        raw_run_id, separator, raw_sequence = last_event_id.rpartition(":")
        if not separator or raw_run_id != run_id:
            raise HTTPException(status_code=400, detail="Last-Event-ID 不属于当前任务")
        try:
            header_cursor = int(raw_sequence)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Last-Event-ID 事件游标无效") from exc
        if header_cursor < 0:
            raise HTTPException(status_code=400, detail="Last-Event-ID 事件游标无效")
    if after_sequence is not None and header_cursor is not None and after_sequence != header_cursor:
        raise HTTPException(status_code=400, detail="事件游标参数冲突")
    return after_sequence if after_sequence is not None else (header_cursor or 0)


async def _replay_and_subscribe(
    tasks: TaskStore,
    original_run: TaskRun,
    cursor: int,
    subscription: AsyncGenerator[dict[str, str], None] | None,
) -> AsyncIterator[dict[str, str]]:
    """无缝拼接持久日志与实时队列；重复的持久 TaskEvent 只交付一次。"""
    delivered = cursor
    try:
        while True:
            events = await run_in_threadpool(
                tasks.list_events,
                original_run.run_id,
                after_sequence=delivered,
                limit=1000,
            )
            for event in events:
                delivered = event.sequence
                yield _sse_task_event(event, original_run.conversation_id)
            if len(events) < 1000:
                break

        current = await run_in_threadpool(tasks.get_run, original_run.run_id)
        if current is None:
            return
        if current.status not in {"planning", "running", "verifying"}:
            yield _sse_done(current, delivered)
            return
        if subscription is None:
            yield {
                "event": "error",
                "data": json.dumps(
                    {
                        "code": "run_host_unavailable",
                        "message": "任务执行宿主不可用，请刷新状态后从 Checkpoint 恢复。",
                        "retryable": False,
                        "run_id": current.run_id,
                        "run_status": current.status,
                    },
                    ensure_ascii=False,
                ),
            }
            return

        async for item in subscription:
            sequence = _sse_item_sequence(item, original_run.run_id)
            if sequence is not None:
                if sequence <= delivered:
                    continue
                delivered = sequence
            yield item
    finally:
        if subscription is not None:
            await subscription.aclose()


def _sse_item_sequence(item: dict[str, str], run_id: str) -> int | None:
    event_id = item.get("id", "")
    raw_run_id, separator, raw_sequence = event_id.rpartition(":")
    if not separator or raw_run_id != run_id:
        return None
    try:
        sequence = int(raw_sequence)
    except ValueError:
        return None
    return sequence if sequence >= 0 else None


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


def _plan_payload(plan: TaskPlanRecord) -> dict[str, object]:
    return {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "reason": plan.reason,
        "definition": plan.plan,
        "created_at": plan.created_at,
    }


def _step_payload(step: TaskStepRecord) -> dict[str, object]:
    return {
        "step_id": step.logical_id,
        "persisted_step_id": step.step_id,
        "position": step.position,
        "status": step.status,
        "definition": step.definition,
        "started_at": step.started_at,
        "completed_at": step.completed_at,
    }


def _feedback_payload(event: TaskEvent) -> dict[str, object]:
    payload = event.payload
    return {
        "feedback_id": payload.get("feedback_id"),
        "event_id": event.event_id,
        "sequence": event.sequence,
        "rating": payload.get("rating"),
        "comment": payload.get("comment"),
        "evidence_ids": payload.get("evidence_ids", []),
        "artifact_ids": payload.get("artifact_ids", []),
        "created_at": event.occurred_at,
    }


def _tool_audit_payloads(
    invocations: list[ToolInvocation],
    evidence: list[EvidenceRecord],
    started_events: list[TaskEvent],
    cancellation_nodes: list[CancellationNode],
    evidence_ledger: list[EvidenceLedgerEntry],
) -> list[dict[str, object]]:
    """用持久 Invocation、策略、取消树和 Ledger 生成一致只读审计投影。"""
    evidence_by_invocation = {item.invocation_id: item for item in evidence}
    branch_by_invocation = {
        item.invocation_id: item for item in cancellation_nodes if item.invocation_id is not None
    }
    ledger_by_evidence = {item.evidence_id: item for item in evidence_ledger}
    started_by_invocation: dict[str, TaskEvent] = {}
    for event in started_events:
        invocation_id = event.payload.get("invocation_id")
        if isinstance(invocation_id, str):
            started_by_invocation[invocation_id] = event

    payloads: list[dict[str, object]] = []
    for invocation in invocations:
        started = started_by_invocation.get(invocation.invocation_id)
        policy = _object_field(started.payload if started is not None else {}, "policy")
        contract = _object_field(policy, "tool_contract")
        record = evidence_by_invocation.get(invocation.invocation_id)
        branch = branch_by_invocation.get(invocation.invocation_id)
        ledger_entry = ledger_by_evidence.get(record.evidence_id) if record is not None else None
        source = record.source if record is not None else {}
        evidence_contract = _object_field(source, "tool_contract")
        if evidence_contract:
            contract = evidence_contract
        payloads.append(
            {
                "invocation_id": invocation.invocation_id,
                "step_id": invocation.step_id,
                "tool_name": invocation.tool_name,
                "status": invocation.status,
                "service_name": _string_field(contract, "service_name"),
                "tool_version": _string_field(contract, "tool_version"),
                "risk_level": _string_field(contract, "risk_level"),
                "required_permissions": _string_list_field(
                    contract,
                    "required_permissions",
                ),
                "read_only": _bool_field(contract, "read_only"),
                "idempotent": _bool_field(contract, "idempotent"),
                "contract_hash": _string_field(contract, "contract_hash"),
                "policy_allowed": _bool_field(policy, "allowed"),
                "policy_code": _string_field(policy, "code"),
                "permission_snapshot_id": _string_field(
                    policy,
                    "permission_snapshot_id",
                ),
                "parallel": (
                    started.payload.get("parallel") is True if started is not None else False
                ),
                "branch_node_id": branch.node_id if branch is not None else None,
                "cancellation_status": branch.status if branch is not None else None,
                "data_version_hash": (branch.data_version_hash if branch is not None else None),
                "evidence_ledger_sequence": (
                    ledger_entry.sequence if ledger_entry is not None else None
                ),
                "transport": _string_field(source, "transport"),
                "gateway_health": _string_field(source, "mcp_gateway_health"),
                "gateway_generation": _int_field(
                    source,
                    "mcp_gateway_generation",
                ),
                "degraded": _bool_field(source, "mcp_degraded"),
                "evidence_id": record.evidence_id if record is not None else None,
                "evidence_result_hash": (record.result_hash if record is not None else None),
                "artifact_id": invocation.artifact_id,
                "started_at": invocation.started_at,
                "completed_at": invocation.completed_at,
            }
        )
    return payloads


def _execution_control_payload(
    scope: TaskExecutionScope | None,
    dataset_bindings: list[TaskDatasetBinding],
    cancellation_nodes: list[CancellationNode],
    evidence_ledger: list[EvidenceLedgerEntry],
    data_version_hash: str | None,
) -> dict[str, object] | None:
    """投影有界 TaskRun 执行作用域，不暴露工具结果或数据正文。"""
    if scope is None:
        return None
    root = next(
        (item for item in cancellation_nodes if item.parent_node_id is None),
        None,
    )
    branches = [item for item in cancellation_nodes if item.parent_node_id is not None]
    return {
        "schema_version": scope.schema_version,
        "max_tool_calls": scope.max_tool_calls,
        "max_parallelism": scope.max_parallelism,
        "data_version_hash": data_version_hash,
        "dataset_version_count": len(dataset_bindings),
        "evidence_ledger_version": len(evidence_ledger),
        "root_status": root.status if root is not None else None,
        "active_branch_count": sum(item.status == "active" for item in branches),
        "cancel_requested_branch_count": sum(
            item.status == "cancel_requested" for item in branches
        ),
    }


def _object_field(value: object, key: str) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    item = value.get(key)
    if not isinstance(item, dict):
        return {}
    return {str(name): field for name, field in item.items()}


def _string_field(value: dict[str, object], key: str) -> str | None:
    item = value.get(key)
    return item if isinstance(item, str) and item else None


def _string_list_field(value: dict[str, object], key: str) -> list[str]:
    item = value.get(key)
    if not isinstance(item, list):
        return []
    return [field for field in item if isinstance(field, str)]


def _bool_field(value: dict[str, object], key: str) -> bool | None:
    item = value.get(key)
    return item if isinstance(item, bool) else None


def _int_field(value: dict[str, object], key: str) -> int | None:
    item = value.get(key)
    return item if isinstance(item, int) and not isinstance(item, bool) else None


def _approval_payload(approval: ApprovalRecord) -> dict[str, object]:
    return {
        "approval_id": approval.approval_id,
        "run_id": approval.run_id,
        "plan_id": approval.plan_id,
        "plan_version": approval.plan_version,
        "step_id": approval.step_logical_id,
        "tool_name": approval.tool_name,
        "tool_schema_hash": approval.tool_schema_hash,
        "parameter_summary_hash": approval.parameter_summary_hash,
        "risk_level": approval.risk_level,
        "status": approval.status,
        "version": approval.version,
        "expires_at": approval.expires_at,
        "decision_reason": approval.decision_reason,
        "requested_at": approval.requested_at,
        "updated_at": approval.updated_at,
        "decided_at": approval.decided_at,
        "consumed_at": approval.consumed_at,
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
            subject_id=principal.user_id,
        ),
        mcp_config=mcp_client_config_from_settings(execution.settings),
        enabled_capability_profiles=enabled_capability_profiles_from_settings(execution.settings),
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
        max_parallel_tools=settings.agent_max_parallel_tools,
        tool_result_max_chars=settings.agent_tool_result_max_chars,
        registry_max_entries=settings.agent_registry_max_entries,
        run_timeout_seconds=settings.agent_run_timeout_seconds,
        model_timeout_seconds=settings.agent_model_timeout_seconds,
        tool_timeout_seconds=settings.agent_tool_timeout_seconds,
        approval_ttl_seconds=settings.agent_approval_ttl_seconds,
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
