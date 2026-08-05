"""Public API schemas for stage-5A governed domain definitions."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from packages.common.identifiers import DATASET_REF_PATTERN
from pydantic import BaseModel, Field, StringConstraints

SemanticKey = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z][a-zA-Z0-9_.-]{0,99}$",
    ),
]
BoundedText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]
SourceRef = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
]
IsoInstant = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class DomainDefinitionCreate(BaseModel):
    """Publish one immutable executable metric definition version."""

    semantic_key: SemanticKey
    version: int = Field(ge=1)
    title: BoundedText
    description: Annotated[str, StringConstraints(strip_whitespace=True, max_length=4000)] = ""
    formula: dict[str, Any]
    grain: list[SemanticKey] = Field(default_factory=list, max_length=20)
    scope: dict[str, Any] = Field(default_factory=dict)
    owner: BoundedText
    source_ref: SourceRef
    effective_from: IsoInstant
    effective_to: IsoInstant | None = None
    definition_kind: Literal["metric"] = "metric"


class DomainDefinitionResponse(BaseModel):
    """Safe immutable definition view; tenant and idempotency data stay internal."""

    definition_id: str
    project_id: str
    semantic_key: str
    definition_kind: Literal["metric"]
    version: int
    title: str
    description: str
    formula: dict[str, Any]
    formula_hash: str
    grain: list[str]
    scope: dict[str, Any]
    owner: str
    source_ref: str
    effective_from: str
    effective_to: str | None
    resource_uri: str
    created_at: str


class DomainDefinitionMutationResponse(BaseModel):
    """Definition creation result including overlap outcome."""

    definition: DomainDefinitionResponse
    outcome: Literal["created", "conflict", "replayed"]


class CompiledInvocationResponse(BaseModel):
    """Governed Tool call produced from a trusted formula and field mappings."""

    definition_id: str
    definition_version: int
    formula_hash: str
    tool_name: Literal["aggregate_preview"]
    arguments: dict[str, Any]


class DomainDefinitionResolutionResponse(BaseModel):
    """Effective-date resolution; conflict candidates have no selected winner."""

    semantic_key: str
    as_of: str
    status: Literal["resolved", "conflict", "expired", "missing"]
    requires_clarification: bool
    candidates: list[DomainDefinitionResponse]
    compiled_invocation: CompiledInvocationResponse | None = None
    compilation_status: Literal["not_requested", "ready", "missing_mapping"]


class DomainFieldMappingCreate(BaseModel):
    """Bind one immutable dataset field to a definition concept."""

    dataset_ref: Annotated[
        str, StringConstraints(strip_whitespace=True, pattern=DATASET_REF_PATTERN)
    ]
    concept_key: SemanticKey
    field_name: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)
    ]
    source_ref: SourceRef


class DomainFieldMappingResponse(BaseModel):
    """Safe project-scoped field mapping view."""

    mapping_id: str
    project_id: str
    dataset_ref: str
    concept_key: str
    field_name: str
    source_ref: str
    created_at: str


class ReportDefinitionBindingResponse(BaseModel):
    """One safe report → data invocation → definition → Claim binding."""

    analysis_id: str
    data_artifact_id: str
    data_invocation_id: str
    data_evidence_id: str
    definition_evidence_id: str
    claim_ids: list[str]
    definition_id: str
    definition_version: int
    semantic_key: str
    formula_hash: str
    resource_uri: str
    source_ref: str


class ReportDefinitionReviewResponse(BaseModel):
    """Deterministic historical report definition-lineage review."""

    project_id: str
    report_id: str
    report_artifact_id: str
    status: Literal["verified", "degraded", "not_applicable"]
    bindings: list[ReportDefinitionBindingResponse]
    issues: list[str]
