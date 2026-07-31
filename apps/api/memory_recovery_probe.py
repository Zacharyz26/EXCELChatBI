"""Compose 3A-3E 门禁：验证运行、记忆、压缩、工件与血缘联合恢复。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from pathlib import Path

from packages.common.config import Settings, get_settings
from packages.governance.permissions import Principal
from packages.session.compaction import CompactionStore
from packages.session.coref import ReferenceResolver, find_reference_assumption
from packages.session.lineage import LineageStore
from packages.session.memory_models import MemoryDraft
from packages.session.memory_refs import (
    MemoryReferenceResolver,
    find_memory_reference_assumptions,
    memory_reference_semantic_key,
    memory_reference_summary,
)
from packages.session.memory_store import MemoryStore
from packages.session.models import Artifact
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from apps.orchestrator.control.contracts import build_minimal_contract


def seed_probe(
    settings: Settings,
    *,
    original_run_id: str,
    principal: Principal,
) -> dict[str, object]:
    """用已完成 run 的 Artifact 创建非空记忆快照和 paused 恢复任务。"""
    session = SessionStore(settings.chat_db_path)
    tasks = TaskStore(session.db_path)
    original = tasks.get_run(original_run_id)
    if original is None or original.status != "completed":
        raise RuntimeError("恢复探针要求一个已完成的原始 TaskRun")
    if (
        session.project_role(
            original.project_id,
            user_id=principal.user_id,
            tenant_id=principal.tenant_scope,
        )
        not in {"owner", "editor"}
    ):
        raise RuntimeError("恢复探针主体没有项目写权限")
    artifacts = {
        artifact.id: artifact
        for artifact in session.list_artifacts(original.conversation_id)
    }
    evidence = tasks.list_evidence(original.run_id)
    artifact = _select_report_artifact(
        artifacts,
        evidence,
        report_dir=settings.report_dir,
    )
    confirmation = session.append_message(
        conversation_id=original.conversation_id,
        role="user",
        content="确认“恢复报告”始终指向当前已验证报告工件。",
    )
    memory_store = MemoryStore(session)
    memory = memory_store.remember(
        project_id=original.project_id,
        principal=principal,
        draft=MemoryDraft(
            scope="project",
            kind="confirmed_decision",
            semantic_key=memory_reference_semantic_key(
                kind="confirmed_decision",
                alias="恢复报告",
            ),
            content_summary=memory_reference_summary(
                kind="confirmed_decision",
                alias="恢复报告",
            ),
            source_type="user_confirmation",
            source_ref=confirmation.id,
            source_hash=_text_hash(confirmation.content),
            confidence=1.0,
        ),
        idempotency_key=f"compose-recovery-memory:{original.run_id}",
    ).record
    memory_store.add_link(
        memory.memory_id,
        project_id=original.project_id,
        principal=principal,
        target_type="artifact",
        target_ref=artifact.id,
    )
    for index in range(2):
        session.append_message(
            conversation_id=original.conversation_id,
            role="user",
            content=f"恢复探针历史问题 {index}：" + "甲" * 80,
        )
        session.append_message(
            conversation_id=original.conversation_id,
            role="assistant",
            content=f"恢复探针历史答复 {index}：" + "乙" * 80,
        )
    probe_message = session.append_message(
        conversation_id=original.conversation_id,
        role="user",
        content="验证容器重建后 TaskRun 与记忆快照保持一致。",
    )
    probe_run_id = uuid.uuid4().hex
    contract = build_minimal_contract(
        run_id=probe_run_id,
        user_text=probe_message.content,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    probe_run, _ = tasks.create_run(
        project_id=original.project_id,
        conversation_id=original.conversation_id,
        user_message_id=probe_message.id,
        contract=contract,
        budget={"max_tool_calls": 1},
    )
    compaction_store = CompactionStore(session)
    fixed_result = compaction_store.compact_if_needed(
        project_id=original.project_id,
        conversation_id=original.conversation_id,
        principal=principal,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
    )
    if fixed_result.view is None:
        raise RuntimeError("恢复探针未创建固定上下文压缩版本")
    fixed_compaction = fixed_result.view
    snapshot, records = memory_store.create_snapshot(
        project_id=original.project_id,
        principal=principal,
        conversation_id=original.conversation_id,
        run_id=probe_run.run_id,
        compaction_id=fixed_compaction.record.compaction_id,
    )
    if memory.memory_id not in {record.memory_id for record in records}:
        raise RuntimeError("恢复探针记忆未进入 TaskRun 快照")
    reference_resolution = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).resolve(
        "继续使用刚才的报告",
        project_id=original.project_id,
        conversation_id=original.conversation_id,
        principal=principal,
    )
    memory_reference_resolution = MemoryReferenceResolver(
        session,
        memory_store,
        audit_recorder=lambda _event: None,
    ).resolve(
        "继续使用恢复报告",
        project_id=original.project_id,
        conversation_id=original.conversation_id,
        memory_snapshot_id=snapshot.memory_snapshot_id,
        principal=principal,
    )
    reference_assumption = reference_resolution.assumption()
    memory_assumptions = memory_reference_resolution.assumptions()
    if (
        reference_resolution.status != "resolved"
        or reference_assumption is None
        or [target.reference_id for target in reference_resolution.targets]
        != [artifact.id]
        or memory_reference_resolution.status != "resolved"
        or len(memory_assumptions) != 1
        or [target.reference_id for target in memory_reference_resolution.targets]
        != [artifact.id]
    ):
        raise RuntimeError("恢复探针未建立唯一 Host 引用绑定")
    planned_run, plan, _, _ = tasks.save_plan(
        probe_run.run_id,
        expected_version=probe_run.state_version,
        plan={
            "schema_version": 1,
            "summary": "恢复固定 Host 引用",
            "steps": [],
            "assumptions": [reference_assumption, *memory_assumptions],
            "clarifications": [],
        },
        reason="compose_recovery_reference_probe",
        planner={"route": "host-probe"},
    )
    session.append_message(
        conversation_id=original.conversation_id,
        role="assistant",
        content="压缩快照固定后新增的答复。" + "丙" * 80,
    )
    session.append_message(
        conversation_id=original.conversation_id,
        role="user",
        content="压缩快照固定后新增的问题。" + "丁" * 80,
    )
    latest_result = compaction_store.compact_if_needed(
        project_id=original.project_id,
        conversation_id=original.conversation_id,
        principal=principal,
        trigger_chars=100,
        keep_recent=2,
        summary_max_chars=800,
    )
    if (
        latest_result.view is None
        or latest_result.view.record.compaction_id
        == fixed_compaction.record.compaction_id
    ):
        raise RuntimeError("恢复探针未创建用于漂移检测的新压缩版本")
    running, _ = tasks.transition(
        probe_run.run_id,
        expected_version=planned_run.state_version,
        status="running",
        event_type="run.started",
        payload={"reason": "compose_recovery_probe"},
    )
    paused, _ = tasks.transition(
        probe_run.run_id,
        expected_version=running.state_version,
        status="paused",
        event_type="run.paused",
        payload={"reason": "compose_recovery_probe"},
        checkpoint_reason="compose_recovery_probe",
    )
    lineage = LineageStore(
        session,
        audit_recorder=lambda _event: None,
    ).build_graph(
        project_id=original.project_id,
        principal=principal,
    )
    if lineage.integrity_status != "ok":
        raise RuntimeError("恢复探针初始血缘完整性检查未通过")
    return {
        "status": "seeded",
        "original_run_id": original.run_id,
        "probe_run_id": paused.run_id,
        "memory_snapshot_id": snapshot.memory_snapshot_id,
        "memory_content_hash": snapshot.content_hash,
        "compaction_id": fixed_compaction.record.compaction_id,
        "compaction_summary_hash": fixed_compaction.record.summary_hash,
        "latest_compaction_id": latest_result.view.record.compaction_id,
        "memory_id": memory.memory_id,
        "artifact_id": artifact.id,
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "plan_hash": _json_hash(plan.plan),
        "reference_resolution_hash": reference_resolution.resolution_hash,
        "memory_reference_resolution_hash": (
            memory_reference_resolution.resolution_hash
        ),
        "lineage_graph_hash": lineage.graph_hash,
        "lineage_node_count": lineage.total_nodes,
        "lineage_edge_count": lineage.total_edges,
        "project_id": original.project_id,
        "conversation_id": original.conversation_id,
    }


def verify_probe(
    settings: Settings,
    *,
    original_run_id: str,
    probe_run_id: str,
    memory_snapshot_id: str,
    memory_content_hash: str,
    compaction_id: str,
    compaction_summary_hash: str,
    latest_compaction_id: str,
    memory_id: str,
    artifact_id: str,
    plan_id: str,
    plan_version: int,
    plan_hash: str,
    reference_resolution_hash: str,
    memory_reference_resolution_hash: str,
    lineage_graph_hash: str,
    lineage_node_count: int,
    lineage_edge_count: int,
    principal: Principal,
) -> dict[str, object]:
    """验证重启/恢复后引用、快照内容、暂停状态、文件和副作用计数。"""
    session = SessionStore(settings.chat_db_path)
    tasks = TaskStore(session.db_path)
    original = tasks.get_run(original_run_id)
    probe = tasks.get_run(probe_run_id)
    if original is None or original.status != "completed":
        raise RuntimeError("原始 TaskRun 未恢复为 completed")
    if probe is None or probe.status != "paused":
        raise RuntimeError("恢复探针 TaskRun 未保持 paused")
    plan = tasks.get_active_plan(probe_run_id)
    if (
        plan is None
        or plan.plan_id != plan_id
        or plan.version != plan_version
        or _json_hash(plan.plan) != plan_hash
    ):
        raise RuntimeError("恢复探针 TaskPlan 在恢复后发生漂移")
    snapshot_result = MemoryStore(session).get_snapshot(
        memory_snapshot_id,
        principal=principal,
    )
    if snapshot_result is None:
        raise RuntimeError("MemorySnapshot 在恢复后不可读")
    snapshot, records = snapshot_result
    if (
        snapshot.run_id != probe_run_id
        or snapshot.content_hash != memory_content_hash
        or snapshot.compaction_id != compaction_id
        or memory_id not in {record.memory_id for record in records}
    ):
        raise RuntimeError("MemorySnapshot 在恢复后发生漂移")
    compactions = CompactionStore(session)
    fixed_compaction = compactions.get_view(
        compaction_id,
        project_id=probe.project_id,
        conversation_id=probe.conversation_id,
        principal=principal,
    )
    latest_compaction = compactions.get_latest(
        project_id=probe.project_id,
        conversation_id=probe.conversation_id,
        principal=principal,
    )
    if (
        fixed_compaction is None
        or fixed_compaction.record.summary_hash != compaction_summary_hash
        or latest_compaction is None
        or latest_compaction.record.compaction_id != latest_compaction_id
        or latest_compaction.record.compaction_id == compaction_id
    ):
        raise RuntimeError("ConversationCompaction 在恢复后发生漂移")
    memory = MemoryStore(session).get_record(memory_id, principal=principal)
    if memory is None or memory.status != "active":
        raise RuntimeError("MemoryRecord 在恢复后不可用")
    reference_assumption = find_reference_assumption(plan.plan.get("assumptions", []))
    memory_assumptions = find_memory_reference_assumptions(
        plan.plan.get("assumptions", [])
    )
    if reference_assumption is None or len(memory_assumptions) != 1:
        raise RuntimeError("恢复探针 TaskPlan 缺少唯一 Host 引用证明")
    restored_reference = ReferenceResolver(
        session,
        audit_recorder=lambda _event: None,
    ).restore(
        reference_assumption,
        query="恢复固定对象引用",
        project_id=probe.project_id,
        conversation_id=probe.conversation_id,
        principal=principal,
    )
    restored_memory_reference = MemoryReferenceResolver(
        session,
        MemoryStore(session),
        audit_recorder=lambda _event: None,
    ).restore(
        memory_assumptions,
        query="恢复固定记忆引用",
        project_id=probe.project_id,
        conversation_id=probe.conversation_id,
        memory_snapshot_id=memory_snapshot_id,
        principal=principal,
    )
    if (
        restored_reference.resolution_hash != reference_resolution_hash
        or [target.reference_id for target in restored_reference.targets]
        != [artifact_id]
        or restored_memory_reference.resolution_hash
        != memory_reference_resolution_hash
        or [target.reference_id for target in restored_memory_reference.targets]
        != [artifact_id]
    ):
        raise RuntimeError("Host 引用绑定在恢复后发生漂移")
    artifacts = {
        artifact.id: artifact
        for artifact in session.list_artifacts(original.conversation_id)
    }
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise RuntimeError("Artifact 在恢复后不可读")
    _require_artifact_files(artifact, settings.report_dir)
    report_completions = [
        event
        for event in tasks.list_events(original.run_id)
        if event.event_type == "step.completed"
        and event.payload.get("tool") == "generate_report"
    ]
    if len(report_completions) != 1:
        raise RuntimeError("报告工具副作用在恢复后发生重复或丢失")
    if tasks.list_invocations(probe_run_id):
        raise RuntimeError("恢复探针不得产生工具调用")
    lineage = LineageStore(
        session,
        audit_recorder=lambda _event: None,
    ).build_graph(
        project_id=probe.project_id,
        principal=principal,
    )
    if (
        lineage.integrity_status != "ok"
        or lineage.graph_hash != lineage_graph_hash
        or lineage.total_nodes != lineage_node_count
        or lineage.total_edges != lineage_edge_count
    ):
        raise RuntimeError("项目血缘图在恢复后发生漂移")
    return {
        "status": "verified",
        "original_run_status": original.status,
        "probe_run_status": probe.status,
        "memory_snapshot_id": snapshot.memory_snapshot_id,
        "memory_content_hash": snapshot.content_hash,
        "memory_records": snapshot.record_count,
        "compaction_id": fixed_compaction.record.compaction_id,
        "compaction_summary_hash": fixed_compaction.record.summary_hash,
        "latest_compaction_id": latest_compaction.record.compaction_id,
        "artifact_id": artifact.id,
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "plan_hash": _json_hash(plan.plan),
        "reference_resolution_hash": restored_reference.resolution_hash,
        "memory_reference_resolution_hash": (
            restored_memory_reference.resolution_hash
        ),
        "lineage_graph_hash": lineage.graph_hash,
        "lineage_node_count": lineage.total_nodes,
        "lineage_edge_count": lineage.total_edges,
        "probe_invocation_count": 0,
        "report_completion_count": len(report_completions),
    }


def disturb_probe_state(
    settings: Settings,
    *,
    artifact_id: str,
) -> dict[str, object]:
    """仅供隔离 Compose E2E：移动 SQLite 与报告文件以证明离线恢复有效。"""
    if os.environ.get("CHATBI_RECOVERY_PROBE_ALLOW_DESTRUCTIVE") != "1":
        raise RuntimeError("恢复破坏探针未显式授权")
    database = Path(settings.chat_db_path).resolve()
    report_root = Path(settings.report_dir).resolve()
    if database == Path("/") or len(database.parts) < 4:
        raise RuntimeError("恢复破坏探针数据库路径范围过大")
    session = SessionStore(str(database))
    artifacts = {artifact.id: artifact for artifact in session.list_report_artifacts()}
    artifact = artifacts.get(artifact_id)
    if artifact is None:
        raise RuntimeError("恢复破坏探针 Artifact 不存在")
    report_files = _artifact_files(artifact, report_root)
    moved: list[str] = []
    for path in report_files:
        target = path.with_name(f".{path.name}.recovery-probe-lost")
        os.replace(path, target)
        moved.append(path.name)
    for path in (
        database,
        database.with_name(database.name + "-wal"),
        database.with_name(database.name + "-shm"),
    ):
        if path.exists():
            target = path.with_name(f".{path.name}.recovery-probe-lost")
            os.replace(path, target)
            moved.append(path.name)
    return {"status": "disturbed", "moved": sorted(moved)}


def _select_report_artifact(
    artifacts: dict[str, Artifact],
    evidence: Sequence[object],
    *,
    report_dir: str | Path,
) -> Artifact:
    for item in reversed(evidence):
        artifact_id = getattr(item, "artifact_id", None)
        artifact = artifacts.get(artifact_id) if isinstance(artifact_id, str) else None
        if artifact is not None and artifact.type == "report":
            _require_artifact_files(artifact, report_dir)
            return artifact
    raise RuntimeError("原始 TaskRun 缺少报告 Evidence/Artifact")


def _text_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_artifact_files(artifact: Artifact, report_dir: str | Path) -> None:
    files = _artifact_files(artifact, Path(report_dir).resolve())
    if not files or any(not path.is_file() or path.stat().st_size <= 0 for path in files):
        raise RuntimeError("Artifact 文件缺失或为空")


def _artifact_files(artifact: Artifact, report_root: Path) -> list[Path]:
    payload = artifact.payload or {}
    report_id = payload.get("report_id")
    if (
        artifact.type != "report"
        or not isinstance(report_id, str)
        or len(report_id) != 32
        or any(char not in "0123456789abcdef" for char in report_id)
    ):
        raise RuntimeError("恢复探针只接受合法报告 Artifact")
    files: list[Path] = []
    for suffix, url_key in ((".md", "md_url"), (".pdf", "pdf_url")):
        if url_key not in payload:
            continue
        path = (report_root / f"{report_id}{suffix}").resolve()
        if path.parent != report_root:
            raise RuntimeError("Artifact 文件越过报告目录")
        files.append(path)
    return files


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="v2.5 3A-3E Compose 恢复探针")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed")
    seed.add_argument("--original-run-id", required=True)
    _add_principal(seed)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--original-run-id", required=True)
    verify.add_argument("--probe-run-id", required=True)
    verify.add_argument("--memory-snapshot-id", required=True)
    verify.add_argument("--memory-content-hash", required=True)
    verify.add_argument("--compaction-id", required=True)
    verify.add_argument("--compaction-summary-hash", required=True)
    verify.add_argument("--latest-compaction-id", required=True)
    verify.add_argument("--memory-id", required=True)
    verify.add_argument("--artifact-id", required=True)
    verify.add_argument("--plan-id", required=True)
    verify.add_argument("--plan-version", required=True, type=int)
    verify.add_argument("--plan-hash", required=True)
    verify.add_argument("--reference-resolution-hash", required=True)
    verify.add_argument("--memory-reference-resolution-hash", required=True)
    verify.add_argument("--lineage-graph-hash", required=True)
    verify.add_argument("--lineage-node-count", required=True, type=int)
    verify.add_argument("--lineage-edge-count", required=True, type=int)
    _add_principal(verify)
    disturb = subparsers.add_parser("disturb")
    disturb.add_argument("--artifact-id", required=True)
    return parser


def _add_principal(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--user-id", default="local-user")
    parser.add_argument("--tenant-id", default="local")


def main() -> None:
    args = _parser().parse_args()
    settings = get_settings()
    if args.command == "seed":
        result = seed_probe(
            settings,
            original_run_id=args.original_run_id,
            principal=Principal(args.user_id, args.tenant_id),
        )
    elif args.command == "verify":
        result = verify_probe(
            settings,
            original_run_id=args.original_run_id,
            probe_run_id=args.probe_run_id,
            memory_snapshot_id=args.memory_snapshot_id,
            memory_content_hash=args.memory_content_hash,
            compaction_id=args.compaction_id,
            compaction_summary_hash=args.compaction_summary_hash,
            latest_compaction_id=args.latest_compaction_id,
            memory_id=args.memory_id,
            artifact_id=args.artifact_id,
            plan_id=args.plan_id,
            plan_version=args.plan_version,
            plan_hash=args.plan_hash,
            reference_resolution_hash=args.reference_resolution_hash,
            memory_reference_resolution_hash=args.memory_reference_resolution_hash,
            lineage_graph_hash=args.lineage_graph_hash,
            lineage_node_count=args.lineage_node_count,
            lineage_edge_count=args.lineage_edge_count,
            principal=Principal(args.user_id, args.tenant_id),
        )
    else:
        result = disturb_probe_state(settings, artifact_id=args.artifact_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
