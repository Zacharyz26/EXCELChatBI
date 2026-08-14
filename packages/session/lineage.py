"""从 SQLite 真相表构建项目隔离、可恢复的确定性血缘图。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from packages.governance.audit import AuditEvent
from packages.governance.audit import record as record_audit
from packages.governance.permissions import Principal

if TYPE_CHECKING:
    from packages.session.store import SessionStore

LineageNodeType = Literal["dataset", "analysis", "artifact", "evidence", "claim"]
LineageNodeStatus = Literal["active", "deleted", "succeeded", "failed", "unknown", "running"]
LineageRelation = Literal[
    "derived_from",
    "used_by",
    "produced",
    "profiled_as",
    "included_in",
    "substantiates",
    "supports",
]


class LineageAccessDenied(PermissionError):
    """主体不能访问指定项目或对话的血缘图。"""


@dataclass(frozen=True, slots=True)
class LineageNode:
    node_id: str
    node_type: LineageNodeType
    resource_ref: str
    label: str
    status: LineageNodeStatus
    conversation_id: str | None
    run_id: str | None
    metadata: dict[str, Any]
    created_at: str | None


@dataclass(frozen=True, slots=True)
class LineageEdge:
    source: str
    target: str
    relation: LineageRelation


@dataclass(frozen=True, slots=True)
class LineageIssue:
    code: str
    count: int


@dataclass(frozen=True, slots=True)
class LineageGraph:
    project_id: str
    nodes: tuple[LineageNode, ...]
    edges: tuple[LineageEdge, ...]
    graph_hash: str
    integrity_status: Literal["ok", "degraded"]
    issues: tuple[LineageIssue, ...]
    total_nodes: int
    total_edges: int
    truncated: bool


class LineageStore:
    """不复制业务结果，通过已有外键和不可变 Dataset 锚点组装来源图。"""

    def __init__(
        self,
        session_store: SessionStore,
        *,
        audit_recorder: Callable[[AuditEvent], None] = record_audit,
    ) -> None:
        self._path = Path(session_store.db_path)
        self._audit_recorder = audit_recorder

    def build_graph(
        self,
        *,
        project_id: str,
        principal: Principal,
        conversation_id: str | None = None,
        max_nodes: int = 500,
    ) -> LineageGraph:
        """返回主体可见项目的有界血缘图；内部路径、参数和结果正文不会进入响应。"""
        try:
            graph = self._build_graph(
                project_id=project_id,
                principal=principal,
                conversation_id=conversation_id,
                max_nodes=max_nodes,
            )
        except Exception as exc:
            self._audit(
                project_id=project_id,
                principal=principal,
                outcome="denied",
                detail={"reason_code": type(exc).__name__},
            )
            raise
        self._audit(
            project_id=project_id,
            principal=principal,
            outcome="allowed",
            detail={
                "graph_hash": graph.graph_hash,
                "node_count": graph.total_nodes,
                "edge_count": graph.total_edges,
                "integrity_status": graph.integrity_status,
                "truncated": graph.truncated,
            },
        )
        return graph

    def _build_graph(
        self,
        *,
        project_id: str,
        principal: Principal,
        conversation_id: str | None,
        max_nodes: int,
    ) -> LineageGraph:
        if not 1 <= max_nodes <= 2_000:
            raise ValueError("max_nodes 必须在 1 到 2000 之间")
        with self._connection() as connection:
            _require_scope(
                connection,
                project_id=project_id,
                conversation_id=conversation_id,
                principal=principal,
            )
            dataset_rows = connection.execute(
                """
                SELECT ref, filename, parent_ref AS lineage_parent_ref,
                       created_at, deleted_at
                FROM dataset_lineage_anchors
                WHERE project_id = ?
                ORDER BY created_at, rowid
                """,
                (project_id,),
            ).fetchall()
            dataset_edge_rows = connection.execute(
                """
                SELECT child_ref, parent_ref, ordinal
                FROM dataset_lineage_edges
                WHERE child_ref IN (
                    SELECT ref FROM dataset_lineage_anchors WHERE project_id = ?
                )
                ORDER BY child_ref, ordinal
                """,
                (project_id,),
            ).fetchall()
            scope_sql, scope_args = _conversation_scope(conversation_id)
            artifact_rows = connection.execute(
                f"""
                SELECT artifact.id, artifact.conversation_id, artifact.type,
                       artifact.source_tool, artifact.params_json,
                       artifact.lineage_dataset_ref, artifact.created_at
                FROM artifacts AS artifact
                JOIN conversations AS conversation
                  ON conversation.id = artifact.conversation_id
                WHERE conversation.project_id = ? {scope_sql}
                ORDER BY artifact.created_at, artifact.rowid
                """,
                (project_id, *scope_args),
            ).fetchall()
            invocation_rows = connection.execute(
                f"""
                SELECT invocation.invocation_id, invocation.run_id,
                       invocation.tool_name, invocation.args_json,
                       invocation.status, invocation.result_hash,
                       invocation.artifact_id, invocation.started_at,
                       run.conversation_id
                FROM tool_invocations AS invocation
                JOIN task_runs AS run ON run.run_id = invocation.run_id
                WHERE run.project_id = ? {scope_sql.replace("artifact.", "run.")}
                ORDER BY invocation.started_at, invocation.rowid
                """,
                (project_id, *scope_args),
            ).fetchall()
            evidence_rows = connection.execute(
                f"""
                SELECT evidence.evidence_id, evidence.run_id,
                       evidence.invocation_id, evidence.artifact_id,
                       evidence.kind, evidence.result_hash, evidence.created_at,
                       run.conversation_id
                FROM evidence
                JOIN task_runs AS run ON run.run_id = evidence.run_id
                WHERE run.project_id = ? {scope_sql.replace("artifact.", "run.")}
                ORDER BY evidence.created_at, evidence.rowid
                """,
                (project_id, *scope_args),
            ).fetchall()
            claim_rows = connection.execute(
                f"""
                SELECT claim.claim_id, claim.run_id, claim.statement,
                       claim.claim_kind, claim.created_at, run.conversation_id
                FROM claims AS claim
                JOIN task_runs AS run ON run.run_id = claim.run_id
                WHERE run.project_id = ? {scope_sql.replace("artifact.", "run.")}
                ORDER BY claim.created_at, claim.rowid
                """,
                (project_id, *scope_args),
            ).fetchall()
            claim_evidence_rows = connection.execute(
                f"""
                SELECT link.claim_id, link.evidence_id
                FROM claim_evidence AS link
                JOIN claims AS claim ON claim.claim_id = link.claim_id
                JOIN task_runs AS run ON run.run_id = claim.run_id
                WHERE run.project_id = ? {scope_sql.replace("artifact.", "run.")}
                ORDER BY link.rowid
                """,
                (project_id, *scope_args),
            ).fetchall()
            issues = _integrity_issues(connection, project_id=project_id)

        nodes: dict[str, LineageNode] = {}
        edges: set[tuple[str, str, LineageRelation]] = set()
        dataset_anchors: dict[str, sqlite3.Row] = {
            str(row["ref"]): row for row in dataset_rows
        }
        dataset_parents: dict[str, list[str]] = {}
        for row in dataset_edge_rows:
            dataset_parents.setdefault(str(row["child_ref"]), []).append(
                str(row["parent_ref"])
            )
        visible_dataset_refs = set(dataset_anchors)
        if conversation_id is not None:
            visible_dataset_refs = {
                dataset_ref
                for row in artifact_rows
                if (
                    dataset_ref := _optional_text(row["lineage_dataset_ref"])
                )
                is not None
            }
            for row in invocation_rows:
                visible_dataset_refs.update(
                    _dataset_refs(_json_object(row["args_json"]))
                )
            frontier = list(visible_dataset_refs)
            while frontier:
                child_ref = frontier.pop()
                for parent_ref in dataset_parents.get(child_ref, ()):
                    if parent_ref not in visible_dataset_refs:
                        visible_dataset_refs.add(parent_ref)
                        frontier.append(parent_ref)

        def add_node(node: LineageNode) -> None:
            nodes.setdefault(node.node_id, node)

        def add_dataset(dataset_ref: str) -> str:
            node_id = _node_id("dataset", dataset_ref)
            if node_id in nodes:
                return node_id
            row = dataset_anchors.get(dataset_ref)
            is_active = row is not None and row["deleted_at"] is None
            add_node(
                LineageNode(
                    node_id=node_id,
                    node_type="dataset",
                    resource_ref=dataset_ref,
                    label=(
                        str(row["filename"])
                        if row is not None
                        else "已删除数据集"
                    ),
                    status="active" if is_active else "deleted",
                    conversation_id=None,
                    run_id=None,
                    metadata={
                        "derived": bool(
                            row is not None and dataset_parents.get(dataset_ref)
                        ),
                        "parent_count": len(dataset_parents.get(dataset_ref, ())),
                    },
                    created_at=(
                        str(row["created_at"]) if row is not None else None
                    ),
                )
            )
            return node_id

        for row in dataset_rows:
            if str(row["ref"]) not in visible_dataset_refs:
                continue
            child = add_dataset(str(row["ref"]))
            for parent_ref in dataset_parents.get(str(row["ref"]), ()):
                parent = add_dataset(parent_ref)
                edges.add((parent, child, "derived_from"))

        artifact_nodes: dict[str, str] = {}
        logical_analysis_sources: dict[str, str] = {}
        artifact_rows_by_id = {
            str(row["id"]): row for row in artifact_rows
        }
        for row in artifact_rows:
            artifact_id = str(row["id"])
            node_id = _node_id("artifact", artifact_id)
            params = _json_object(row["params_json"])
            analysis_id = _optional_mapping_text(params, "analysis_id")
            add_node(
                LineageNode(
                    node_id=node_id,
                    node_type="artifact",
                    resource_ref=artifact_id,
                    label=_artifact_label(
                        str(row["type"]),
                        _optional_text(row["source_tool"]),
                    ),
                    status="active",
                    conversation_id=str(row["conversation_id"]),
                    run_id=None,
                    metadata={
                        "artifact_type": str(row["type"]),
                        "source_tool": _optional_text(row["source_tool"]),
                        "analysis_id": analysis_id,
                    },
                    created_at=str(row["created_at"]),
                )
            )
            artifact_nodes[artifact_id] = node_id
            if analysis_id is not None:
                logical_analysis_sources[analysis_id] = node_id
            dataset_ref = _optional_text(row["lineage_dataset_ref"])
            if dataset_ref is not None:
                add_dataset(dataset_ref)

        analysis_nodes: dict[str, str] = {}
        artifact_producers: dict[str, str] = {}
        for row in invocation_rows:
            invocation_id = str(row["invocation_id"])
            node_id = _node_id("analysis", invocation_id)
            produced_artifact_id = _optional_text(row["artifact_id"])
            artifact_row = (
                artifact_rows_by_id.get(produced_artifact_id)
                if produced_artifact_id is not None
                else None
            )
            artifact_params = (
                _json_object(artifact_row["params_json"])
                if artifact_row is not None
                else {}
            )
            logical_analysis_id = _optional_mapping_text(
                artifact_params,
                "analysis_id",
            )
            status = str(row["status"])
            add_node(
                LineageNode(
                    node_id=node_id,
                    node_type="analysis",
                    resource_ref=invocation_id,
                    label=str(row["tool_name"]),
                    status=_analysis_status(status),
                    conversation_id=str(row["conversation_id"]),
                    run_id=str(row["run_id"]),
                    metadata={
                        "tool_name": str(row["tool_name"]),
                        "analysis_id": logical_analysis_id,
                        "result_hash": _optional_text(row["result_hash"]),
                    },
                    created_at=str(row["started_at"]),
                )
            )
            analysis_nodes[invocation_id] = node_id
            if logical_analysis_id is not None:
                logical_analysis_sources[logical_analysis_id] = node_id
            for dataset_ref in _dataset_refs(_json_object(row["args_json"])):
                if dataset_ref in dataset_anchors or _is_dataset_ref(dataset_ref):
                    edges.add((add_dataset(dataset_ref), node_id, "used_by"))
            if (
                produced_artifact_id is not None
                and produced_artifact_id in artifact_nodes
            ):
                artifact_node = artifact_nodes[produced_artifact_id]
                edges.add((node_id, artifact_node, "produced"))
                artifact_producers[produced_artifact_id] = node_id
                if artifact_row is not None:
                    dataset_ref = _optional_text(
                        artifact_row["lineage_dataset_ref"]
                    )
                    if dataset_ref is not None:
                        edges.add((add_dataset(dataset_ref), node_id, "used_by"))

        for row in artifact_rows:
            artifact_id = str(row["id"])
            artifact_node = artifact_nodes[artifact_id]
            dataset_ref = _optional_text(row["lineage_dataset_ref"])
            if dataset_ref is not None and artifact_id not in artifact_producers:
                edges.add((add_dataset(dataset_ref), artifact_node, "profiled_as"))
            params = _json_object(row["params_json"])
            analysis_ids = params.get("analysis_ids")
            if isinstance(analysis_ids, list):
                for analysis_id in analysis_ids:
                    if not isinstance(analysis_id, str):
                        continue
                    source = logical_analysis_sources.get(analysis_id)
                    if source is not None and source != artifact_node:
                        edges.add((source, artifact_node, "included_in"))

        evidence_nodes: dict[str, str] = {}
        for row in evidence_rows:
            evidence_id = str(row["evidence_id"])
            node_id = _node_id("evidence", evidence_id)
            add_node(
                LineageNode(
                    node_id=node_id,
                    node_type="evidence",
                    resource_ref=evidence_id,
                    label=f"Evidence · {row['kind']}",
                    status="active",
                    conversation_id=str(row["conversation_id"]),
                    run_id=str(row["run_id"]),
                    metadata={
                        "kind": str(row["kind"]),
                        "result_hash": str(row["result_hash"]),
                    },
                    created_at=str(row["created_at"]),
                )
            )
            evidence_nodes[evidence_id] = node_id
            evidence_artifact_id = _optional_text(row["artifact_id"])
            invocation_id = str(row["invocation_id"])
            if (
                evidence_artifact_id is not None
                and evidence_artifact_id in artifact_nodes
            ):
                edges.add(
                    (
                        artifact_nodes[evidence_artifact_id],
                        node_id,
                        "substantiates",
                    )
                )
            elif invocation_id in analysis_nodes:
                edges.add(
                    (analysis_nodes[invocation_id], node_id, "substantiates")
                )

        claim_nodes: dict[str, str] = {}
        evidence_count_by_claim: dict[str, int] = {}
        for row in claim_evidence_rows:
            claim_id = str(row["claim_id"])
            evidence_count_by_claim[claim_id] = (
                evidence_count_by_claim.get(claim_id, 0) + 1
            )
        for row in claim_rows:
            claim_id = str(row["claim_id"])
            node_id = _node_id("claim", claim_id)
            add_node(
                LineageNode(
                    node_id=node_id,
                    node_type="claim",
                    resource_ref=claim_id,
                    label=str(row["statement"])[:2_000],
                    status="active",
                    conversation_id=str(row["conversation_id"]),
                    run_id=str(row["run_id"]),
                    metadata={
                        "claim_kind": str(row["claim_kind"]),
                        "evidence_count": evidence_count_by_claim.get(claim_id, 0),
                    },
                    created_at=str(row["created_at"]),
                )
            )
            claim_nodes[claim_id] = node_id
        for row in claim_evidence_rows:
            claim_node = claim_nodes.get(str(row["claim_id"]))
            evidence_node = evidence_nodes.get(str(row["evidence_id"]))
            if claim_node is not None and evidence_node is not None:
                edges.add((evidence_node, claim_node, "supports"))

        all_nodes = sorted(
            nodes.values(),
            key=lambda item: (
                item.created_at or "",
                item.node_type,
                item.node_id,
            ),
        )
        all_edges = tuple(
            LineageEdge(source, target, relation)
            for source, target, relation in sorted(edges)
        )
        graph_hash = _graph_hash(all_nodes, all_edges)
        selected_nodes = all_nodes[-max_nodes:]
        selected_ids = {node.node_id for node in selected_nodes}
        selected_edges = tuple(
            edge
            for edge in all_edges
            if edge.source in selected_ids and edge.target in selected_ids
        )
        return LineageGraph(
            project_id=project_id,
            nodes=tuple(selected_nodes),
            edges=selected_edges,
            graph_hash=graph_hash,
            integrity_status="ok" if not issues else "degraded",
            issues=issues,
            total_nodes=len(all_nodes),
            total_edges=len(all_edges),
            truncated=len(selected_nodes) != len(all_nodes),
        )

    def _audit(
        self,
        *,
        project_id: str,
        principal: Principal,
        outcome: Literal["allowed", "denied"],
        detail: dict[str, Any],
    ) -> None:
        self._audit_recorder(
            AuditEvent(
                actor=principal.user_id,
                tenant_id=principal.tenant_scope,
                action="lineage.read",
                resource="lineage_control_plane",
                outcome=outcome,
                project_id=project_id,
                detail=detail,
            )
        )

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self._path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            yield connection
        finally:
            connection.close()


def inspect_lineage_connection(connection: sqlite3.Connection) -> dict[str, object]:
    """生成不含正文的全库血缘完整性摘要，供 readiness/备份恢复比对。"""
    issues = _integrity_issues(connection, project_id=None)
    tables = (
        (
            "dataset_lineage_anchors",
            "ref, project_id, parent_ref, created_at, deleted_at",
        ),
        (
            "dataset_lineage_edges",
            "child_ref, parent_ref, parent_role, ordinal, created_at",
        ),
        (
            "artifacts",
            "id, conversation_id, lineage_dataset_ref, source_tool, created_at",
        ),
        (
            "tool_invocations",
            "invocation_id, run_id, artifact_id, args_hash, result_hash, status",
        ),
        (
            "evidence",
            "evidence_id, run_id, invocation_id, artifact_id, result_hash",
        ),
        ("claims", "claim_id, run_id, claim_kind, created_at"),
        ("claim_evidence", "claim_id, evidence_id"),
    )
    canonical: list[dict[str, object]] = []
    counts: dict[str, int] = {}
    for table, columns in tables:
        rows = connection.execute(
            f'SELECT {columns} FROM "{table}" ORDER BY rowid'
        ).fetchall()
        counts[table] = len(rows)
        canonical.append(
            {
                "table": table,
                "rows": [list(row) for row in rows],
            }
        )
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "version": "lineage-v2",
        "integrity": "ok" if not issues else "degraded",
        "content_hash": hashlib.sha256(encoded).hexdigest(),
        "counts": counts,
        "issues": [asdict(issue) for issue in issues],
    }


def _integrity_issues(
    connection: sqlite3.Connection,
    *,
    project_id: str | None,
) -> tuple[LineageIssue, ...]:
    project_clause = "" if project_id is None else " AND run.project_id = ?"
    args: tuple[object, ...] = () if project_id is None else (project_id,)
    checks = {
        "claim_evidence_cross_run": (
            f"""
            SELECT COUNT(*)
            FROM claim_evidence AS link
            JOIN claims AS claim ON claim.claim_id = link.claim_id
            JOIN evidence ON evidence.evidence_id = link.evidence_id
            JOIN task_runs AS run ON run.run_id = claim.run_id
            WHERE claim.run_id != evidence.run_id {project_clause}
            """,
            args,
        ),
        "invocation_artifact_cross_project": (
            f"""
            SELECT COUNT(*)
            FROM tool_invocations AS invocation
            JOIN task_runs AS run ON run.run_id = invocation.run_id
            JOIN artifacts AS artifact ON artifact.id = invocation.artifact_id
            JOIN conversations AS conversation
              ON conversation.id = artifact.conversation_id
            WHERE run.project_id != conversation.project_id {project_clause}
            """,
            args,
        ),
        "evidence_artifact_cross_project": (
            f"""
            SELECT COUNT(*)
            FROM evidence
            JOIN task_runs AS run ON run.run_id = evidence.run_id
            JOIN artifacts AS artifact ON artifact.id = evidence.artifact_id
            JOIN conversations AS conversation
              ON conversation.id = artifact.conversation_id
            WHERE run.project_id != conversation.project_id {project_clause}
            """,
            args,
        ),
        "artifact_dataset_cross_project": (
            """
            SELECT COUNT(*)
            FROM artifacts AS artifact
            JOIN conversations AS conversation
              ON conversation.id = artifact.conversation_id
            JOIN dataset_lineage_anchors AS dataset
              ON dataset.ref = artifact.lineage_dataset_ref
            WHERE conversation.project_id != dataset.project_id
              AND (? IS NULL OR conversation.project_id = ?)
            """,
            (project_id, project_id),
        ),
        "dataset_parent_cross_project": (
            """
            SELECT COUNT(*)
            FROM dataset_lineage_edges AS edge
            JOIN dataset_lineage_anchors AS child ON child.ref = edge.child_ref
            JOIN dataset_lineage_anchors AS parent ON parent.ref = edge.parent_ref
            WHERE child.project_id != parent.project_id
              AND (? IS NULL OR child.project_id = ?)
            """,
            (project_id, project_id),
        ),
        "lineage_anchor_drift": (
            """
            SELECT (
                SELECT COUNT(*) FROM datasets AS dataset
                LEFT JOIN dataset_lineage_anchors AS anchor
                  ON anchor.ref = dataset.ref
                WHERE (
                        anchor.ref IS NULL
                        OR dataset.project_id != anchor.project_id
                        OR dataset.filename != anchor.filename
                        OR dataset.lineage_parent_ref IS NOT anchor.parent_ref
                      )
                  AND (? IS NULL OR dataset.project_id = ?)
            ) + (
                SELECT COUNT(*) FROM artifacts AS artifact
                JOIN conversations AS conversation
                  ON conversation.id = artifact.conversation_id
                WHERE artifact.dataset_ref IS NOT NULL
                  AND artifact.dataset_ref IS NOT artifact.lineage_dataset_ref
                  AND (? IS NULL OR conversation.project_id = ?)
            )
            """,
            (project_id, project_id, project_id, project_id),
        ),
        "dataset_lineage_edge_drift": (
            """
            SELECT COUNT(*)
            FROM dataset_lineage_anchors AS anchor
            LEFT JOIN dataset_lineage_edges AS edge
              ON edge.child_ref = anchor.ref AND edge.ordinal = 0
            WHERE (
                    (anchor.parent_ref IS NULL AND edge.parent_ref IS NOT NULL)
                    OR (anchor.parent_ref IS NOT edge.parent_ref)
                  )
              AND (? IS NULL OR anchor.project_id = ?)
            """,
            (project_id, project_id),
        ),
    }
    issues: list[LineageIssue] = []
    for code, (query, parameters) in checks.items():
        row = connection.execute(query, parameters).fetchone()
        count = int(row[0]) if row is not None else 0
        if count:
            issues.append(LineageIssue(code=code, count=count))
    return tuple(issues)


def _require_scope(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    conversation_id: str | None,
    principal: Principal,
) -> None:
    membership = connection.execute(
        """
        SELECT 1 FROM project_memberships
        WHERE project_id = ? AND user_id = ? AND tenant_id = ?
        """,
        (project_id, principal.user_id, principal.tenant_scope),
    ).fetchone()
    if membership is None:
        raise LineageAccessDenied("血缘项目不存在")
    if conversation_id is not None:
        conversation = connection.execute(
            """
            SELECT 1 FROM conversations
            WHERE id = ? AND project_id = ?
            """,
            (conversation_id, project_id),
        ).fetchone()
        if conversation is None:
            raise LineageAccessDenied("血缘对话不存在")


def _conversation_scope(conversation_id: str | None) -> tuple[str, tuple[object, ...]]:
    if conversation_id is None:
        return "", ()
    return "AND artifact.conversation_id = ?", (conversation_id,)


def _node_id(node_type: LineageNodeType, resource_ref: str) -> str:
    return f"{node_type}:{resource_ref}"


def _artifact_label(artifact_type: str, source_tool: str | None) -> str:
    return f"{artifact_type} · {source_tool}" if source_tool else artifact_type


def _analysis_status(value: str) -> LineageNodeStatus:
    if value in {"running", "succeeded", "failed", "unknown"}:
        return value  # type: ignore[return-value]
    return "unknown"


def _json_object(value: object) -> dict[str, Any]:
    if value is None:
        return {}
    try:
        parsed = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_mapping_text(value: dict[str, Any], key: str) -> str | None:
    item = value.get(key)
    return item if isinstance(item, str) and item.strip() else None


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value) else None


def _dataset_refs(value: object) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "dataset_ref",
                "left_dataset_ref",
                "right_dataset_ref",
            } and isinstance(item, str):
                refs.add(item)
            elif key == "dataset_refs" and isinstance(item, list):
                refs.update(entry for entry in item if isinstance(entry, str))
            elif isinstance(item, dict | list):
                refs.update(_dataset_refs(item))
    elif isinstance(value, list):
        for item in value:
            refs.update(_dataset_refs(item))
    return refs


def _is_dataset_ref(value: str) -> bool:
    return len(value) == 32 and all(char in "0123456789abcdef" for char in value)


def _graph_hash(
    nodes: list[LineageNode],
    edges: tuple[LineageEdge, ...],
) -> str:
    payload = {
        "version": "lineage-v2",
        "nodes": [asdict(node) for node in nodes],
        "edges": [asdict(edge) for edge in edges],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
