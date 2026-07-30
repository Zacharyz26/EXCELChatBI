"""Compose 3A 门禁：创建并验证 TaskRun/MemorySnapshot/Artifact 联合恢复状态。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import uuid
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from packages.common.config import Settings, get_settings
from packages.governance.permissions import Principal
from packages.session.memory_models import MemoryDraft
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
    source_hash = _artifact_source_hash(artifact)
    memory_store = MemoryStore(session)
    memory = memory_store.remember(
        project_id=original.project_id,
        principal=principal,
        draft=MemoryDraft(
            scope="project",
            kind="confirmed_decision",
            semantic_key=f"compose-recovery.{original.run_id}",
            content_summary="Compose 恢复探针应继续使用已验证报告工件",
            source_type="artifact",
            source_ref=artifact.id,
            source_hash=source_hash,
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
    snapshot, records = memory_store.create_snapshot(
        project_id=original.project_id,
        principal=principal,
        conversation_id=original.conversation_id,
        run_id=probe_run.run_id,
    )
    if memory.memory_id not in {record.memory_id for record in records}:
        raise RuntimeError("恢复探针记忆未进入 TaskRun 快照")
    running, _ = tasks.transition(
        probe_run.run_id,
        expected_version=probe_run.state_version,
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
    return {
        "status": "seeded",
        "original_run_id": original.run_id,
        "probe_run_id": paused.run_id,
        "memory_snapshot_id": snapshot.memory_snapshot_id,
        "memory_content_hash": snapshot.content_hash,
        "memory_id": memory.memory_id,
        "artifact_id": artifact.id,
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
    memory_id: str,
    artifact_id: str,
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
        or memory_id not in {record.memory_id for record in records}
    ):
        raise RuntimeError("MemorySnapshot 在恢复后发生漂移")
    memory = MemoryStore(session).get_record(memory_id, principal=principal)
    if memory is None or memory.status != "active":
        raise RuntimeError("MemoryRecord 在恢复后不可用")
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
    return {
        "status": "verified",
        "original_run_status": original.status,
        "probe_run_status": probe.status,
        "memory_snapshot_id": snapshot.memory_snapshot_id,
        "memory_content_hash": snapshot.content_hash,
        "memory_records": snapshot.record_count,
        "artifact_id": artifact.id,
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


def _artifact_source_hash(artifact: Artifact) -> str:
    encoded = json.dumps(
        asdict(artifact),
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
    parser = argparse.ArgumentParser(description="v2.5 3A Compose 恢复探针")
    subparsers = parser.add_subparsers(dest="command", required=True)
    seed = subparsers.add_parser("seed")
    seed.add_argument("--original-run-id", required=True)
    _add_principal(seed)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--original-run-id", required=True)
    verify.add_argument("--probe-run-id", required=True)
    verify.add_argument("--memory-snapshot-id", required=True)
    verify.add_argument("--memory-content-hash", required=True)
    verify.add_argument("--memory-id", required=True)
    verify.add_argument("--artifact-id", required=True)
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
            memory_id=args.memory_id,
            artifact_id=args.artifact_id,
            principal=Principal(args.user_id, args.tenant_id),
        )
    else:
        result = disturb_probe_state(settings, artifact_id=args.artifact_id)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
