"""v2.5 3E 领域中立血缘完整性、隔离与恢复质量门禁。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apps.orchestrator.control.contracts import build_minimal_contract  # noqa: E402
from packages.governance.permissions import Principal  # noqa: E402
from packages.session.lineage import LineageGraph, LineageStore  # noqa: E402
from packages.session.models import ArtifactDraft  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402
from packages.session.task_models import ClaimDraft  # noqa: E402
from packages.session.task_store import TaskStore  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "lineage_quality_eval_set.jsonl"
THRESHOLDS = {
    "contract_rate": 1.0,
    "deterministic_reopen_rate": 1.0,
    "safe_metadata_rate": 1.0,
    "isolation_rate": 1.0,
    "deleted_anchor_retention_rate": 1.0,
    "integrity_detection_rate": 1.0,
}
_FIXTURES = frozenset({"profile", "complete", "deletion", "isolation", "bounded", "drift"})
_NODE_TYPES = frozenset({"dataset", "analysis", "artifact", "evidence", "claim"})
_RELATIONS = frozenset(
    {
        "derived_from",
        "used_by",
        "produced",
        "profiled_as",
        "included_in",
        "substantiates",
        "supports",
    }
)
_PRIVATE_METADATA_KEYS = frozenset(
    {
        "args",
        "args_json",
        "payload",
        "payload_json",
        "file_ref",
        "profile",
        "profile_json",
        "transform",
        "transform_json",
    }
)
_PRINCIPAL = Principal(user_id="lineage-quality-owner", tenant_id="lineage-quality-tenant")


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """加载冻结用例并拒绝未知 fixture、重复 ID 或宽松期望。"""
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    expected_keys = {
        "id",
        "fixture",
        "max_nodes",
        "expected_node_types",
        "expected_relations",
        "expected_integrity",
        "expected_truncated",
        "minimum_total_nodes",
        "deleted_dataset_count",
    }
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise ValueError(f"第 {line_number} 行字段集合无效")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"第 {line_number} 行缺少 case id")
        if case_id in seen:
            raise ValueError(f"case id 重复: {case_id}")
        seen.add(case_id)
        if raw.get("fixture") not in _FIXTURES:
            raise ValueError(f"{case_id}: fixture 无效")
        max_nodes = raw.get("max_nodes")
        if (
            isinstance(max_nodes, bool)
            or not isinstance(max_nodes, int)
            or not 1 <= max_nodes <= 2_000
        ):
            raise ValueError(f"{case_id}: max_nodes 无效")
        node_types = raw.get("expected_node_types")
        if (
            not isinstance(node_types, list)
            or not node_types
            or set(node_types) - _NODE_TYPES
            or len(node_types) != len(set(node_types))
        ):
            raise ValueError(f"{case_id}: expected_node_types 无效")
        relations = raw.get("expected_relations")
        if (
            not isinstance(relations, list)
            or set(relations) - _RELATIONS
            or len(relations) != len(set(relations))
        ):
            raise ValueError(f"{case_id}: expected_relations 无效")
        if raw.get("expected_integrity") not in {"ok", "degraded"}:
            raise ValueError(f"{case_id}: expected_integrity 无效")
        if not isinstance(raw.get("expected_truncated"), bool):
            raise ValueError(f"{case_id}: expected_truncated 必须是布尔值")
        for key in ("minimum_total_nodes", "deleted_dataset_count"):
            value = raw.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{case_id}: {key} 无效")
        cases.append(raw)
    if not cases:
        raise ValueError("血缘质量用例为空")
    return cases


def run_evaluation(
    cases: list[dict[str, Any]],
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """通过真实 v6 SQLite、TaskStore 和 LineageStore 执行冻结用例。"""
    if working_dir is None:
        with tempfile.TemporaryDirectory(prefix="chatbi-lineage-eval-") as temporary:
            return _evaluate_in_directory(cases, Path(temporary))
    working_dir.mkdir(parents=True, exist_ok=True)
    return _evaluate_in_directory(cases, working_dir)


def _evaluate_in_directory(
    cases: list[dict[str, Any]],
    working_dir: Path,
) -> dict[str, Any]:
    rows = [
        _evaluate_case(case, working_dir / f"{case['id']}.db")
        for case in cases
    ]
    count = len(rows)
    metrics = {
        metric: sum(bool(row[field]) for row in rows) / count
        for metric, field in (
            ("contract_rate", "contract"),
            ("deterministic_reopen_rate", "deterministic_reopen"),
            ("safe_metadata_rate", "safe_metadata"),
            ("isolation_rate", "isolated"),
            ("deleted_anchor_retention_rate", "deleted_anchor_retention"),
            ("integrity_detection_rate", "integrity_detection"),
        )
    }
    misses = {
        metric: {"actual": metrics[metric], "required": required}
        for metric, required in THRESHOLDS.items()
        if metrics[metric] < required
    }
    return {
        "evaluation": "v2.5_lineage_quality",
        "case_set_sha256": hashlib.sha256(
            json.dumps(
                cases,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "case_count": count,
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses and all(row["passed"] for row in rows),
        "misses": misses,
        "contains_resource_ids": False,
        "contains_content": False,
    }


def _evaluate_case(case: dict[str, Any], database: Path) -> dict[str, Any]:
    session, project_id, foreign_ref = _seed_fixture(str(case["fixture"]), database)
    lineage = LineageStore(session, audit_recorder=lambda _event: None)
    graph = lineage.build_graph(
        project_id=project_id,
        principal=_PRINCIPAL,
        max_nodes=int(case["max_nodes"]),
    )
    repeated = lineage.build_graph(
        project_id=project_id,
        principal=_PRINCIPAL,
        max_nodes=int(case["max_nodes"]),
    )
    reopened = LineageStore(
        SessionStore(str(database)),
        audit_recorder=lambda _event: None,
    ).build_graph(
        project_id=project_id,
        principal=_PRINCIPAL,
        max_nodes=int(case["max_nodes"]),
    )
    actual_types = sorted({node.node_type for node in graph.nodes})
    actual_relations = sorted({edge.relation for edge in graph.edges})
    deleted_count = sum(
        node.node_type == "dataset" and node.status == "deleted"
        for node in graph.nodes
    )
    contract = (
        actual_types == sorted(case["expected_node_types"])
        and actual_relations == sorted(case["expected_relations"])
        and graph.integrity_status == case["expected_integrity"]
        and graph.truncated is case["expected_truncated"]
        and graph.total_nodes >= int(case["minimum_total_nodes"])
    )
    deterministic_reopen = (
        graph.graph_hash == repeated.graph_hash == reopened.graph_hash
        and graph.total_nodes == reopened.total_nodes
        and graph.total_edges == reopened.total_edges
    )
    safe_metadata = _safe_metadata(graph)
    isolated = foreign_ref is None or foreign_ref not in {
        node.resource_ref for node in graph.nodes
    }
    expected_deleted = int(case["deleted_dataset_count"])
    deleted_anchor_retention = expected_deleted == 0 or deleted_count == expected_deleted
    expected_integrity = str(case["expected_integrity"])
    integrity_detection = (
        (expected_integrity == "degraded" and bool(graph.issues))
        or (expected_integrity == "ok" and not graph.issues)
    )
    checks = {
        "contract": contract,
        "deterministic_reopen": deterministic_reopen,
        "safe_metadata": safe_metadata,
        "isolated": isolated,
        "deleted_anchor_retention": deleted_anchor_retention,
        "integrity_detection": integrity_detection,
    }
    return {
        "id": str(case["id"]),
        **checks,
        "passed": all(checks.values()),
        "node_count": graph.total_nodes,
        "edge_count": graph.total_edges,
        "visible_node_count": len(graph.nodes),
        "visible_edge_count": len(graph.edges),
        "deleted_dataset_count": deleted_count,
        "integrity_status": graph.integrity_status,
        "issue_count": sum(issue.count for issue in graph.issues),
        "graph_hash": graph.graph_hash,
    }


def _seed_fixture(
    fixture: str,
    database: Path,
) -> tuple[SessionStore, str, str | None]:
    session = SessionStore(str(database))
    project = session.create_project(
        f"血缘质量-{fixture}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    message = session.append_message(
        conversation_id=conversation.id,
        role="user",
        content="检查设备对象并保留来源证明",
    )
    parent_ref = "a" * 32
    child_ref = "b" * 32
    session.register_dataset(
        ref=parent_ref,
        project_id=project.id,
        filename="设备对象.xlsx",
        profile={"row_count": 3},
    )
    foreign_ref: str | None = None
    if fixture in {"deletion", "drift"}:
        session.register_dataset(
            ref=child_ref,
            project_id=project.id,
            filename="设备对象-规范化.parquet",
            profile={"row_count": 2},
            parent_ref=parent_ref,
            transform={"operation": "normalize"},
        )
    if fixture == "profile":
        session.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="profile",
            payload={"row_count": 3},
            source_tool="get_data_profile",
            dataset_ref=parent_ref,
        )
    elif fixture == "complete":
        _seed_complete_chain(session, project.id, conversation.id, message.id, parent_ref)
    elif fixture == "deletion":
        session.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="profile",
            payload={"row_count": 2},
            source_tool="get_data_profile",
            dataset_ref=child_ref,
        )
        session.delete_dataset(parent_ref)
        session.delete_dataset(child_ref)
    elif fixture == "isolation":
        foreign = session.create_project(
            "其他租户项目",
            owner_user_id="foreign-owner",
            tenant_id="foreign-tenant",
        )
        foreign_ref = "f" * 32
        session.register_dataset(
            ref=foreign_ref,
            project_id=foreign.id,
            filename="隔离对象.csv",
            profile={"row_count": 1},
        )
    elif fixture == "bounded":
        for index, ref in enumerate(("b" * 32, "c" * 32, "d" * 32), 1):
            session.register_dataset(
                ref=ref,
                project_id=project.id,
                filename=f"对象分片-{index}.csv",
                profile={"row_count": index},
            )
    elif fixture == "drift":
        with sqlite3.connect(session.db_path) as connection:
            connection.execute(
                'DROP TRIGGER "trg_datasets_lineage_anchor_immutable"'
            )
            connection.execute(
                "UPDATE datasets SET lineage_parent_ref = NULL WHERE ref = ?",
                (child_ref,),
            )
    return session, project.id, foreign_ref


def _seed_complete_chain(
    session: SessionStore,
    project_id: str,
    conversation_id: str,
    user_message_id: str,
    dataset_ref: str,
) -> None:
    tasks = TaskStore(session.db_path)
    contract = build_minimal_contract(
        run_id="lineage-quality-run",
        user_text="检查设备对象",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    planning, _ = tasks.create_run(
        project_id=project_id,
        conversation_id=conversation_id,
        user_message_id=user_message_id,
        contract=contract,
        budget={"max_tool_calls": 1},
    )
    running, _ = tasks.transition(
        planning.run_id,
        expected_version=planning.state_version,
        status="running",
        event_type="run.started",
        payload={},
    )
    invocation, _ = tasks.start_invocation(
        run_id=running.run_id,
        tool_call_id="quality-profile-call",
        tool_name="get_data_profile",
        arguments={"dataset_ref": dataset_ref},
        idempotency_key="lineage-quality-profile",
    )
    assistant = session.append_message(
        conversation_id=conversation_id,
        role="assistant",
        content="对象画像已形成可验证结论",
    )
    _, _, evidence, _, _, _ = tasks.commit_tool_success(
        invocation.invocation_id,
        expected_version=running.state_version,
        assistant_message_id=assistant.id,
        result={"row_count": 3},
        evidence_kind="tool_result",
        evidence_source={"tool": "get_data_profile"},
        evidence_summary={"summary": "共 3 个对象"},
        artifact_draft=ArtifactDraft(
            type="profile",
            payload={"row_count": 3},
            file_ref=None,
            source_tool="get_data_profile",
            params={"analysis_id": "object-profile"},
            dataset_ref=dataset_ref,
        ),
    )
    if evidence is None:
        raise RuntimeError("血缘质量 fixture 未生成 Evidence")
    tasks.replace_claims(
        planning.run_id,
        [
            ClaimDraft(
                statement="已识别 3 个对象。",
                claim_kind="numeric",
                value_refs=(),
                evidence_ids=(evidence.evidence_id,),
            )
        ],
    )


def _safe_metadata(graph: LineageGraph) -> bool:
    return all(
        not (_PRIVATE_METADATA_KEYS & set(node.metadata))
        for node in graph.nodes
    )


def _print_human(report: dict[str, Any]) -> None:
    print(f"血缘质量：{report['case_count']} 个领域中立场景")
    for name, value in report["metrics"].items():
        print(f"- {name}: {value:.0%}")
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 3E 血缘质量门禁")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--enforce", action="store_true")
    parser.add_argument("--json-output")
    args = parser.parse_args()
    try:
        cases = load_cases(args.cases)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.validate_only:
        print(f"血缘质量用例契约有效：{len(cases)} cases")
        return 0
    report = run_evaluation(cases)
    _print_human(report)
    if args.json_output:
        output = Path(args.json_output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"JSON 报告：{output}")
    if args.enforce and not report["passed"]:
        print(f"血缘质量门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("血缘质量门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
