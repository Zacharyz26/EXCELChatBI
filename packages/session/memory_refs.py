"""基于固定 MemorySnapshot 的受治理实体/字段引用解析。

只有显式 ``memory-reference-v1`` 结构、用户确认来源和项目内唯一资源 Link 才能
成为可执行引用。普通记忆摘要继续只用于上下文，不会被解析器猜成结构化映射。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from packages.governance.audit import AuditEvent, AuditOutcome
from packages.governance.audit import record as record_audit
from packages.governance.permissions import Principal
from packages.session.coref import ReferenceTarget
from packages.session.memory_models import MemoryKind, MemoryRecord
from packages.session.memory_policy import MIN_MEMORY_SELECTION_CONFIDENCE
from packages.session.memory_store import MemoryStore
from packages.session.models import Artifact, Dataset
from packages.session.store import SessionStore

MEMORY_REFERENCE_SCHEMA = "memory-reference-v1"
MEMORY_REFERENCE_ASSUMPTION_PREFIX = "HOST_MEMORY_REF_V1:"
_HOST_ANNOTATION_PREFIX = "[Host 已验证记忆引用 memory-reference-v1]"
_SUPPORTED_KINDS = frozenset({"entity_mapping", "field_alias", "confirmed_decision"})
_MAX_BINDINGS = 5
_MAX_ALIAS_CHARS = 80
_MAX_CANONICAL_FIELD_CHARS = 200
_MEMORY_ID_PATTERN = re.compile(
    r"memory[_ ]?id\s*[:=]\s*([0-9a-f]{32})(?![0-9a-f])",
    re.I,
)

MemoryReferenceStatus = Literal[
    "no_reference",
    "resolved",
    "ambiguous",
    "unresolved",
]


class MemoryReferenceAccessDenied(PermissionError):
    """固定记忆映射已失效、被篡改或不再属于当前治理作用域。"""


@dataclass(frozen=True, slots=True)
class MemoryReferenceBinding:
    """一个从固定快照、live 生命周期和资源 Link 共同验证的映射。"""

    memory_id: str
    memory_version: int
    memory_kind: MemoryKind
    alias: str
    target_kind: Literal["artifact", "dataset"]
    target_ref: str
    canonical_field: str | None
    source_hash: str
    scope: str
    conflict_override: bool
    target: ReferenceTarget
    binding_hash: str

    def annotation_dict(self) -> dict[str, str | int | None]:
        return {
            "memory_id": self.memory_id,
            "memory_version": self.memory_version,
            "memory_kind": self.memory_kind,
            "alias": self.alias,
            "target_kind": self.target_kind,
            "target_ref": self.target_ref,
            "canonical_field": self.canonical_field,
            "conflict_override": self.conflict_override,
            "binding_hash": self.binding_hash,
        }

    def assumption(self) -> str:
        payload = {
            "h": self.binding_hash,
            "m": self.memory_id,
            "o": self.conflict_override,
            "v": self.memory_version,
        }
        return MEMORY_REFERENCE_ASSUMPTION_PREFIX + _stable_json(payload)


@dataclass(frozen=True, slots=True)
class MemoryReferenceChoice:
    memory_id: str
    memory_version: int
    label: str
    position: int

    def to_dict(self) -> dict[str, str | int]:
        return {
            "memory_id": self.memory_id,
            "memory_version": self.memory_version,
            "label": self.label,
            "position": self.position,
        }


@dataclass(frozen=True, slots=True)
class MemoryReferenceResolution:
    status: MemoryReferenceStatus
    original_query: str
    rewritten_query: str
    bindings: tuple[MemoryReferenceBinding, ...]
    choices: tuple[MemoryReferenceChoice, ...]
    reason_code: str | None
    resolution_hash: str

    @property
    def targets(self) -> tuple[ReferenceTarget, ...]:
        return tuple(binding.target for binding in self.bindings)

    def assumptions(self) -> tuple[str, ...]:
        if self.status != "resolved":
            return ()
        return tuple(binding.assumption() for binding in self.bindings)

    def annotate(self, query: str) -> str:
        if self.status != "resolved":
            return query
        return (
            query
            + "\n\n"
            + _HOST_ANNOTATION_PREFIX
            + " "
            + _stable_json(
                {
                    "resolution_hash": self.resolution_hash,
                    "bindings": [binding.annotation_dict() for binding in self.bindings],
                }
            )
        )

    def clarification(self) -> dict[str, object] | None:
        if self.status not in {"ambiguous", "unresolved"}:
            return None
        if self.reason_code == "memory_reference_limit_exceeded":
            question = f"一次最多可使用 {_MAX_BINDINGS} 个已确认映射，" "请缩小范围或分批处理。"
        elif self.choices:
            prefix = "该已确认名称有多个受治理映射，请明确选择："
            labels: list[str] = []
            for choice in self.choices[:8]:
                label = f"{choice.position}. {choice.label}" f"（memory_id={choice.memory_id}）"
                if len(prefix + "；".join([*labels, label])) > 500:
                    break
                labels.append(label)
            question = prefix + "；".join(labels)
        else:
            question = (
                "该已确认名称的映射当前不可安全使用，请提供明确的 "
                "analysis_id、dataset_ref 或有效 memory_id。"
            )
        return {
            "question_id": "memory_reference_target",
            "about": "memory_reference_target",
            "question": question,
            "reason": "失效或冲突的长期记忆不能用于选择数据或工件。",
            "blocking": True,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class _MemoryReferenceSpec:
    alias: str
    canonical_field: str | None


class _BindingError(ValueError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class MemoryReferenceResolver:
    """从固定 MemorySnapshot 解析显式结构化映射，并用 live 状态失败关闭。"""

    def __init__(
        self,
        session_store: SessionStore,
        memory_store: MemoryStore,
        *,
        audit_recorder: Callable[[AuditEvent], None] = record_audit,
    ) -> None:
        self._sessions = session_store
        self._memories = memory_store
        self._audit_recorder = audit_recorder

    def resolve(
        self,
        query: str,
        *,
        project_id: str,
        conversation_id: str,
        memory_snapshot_id: str,
        principal: Principal,
    ) -> MemoryReferenceResolution:
        clean = _clean_query(query)
        try:
            snapshot_records, governed_records = self._records(
                project_id=project_id,
                conversation_id=conversation_id,
                memory_snapshot_id=memory_snapshot_id,
                principal=principal,
            )
            result = self._resolve_records(
                clean,
                project_id=project_id,
                conversation_id=conversation_id,
                principal=principal,
                snapshot_records=snapshot_records,
                governed_records=governed_records,
            )
        except Exception as exc:
            self._audit(
                action="memory_reference.resolve",
                project_id=project_id,
                principal=principal,
                outcome=_failure_outcome(exc),
                detail={"reason_code": type(exc).__name__},
            )
            raise
        self._audit_resolution(
            action="memory_reference.resolve",
            project_id=project_id,
            principal=principal,
            result=result,
        )
        return result

    def restore(
        self,
        assumptions: tuple[str, ...],
        *,
        query: str,
        project_id: str,
        conversation_id: str,
        memory_snapshot_id: str,
        principal: Principal,
    ) -> MemoryReferenceResolution:
        clean = _clean_query(query)
        try:
            snapshot_records, governed_records = self._records(
                project_id=project_id,
                conversation_id=conversation_id,
                memory_snapshot_id=memory_snapshot_id,
                principal=principal,
            )
            snapshot_by_id = {record.memory_id: record for record in snapshot_records}
            governed_by_id = {record.memory_id: record for record in governed_records}
            restored: list[MemoryReferenceBinding] = []
            seen: set[str] = set()
            for assumption in assumptions:
                (
                    memory_id,
                    version,
                    expected_hash,
                    conflict_override,
                ) = _parse_assumption(assumption)
                if memory_id in seen:
                    raise ValueError("记忆引用恢复包含重复目标")
                seen.add(memory_id)
                snapshot_record = snapshot_by_id.get(memory_id)
                live_record = governed_by_id.get(memory_id)
                if (
                    snapshot_record is None
                    or snapshot_record.version != version
                    or live_record is None
                    or live_record.version != version
                    or live_record.status != "active"
                ):
                    raise MemoryReferenceAccessDenied("记忆引用版本或状态已失效")
                if _record_gate_reason(live_record) is not None:
                    raise MemoryReferenceAccessDenied("记忆引用当前不可选择")
                if (
                    snapshot_record.content_summary != live_record.content_summary
                    or snapshot_record.source_hash != live_record.source_hash
                    or snapshot_record.semantic_key != live_record.semantic_key
                ):
                    raise MemoryReferenceAccessDenied("记忆引用内容发生漂移")
                if not conflict_override and _has_live_conflict(live_record, governed_records):
                    raise MemoryReferenceAccessDenied("记忆引用出现未解决冲突")
                binding = self._binding(
                    live_record,
                    project_id=project_id,
                    conversation_id=conversation_id,
                    principal=principal,
                    conflict_override=conflict_override,
                )
                if binding.binding_hash != expected_hash:
                    raise MemoryReferenceAccessDenied("记忆引用 hash 不一致")
                restored.append(binding)
            result = _resolved(clean, restored)
        except Exception as exc:
            self._audit(
                action="memory_reference.restore",
                project_id=project_id,
                principal=principal,
                outcome=_failure_outcome(exc),
                detail={"reason_code": type(exc).__name__},
            )
            raise
        self._audit_resolution(
            action="memory_reference.restore",
            project_id=project_id,
            principal=principal,
            result=result,
        )
        return result

    def _records(
        self,
        *,
        project_id: str,
        conversation_id: str,
        memory_snapshot_id: str,
        principal: Principal,
    ) -> tuple[tuple[MemoryRecord, ...], list[MemoryRecord]]:
        snapshot_result = self._memories.get_snapshot(
            memory_snapshot_id,
            principal=principal,
        )
        if snapshot_result is None:
            raise MemoryReferenceAccessDenied("记忆快照不存在")
        snapshot, snapshot_records = snapshot_result
        if snapshot.project_id != project_id or snapshot.conversation_id != conversation_id:
            raise MemoryReferenceAccessDenied("记忆快照作用域不一致")
        governed_records = self._memories.list_records_for_governance(
            project_id=project_id,
            principal=principal,
            conversation_id=conversation_id,
        )
        return snapshot_records, governed_records

    def _resolve_records(
        self,
        query: str,
        *,
        project_id: str,
        conversation_id: str,
        principal: Principal,
        snapshot_records: tuple[MemoryRecord, ...],
        governed_records: list[MemoryRecord],
    ) -> MemoryReferenceResolution:
        explicit_ids = {match.group(1).lower() for match in _MEMORY_ID_PATTERN.finditer(query)}
        parsed: list[tuple[MemoryRecord, _MemoryReferenceSpec]] = []
        for record in governed_records:
            spec = _parse_spec(record)
            if spec is None:
                continue
            if explicit_ids:
                if record.memory_id in explicit_ids:
                    parsed.append((record, spec))
            elif spec.alias.casefold() in query.casefold():
                parsed.append((record, spec))
        if not parsed:
            if explicit_ids:
                return _result(
                    status="unresolved",
                    query=query,
                    bindings=(),
                    choices=(),
                    reason_code="memory_reference_not_found",
                )
            return _result(
                status="no_reference",
                query=query,
                bindings=(),
                choices=(),
                reason_code=None,
            )

        snapshot_keys = {(record.memory_id, record.version) for record in snapshot_records}
        by_alias: dict[str, list[tuple[MemoryRecord, _MemoryReferenceSpec]]] = {}
        for record, spec in parsed:
            by_alias.setdefault(spec.alias.casefold(), []).append((record, spec))

        bindings: list[MemoryReferenceBinding] = []
        choices: list[MemoryReferenceChoice] = []
        issue_codes: list[str] = []
        for candidates in by_alias.values():
            if not explicit_ids and any(record.status == "conflict" for record, _ in candidates):
                issue_codes.append("memory_reference_conflict")
            eligible = [
                (record, spec)
                for record, spec in candidates
                if (record.memory_id, record.version) in snapshot_keys
                and record.status == "active"
                and _record_gate_reason(record) is None
            ]
            if explicit_ids:
                eligible = [pair for pair in eligible if pair[0].memory_id in explicit_ids]
            if not eligible:
                issue_codes.append(_candidate_gate_reason(candidates))
                continue

            resolved_for_alias: list[MemoryReferenceBinding] = []
            for record, _ in eligible:
                try:
                    resolved_for_alias.append(
                        self._binding(
                            record,
                            project_id=project_id,
                            conversation_id=conversation_id,
                            principal=principal,
                            conflict_override=bool(explicit_ids),
                        )
                    )
                except _BindingError as exc:
                    issue_codes.append(exc.reason_code)
            resolved_for_alias = _dedupe_bindings(resolved_for_alias)
            if len(resolved_for_alias) == 1 and (
                explicit_ids or "memory_reference_conflict" not in issue_codes
            ):
                bindings.extend(resolved_for_alias)
                continue
            if resolved_for_alias:
                issue_codes.append("memory_reference_ambiguous")
                choices.extend(_binding_choices(resolved_for_alias))

        bindings = _dedupe_bindings(bindings)
        if issue_codes:
            status: MemoryReferenceStatus = (
                "ambiguous"
                if choices or "memory_reference_ambiguous" in issue_codes
                else "unresolved"
            )
            return _result(
                status=status,
                query=query,
                bindings=(),
                choices=tuple(_dedupe_choices(choices)),
                reason_code=issue_codes[0],
            )
        if len(bindings) > _MAX_BINDINGS:
            return _result(
                status="unresolved",
                query=query,
                bindings=(),
                choices=(),
                reason_code="memory_reference_limit_exceeded",
            )
        return _resolved(query, bindings)

    def _binding(
        self,
        record: MemoryRecord,
        *,
        project_id: str,
        conversation_id: str,
        principal: Principal,
        conflict_override: bool = False,
    ) -> MemoryReferenceBinding:
        spec = _parse_spec(record)
        if spec is None:
            raise _BindingError("memory_reference_contract_invalid")
        links = self._memories.list_links(record.memory_id, principal=principal)
        allowed_types = {"dataset"} if record.kind == "field_alias" else {"artifact", "dataset"}
        target_links = [link for link in links if link.target_type in allowed_types]
        if len(target_links) != 1:
            raise _BindingError("memory_reference_link_invalid")
        link = target_links[0]
        if link.project_id != project_id:
            raise _BindingError("memory_reference_cross_project")

        artifacts_by_id = {
            artifact.id: artifact for artifact in self._sessions.list_artifacts(conversation_id)
        }
        datasets_by_ref = {
            dataset.ref: dataset for dataset in self._sessions.list_datasets(project_id)
        }
        target: ReferenceTarget
        if link.target_type == "artifact":
            artifact = artifacts_by_id.get(link.target_ref)
            if artifact is None:
                raise _BindingError("memory_reference_target_missing")
            target = _artifact_target(artifact, spec.alias)
            target_kind: Literal["artifact", "dataset"] = "artifact"
        else:
            dataset = datasets_by_ref.get(link.target_ref)
            if dataset is None:
                raise _BindingError("memory_reference_target_missing")
            target = _dataset_target(dataset, spec.alias)
            target_kind = "dataset"

        payload = {
            "memory_id": record.memory_id,
            "memory_version": record.version,
            "memory_kind": record.kind,
            "alias": spec.alias,
            "target_kind": target_kind,
            "target_ref": link.target_ref,
            "canonical_field": spec.canonical_field,
            "source_hash": record.source_hash,
            "scope": record.scope,
            "conflict_override": conflict_override,
        }
        binding_hash = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
        return MemoryReferenceBinding(
            memory_id=record.memory_id,
            memory_version=record.version,
            memory_kind=record.kind,
            alias=spec.alias,
            target_kind=target_kind,
            target_ref=link.target_ref,
            canonical_field=spec.canonical_field,
            source_hash=record.source_hash,
            scope=record.scope,
            conflict_override=conflict_override,
            target=target,
            binding_hash=binding_hash,
        )

    def _audit_resolution(
        self,
        *,
        action: str,
        project_id: str,
        principal: Principal,
        result: MemoryReferenceResolution,
    ) -> None:
        self._audit(
            action=action,
            project_id=project_id,
            principal=principal,
            outcome="allowed",
            detail={
                "status": result.status,
                "reason_code": result.reason_code,
                "resolution_hash": result.resolution_hash,
                "memory_ids": [binding.memory_id for binding in result.bindings],
                "target_ids": [binding.target_ref for binding in result.bindings],
                "candidate_count": len(result.choices),
            },
        )

    def _audit(
        self,
        *,
        action: str,
        project_id: str,
        principal: Principal,
        outcome: AuditOutcome,
        detail: dict[str, object],
    ) -> None:
        self._audit_recorder(
            AuditEvent(
                actor=principal.user_id,
                tenant_id=principal.tenant_scope,
                action=action,
                resource="memory_reference_control_plane",
                outcome=outcome,
                project_id=project_id,
                detail=detail,
            )
        )


def memory_reference_summary(
    *,
    kind: MemoryKind,
    alias: str,
    canonical_field: str | None = None,
) -> str:
    """生成 3C-2 可执行映射摘要；写入仍必须经过 MemoryStore/Policy。"""
    clean_alias = _bounded_text(alias, "alias", _MAX_ALIAS_CHARS)
    if kind not in _SUPPORTED_KINDS:
        raise ValueError("该记忆 kind 不能作为可执行引用")
    payload: dict[str, str] = {
        "schema": MEMORY_REFERENCE_SCHEMA,
        "alias": clean_alias,
    }
    if kind == "field_alias":
        if canonical_field is None:
            raise ValueError("field_alias 必须提供 canonical_field")
        payload["canonical_field"] = _bounded_text(
            canonical_field,
            "canonical_field",
            _MAX_CANONICAL_FIELD_CHARS,
        )
    elif canonical_field is not None:
        raise ValueError("只有 field_alias 可以提供 canonical_field")
    return _stable_json(payload)


def memory_reference_semantic_key(*, kind: MemoryKind, alias: str) -> str:
    """为结构化映射生成不泄露 alias 的稳定语义键。"""
    clean_alias = _bounded_text(alias, "alias", _MAX_ALIAS_CHARS)
    if kind not in _SUPPORTED_KINDS:
        raise ValueError("该记忆 kind 不能作为可执行引用")
    digest = hashlib.sha256(clean_alias.casefold().encode("utf-8")).hexdigest()
    return f"reference.{kind}.{digest[:16]}"


def find_memory_reference_assumptions(values: object) -> tuple[str, ...]:
    if not isinstance(values, list | tuple):
        return ()
    matches = tuple(
        value
        for value in values
        if isinstance(value, str) and value.startswith(MEMORY_REFERENCE_ASSUMPTION_PREFIX)
    )
    if len(matches) > _MAX_BINDINGS:
        raise ValueError("持久化计划的记忆引用数量超过上限")
    memory_ids = [_parse_assumption(value)[0] for value in matches]
    if len(memory_ids) != len(set(memory_ids)):
        raise ValueError("持久化计划包含重复记忆引用")
    return matches


def _parse_spec(record: MemoryRecord) -> _MemoryReferenceSpec | None:
    if record.kind not in _SUPPORTED_KINDS:
        return None
    if record.source_type != "user_confirmation":
        return None
    try:
        payload = json.loads(record.content_summary)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or payload.get("schema") != MEMORY_REFERENCE_SCHEMA:
        return None
    expected_keys = (
        {"schema", "alias", "canonical_field"}
        if record.kind == "field_alias"
        else {"schema", "alias"}
    )
    if set(payload) != expected_keys:
        return None
    alias = payload.get("alias")
    if not isinstance(alias, str):
        return None
    try:
        clean_alias = _bounded_text(alias, "alias", _MAX_ALIAS_CHARS)
    except ValueError:
        return None
    if record.semantic_key != memory_reference_semantic_key(
        kind=record.kind,
        alias=clean_alias,
    ):
        return None
    canonical_field = payload.get("canonical_field")
    if record.kind == "field_alias":
        if not isinstance(canonical_field, str):
            return None
        try:
            canonical_field = _bounded_text(
                canonical_field,
                "canonical_field",
                _MAX_CANONICAL_FIELD_CHARS,
            )
        except ValueError:
            return None
    else:
        canonical_field = None
    return _MemoryReferenceSpec(
        alias=clean_alias,
        canonical_field=canonical_field,
    )


def _record_gate_reason(record: MemoryRecord) -> str | None:
    if record.status != "active":
        return f"memory_reference_{record.status}"
    if record.confidence < MIN_MEMORY_SELECTION_CONFIDENCE:
        return "memory_reference_low_confidence"
    now = datetime.now(UTC)
    if _timestamp(record.valid_from) > now:
        return "memory_reference_not_yet_valid"
    if record.expires_at is not None and _timestamp(record.expires_at) <= now:
        return "memory_reference_expired"
    return None


def _candidate_gate_reason(
    candidates: list[tuple[MemoryRecord, _MemoryReferenceSpec]],
) -> str:
    reasons = [
        reason for record, _ in candidates if (reason := _record_gate_reason(record)) is not None
    ]
    priorities = (
        "memory_reference_conflict",
        "memory_reference_deleted",
        "memory_reference_expired",
        "memory_reference_low_confidence",
        "memory_reference_not_yet_valid",
        "memory_reference_superseded",
    )
    for reason in priorities:
        if reason in reasons:
            return reason
    return reasons[0] if reasons else "memory_reference_not_in_snapshot"


def _has_live_conflict(
    record: MemoryRecord,
    governed_records: list[MemoryRecord],
) -> bool:
    return any(
        candidate.status == "conflict"
        and candidate.semantic_key == record.semantic_key
        and candidate.scope == record.scope
        and candidate.scope_key == record.scope_key
        for candidate in governed_records
    )


def _parse_assumption(value: str) -> tuple[str, int, str, bool]:
    if not value.startswith(MEMORY_REFERENCE_ASSUMPTION_PREFIX):
        raise ValueError("记忆引用恢复契约前缀无效")
    try:
        payload = json.loads(value.removeprefix(MEMORY_REFERENCE_ASSUMPTION_PREFIX))
    except json.JSONDecodeError as exc:
        raise ValueError("记忆引用恢复契约 JSON 无效") from exc
    if not isinstance(payload, dict) or set(payload) != {"h", "m", "o", "v"}:
        raise ValueError("记忆引用恢复契约字段无效")
    memory_id = payload.get("m")
    version = payload.get("v")
    binding_hash = payload.get("h")
    conflict_override = payload.get("o")
    if (
        not isinstance(memory_id, str)
        or not re.fullmatch(r"[0-9a-f]{32}", memory_id)
        or isinstance(version, bool)
        or not isinstance(version, int)
        or version < 1
        or not isinstance(binding_hash, str)
        or not re.fullmatch(r"[0-9a-f]{64}", binding_hash)
        or not isinstance(conflict_override, bool)
    ):
        raise ValueError("记忆引用恢复契约值无效")
    return memory_id, version, binding_hash, conflict_override


def _resolved(
    query: str,
    bindings: list[MemoryReferenceBinding],
) -> MemoryReferenceResolution:
    clean_bindings = _dedupe_bindings(bindings)
    return _result(
        status="resolved",
        query=query,
        bindings=tuple(clean_bindings),
        choices=(),
        reason_code=None,
    )


def _result(
    *,
    status: MemoryReferenceStatus,
    query: str,
    bindings: tuple[MemoryReferenceBinding, ...],
    choices: tuple[MemoryReferenceChoice, ...],
    reason_code: str | None,
) -> MemoryReferenceResolution:
    payload = {
        "schema": MEMORY_REFERENCE_SCHEMA,
        "status": status,
        "binding_hashes": [binding.binding_hash for binding in bindings],
        "reason_code": reason_code,
    }
    resolution_hash = hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()
    result = MemoryReferenceResolution(
        status=status,
        original_query=query,
        rewritten_query=query,
        bindings=bindings,
        choices=choices,
        reason_code=reason_code,
        resolution_hash=resolution_hash,
    )
    if status == "resolved":
        return MemoryReferenceResolution(
            status=result.status,
            original_query=result.original_query,
            rewritten_query=result.annotate(query),
            bindings=result.bindings,
            choices=result.choices,
            reason_code=result.reason_code,
            resolution_hash=result.resolution_hash,
        )
    return result


def _artifact_target(artifact: Artifact, mention: str) -> ReferenceTarget:
    params = artifact.params or {}
    analysis_id = params.get("analysis_id")
    return ReferenceTarget(
        kind="artifact",
        reference_id=artifact.id,
        mention=mention,
        artifact_type=artifact.type,
        analysis_id=(
            analysis_id.strip().lower()
            if isinstance(analysis_id, str) and analysis_id.strip()
            else artifact.id
        ),
        dataset_ref=artifact.dataset_ref,
    )


def _dataset_target(dataset: Dataset, mention: str) -> ReferenceTarget:
    return ReferenceTarget(
        kind="dataset",
        reference_id=dataset.ref,
        mention=mention,
        dataset_ref=dataset.ref,
    )


def _binding_choices(
    bindings: list[MemoryReferenceBinding],
) -> list[MemoryReferenceChoice]:
    return [
        MemoryReferenceChoice(
            memory_id=binding.memory_id,
            memory_version=binding.memory_version,
            label=(f"{binding.memory_kind} / {binding.target_kind}=" f"{binding.target_ref}"),
            position=position,
        )
        for position, binding in enumerate(bindings, 1)
    ]


def _dedupe_bindings(
    bindings: list[MemoryReferenceBinding],
) -> list[MemoryReferenceBinding]:
    seen: set[tuple[str, str, str | None]] = set()
    result: list[MemoryReferenceBinding] = []
    for binding in bindings:
        key = (
            binding.target_kind,
            binding.target_ref,
            binding.canonical_field,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(binding)
    return result


def _dedupe_choices(
    choices: list[MemoryReferenceChoice],
) -> list[MemoryReferenceChoice]:
    seen: set[str] = set()
    result: list[MemoryReferenceChoice] = []
    for choice in choices:
        if choice.memory_id in seen:
            continue
        seen.add(choice.memory_id)
        result.append(choice)
    return result


def _failure_outcome(exc: Exception) -> AuditOutcome:
    return "denied" if isinstance(exc, MemoryReferenceAccessDenied | ValueError) else "error"


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MemoryReferenceAccessDenied("记忆引用时间戳无效") from exc
    if parsed.tzinfo is None:
        raise MemoryReferenceAccessDenied("记忆引用时间戳缺少时区")
    return parsed.astimezone(UTC)


def _bounded_text(value: str, label: str, maximum: int) -> str:
    clean = value.strip()
    if not clean:
        raise ValueError(f"{label} 不能为空")
    if len(clean) > maximum:
        raise ValueError(f"{label} 超过 {maximum} 字符")
    if any(ord(char) < 32 for char in clean):
        raise ValueError(f"{label} 包含控制字符")
    return clean


def _clean_query(query: str) -> str:
    if not isinstance(query, str):
        raise ValueError("query 必须是字符串")
    clean = query.strip()
    if not clean:
        raise ValueError("query 不能为空")
    if len(clean) > 20_000:
        raise ValueError("query 超过 20000 字符")
    return clean


def _stable_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
