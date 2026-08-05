"""Stage-5A contracts for governed domain definitions and field mappings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from packages.session.models import JsonObject

DefinitionKind = Literal["metric"]
DefinitionResolutionStatus = Literal["resolved", "conflict", "expired", "missing"]
DefinitionWriteOutcome = Literal["created", "conflict", "replayed"]
ReportDefinitionReviewStatus = Literal["verified", "degraded", "not_applicable"]


@dataclass(frozen=True, slots=True)
class DomainDefinitionDraft:
    """A candidate immutable metric definition supplied by a project editor."""

    semantic_key: str
    version: int
    title: str
    description: str
    formula: JsonObject
    grain: tuple[str, ...]
    scope: JsonObject
    owner: str
    source_ref: str
    effective_from: str
    effective_to: str | None = None
    definition_kind: DefinitionKind = "metric"


@dataclass(frozen=True, slots=True)
class DomainDefinition:
    """One immutable, effective-dated metric definition version."""

    definition_id: str
    tenant_id: str
    project_id: str
    semantic_key: str
    definition_kind: DefinitionKind
    version: int
    title: str
    description: str
    formula: JsonObject
    formula_hash: str
    grain: tuple[str, ...]
    scope: JsonObject
    owner: str
    source_ref: str
    effective_from: str
    effective_to: str | None
    resource_uri: str
    created_by_user_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DomainDefinitionWriteResult:
    """Definition creation result; conflicts remain stored but unresolved."""

    definition: DomainDefinition
    outcome: DefinitionWriteOutcome


@dataclass(frozen=True, slots=True)
class DomainFieldMapping:
    """An immutable mapping from a dataset field to a knowledge concept."""

    mapping_id: str
    tenant_id: str
    project_id: str
    dataset_ref: str
    concept_key: str
    field_name: str
    source_ref: str
    created_by_user_id: str
    created_at: str


@dataclass(frozen=True, slots=True)
class DefinitionResolution:
    """Effective-date resolution; conflict never contains an implicit winner."""

    semantic_key: str
    as_of: str
    status: DefinitionResolutionStatus
    candidates: tuple[DomainDefinition, ...]

    @property
    def definition(self) -> DomainDefinition | None:
        if self.status == "resolved" and len(self.candidates) == 1:
            return self.candidates[0]
        return None

    @property
    def requires_clarification(self) -> bool:
        return self.status == "conflict"


@dataclass(frozen=True, slots=True)
class CompiledInvocation:
    """A definition formula compiled into one governed, allowlisted Tool call."""

    definition_id: str
    definition_version: int
    formula_hash: str
    tool_name: Literal["aggregate_preview"]
    arguments: JsonObject


@dataclass(frozen=True, slots=True)
class ReportDefinitionBinding:
    """One report analysis traced through data and definition Evidence to Claims."""

    analysis_id: str
    data_artifact_id: str
    data_invocation_id: str
    data_evidence_id: str
    definition_evidence_id: str
    claim_ids: tuple[str, ...]
    definition_id: str
    definition_version: int
    semantic_key: str
    formula_hash: str
    resource_uri: str
    source_ref: str


@dataclass(frozen=True, slots=True)
class ReportDefinitionReview:
    """Safe, deterministic review of an existing report's definition lineage."""

    project_id: str
    report_id: str
    report_artifact_id: str
    status: ReportDefinitionReviewStatus
    bindings: tuple[ReportDefinitionBinding, ...]
    issues: tuple[str, ...]
