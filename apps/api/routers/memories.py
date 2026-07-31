"""v2.5 阶段 3D：项目记忆的查询、纠正和软删除 API。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from packages.governance.permissions import Principal
from packages.session.memory_models import (
    MemoryDraft,
    MemoryKind,
    MemoryLink,
    MemoryRecord,
    MemoryScope,
    MemoryStatus,
)
from packages.session.memory_policy import MemoryPolicyViolation
from packages.session.memory_store import (
    MemoryAccessDenied,
    MemoryIdempotencyConflict,
    MemoryStore,
    MemoryVersionConflict,
)
from packages.session.store import SessionStore

from apps.api.auth import current_principal_dep
from apps.api.deps import session_store_dep
from apps.api.schemas import (
    MemoryLinkResponse,
    MemoryListResponse,
    MemoryMutationResponse,
    MemoryResponse,
    MemoryRevisionRequest,
)

router = APIRouter(prefix="/projects/{project_id}/memories", tags=["memories"])

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        description="一次治理命令的稳定幂等键",
    ),
]


@router.get("", response_model=MemoryListResponse)
def list_memories(
    project_id: str,
    memory_status: Annotated[MemoryStatus | None, Query(alias="status")] = None,
    scope: MemoryScope | None = None,
    kind: MemoryKind | None = None,
    conversation_id: str | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> MemoryListResponse:
    """列出当前主体可见的全部生命周期版本，可按状态和作用域筛选。"""
    memories = MemoryStore(store)
    try:
        records = memories.list_records_for_governance(
            project_id=project_id,
            principal=principal,
            conversation_id=conversation_id,
        )
    except MemoryAccessDenied as exc:
        raise _memory_error(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="记忆范围不存在") from exc

    filtered = [
        record
        for record in records
        if (memory_status is None or record.status == memory_status)
        and (scope is None or record.scope == scope)
        and (kind is None or record.kind == kind)
    ]
    filtered.sort(
        key=lambda record: (record.updated_at, record.created_at, record.memory_id),
        reverse=True,
    )
    selected = filtered[offset : offset + limit]
    return MemoryListResponse(
        items=[_response(memories, record, principal) for record in selected],
        total=len(filtered),
        offset=offset,
        limit=limit,
    )


@router.get("/{memory_id}", response_model=MemoryResponse)
def get_memory(
    project_id: str,
    memory_id: str,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> MemoryResponse:
    """读取一条主体可见的记忆及其资源关联。"""
    memories = MemoryStore(store)
    record = memories.get_record(memory_id, principal=principal)
    if record is None or record.project_id != project_id:
        raise HTTPException(status_code=404, detail="记忆不存在")
    return _response(memories, record, principal)


@router.patch("/{memory_id}", response_model=MemoryMutationResponse)
def revise_memory(
    project_id: str,
    memory_id: str,
    req: MemoryRevisionRequest,
    idempotency_key: IdempotencyKey,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> MemoryMutationResponse:
    """以新不可变版本纠正 active 记忆，拒绝覆盖已变化的版本。"""
    memories = MemoryStore(store)
    current = memories.get_record(memory_id, principal=principal)
    if current is None or current.project_id != project_id:
        raise HTTPException(status_code=404, detail="记忆不存在")
    draft = MemoryDraft(
        scope=current.scope,
        kind=current.kind,
        semantic_key=current.semantic_key,
        content_summary=req.content_summary,
        source_type=current.source_type,
        source_ref=current.source_ref,
        source_hash=current.source_hash,
        confidence=req.confidence,
        conversation_id=current.conversation_id,
        valid_from=current.valid_from,
        expires_at=req.expires_at,
    )
    try:
        result = memories.revise(
            memory_id,
            project_id=project_id,
            principal=principal,
            expected_version=req.expected_version,
            draft=draft,
            idempotency_key=idempotency_key,
        )
    except (
        MemoryAccessDenied,
        MemoryIdempotencyConflict,
        MemoryVersionConflict,
        MemoryPolicyViolation,
        ValueError,
    ) as exc:
        raise _memory_error(exc) from exc
    return MemoryMutationResponse(
        memory=_response(memories, result.record, principal),
        outcome=result.outcome,
    )


@router.delete("/{memory_id}", response_model=MemoryResponse)
def delete_memory(
    project_id: str,
    memory_id: str,
    expected_version: Annotated[int, Query(ge=1)],
    idempotency_key: IdempotencyKey,
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> MemoryResponse:
    """软删除 active/conflict 记忆；固定历史快照不被改写。"""
    memories = MemoryStore(store)
    try:
        deleted = memories.soft_delete(
            memory_id,
            project_id=project_id,
            principal=principal,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
        )
    except (
        MemoryAccessDenied,
        MemoryIdempotencyConflict,
        MemoryVersionConflict,
        ValueError,
    ) as exc:
        raise _memory_error(exc) from exc
    return _response(memories, deleted, principal)


def _response(
    store: MemoryStore,
    record: MemoryRecord,
    principal: Principal,
) -> MemoryResponse:
    return MemoryResponse(
        memory_id=record.memory_id,
        project_id=record.project_id,
        scope=record.scope,
        conversation_id=record.conversation_id,
        kind=record.kind,
        content_summary=record.content_summary,
        source_type=record.source_type,
        confidence=record.confidence,
        valid_from=record.valid_from,
        expires_at=record.expires_at,
        version=record.version,
        status=record.status,
        supersedes_id=record.supersedes_id,
        conflicts_with_id=record.conflicts_with_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
        deleted_at=record.deleted_at,
        links=[
            _link_response(link)
            for link in store.list_links(record.memory_id, principal=principal)
        ],
    )


def _link_response(link: MemoryLink) -> MemoryLinkResponse:
    return MemoryLinkResponse(
        target_type=link.target_type,
        target_ref=link.target_ref,
    )


def _memory_error(exc: Exception) -> HTTPException:
    if isinstance(exc, MemoryAccessDenied):
        return HTTPException(status_code=404, detail="记忆不存在")
    if isinstance(exc, MemoryIdempotencyConflict | MemoryVersionConflict):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))
