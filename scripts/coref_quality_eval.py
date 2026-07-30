"""v2.5 3C 领域中立指代解析质量门禁。

评测直接运行 Host ``ReferenceResolver`` 与 ``MemoryReferenceResolver``，不调用模型。
冻结用例覆盖确定解析、应澄清、删除/过期/版本漂移和跨项目攻击；报告不包含查询正文、
别名、资源 ID 或记忆正文。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.governance.permissions import Principal  # noqa: E402
from packages.session.coref import ReferenceResolution, ReferenceResolver  # noqa: E402
from packages.session.memory_models import (  # noqa: E402
    MemoryDraft,
    MemoryKind,
    MemoryRecord,
    MemoryScope,
)
from packages.session.memory_refs import (  # noqa: E402
    MemoryReferenceResolution,
    MemoryReferenceResolver,
    memory_reference_semantic_key,
    memory_reference_summary,
)
from packages.session.memory_store import MemoryStore  # noqa: E402
from packages.session.store import SessionStore  # noqa: E402

DEFAULT_CASES = Path(__file__).parent / "coref_quality_eval_set.jsonl"
THRESHOLDS = {
    "deterministic_resolution_rate": {"operator": ">=", "value": 1.0},
    "misbinding_rate": {"operator": "<=", "value": 0.0},
    "clarification_recall": {"operator": ">=", "value": 1.0},
    "cross_project_leak_count": {"operator": "<=", "value": 0},
}
_STATUSES = frozenset({"no_reference", "resolved", "ambiguous", "unresolved"})
_RESOLVERS = frozenset({"coref", "memory"})
_FIXTURES = frozenset(
    {
        "base",
        "cross_project_dataset",
        "deleted_artifact",
        "cross_conversation_artifact",
        "entity_unique",
        "field_alias",
        "confirmed_artifact",
        "scope_ambiguous",
        "conflict",
        "expired",
        "deleted",
        "cross_project_memory",
        "legacy_summary",
        "superseded_after_snapshot",
    }
)
_PRINCIPAL = Principal(user_id="coref-quality-owner", tenant_id="coref-quality-tenant")


@dataclass(slots=True)
class _Workspace:
    session: SessionStore
    memories: MemoryStore
    project_id: str
    conversation_id: str
    datasets: dict[str, str]
    artifacts: dict[str, str]
    analyses: dict[str, str]
    tokens: dict[str, str]
    foreign_refs: set[str]
    memory_snapshot_id: str | None = None


def load_cases(path: Path = DEFAULT_CASES) -> list[dict[str, Any]]:
    """加载并验证冻结用例，拒绝宽松或自相矛盾的期望。"""
    cases: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"第 {line_number} 行不是合法 JSON") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"第 {line_number} 行必须是对象")
        expected_keys = {
            "id",
            "resolver",
            "fixture",
            "query",
            "expected_status",
            "expected_targets",
            "expected_reason",
            "clarification_required",
            "cross_project_attack",
        }
        if set(raw) != expected_keys:
            raise ValueError(f"第 {line_number} 行字段集合无效")
        case_id = raw.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"第 {line_number} 行缺少 case id")
        if case_id in seen:
            raise ValueError(f"case id 重复: {case_id}")
        seen.add(case_id)
        if raw.get("resolver") not in _RESOLVERS:
            raise ValueError(f"{case_id}: resolver 无效")
        if raw.get("fixture") not in _FIXTURES:
            raise ValueError(f"{case_id}: fixture 无效")
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip() or len(query) > 500:
            raise ValueError(f"{case_id}: query 无效")
        status = raw.get("expected_status")
        if status not in _STATUSES:
            raise ValueError(f"{case_id}: expected_status 无效")
        targets = raw.get("expected_targets")
        if not isinstance(targets, list) or not all(
            isinstance(item, str) and item.startswith(("artifact:", "dataset:"))
            for item in targets
        ):
            raise ValueError(f"{case_id}: expected_targets 无效")
        if (status == "resolved") != bool(targets):
            raise ValueError(f"{case_id}: resolved 状态和目标不一致")
        reason = raw.get("expected_reason")
        if reason is not None and (not isinstance(reason, str) or not reason):
            raise ValueError(f"{case_id}: expected_reason 无效")
        if status in {"resolved", "no_reference"} and reason is not None:
            raise ValueError(f"{case_id}: 成功/no_reference 不应设置 reason")
        for field in ("clarification_required", "cross_project_attack"):
            if not isinstance(raw.get(field), bool):
                raise ValueError(f"{case_id}: {field} 必须是布尔值")
        cases.append(raw)
    if not cases:
        raise ValueError("指代质量用例为空")
    return cases


def run_evaluation(
    cases: list[dict[str, Any]],
    *,
    working_dir: Path | None = None,
) -> dict[str, Any]:
    """通过真实 SQLite Store 和两个 Host Resolver 执行冻结用例。"""
    if working_dir is None:
        with tempfile.TemporaryDirectory(prefix="chatbi-coref-eval-") as temporary:
            return _evaluate_in_directory(cases, Path(temporary))
    working_dir.mkdir(parents=True, exist_ok=True)
    return _evaluate_in_directory(cases, working_dir)


def _evaluate_in_directory(
    cases: list[dict[str, Any]],
    working_dir: Path,
) -> dict[str, Any]:
    rows = [_evaluate_case(case, working_dir / f"{case['id']}.db") for case in cases]
    expected_resolved = sum(row["expected_resolved"] for row in rows)
    expected_clarifications = sum(row["expected_clarification"] for row in rows)
    resolution_rate = (
        sum(row["correct_resolution"] for row in rows) / expected_resolved
        if expected_resolved
        else 1.0
    )
    clarification_recall = (
        sum(row["correct_clarification"] for row in rows) / expected_clarifications
        if expected_clarifications
        else 1.0
    )
    metrics: dict[str, float | int] = {
        "deterministic_resolution_rate": resolution_rate,
        "misbinding_rate": sum(row["misbound"] for row in rows) / len(rows),
        "clarification_recall": clarification_recall,
        "cross_project_leak_count": sum(row["cross_project_leaked"] for row in rows),
    }
    misses = _threshold_misses(metrics)
    return {
        "evaluation": "v2.5_coref_quality",
        "case_set_sha256": hashlib.sha256(
            json.dumps(
                cases,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest(),
        "case_count": len(rows),
        "metrics": metrics,
        "thresholds": THRESHOLDS,
        "cases": rows,
        "passed": not misses and all(row["passed"] for row in rows),
        "misses": misses,
        "contains_query_text": False,
        "contains_resource_ids": False,
    }


def _evaluate_case(case: dict[str, Any], database: Path) -> dict[str, Any]:
    workspace = _seed_workspace(case, database)
    query = _render_query(str(case["query"]), workspace.tokens)
    if case["resolver"] == "coref":
        resolution: ReferenceResolution | MemoryReferenceResolution = ReferenceResolver(
            workspace.session,
            audit_recorder=lambda _event: None,
        ).resolve(
            query,
            project_id=workspace.project_id,
            conversation_id=workspace.conversation_id,
            principal=_PRINCIPAL,
        )
    else:
        if workspace.memory_snapshot_id is None:
            raise RuntimeError(f"{case['id']}: memory fixture 未创建快照")
        resolution = MemoryReferenceResolver(
            workspace.session,
            workspace.memories,
            audit_recorder=lambda _event: None,
        ).resolve(
            query,
            project_id=workspace.project_id,
            conversation_id=workspace.conversation_id,
            memory_snapshot_id=workspace.memory_snapshot_id,
            principal=_PRINCIPAL,
        )
    actual_targets = _target_keys(resolution, workspace)
    expected_targets = list(case["expected_targets"])
    status_match = resolution.status == case["expected_status"]
    targets_match = actual_targets == expected_targets
    reason_match = resolution.reason_code == case["expected_reason"]
    clarification = resolution.clarification()
    clarification_match = (
        clarification is not None
        if case["clarification_required"]
        else clarification is None
    )
    cross_project_leaked = bool(
        case["cross_project_attack"]
        and (
            resolution.status == "resolved"
            or any(
                target.reference_id in workspace.foreign_refs
                for target in resolution.targets
            )
        )
    )
    expected_resolved = case["expected_status"] == "resolved"
    correct_resolution = expected_resolved and status_match and targets_match
    misbound = resolution.status == "resolved" and (
        not expected_resolved or not targets_match
    )
    expected_clarification = bool(case["clarification_required"])
    correct_clarification = expected_clarification and clarification is not None
    checks = {
        "status_match": status_match,
        "targets_match": targets_match,
        "reason_match": reason_match,
        "clarification_match": clarification_match,
        "no_cross_project_leak": not cross_project_leaked,
    }
    return {
        "id": case["id"],
        "resolver": case["resolver"],
        "actual_status": resolution.status,
        "reason_code": resolution.reason_code,
        "target_count": len(resolution.targets),
        "choice_count": len(resolution.choices),
        "resolution_hash": resolution.resolution_hash,
        **checks,
        "expected_resolved": expected_resolved,
        "correct_resolution": correct_resolution,
        "misbound": misbound,
        "expected_clarification": expected_clarification,
        "correct_clarification": correct_clarification,
        "cross_project_leaked": cross_project_leaked,
        "passed": all(checks.values()),
    }


def _seed_workspace(case: dict[str, Any], database: Path) -> _Workspace:
    session = SessionStore(str(database))
    project = session.create_project(
        f"coref-quality-{case['id']}",
        owner_user_id=_PRINCIPAL.user_id,
        tenant_id=_PRINCIPAL.tenant_scope,
    )
    conversation = session.create_conversation(project.id)
    dataset_names = {
        "d1": "设备清单.xlsx",
        "d2": "遥测样本.csv",
        "d3": "重复名称.csv",
        "d4": "重复名称.csv",
    }
    datasets: dict[str, str] = {}
    for key, filename in dataset_names.items():
        reference = hashlib.sha256(f"{case['id']}:{key}".encode()).hexdigest()[:32]
        session.register_dataset(
            ref=reference,
            project_id=project.id,
            filename=filename,
            profile={"column_count": 2},
        )
        datasets[key] = reference
    artifact_message = session.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="领域中立质量工件已生成。",
    )
    artifact_specs = (
        ("chart1", "chart", "gen_chart", "d1"),
        ("trend1", "stats", "trend_analysis", "d1"),
        ("chart2", "chart", "gen_chart", "d2"),
        ("trend2", "stats", "trend_analysis", "d2"),
        ("report1", "report", "generate_report", "d2"),
    )
    artifacts: dict[str, str] = {}
    analyses: dict[str, str] = {}
    for key, artifact_type, source_tool, dataset_key in artifact_specs:
        analysis_id = hashlib.sha256(
            f"{case['id']}:analysis:{key}".encode()
        ).hexdigest()[:12]
        artifact = session.create_artifact(
            conversation_id=conversation.id,
            message_id=artifact_message.id,
            type=artifact_type,
            payload={"fixture": key},
            source_tool=source_tool,
            params={"analysis_id": analysis_id},
            dataset_ref=datasets[dataset_key],
        )
        artifacts[key] = artifact.id
        analyses[key] = analysis_id
    workspace = _Workspace(
        session=session,
        memories=MemoryStore(session, audit_recorder=lambda _event: None),
        project_id=project.id,
        conversation_id=conversation.id,
        datasets=datasets,
        artifacts=artifacts,
        analyses=analyses,
        tokens={
            **{f"analysis:{key}": value for key, value in analyses.items()},
        },
        foreign_refs=set(),
    )
    fixture = str(case["fixture"])
    if fixture == "cross_project_dataset":
        other_project = session.create_project(
            "隔离项目",
            owner_user_id=_PRINCIPAL.user_id,
            tenant_id=_PRINCIPAL.tenant_scope,
        )
        other_ref = hashlib.sha256(f"{case['id']}:outside".encode()).hexdigest()[:32]
        session.register_dataset(
            ref=other_ref,
            project_id=other_project.id,
            filename="隔离样本.csv",
            profile={},
        )
        workspace.tokens["other_dataset"] = other_ref
        workspace.foreign_refs.add(other_ref)
    elif fixture == "deleted_artifact":
        session.delete_artifact(artifacts["chart2"])
    elif fixture == "cross_conversation_artifact":
        other_conversation = session.create_conversation(project.id)
        other_message = session.append_message(
            conversation_id=other_conversation.id,
            role="assistant",
            content="另一个对话工件。",
        )
        other_analysis = hashlib.sha256(
            f"{case['id']}:other-analysis".encode()
        ).hexdigest()[:12]
        other_artifact = session.create_artifact(
            conversation_id=other_conversation.id,
            message_id=other_message.id,
            type="chart",
            payload={"fixture": "other"},
            source_tool="gen_chart",
            params={"analysis_id": other_analysis},
            dataset_ref=datasets["d1"],
        )
        workspace.tokens["other_analysis"] = other_analysis
        workspace.foreign_refs.add(other_artifact.id)
    if case["resolver"] == "memory":
        _seed_memory_fixture(workspace, fixture)
    return workspace


def _seed_memory_fixture(workspace: _Workspace, fixture: str) -> None:
    snapshot_before_mutation = False
    if fixture == "entity_unique":
        _remember(
            workspace,
            alias="主样本",
            kind="entity_mapping",
            target=("dataset", workspace.datasets["d2"]),
            key="entity",
        )
    elif fixture == "field_alias":
        _remember(
            workspace,
            alias="请求编号",
            kind="field_alias",
            target=("dataset", workspace.datasets["d1"]),
            canonical_field="工单编号",
            key="field",
        )
    elif fixture == "confirmed_artifact":
        _remember(
            workspace,
            alias="已确认图",
            kind="confirmed_decision",
            target=("artifact", workspace.artifacts["chart2"]),
            key="decision",
        )
    elif fixture == "scope_ambiguous":
        _remember(
            workspace,
            alias="当前批次",
            kind="entity_mapping",
            target=("dataset", workspace.datasets["d1"]),
            key="scope-project",
        )
        conversation_record = _remember(
            workspace,
            alias="当前批次",
            kind="entity_mapping",
            target=("dataset", workspace.datasets["d2"]),
            scope="conversation",
            key="scope-conversation",
        )
        workspace.tokens["memory:conversation"] = conversation_record.memory_id
    elif fixture == "conflict":
        original = _remember(
            workspace,
            alias="固定工件",
            kind="confirmed_decision",
            target=("artifact", workspace.artifacts["chart1"]),
            key="conflict-active",
        )
        conflict = _remember(
            workspace,
            alias="固定工件",
            kind="confirmed_decision",
            target=("dataset", workspace.datasets["d2"]),
            key="conflict-candidate",
        )
        if original.status != "active" or conflict.status != "conflict":
            raise RuntimeError("冲突 fixture 未形成 active/conflict")
    elif fixture == "expired":
        _remember(
            workspace,
            alias="历史样本",
            kind="entity_mapping",
            target=("dataset", workspace.datasets["d1"]),
            valid_from="2025-01-01T00:00:00Z",
            expires_at="2025-02-01T00:00:00Z",
            key="expired",
        )
    elif fixture == "deleted":
        record = _remember(
            workspace,
            alias="废弃样本",
            kind="entity_mapping",
            target=("dataset", workspace.datasets["d1"]),
            key="deleted",
        )
        workspace.memories.soft_delete(
            record.memory_id,
            project_id=workspace.project_id,
            principal=_PRINCIPAL,
            expected_version=record.version,
            idempotency_key="quality-delete",
        )
    elif fixture == "cross_project_memory":
        other_project = workspace.session.create_project(
            "隔离记忆项目",
            owner_user_id=_PRINCIPAL.user_id,
            tenant_id=_PRINCIPAL.tenant_scope,
        )
        other_conversation = workspace.session.create_conversation(other_project.id)
        other_ref = hashlib.sha256(b"foreign-memory-dataset").hexdigest()[:32]
        workspace.session.register_dataset(
            ref=other_ref,
            project_id=other_project.id,
            filename="隔离记忆样本.csv",
            profile={},
        )
        source = workspace.session.append_message(
            conversation_id=other_conversation.id,
            role="user",
            content="确认外部样本映射。",
        )
        other_memories = MemoryStore(
            workspace.session,
            audit_recorder=lambda _event: None,
        )
        record = other_memories.remember(
            project_id=other_project.id,
            principal=_PRINCIPAL,
            draft=MemoryDraft(
                scope="project",
                kind="entity_mapping",
                semantic_key=memory_reference_semantic_key(
                    kind="entity_mapping",
                    alias="外部样本",
                ),
                content_summary=memory_reference_summary(
                    kind="entity_mapping",
                    alias="外部样本",
                ),
                source_type="user_confirmation",
                source_ref=source.id,
                source_hash=_text_hash(source.content),
                confidence=1.0,
            ),
            idempotency_key="foreign-memory",
        ).record
        other_memories.add_link(
            record.memory_id,
            project_id=other_project.id,
            principal=_PRINCIPAL,
            target_type="dataset",
            target_ref=other_ref,
        )
        workspace.tokens["other_memory"] = record.memory_id
        workspace.foreign_refs.add(other_ref)
    elif fixture == "legacy_summary":
        source = workspace.session.append_message(
            conversation_id=workspace.conversation_id,
            role="user",
            content="确认旧别名。",
        )
        workspace.memories.remember(
            project_id=workspace.project_id,
            principal=_PRINCIPAL,
            draft=MemoryDraft(
                scope="project",
                kind="field_alias",
                semantic_key="legacy.old-alias",
                content_summary="旧别名代表当前字段",
                source_type="user_confirmation",
                source_ref=source.id,
                source_hash=_text_hash(source.content),
                confidence=1.0,
            ),
            idempotency_key="legacy-summary",
        )
    elif fixture == "superseded_after_snapshot":
        original = _remember(
            workspace,
            alias="固定版本",
            kind="entity_mapping",
            target=("dataset", workspace.datasets["d1"]),
            key="superseded-original",
        )
        workspace.memory_snapshot_id = _snapshot(workspace)
        snapshot_before_mutation = True
        source = workspace.session.append_message(
            conversation_id=workspace.conversation_id,
            role="user",
            content="确认固定版本改为第二个样本。",
        )
        revised = workspace.memories.revise(
            original.memory_id,
            project_id=workspace.project_id,
            principal=_PRINCIPAL,
            expected_version=original.version,
            draft=MemoryDraft(
                scope="project",
                kind="entity_mapping",
                semantic_key=original.semantic_key,
                content_summary=original.content_summary,
                source_type="user_confirmation",
                source_ref=source.id,
                source_hash=_text_hash(source.content),
                confidence=1.0,
            ),
            idempotency_key="quality-supersede",
        ).record
        workspace.memories.add_link(
            revised.memory_id,
            project_id=workspace.project_id,
            principal=_PRINCIPAL,
            target_type="dataset",
            target_ref=workspace.datasets["d2"],
        )
    else:
        raise RuntimeError(f"不支持的 memory fixture: {fixture}")
    if not snapshot_before_mutation:
        workspace.memory_snapshot_id = _snapshot(workspace)


def _remember(
    workspace: _Workspace,
    *,
    alias: str,
    kind: MemoryKind,
    target: tuple[Literal["artifact", "dataset"], str],
    key: str,
    scope: MemoryScope = "project",
    canonical_field: str | None = None,
    valid_from: str | None = None,
    expires_at: str | None = None,
) -> MemoryRecord:
    source = workspace.session.append_message(
        conversation_id=workspace.conversation_id,
        role="user",
        content=f"确认 {key} 引用。",
    )
    record = workspace.memories.remember(
        project_id=workspace.project_id,
        principal=_PRINCIPAL,
        draft=MemoryDraft(
            scope=scope,
            kind=kind,
            semantic_key=memory_reference_semantic_key(kind=kind, alias=alias),
            content_summary=memory_reference_summary(
                kind=kind,
                alias=alias,
                canonical_field=canonical_field,
            ),
            source_type="user_confirmation",
            source_ref=source.id,
            source_hash=_text_hash(source.content),
            confidence=1.0,
            conversation_id=(
                workspace.conversation_id if scope == "conversation" else None
            ),
            valid_from=valid_from,
            expires_at=expires_at,
        ),
        idempotency_key=f"quality-{key}",
    ).record
    workspace.memories.add_link(
        record.memory_id,
        project_id=workspace.project_id,
        principal=_PRINCIPAL,
        target_type=target[0],
        target_ref=target[1],
    )
    return record


def _snapshot(workspace: _Workspace) -> str:
    snapshot, _ = workspace.memories.create_snapshot(
        project_id=workspace.project_id,
        conversation_id=workspace.conversation_id,
        principal=_PRINCIPAL,
    )
    return snapshot.memory_snapshot_id


def _target_keys(
    resolution: ReferenceResolution | MemoryReferenceResolution,
    workspace: _Workspace,
) -> list[str]:
    artifact_keys = {value: key for key, value in workspace.artifacts.items()}
    dataset_keys = {value: key for key, value in workspace.datasets.items()}
    result: list[str] = []
    for target in resolution.targets:
        if target.kind == "artifact":
            key = artifact_keys.get(target.reference_id, "foreign")
            result.append(f"artifact:{key}")
        else:
            key = dataset_keys.get(target.reference_id, "foreign")
            result.append(f"dataset:{key}")
    return result


def _render_query(template: str, tokens: dict[str, str]) -> str:
    query = template
    for key, value in tokens.items():
        query = query.replace("{{" + key + "}}", value)
    if "{{" in query or "}}" in query:
        raise ValueError("质量用例引用了未知模板 token")
    return query


def _threshold_misses(metrics: dict[str, float | int]) -> dict[str, object]:
    misses: dict[str, object] = {}
    for name, threshold in THRESHOLDS.items():
        value = metrics[name]
        required = cast(float | int, threshold["value"])
        operator = threshold["operator"]
        if (operator == ">=" and value < required) or (
            operator == "<=" and value > required
        ):
            misses[name] = {
                "actual": value,
                "operator": operator,
                "required": required,
            }
    return misses


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _print_human(report: dict[str, Any]) -> None:
    print(f"指代解析质量：{report['case_count']} 个领域中立场景")
    metrics = report["metrics"]
    print(f"- deterministic_resolution_rate: {metrics['deterministic_resolution_rate']:.0%}")
    print(f"- misbinding_rate: {metrics['misbinding_rate']:.0%}")
    print(f"- clarification_recall: {metrics['clarification_recall']:.0%}")
    print(f"- cross_project_leak_count: {metrics['cross_project_leak_count']}")
    failed = [row["id"] for row in report["cases"] if not row["passed"]]
    if failed:
        print(f"未通过用例：{', '.join(failed)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="v2.5 3C 指代解析质量门禁")
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
        print(f"指代质量用例契约有效：{len(cases)} cases")
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
        print(f"指代解析质量门禁未通过：{report['misses']}")
        return 1
    if args.enforce:
        print("指代解析质量门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
