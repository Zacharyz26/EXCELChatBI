"""Stage-5A API for versioned definitions and executable field mappings."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from packages.common.identifiers import DATASET_REF_PATTERN
from packages.governance.permissions import Principal
from packages.knowledge.domain_models import (
    DomainDefinition,
    DomainDefinitionDraft,
    DomainFieldMapping,
)
from packages.knowledge.domain_store import (
    DomainAccessDenied,
    DomainDefinitionStore,
    DomainIdempotencyConflict,
    DomainMappingConflict,
    DomainVersionConflict,
)
from packages.knowledge.formula import FormulaMappingMissing
from packages.session.store import SessionStore

from apps.api.auth import current_principal_dep
from apps.api.deps import session_store_dep
from apps.api.domain_schemas import (
    CompiledInvocationResponse,
    DomainDefinitionCreate,
    DomainDefinitionMutationResponse,
    DomainDefinitionResolutionResponse,
    DomainDefinitionResponse,
    DomainFieldMappingCreate,
    DomainFieldMappingResponse,
)

router = APIRouter(
    prefix="/projects/{project_id}/domain-definitions",
    tags=["domain-definitions"],
)

IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=200,
        description="领域定义发布请求的稳定幂等键",
    ),
]


@router.post("", response_model=DomainDefinitionMutationResponse)
def create_definition(
    project_id: str,
    req: DomainDefinitionCreate,
    idempotency_key: IdempotencyKey,
    session: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> DomainDefinitionMutationResponse:
    """Publish an immutable definition; interval overlap is returned as conflict."""
    store = DomainDefinitionStore(session)
    try:
        result = store.create_definition(
            project_id=project_id,
            principal=principal,
            idempotency_key=idempotency_key,
            draft=DomainDefinitionDraft(
                semantic_key=req.semantic_key,
                version=req.version,
                title=req.title,
                description=req.description,
                formula=req.formula,
                grain=tuple(req.grain),
                scope=req.scope,
                owner=req.owner,
                source_ref=req.source_ref,
                effective_from=req.effective_from,
                effective_to=req.effective_to,
                definition_kind=req.definition_kind,
            ),
        )
    except (
        DomainAccessDenied,
        DomainIdempotencyConflict,
        DomainVersionConflict,
        ValueError,
    ) as exc:
        raise _domain_error(exc) from exc
    return DomainDefinitionMutationResponse(
        definition=_definition_response(result.definition), outcome=result.outcome
    )


@router.get("", response_model=list[DomainDefinitionResponse])
def list_definitions(
    project_id: str,
    semantic_key: str | None = None,
    session: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> list[DomainDefinitionResponse]:
    """List all historical versions without implying an effective winner."""
    try:
        records = DomainDefinitionStore(session).list_definitions(
            project_id=project_id,
            principal=principal,
            semantic_key=semantic_key,
        )
    except (DomainAccessDenied, ValueError) as exc:
        raise _domain_error(exc) from exc
    return [_definition_response(record) for record in records]


@router.get("/resolve", response_model=DomainDefinitionResolutionResponse)
def resolve_definition(
    project_id: str,
    semantic_key: Annotated[str, Query(min_length=1, max_length=100)],
    as_of: Annotated[str | None, Query(max_length=64)] = None,
    dataset_ref: Annotated[str | None, Query(pattern=DATASET_REF_PATTERN)] = None,
    session: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> DomainDefinitionResolutionResponse:
    """Resolve by effective date and optionally compile a governed data invocation."""
    store = DomainDefinitionStore(session)
    try:
        resolution = store.resolve(
            project_id=project_id,
            semantic_key=semantic_key,
            principal=principal,
            as_of=as_of,
        )
        compiled = None
        compilation_status: Literal["not_requested", "ready", "missing_mapping"] = (
            "not_requested"
        )
        if dataset_ref is not None and resolution.definition is not None:
            try:
                invocation = store.compile(
                    definition=resolution.definition,
                    dataset_ref=dataset_ref,
                    principal=principal,
                )
            except FormulaMappingMissing:
                compilation_status = "missing_mapping"
            else:
                compiled = CompiledInvocationResponse(
                    definition_id=invocation.definition_id,
                    definition_version=invocation.definition_version,
                    formula_hash=invocation.formula_hash,
                    tool_name=invocation.tool_name,
                    arguments=invocation.arguments,
                )
                compilation_status = "ready"
    except (DomainAccessDenied, ValueError) as exc:
        raise _domain_error(exc) from exc
    return DomainDefinitionResolutionResponse(
        semantic_key=resolution.semantic_key,
        as_of=resolution.as_of,
        status=resolution.status,
        requires_clarification=resolution.requires_clarification,
        candidates=[_definition_response(item) for item in resolution.candidates],
        compiled_invocation=compiled,
        compilation_status=compilation_status,
    )


@router.post("/field-mappings", response_model=DomainFieldMappingResponse)
def create_field_mapping(
    project_id: str,
    req: DomainFieldMappingCreate,
    session: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> DomainFieldMappingResponse:
    """Bind a dataset field to a definition concept after project checks."""
    try:
        mapping = DomainDefinitionStore(session).register_field_mapping(
            project_id=project_id,
            dataset_ref=req.dataset_ref,
            concept_key=req.concept_key,
            field_name=req.field_name,
            source_ref=req.source_ref,
            principal=principal,
        )
    except (DomainAccessDenied, DomainMappingConflict, ValueError) as exc:
        raise _domain_error(exc) from exc
    return _mapping_response(mapping)


@router.get("/field-mappings", response_model=list[DomainFieldMappingResponse])
def list_field_mappings(
    project_id: str,
    dataset_ref: Annotated[str, Query(pattern=DATASET_REF_PATTERN)],
    session: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> list[DomainFieldMappingResponse]:
    """List mappings only inside the authenticated project and tenant."""
    try:
        mappings = DomainDefinitionStore(session).list_field_mappings(
            project_id=project_id,
            dataset_ref=dataset_ref,
            principal=principal,
        )
    except (DomainAccessDenied, ValueError) as exc:
        raise _domain_error(exc) from exc
    return [_mapping_response(item) for item in mappings]


@router.get("/{definition_id}", response_model=DomainDefinitionResponse)
def get_definition(
    project_id: str,
    definition_id: str,
    session: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> DomainDefinitionResponse:
    """Locate an exact historical definition version by opaque identifier."""
    record = DomainDefinitionStore(session).get_definition(
        definition_id, principal=principal
    )
    if record is None or record.project_id != project_id:
        raise HTTPException(status_code=404, detail="领域定义不存在")
    return _definition_response(record)


def _definition_response(record: DomainDefinition) -> DomainDefinitionResponse:
    return DomainDefinitionResponse(
        definition_id=record.definition_id,
        project_id=record.project_id,
        semantic_key=record.semantic_key,
        definition_kind=record.definition_kind,
        version=record.version,
        title=record.title,
        description=record.description,
        formula=record.formula,
        formula_hash=record.formula_hash,
        grain=list(record.grain),
        scope=record.scope,
        owner=record.owner,
        source_ref=record.source_ref,
        effective_from=record.effective_from,
        effective_to=record.effective_to,
        resource_uri=record.resource_uri,
        created_at=record.created_at,
    )


def _mapping_response(record: DomainFieldMapping) -> DomainFieldMappingResponse:
    return DomainFieldMappingResponse(
        mapping_id=record.mapping_id,
        project_id=record.project_id,
        dataset_ref=record.dataset_ref,
        concept_key=record.concept_key,
        field_name=record.field_name,
        source_ref=record.source_ref,
        created_at=record.created_at,
    )


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DomainAccessDenied):
        return HTTPException(status_code=404, detail="领域定义项目不存在")
    if isinstance(
        exc,
        DomainIdempotencyConflict | DomainVersionConflict | DomainMappingConflict,
    ):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))
