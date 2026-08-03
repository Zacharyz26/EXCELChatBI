"""Authorized SQLite repository for versioned executable domain definitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from packages.governance.permissions import Principal
from packages.knowledge.domain_models import (
    CompiledInvocation,
    DefinitionResolution,
    DefinitionResolutionStatus,
    DomainDefinition,
    DomainDefinitionDraft,
    DomainDefinitionWriteResult,
    DomainFieldMapping,
)
from packages.knowledge.formula import (
    compile_formula,
    formula_hash,
    normalize_formula,
    normalize_semantic_key,
)
from packages.session.models import JsonObject
from packages.session.store import SessionStore


class DomainAccessDenied(PermissionError):
    """The authenticated subject cannot discover the requested project resource."""


class DomainIdempotencyConflict(RuntimeError):
    """One idempotency key was reused for a different definition request."""


class DomainVersionConflict(RuntimeError):
    """One semantic version already exists with different content."""


class DomainMappingConflict(RuntimeError):
    """A dataset concept is already mapped to another field."""


class DomainDefinitionStore:
    """Share the SessionStore database while keeping definitions append-only."""

    def __init__(self, session_store: SessionStore, *, read_only: bool = False) -> None:
        self._path = Path(session_store.db_path)
        self._read_only = read_only

    def create_definition(
        self,
        *,
        project_id: str,
        principal: Principal,
        draft: DomainDefinitionDraft,
        idempotency_key: str,
    ) -> DomainDefinitionWriteResult:
        """Publish one immutable version and report interval overlap as conflict."""
        if self._read_only:
            raise RuntimeError("只读领域定义存储禁止写入")
        prepared = _normalize_draft(draft)
        clean_idempotency_key = _bounded_text(
            idempotency_key, "幂等键", maximum=200
        )
        request_hash = _stable_hash(asdict(prepared))
        now = _utc_now()
        definition_id = uuid.uuid4().hex
        with self._connection(write=True) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection, project_id=project_id, principal=principal, write=True
            )
            replay_row = connection.execute(
                """
                SELECT * FROM domain_definitions
                WHERE tenant_id = ? AND project_id = ? AND idempotency_key = ?
                """,
                (principal.tenant_scope, project_id, clean_idempotency_key),
            ).fetchone()
            if replay_row is not None:
                if str(replay_row["request_hash"]) != request_hash:
                    raise DomainIdempotencyConflict("领域定义幂等键已绑定不同请求")
                return DomainDefinitionWriteResult(
                    definition=_definition_from_row(replay_row), outcome="replayed"
                )

            version_row = connection.execute(
                """
                SELECT * FROM domain_definitions
                WHERE tenant_id = ? AND project_id = ?
                  AND semantic_key = ? AND version = ?
                """,
                (
                    principal.tenant_scope,
                    project_id,
                    prepared.semantic_key,
                    prepared.version,
                ),
            ).fetchone()
            if version_row is not None:
                raise DomainVersionConflict("领域定义语义版本已存在")

            resource_uri = f"chatbi://domain-definitions/{definition_id}"
            digest = formula_hash(prepared.formula)
            connection.execute(
                """
                INSERT INTO domain_definitions(
                    definition_id, tenant_id, project_id, semantic_key,
                    definition_kind, version, title, description, formula_json,
                    formula_hash, grain_json, scope_json, owner, source_ref,
                    effective_from, effective_to, resource_uri,
                    created_by_user_id, idempotency_key, request_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    definition_id,
                    principal.tenant_scope,
                    project_id,
                    prepared.semantic_key,
                    prepared.definition_kind,
                    prepared.version,
                    prepared.title,
                    prepared.description,
                    _dump_json(prepared.formula),
                    digest,
                    _dump_json(list(prepared.grain)),
                    _dump_json(prepared.scope),
                    prepared.owner,
                    prepared.source_ref,
                    prepared.effective_from,
                    prepared.effective_to,
                    resource_uri,
                    principal.user_id,
                    clean_idempotency_key,
                    request_hash,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM domain_definitions WHERE definition_id = ?",
                (definition_id,),
            ).fetchone()
            assert row is not None
            conflict = connection.execute(
                """
                SELECT 1 FROM domain_definitions
                WHERE tenant_id = ? AND project_id = ? AND semantic_key = ?
                  AND definition_id != ?
                  AND (? IS NULL OR effective_from < ?)
                  AND (effective_to IS NULL OR effective_to > ?)
                LIMIT 1
                """,
                (
                    principal.tenant_scope,
                    project_id,
                    prepared.semantic_key,
                    definition_id,
                    prepared.effective_to,
                    prepared.effective_to,
                    prepared.effective_from,
                ),
            ).fetchone()
        return DomainDefinitionWriteResult(
            definition=_definition_from_row(row),
            outcome="conflict" if conflict is not None else "created",
        )

    def get_definition(
        self,
        definition_id: str,
        *,
        principal: Principal,
    ) -> DomainDefinition | None:
        """Read one historical version after tenant/project membership filtering."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM domain_definitions WHERE definition_id = ?",
                (definition_id,),
            ).fetchone()
            if row is None or str(row["tenant_id"]) != principal.tenant_scope:
                return None
            try:
                self._require_project_role(
                    connection,
                    project_id=str(row["project_id"]),
                    principal=principal,
                    write=False,
                )
            except DomainAccessDenied:
                return None
        return _definition_from_row(row)

    def list_definitions(
        self,
        *,
        project_id: str,
        principal: Principal,
        semantic_key: str | None = None,
    ) -> tuple[DomainDefinition, ...]:
        """List all visible versions; no effective-date winner is implied."""
        clean_key = (
            normalize_semantic_key(semantic_key) if semantic_key is not None else None
        )
        with self._connection() as connection:
            self._require_project_role(
                connection, project_id=project_id, principal=principal, write=False
            )
            if clean_key is None:
                rows = connection.execute(
                    """
                    SELECT * FROM domain_definitions
                    WHERE tenant_id = ? AND project_id = ?
                    ORDER BY semantic_key, version, created_at
                    """,
                    (principal.tenant_scope, project_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM domain_definitions
                    WHERE tenant_id = ? AND project_id = ? AND semantic_key = ?
                    ORDER BY version, created_at
                    """,
                    (principal.tenant_scope, project_id, clean_key),
                ).fetchall()
        return tuple(_definition_from_row(row) for row in rows)

    def resolve(
        self,
        *,
        project_id: str,
        semantic_key: str,
        principal: Principal,
        as_of: str | None = None,
    ) -> DefinitionResolution:
        """Resolve one effective instant without silently selecting conflicts."""
        normalized_as_of = _normalize_instant(as_of or _utc_now(), "生效时间")
        clean_key = normalize_semantic_key(semantic_key)
        with self._connection() as connection:
            self._require_project_role(
                connection, project_id=project_id, principal=principal, write=False
            )
            candidates = self._effective_rows(
                connection,
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                semantic_key=clean_key,
                as_of=normalized_as_of,
            )
            status = self._resolution_status(
                connection,
                tenant_id=principal.tenant_scope,
                project_id=project_id,
                semantic_key=clean_key,
                as_of=normalized_as_of,
                candidate_count=len(candidates),
            )
        return DefinitionResolution(
            semantic_key=clean_key,
            as_of=normalized_as_of,
            status=status,
            candidates=tuple(_definition_from_row(row) for row in candidates),
        )

    def resolve_for_subject(
        self,
        *,
        project_id: str,
        semantic_key: str,
        subject_id: str,
        as_of: str | None = None,
    ) -> DefinitionResolution:
        """Resolve from signed MCP subject context without accepting tenant arguments."""
        principal = self._principal_for_subject(
            project_id=project_id, subject_id=subject_id
        )
        return self.resolve(
            project_id=project_id,
            semantic_key=semantic_key,
            principal=principal,
            as_of=as_of,
        )

    def register_field_mapping(
        self,
        *,
        project_id: str,
        dataset_ref: str,
        concept_key: str,
        field_name: str,
        source_ref: str,
        principal: Principal,
    ) -> DomainFieldMapping:
        """Register an immutable dataset-specific concept mapping."""
        if self._read_only:
            raise RuntimeError("只读领域定义存储禁止写入")
        clean_concept = normalize_semantic_key(concept_key, label="概念键")
        clean_field = _bounded_text(field_name, "字段名", maximum=255)
        clean_source = _source_ref(source_ref)
        now = _utc_now()
        mapping_id = uuid.uuid4().hex
        with self._connection(write=True) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_project_role(
                connection, project_id=project_id, principal=principal, write=True
            )
            dataset = connection.execute(
                "SELECT project_id, profile_json FROM datasets WHERE ref = ?",
                (dataset_ref,),
            ).fetchone()
            if dataset is None or str(dataset["project_id"]) != project_id:
                raise ValueError("字段映射数据集不存在")
            _require_profile_field(str(dataset["profile_json"]), clean_field)
            existing = connection.execute(
                """
                SELECT * FROM domain_field_mappings
                WHERE tenant_id = ? AND project_id = ?
                  AND dataset_ref = ? AND concept_key = ?
                """,
                (
                    principal.tenant_scope,
                    project_id,
                    dataset_ref,
                    clean_concept,
                ),
            ).fetchone()
            if existing is not None:
                record = _mapping_from_row(existing)
                if record.field_name == clean_field and record.source_ref == clean_source:
                    return record
                raise DomainMappingConflict("数据集概念已映射到不同字段")
            connection.execute(
                """
                INSERT INTO domain_field_mappings(
                    mapping_id, tenant_id, project_id, dataset_ref, concept_key,
                    field_name, source_ref, created_by_user_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mapping_id,
                    principal.tenant_scope,
                    project_id,
                    dataset_ref,
                    clean_concept,
                    clean_field,
                    clean_source,
                    principal.user_id,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM domain_field_mappings WHERE mapping_id = ?",
                (mapping_id,),
            ).fetchone()
            assert row is not None
        return _mapping_from_row(row)

    def list_field_mappings(
        self,
        *,
        project_id: str,
        dataset_ref: str,
        principal: Principal,
    ) -> tuple[DomainFieldMapping, ...]:
        """List mappings visible to one authenticated project member."""
        with self._connection() as connection:
            self._require_project_role(
                connection, project_id=project_id, principal=principal, write=False
            )
            rows = connection.execute(
                """
                SELECT * FROM domain_field_mappings
                WHERE tenant_id = ? AND project_id = ? AND dataset_ref = ?
                ORDER BY concept_key, created_at
                """,
                (principal.tenant_scope, project_id, dataset_ref),
            ).fetchall()
        return tuple(_mapping_from_row(row) for row in rows)

    def compile_for_subject(
        self,
        *,
        definition: DomainDefinition,
        dataset_ref: str,
        subject_id: str,
    ) -> CompiledInvocation:
        """Compile using only mappings visible to the signed MCP subject."""
        principal = self._principal_for_subject(
            project_id=definition.project_id, subject_id=subject_id
        )
        if definition.tenant_id != principal.tenant_scope:
            raise DomainAccessDenied("领域定义不存在")
        mappings = self.list_field_mappings(
            project_id=definition.project_id,
            dataset_ref=dataset_ref,
            principal=principal,
        )
        return compile_formula(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            formula=definition.formula,
            expected_formula_hash=definition.formula_hash,
            dataset_ref=dataset_ref,
            concept_fields={item.concept_key: item.field_name for item in mappings},
        )

    def compile(
        self,
        *,
        definition: DomainDefinition,
        dataset_ref: str,
        principal: Principal,
    ) -> CompiledInvocation:
        """Compile a resolved definition for an authenticated API principal."""
        if definition.tenant_id != principal.tenant_scope:
            raise DomainAccessDenied("领域定义不存在")
        mappings = self.list_field_mappings(
            project_id=definition.project_id,
            dataset_ref=dataset_ref,
            principal=principal,
        )
        return compile_formula(
            definition_id=definition.definition_id,
            definition_version=definition.version,
            formula=definition.formula,
            expected_formula_hash=definition.formula_hash,
            dataset_ref=dataset_ref,
            concept_fields={item.concept_key: item.field_name for item in mappings},
        )

    def _principal_for_subject(self, *, project_id: str, subject_id: str) -> Principal:
        clean_subject = _bounded_text(subject_id, "调用主体", maximum=200)
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT tenant_id FROM project_memberships
                WHERE project_id = ? AND user_id = ?
                """,
                (project_id, clean_subject),
            ).fetchall()
        tenants = {str(row["tenant_id"]) for row in rows}
        if len(tenants) != 1:
            raise DomainAccessDenied("领域定义项目不存在")
        return Principal(user_id=clean_subject, tenant_id=next(iter(tenants)))

    @staticmethod
    def _effective_rows(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        project_id: str,
        semantic_key: str,
        as_of: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT * FROM domain_definitions
            WHERE tenant_id = ? AND project_id = ? AND semantic_key = ?
              AND effective_from <= ?
              AND (effective_to IS NULL OR effective_to > ?)
            ORDER BY version, created_at
            """,
            (tenant_id, project_id, semantic_key, as_of, as_of),
        ).fetchall()

    @staticmethod
    def _resolution_status(
        connection: sqlite3.Connection,
        *,
        tenant_id: str,
        project_id: str,
        semantic_key: str,
        as_of: str,
        candidate_count: int,
    ) -> DefinitionResolutionStatus:
        if candidate_count == 1:
            return "resolved"
        if candidate_count > 1:
            return "conflict"
        expired = connection.execute(
            """
            SELECT 1 FROM domain_definitions
            WHERE tenant_id = ? AND project_id = ? AND semantic_key = ?
              AND effective_to IS NOT NULL AND effective_to <= ?
            LIMIT 1
            """,
            (tenant_id, project_id, semantic_key, as_of),
        ).fetchone()
        return "expired" if expired is not None else "missing"

    def _require_project_role(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        principal: Principal,
        write: bool,
    ) -> str:
        row = connection.execute(
            """
            SELECT role FROM project_memberships
            WHERE project_id = ? AND user_id = ? AND tenant_id = ?
            """,
            (project_id, principal.user_id, principal.tenant_scope),
        ).fetchone()
        if row is None:
            raise DomainAccessDenied("领域定义项目不存在")
        role = str(row["role"])
        if write and role not in {"owner", "editor"}:
            raise DomainAccessDenied("领域定义项目不存在")
        return role

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write and self._read_only:
            raise RuntimeError("只读领域定义存储禁止写入")
        if self._read_only:
            connection = sqlite3.connect(
                f"file:{self._path.resolve().as_posix()}?mode=ro",
                timeout=5.0,
                uri=True,
            )
        else:
            connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()


def _normalize_draft(draft: DomainDefinitionDraft) -> DomainDefinitionDraft:
    if draft.definition_kind != "metric":
        raise ValueError("阶段 5A 仅支持 metric 领域定义")
    if isinstance(draft.version, bool) or draft.version <= 0:
        raise ValueError("领域定义版本必须为正整数")
    effective_from = _normalize_instant(draft.effective_from, "生效起点")
    effective_to = (
        _normalize_instant(draft.effective_to, "生效终点")
        if draft.effective_to is not None
        else None
    )
    if effective_to is not None and effective_to <= effective_from:
        raise ValueError("领域定义生效终点必须晚于起点")
    grain = tuple(
        normalize_semantic_key(item, label="粒度概念") for item in draft.grain
    )
    if len(grain) > 20:
        raise ValueError("领域定义粒度概念超过上限 20")
    if len(set(grain)) != len(grain):
        raise ValueError("领域定义粒度概念不能重复")
    if not isinstance(draft.scope, dict):
        raise ValueError("领域定义 scope 必须是对象")
    scope = cast(JsonObject, _json_copy(draft.scope))
    if len(_dump_json(scope)) > 8000:
        raise ValueError("领域定义 scope 超过字符上限 8000")
    return DomainDefinitionDraft(
        semantic_key=normalize_semantic_key(draft.semantic_key),
        version=draft.version,
        title=_bounded_text(draft.title, "定义标题", maximum=200),
        description=_bounded_text(
            draft.description, "定义说明", maximum=4000, allow_empty=True
        ),
        formula=normalize_formula(draft.formula),
        grain=grain,
        scope=scope,
        owner=_bounded_text(draft.owner, "定义所有者", maximum=200),
        source_ref=_source_ref(draft.source_ref),
        effective_from=effective_from,
        effective_to=effective_to,
        definition_kind="metric",
    )


def _source_ref(value: str) -> str:
    clean = _bounded_text(value, "来源引用", maximum=1000)
    parsed = urlparse(clean)
    if parsed.scheme not in {"https", "urn", "chatbi"}:
        raise ValueError("来源引用仅允许 https、urn 或 chatbi URI")
    if parsed.scheme == "https" and not parsed.netloc:
        raise ValueError("https 来源引用缺少主机")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("来源引用禁止内嵌凭据")
    if parsed.scheme == "chatbi" and not parsed.netloc:
        raise ValueError("chatbi 来源引用缺少不透明资源类型")
    _scheme, separator, remainder = clean.partition(":")
    assert separator
    return f"{parsed.scheme}:{remainder}"


def _normalize_instant(value: str, label: str) -> str:
    clean = _bounded_text(value, label, maximum=64)
    try:
        parsed = datetime.fromisoformat(clean.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label}不是合法 ISO 8601 时间") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{label}必须包含时区")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _bounded_text(
    value: str,
    label: str,
    *,
    maximum: int,
    allow_empty: bool = False,
) -> str:
    clean = value.strip()
    if not clean and not allow_empty:
        raise ValueError(f"{label}不能为空")
    if len(clean) > maximum:
        raise ValueError(f"{label}超过长度上限 {maximum}")
    return clean


def _require_profile_field(profile_json: str, field_name: str) -> None:
    parsed: Any = json.loads(profile_json)
    if not isinstance(parsed, dict):
        return
    raw_columns = parsed.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        return
    names = {
        str(item.get("name"))
        for item in raw_columns
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if names and field_name not in names:
        raise ValueError("字段映射引用的数据集字段不存在")


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _dump_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _json_copy(value: object) -> object:
    return json.loads(_dump_json(value))


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _definition_from_row(row: sqlite3.Row) -> DomainDefinition:
    formula = json.loads(str(row["formula_json"]))
    scope = json.loads(str(row["scope_json"]))
    grain = json.loads(str(row["grain_json"]))
    if not isinstance(formula, dict) or not isinstance(scope, dict):
        raise ValueError("领域定义 JSON 结构损坏")
    if not isinstance(grain, list) or not all(isinstance(item, str) for item in grain):
        raise ValueError("领域定义粒度结构损坏")
    return DomainDefinition(
        definition_id=str(row["definition_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        semantic_key=str(row["semantic_key"]),
        definition_kind="metric",
        version=int(row["version"]),
        title=str(row["title"]),
        description=str(row["description"]),
        formula=cast(JsonObject, formula),
        formula_hash=str(row["formula_hash"]),
        grain=tuple(str(item) for item in grain),
        scope=cast(JsonObject, scope),
        owner=str(row["owner"]),
        source_ref=str(row["source_ref"]),
        effective_from=str(row["effective_from"]),
        effective_to=(
            None if row["effective_to"] is None else str(row["effective_to"])
        ),
        resource_uri=str(row["resource_uri"]),
        created_by_user_id=str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
    )


def _mapping_from_row(row: sqlite3.Row) -> DomainFieldMapping:
    return DomainFieldMapping(
        mapping_id=str(row["mapping_id"]),
        tenant_id=str(row["tenant_id"]),
        project_id=str(row["project_id"]),
        dataset_ref=str(row["dataset_ref"]),
        concept_key=str(row["concept_key"]),
        field_name=str(row["field_name"]),
        source_ref=str(row["source_ref"]),
        created_by_user_id=str(row["created_by_user_id"]),
        created_at=str(row["created_at"]),
    )
