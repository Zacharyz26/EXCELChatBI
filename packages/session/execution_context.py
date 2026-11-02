"""从同一 SQLite 快照投影会话的真实执行来源；不执行任务或写入状态。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, TypedDict

from packages.common.config import get_settings
from packages.common.identifiers import (
    validate_dataset_ref,
)
from packages.orchestration.analysis_requirements import AnalysisRequirements
from packages.orchestration.context_selection import (
    frozen_named_selection,
    frozen_reference_dataset,
    source_intent,
)
from packages.orchestration.dataset_scope import (
    _successful_record,
    operation_admission_error,
    operation_progress,
    verified_analysis_scope,
)
from packages.orchestration.evidence_facts import valid_count_projection
from packages.orchestration.quality_facts import valid_quality_projection
from packages.session.models import Artifact, Dataset, JsonObject
from packages.session.store import SessionStore, _artifact_from_row, _dataset_from_row
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, TaskRun, ToolInvocation
from packages.session.task_store import (
    _data_version_hash,
    _dataset_binding_from_row,
    _evidence_from_row,
    _invocation_from_row,
    _run_from_row,
    verified_contract_record,
)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class FieldBinding:
    """来自实际成功聚合的唯一字段身份；不是模型自报的字段名称。"""

    dataset_ref: str
    column: str
    source_run_id: str
    source_invocation_id: str
    source_evidence_id: str
    source_result_hash: str
    source_scope_version_hash: str

    def to_dict(self) -> JsonObject:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """执行来源与最新任务状态分离；不可用时不返回猜测数据集。"""

    availability: Literal["available", "unavailable", "ambiguous", "none"]
    dataset_ref: str | None = None
    reason_codes: tuple[str, ...] = ()
    source_kind: str | None = None
    source_run_id: str | None = None
    source_invocation_id: str | None = None
    source_artifact_id: str | None = None
    source_occurred_at: str | None = None
    parent_ref: str | None = None
    lineage: tuple[JsonObject, ...] = ()
    lineage_state: Literal["verified", "partial", "unknown"] = "unknown"
    lineage_boundary_ref: str | None = None
    run_id: str | None = None
    run_status: str | None = None
    run_state_version: int | None = None
    scope_version_hash: str | None = None
    scope_version_run_id: str | None = None
    profile_artifact_id: str | None = None
    analysis_ids: tuple[str, ...] = ()
    field_binding: FieldBinding | None = None
    schema_version: int = 1

    def to_dict(self) -> JsonObject:
        payload = asdict(self)
        payload["field_context"] = (
            self.field_binding.to_dict()
            if self.field_binding is not None
            else {
                "column": None,
                "dataset_ref": None,
                "source_run_id": None,
                "source_invocation_id": None,
                "source_evidence_id": None,
                "source_result_hash": None,
                "source_scope_version_hash": None,
            }
        )
        payload.pop("field_binding")
        payload["context_hash"] = _hash(payload)
        return payload


class _RunState(TypedDict, total=False):
    run_id: str
    run_status: str
    run_state_version: int


class _SourceInfo(TypedDict):
    source_kind: str
    source_run_id: str | None
    source_invocation_id: str | None
    source_artifact_id: str | None
    source_occurred_at: str
    parent_ref: str | None
    lineage: tuple[JsonObject, ...]
    lineage_state: Literal["verified", "partial", "unknown"]
    lineage_boundary_ref: str | None
    scope_version_hash: str | None
    scope_version_run_id: str | None


@dataclass(frozen=True, slots=True)
class _SourceEvent:
    ref: str | None
    kind: str
    occurred_at: str
    source_run_id: str | None = None
    invocation_id: str | None = None
    artifact_id: str | None = None
    parent_ref: str | None = None
    error: str | None = None
    lineage: tuple[JsonObject, ...] = ()
    order: int = -1


@dataclass(slots=True)
class _RunProof:
    run: TaskRun
    original_query: str
    contract: JsonObject | None
    requirements: AnalysisRequirements | None
    invocations: list[ToolInvocation]
    evidence: list[EvidenceRecord]
    bindings: list[TaskDatasetBinding]
    scope_hash: str
    error: str | None = None
    evidence_scope: dict[str, str] = field(default_factory=dict)


def resolve_execution_context(store: SessionStore, *, conversation_id: str) -> ExecutionContext:
    """只读 BEGIN 快照；所有来源、证据和状态来自同一次数据库读取。"""
    uri = Path(store.db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        return _resolve_snapshot(connection, conversation_id)
    finally:
        connection.close()


def _load_run(connection: sqlite3.Connection, row: sqlite3.Row) -> _RunProof:
    run = _run_from_row(row)
    message = connection.execute(
        "SELECT content, role, conversation_id FROM messages WHERE id = ?", (run.user_message_id,)
    ).fetchone()
    original = str(message["content"]) if message else ""
    contract_row = connection.execute(
        "SELECT contract_json, contract_hash FROM task_contracts WHERE run_id = ?", (run.run_id,)
    ).fetchone()
    contract = None
    requirements = None
    error = None
    try:
        if (
            message is None
            or message["role"] != "user"
            or message["conversation_id"] != run.conversation_id
        ):
            raise ValueError("source_message_invalid")
        if contract_row is None:
            raise ValueError("source_contract_missing")
        contract = verified_contract_record(run.run_id, str(contract_row[0]), str(contract_row[1]))
        if contract.get("quantity_contract_version") not in {
            "quantity-contract-v1",
            "quantity-contract-v2",
        }:
            raise ValueError("source_contract_unknown_version")
        if contract.get("goal") != run.goal:
            raise ValueError("source_goal_mismatch")
        requirements = AnalysisRequirements.from_dict(contract.get("analysis_requirements"))
        requirements.validate_source(str(contract["goal"]))
    except (ValueError, TypeError, KeyError):
        error = "source_contract_unavailable"
    invocations = [
        _invocation_from_row(item)
        for item in connection.execute(
            "SELECT * FROM tool_invocations WHERE run_id = ? ORDER BY started_at, rowid",
            (run.run_id,),
        )
    ]
    rows = connection.execute(
        """SELECT evidence.*, ledger.data_version_hash AS ledger_scope
           FROM evidence JOIN evidence_ledger_entries AS ledger
             ON ledger.evidence_id = evidence.evidence_id AND ledger.run_id = evidence.run_id
           WHERE evidence.run_id = ? ORDER BY ledger.sequence""",
        (run.run_id,),
    ).fetchall()
    evidence = [_evidence_from_row(item) for item in rows]
    bindings = [
        _dataset_binding_from_row(item)
        for item in connection.execute(
            "SELECT * FROM task_dataset_bindings WHERE run_id = ? ORDER BY bound_at, dataset_ref",
            (run.run_id,),
        )
    ]
    return _RunProof(
        run,
        original,
        contract,
        requirements,
        invocations,
        evidence,
        bindings,
        _data_version_hash(connection, run.run_id),
        error,
        {str(item["evidence_id"]): str(item["ledger_scope"]) for item in rows},
    )


def _governed_source_event(
    connection: sqlite3.Connection,
    proof: _RunProof,
    artifacts: list[Artifact],
    datasets: dict[str, Dataset],
) -> _SourceEvent | None:
    """同一数据库快照重验原合同记忆来源；Artifact 引用不隐式切换数据。"""
    from packages.session.memory_refs import (
        bind_memory_record,
        find_memory_reference_assumptions,
        governed_bindings_allowed,
        governed_selection_proof,
        restore_memory_bindings,
    )
    from packages.session.memory_store import (
        _link_from_row,
        _memory_from_payload,
        _memory_from_row,
    )

    assumptions = find_memory_reference_assumptions(
        proof.contract.get("assumptions", []) if proof.contract else []
    )
    if not assumptions:
        return None
    selected = proof.requirements.dataset_ref if proof.requirements else None
    error = proof.error
    try:
        snapshots = connection.execute(
            "SELECT * FROM memory_snapshots WHERE run_id = ?",
            (proof.run.run_id,),
        ).fetchall()
        if len(snapshots) != 1:
            raise ValueError("memory_snapshot_missing")
        snapshot = snapshots[0]
        if (
            snapshot["project_id"] != proof.run.project_id
            or snapshot["conversation_id"] != proof.run.conversation_id
        ):
            raise ValueError("memory_snapshot_scope_invalid")
        snapshot_records = tuple(
            _memory_from_payload(json.loads(str(row["record_json"])))
            for row in connection.execute(
                "SELECT record_json FROM memory_snapshot_items "
                "WHERE memory_snapshot_id = ? ORDER BY position",
                (snapshot["memory_snapshot_id"],),
            )
        )
        governed_records = [
            _memory_from_row(row)
            for row in connection.execute(
                """SELECT * FROM memory_records WHERE tenant_id = ? AND project_id = ?
                   AND (scope = 'project' OR (scope = 'subject' AND subject_user_id = ?)
                        OR (scope = 'conversation' AND conversation_id = ?))""",
                (
                    snapshot["tenant_id"],
                    proof.run.project_id,
                    snapshot["subject_user_id"],
                    proof.run.conversation_id,
                ),
            )
        ]
        bindings = restore_memory_bindings(
            assumptions,
            snapshot_records=snapshot_records,
            governed_records=governed_records,
            bind=lambda record, override: bind_memory_record(
                record,
                project_id=proof.run.project_id,
                links=[
                    _link_from_row(row)
                    for row in connection.execute(
                        "SELECT * FROM memory_links WHERE memory_id = ? AND tenant_id = ?",
                        (record.memory_id, snapshot["tenant_id"]),
                    )
                ],
                artifacts=artifacts,
                datasets=list(datasets.values()),
                conflict_override=override,
            ),
        )
        # A governed Artifact is a selected output; consuming an old report
        # must not change the default source for the next user turn.
        source_refs = {item.target_ref for item in bindings if item.target_kind == "dataset"}
        if not source_refs:
            return None
        if not governed_bindings_allowed(
            str(proof.contract.get("goal", "")) if proof.contract else "", bindings
        ):
            raise ValueError("governed_source_intent_unavailable")
        original_goal = str(proof.contract.get("goal", "")) if proof.contract else ""
        if frozen_named_selection(
            original_goal, proof.contract.get("assumptions", []) if proof.contract else [], selected
        ) != governed_selection_proof(original_goal, bindings):
            raise ValueError("governed_selection_proof_mismatch")
        if source_refs != {selected} or {item.target.dataset_ref for item in bindings} != {
            selected
        }:
            raise ValueError("governed_source_contract_mismatch")
        if not any(
            binding.binding_kind == "initial" and binding.dataset_ref == selected
            for binding in proof.bindings
        ):
            raise ValueError("governed_source_initial_binding_missing")
    except (ValueError, TypeError, KeyError, PermissionError):
        error = error or "governed_source_proof_unavailable"
    occurred_at = proof.run.created_at
    # A previously ambiguous alias is selected only when the user answers the
    # persisted source question, not retroactively at the initial request.
    for event in connection.execute(
        "SELECT payload_json, occurred_at FROM task_events "
        "WHERE run_id = ? AND event_type = 'clarification.answered' ORDER BY sequence",
        (proof.run.run_id,),
    ):
        payload = json.loads(str(event["payload_json"]))
        if isinstance(payload, dict) and payload.get("question_id") == "memory_reference_target":
            occurred_at = str(event["occurred_at"])
    return _SourceEvent(
        selected,
        "selection",
        occurred_at,
        source_run_id=proof.run.run_id,
        error=error,
    )


def _source_events(
    connection: sqlite3.Connection,
    artifacts: list[Artifact],
    proofs: list[_RunProof],
    datasets: dict[str, Dataset],
) -> list[_SourceEvent]:
    events = []
    for artifact in artifacts:
        if artifact.source_tool != "infer_schema" or artifact.type != "profile":
            continue
        ref = (artifact.params or {}).get("dataset_ref")
        if not isinstance(ref, str):
            continue
        message = connection.execute(
            "SELECT role, conversation_id FROM messages WHERE id = ?", (artifact.message_id,)
        ).fetchone()
        dataset = datasets.get(ref)
        valid = bool(
            message
            and message["role"] == "assistant"
            and message["conversation_id"] == artifact.conversation_id
            and dataset
            and dataset.parent_ref is None
            and artifact.dataset_ref == ref
            and artifact.payload == dataset.profile
        )
        events.append(
            _SourceEvent(
                ref,
                "upload",
                artifact.created_at,
                artifact_id=artifact.id,
                error=None if valid else "upload_source_unavailable",
            )
        )
    for proof in proofs:
        requirements = proof.requirements
        governed_source = _governed_source_event(connection, proof, artifacts, datasets)
        if governed_source is not None:
            events.append(governed_source)
        frozen_ref = frozen_reference_dataset(
            str(proof.contract.get("goal", "")) if proof.contract else "",
            proof.contract.get("assumptions", []) if proof.contract else [],
            requirements.dataset_ref if requirements else None,
        )
        intent = source_intent(proof.original_query, current_dataset_ref=frozen_ref)
        selection = intent.selection
        if selection is not None or intent.reason_code is not None:
            selected = intent.dataset_ref
            initial = any(
                binding.binding_kind == "initial" and binding.dataset_ref == selected
                for binding in proof.bindings
            )
            error = intent.reason_code or proof.error
            if requirements is None or requirements.dataset_ref != selected or not initial:
                error = error or "selection_binding_missing"
            events.append(
                _SourceEvent(
                    selected,
                    "selection",
                    proof.run.created_at,
                    source_run_id=proof.run.run_id,
                    error=error,
                )
            )
        for index, invocation in enumerate(proof.invocations):
            if invocation.status != "succeeded" or invocation.tool_name not in {
                "transform_dataset",
                "join_datasets",
            }:
                continue
            record = _successful_record(invocation, proof.run.run_id, proof.evidence)
            projection = record.summary.get("fact_projection") if record else None
            derived = [
                binding
                for binding in proof.bindings
                if binding.producing_invocation_id == invocation.invocation_id
            ]
            refs = {binding.dataset_ref for binding in derived}
            if isinstance(projection, dict) and isinstance(projection.get("output_ref"), str):
                refs.add(projection["output_ref"])
            ref = next(iter(refs)) if len(refs) == 1 else None
            error = proof.error
            if invocation.tool_name != "transform_dataset":
                error = "multi_parent_context_not_supported"
            elif requirements is None or ref is None or record is None:
                error = "transform_source_unproved"
            elif not valid_count_projection(
                record.summary,
                tool=invocation.tool_name,
                run_id=proof.run.run_id,
                invocation_id=invocation.invocation_id,
                result_hash=invocation.result_hash or "",
            ):
                error = "transform_projection_invalid"
            elif requirements.schema_version == 2:
                admission = operation_admission_error(
                    requirements,
                    run_id=proof.run.run_id,
                    tool_name=invocation.tool_name,
                    arguments=invocation.args,
                    invocations=proof.invocations[:index],
                    evidence=proof.evidence,
                    bindings=proof.bindings,
                    source_artifacts=artifacts,
                )
                progress = operation_progress(
                    requirements,
                    proof.run.run_id,
                    proof.invocations[: index + 1],
                    proof.evidence,
                    proof.bindings,
                )
                if admission or progress.issues or ref not in progress.scope:
                    error = "transform_frozen_scope_invalid"
            elif ref not in verified_analysis_scope(
                requirements,
                proof.run.run_id,
                proof.invocations[: index + 1],
                proof.evidence,
                proof.bindings,
            ):
                error = "legacy_transform_scope_invalid"
            parent = invocation.args.get("dataset_ref")
            if not isinstance(parent, str) or len(derived) != 1 or derived[0].parent_ref != parent:
                error = "transform_binding_invalid"
            edge: JsonObject = {
                "input_ref": parent,
                "output_ref": ref,
                "run_id": proof.run.run_id,
                "invocation_id": invocation.invocation_id,
                "arguments": invocation.args,
            }
            events.append(
                _SourceEvent(
                    ref,
                    "transform",
                    invocation.completed_at or invocation.started_at,
                    source_run_id=proof.run.run_id,
                    invocation_id=invocation.invocation_id,
                    artifact_id=invocation.artifact_id,
                    parent_ref=parent if isinstance(parent, str) else None,
                    error=error,
                    lineage=(edge,),
                    order=index,
                )
            )
    return events


def dataset_is_readable(dataset: Dataset | None) -> bool:
    if dataset is None:
        return False
    try:
        ref = validate_dataset_ref(dataset.ref)
    except ValueError:
        return False
    base = Path(get_settings().dataset_dir).resolve()
    path = (base / (ref + ".parquet")).resolve()
    return path.parent == base and path.is_file()


def _analysis_id(artifact: Artifact) -> str:
    for values in (artifact.params or {}, artifact.payload or {}):
        value = values.get("analysis_id")
        if isinstance(value, str) and value:
            return value
    return artifact.id


def _matching_analysis(
    artifacts: list[Artifact],
    proofs: list[_RunProof],
    ref: str,
    source: _SourceEvent,
    *,
    field_invocation_id: str | None = None,
) -> tuple[list[Artifact], FieldBinding | None]:
    eligible: list[Artifact] = []
    fields: list[FieldBinding] = []
    by_id = {artifact.id: artifact for artifact in artifacts}
    for proof in proofs:
        if proof.error or proof.requirements is None:
            continue
        for index, invocation in enumerate(proof.invocations):
            if invocation.args.get("dataset_ref") != ref:
                continue
            record = _successful_record(invocation, proof.run.run_id, proof.evidence)
            artifact = by_id.get(invocation.artifact_id or "")
            if (
                record is None
                or artifact is None
                or record.artifact_id != artifact.id
                or artifact.dataset_ref != ref
                or artifact.source_tool != invocation.tool_name
                or artifact.type not in {"profile", "stats", "table", "chart"}
            ):
                continue
            context = record.summary.get("numeric_context")
            if not isinstance(context, dict) or context.get("dataset_ref") != ref:
                continue
            if context.get("data_version_hash") != proof.evidence_scope.get(record.evidence_id):
                continue
            if invocation.tool_name == "get_data_profile" and not valid_quality_projection(
                record.summary
            ):
                continue
            if proof.requirements.schema_version == 2 and operation_admission_error(
                proof.requirements,
                run_id=proof.run.run_id,
                tool_name=invocation.tool_name,
                arguments=invocation.args,
                invocations=proof.invocations[:index],
                evidence=proof.evidence,
                bindings=proof.bindings,
                source_artifacts=artifacts,
            ):
                continue
            eligible.append(artifact)
            metric = invocation.args.get("value_col")
            if (
                invocation.tool_name == "aggregate_preview"
                and (field_invocation_id is None or invocation.invocation_id == field_invocation_id)
                and isinstance(metric, str)
                and metric
                and valid_count_projection(
                    record.summary,
                    tool=invocation.tool_name,
                    run_id=proof.run.run_id,
                    invocation_id=invocation.invocation_id,
                    result_hash=invocation.result_hash or "",
                )
                and (invocation.completed_at or invocation.started_at) >= source.occurred_at
            ):
                fields.append(
                    FieldBinding(
                        ref,
                        metric,
                        proof.run.run_id,
                        invocation.invocation_id,
                        record.evidence_id,
                        record.result_hash,
                        proof.evidence_scope[record.evidence_id],
                    )
                )
    columns = {item.column for item in fields}
    field_binding = fields[-1] if len(columns) == 1 else None
    return sorted(eligible, key=lambda artifact: artifact.created_at), field_binding


def _full_lineage(
    source: _SourceEvent, events: list[_SourceEvent], parent_refs: dict[str, tuple[str, ...]]
) -> tuple[tuple[JsonObject, ...], str | None, str | None]:
    """仅重建有实际证明的边；明确输入锚点不必伪造此前的变换历史。"""
    ref = source.ref
    seen: set[str] = set()
    edges: list[JsonObject] = []
    explicit_anchors = {
        event.ref
        for event in events
        if event.kind == "selection"
        and event.error is None
        and (event.occurred_at < source.occurred_at or event.source_run_id == source.source_run_id)
    }
    while ref:
        if ref in seen:
            return (), "source_lineage_cycle", None
        seen.add(ref)
        parents = parent_refs.get(ref)
        if parents is None:
            return (), "source_lineage_anchor_missing", None
        if not parents:
            return tuple(reversed(edges)), None, None
        candidates = [e for e in events if e.kind == "transform" and e.ref == ref]
        proofs = {(e.source_run_id, e.invocation_id): e for e in candidates}
        proved = next(iter(proofs.values())) if len(proofs) == 1 else None
        valid_edge = (
            len(parents) == 1
            and proved is not None
            and proved.error is None
            and proved.parent_ref == parents[0]
            and len(proved.lineage) == 1
        )
        if not valid_edge:
            if ref in explicit_anchors:
                # Host access + exact frozen contract + initial binding proves
                # this Dataset as an input. It does NOT prove earlier edges or
                # import another conversation's Artifact/Evidence.
                return tuple(reversed(edges)), None, ref
            if len(parents) != 1:
                return (), "multi_parent_context_not_supported", None
            if proved is None:
                return (), "source_lineage_proof_ambiguous", None
            return (), "source_lineage_proof_invalid", None
        assert proved is not None
        edges.append(proved.lineage[0])
        ref = proved.parent_ref
    return (), "source_lineage_ref_missing", None


def validate_field_binding(
    store: SessionStore, *, conversation_id: str, raw: JsonObject
) -> FieldBinding:
    """恢复冻结字段时核其原始来源，而非用最新会话字段替换旧绑定。"""
    names = set(FieldBinding.__dataclass_fields__)
    if set(raw) != names or any(not isinstance(raw.get(k), str) or not raw[k] for k in names):
        raise ValueError("字段来源格式无效")
    field_binding = FieldBinding(**raw)
    uri = Path(store.db_path).resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        connection.execute("BEGIN")
        conversation = connection.execute(
            "SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        row = connection.execute(
            "SELECT * FROM task_runs WHERE run_id = ? AND conversation_id = ?",
            (field_binding.source_run_id, conversation_id),
        ).fetchone()
        if conversation is None or row is None or row["project_id"] != conversation["project_id"]:
            raise ValueError("字段来源任务越界或缺失")
        proof = _load_run(connection, row)
        dataset_row = connection.execute(
            "SELECT * FROM datasets WHERE ref = ? AND project_id = ?",
            (field_binding.dataset_ref, conversation["project_id"]),
        ).fetchone()
        dataset = _dataset_from_row(dataset_row) if dataset_row else None
        if proof.error or proof.requirements is None or not dataset_is_readable(dataset):
            raise ValueError("字段来源版本不可用")
        artifacts = [
            _artifact_from_row(r)
            for r in connection.execute(
                "SELECT * FROM artifacts WHERE conversation_id = ? ORDER BY created_at, rowid",
                (conversation_id,),
            )
        ]
        # Use exactly the original frozen invocation for field selection;
        # admission keeps the entire ordered source prefix and artifact set.
        _, proved = _matching_analysis(
            artifacts,
            [proof],
            field_binding.dataset_ref,
            _SourceEvent(field_binding.dataset_ref, "restore", ""),
            field_invocation_id=field_binding.source_invocation_id,
        )
        if proved != field_binding:
            raise ValueError("字段来源证据、字段、版本或 hash 不匹配")
        return field_binding
    finally:
        connection.close()


def _resolve_snapshot(connection: sqlite3.Connection, conversation_id: str) -> ExecutionContext:
    conversation = connection.execute(
        "SELECT project_id FROM conversations WHERE id = ?", (conversation_id,)
    ).fetchone()
    if conversation is None:
        raise ValueError("对话不存在")
    project_id = str(conversation["project_id"])
    artifacts = [
        _artifact_from_row(row)
        for row in connection.execute(
            "SELECT * FROM artifacts WHERE conversation_id = ? ORDER BY created_at, rowid",
            (conversation_id,),
        )
    ]
    datasets = {
        item.ref: item
        for item in (
            _dataset_from_row(row)
            for row in connection.execute(
                "SELECT * FROM datasets WHERE project_id = ?", (project_id,)
            )
        )
    }
    anchors = connection.execute(
        "SELECT ref FROM dataset_lineage_anchors WHERE project_id = ?", (project_id,)
    ).fetchall()
    parent_refs: dict[str, tuple[str, ...]] = {
        str(row["ref"]): tuple(
            str(parent["parent_ref"])
            for parent in connection.execute(
                "SELECT parent_ref FROM dataset_lineage_edges WHERE child_ref = ? ORDER BY ordinal",
                (row["ref"],),
            )
        )
        for row in anchors
    }
    run_rows = connection.execute(
        "SELECT * FROM task_runs WHERE conversation_id = ? AND project_id = ? "
        "ORDER BY created_at, rowid",
        (conversation_id, project_id),
    ).fetchall()
    proofs = [_load_run(connection, row) for row in run_rows]
    latest = proofs[-1].run if proofs else None
    state: _RunState = (
        {
            "run_id": latest.run_id,
            "run_status": latest.status,
            "run_state_version": latest.state_version,
        }
        if latest
        else {}
    )
    events = _source_events(connection, artifacts, proofs, datasets)
    if not events:
        return ExecutionContext("none", **state)
    timestamp = max(event.occurred_at for event in events)
    newest = [event for event in events if event.occurred_at == timestamp]
    # Ordered invocations within one run have causal precedence; unrelated
    # uploads or runs with a tied timestamp remain ambiguous.
    newest = [
        event
        for event in newest
        if not any(
            event.source_run_id is not None
            and other.source_run_id == event.source_run_id
            and other.order > event.order
            for other in newest
        )
    ]
    # Immutable derived refs establish a causal predecessor even when the
    # clock has the same resolution. A later explicit parent reselection is
    # not discarded by this rule; unrelated uploads remain ambiguous.
    superseded: set[tuple[str | None, str, str | None]] = set()
    for event in newest:
        if event.kind != "transform" or event.error:
            continue
        chain, chain_error, _boundary = _full_lineage(event, events, parent_refs)
        if chain_error:
            continue
        ancestors = {edge["input_ref"] for edge in chain}
        for prior in newest:
            if prior.kind in {"upload", "transform"} and prior.ref in ancestors:
                superseded.add(
                    (prior.source_run_id, prior.kind, prior.invocation_id or prior.artifact_id)
                )
    newest = [
        event
        for event in newest
        if (event.source_run_id, event.kind, event.invocation_id or event.artifact_id)
        not in superseded
    ]
    if len({event.ref for event in newest}) != 1:
        return ExecutionContext("ambiguous", reason_codes=("source_event_time_conflict",), **state)
    source = newest[-1]
    if any(event.error for event in newest):
        reason = next(event.error for event in newest if event.error)
    elif not dataset_is_readable(datasets.get(source.ref or "")):
        reason = "current_dataset_unavailable"
    else:
        reason = None
    proof = next((item for item in proofs if item.run.run_id == source.source_run_id), None)
    lineage, lineage_error, lineage_boundary = _full_lineage(source, events, parent_refs)
    reason = reason or lineage_error
    source_info: _SourceInfo = {
        "source_kind": source.kind,
        "source_run_id": source.source_run_id,
        "source_invocation_id": source.invocation_id,
        "source_artifact_id": source.artifact_id,
        "source_occurred_at": source.occurred_at,
        "parent_ref": source.parent_ref,
        "lineage": lineage,
        "lineage_state": "unknown"
        if lineage_error
        else (("partial" if lineage else "unknown") if lineage_boundary else "verified"),
        "lineage_boundary_ref": lineage_boundary,
        "scope_version_hash": proof.scope_hash if proof else None,
        "scope_version_run_id": proof.run.run_id if proof else None,
    }
    if reason or source.ref is None:
        return ExecutionContext(
            "unavailable", reason_codes=(reason or "source_ref_missing",), **source_info, **state
        )
    eligible, field_binding = _matching_analysis(artifacts, proofs, source.ref, source)
    profiles = [artifact for artifact in eligible if artifact.type == "profile"]
    uploaded_profiles = [
        event
        for event in events
        if event.kind == "upload" and event.ref == source.ref and event.error is None
    ]
    profile_id = (
        profiles[-1].id
        if profiles
        else uploaded_profiles[-1].artifact_id
        if uploaded_profiles
        else None
    )
    return ExecutionContext(
        "available",
        source.ref,
        reason_codes=("explicit_anchor_lineage_unproved",) if lineage_boundary else (),
        profile_artifact_id=profile_id,
        analysis_ids=tuple(dict.fromkeys(_analysis_id(item) for item in eligible)),
        field_binding=field_binding,
        **source_info,
        **state,
    )
