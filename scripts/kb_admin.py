#!/usr/bin/env python3
"""知识库存储运维：状态、回滚、代际清理，以及 Local/Lite 离线备份恢复。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.common.config import Settings, get_settings  # noqa: E402
from packages.rag.store import KnowledgeStore, LocalKnowledgeStore  # noqa: E402


def _store(settings: Settings) -> KnowledgeStore:
    if settings.rag_store == "milvus":
        from packages.rag.milvus_store import MilvusKnowledgeStore

        return MilvusKnowledgeStore(
            settings.milvus_uri,
            collection=settings.milvus_collection,
            token=settings.milvus_token,
        )
    return LocalKnowledgeStore(settings.kb_index_dir)


def _offline_files(settings: Settings) -> tuple[str, list[tuple[str, Path]]]:
    if settings.rag_store == "local":
        return "local", [("index.json", Path(settings.kb_index_dir) / "index.json")]
    parsed = urlsplit(settings.milvus_uri)
    if parsed.scheme and parsed.scheme != "file":
        raise RuntimeError(
            "Standalone 备份/恢复请使用官方 milvus-backup；本命令只处理 Local/Milvus Lite"
        )
    database = Path(parsed.path if parsed.scheme == "file" else settings.milvus_uri)
    pointer = database.with_name(
        f"{database.name}.{settings.milvus_collection}.active.json"
    )
    return "milvus_lite", [(database.name, database), (pointer.name, pointer)]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _require_offline_ack(args: argparse.Namespace) -> None:
    if not args.service_stopped:
        raise RuntimeError(
            "备份/恢复前必须停止 API/Milvus Lite 进程，并显式传入 --service-stopped"
        )


def _backup(settings: Settings, args: argparse.Namespace) -> dict[str, object]:
    _require_offline_ack(args)
    backend, files = _offline_files(settings)
    existing = [(name, path) for name, path in files if path.exists()]
    if not existing:
        raise FileNotFoundError("当前配置下没有可备份的知识库索引")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = Path(args.output or settings.kb_backup_dir) / stamp
    output.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, object]] = []
    for name, source in existing:
        target = output / name
        shutil.copy2(source, target)
        records.append(
            {"name": name, "size": target.stat().st_size, "sha256": _sha256(target)}
        )
    manifest: dict[str, object] = {
        "format": 1,
        "backend": backend,
        "collection": settings.milvus_collection,
        "created_at": datetime.now(UTC).isoformat(),
        "files": records,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"status": "created", "path": str(output), **manifest}


def _restore(settings: Settings, args: argparse.Namespace) -> dict[str, object]:
    _require_offline_ack(args)
    if not args.yes:
        raise RuntimeError("恢复会覆盖当前索引，确认后请传入 --yes")
    source_dir = Path(args.input)
    manifest_path = source_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backend, targets = _offline_files(settings)
    if manifest.get("format") != 1 or manifest.get("backend") != backend:
        raise RuntimeError("备份格式或后端类型与当前配置不匹配")
    target_by_name = {name: path for name, path in targets}
    validated: list[tuple[str, Path, Path]] = []
    for record in manifest.get("files", []):
        if not isinstance(record, dict):
            raise RuntimeError("备份 manifest.files 格式错误")
        name = str(record.get("name", ""))
        source = source_dir / name
        target = target_by_name.get(name)
        if target is None or not source.is_file():
            raise RuntimeError(f"备份文件不受当前配置管理或不存在: {name}")
        if _sha256(source) != record.get("sha256"):
            raise RuntimeError(f"备份文件校验失败: {name}")
        validated.append((name, source, target))

    restored_names = {name for name, _, _ in validated}
    required_name = targets[0][0]
    if required_name not in restored_names:
        raise RuntimeError(f"备份缺少主索引文件: {required_name}")

    restored: list[str] = []
    for name, source, target in validated:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(f"{target.suffix}.restore.tmp")
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
        restored.append(name)

    # Lite 的活动集合指针是可选文件；旧环境残留的指针不能穿透到新恢复的数据。
    if backend == "milvus_lite":
        for name, target in targets[1:]:
            if name not in restored_names:
                target.unlink(missing_ok=True)
    return {"status": "restored", "backend": backend, "files": restored}


def _print(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatBI 知识库运维")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="查看存储、活动集合与上一代状态")

    rollback = subparsers.add_parser("rollback", help="切回上一代 Milvus 索引")
    rollback.add_argument("--yes", action="store_true", help="确认执行回滚")

    cleanup = subparsers.add_parser("cleanup", help="清理过旧 Milvus 索引代际")
    cleanup.add_argument("--retain", type=int, default=2)
    cleanup.add_argument("--yes", action="store_true", help="确认执行清理")

    backup = subparsers.add_parser("backup", help="离线备份 Local/Milvus Lite")
    backup.add_argument("--output", help="备份根目录；默认 KB_BACKUP_DIR")
    backup.add_argument("--service-stopped", action="store_true")

    restore = subparsers.add_parser("restore", help="离线恢复 Local/Milvus Lite")
    restore.add_argument("--input", required=True, help="含 manifest.json 的备份目录")
    restore.add_argument("--service-stopped", action="store_true")
    restore.add_argument("--yes", action="store_true", help="确认覆盖当前索引")
    args = parser.parse_args()
    settings = get_settings()

    try:
        if args.command == "backup":
            _print(_backup(settings, args))
            return 0
        if args.command == "restore":
            _print(_restore(settings, args))
            return 0

        store = _store(settings)
        try:
            if args.command == "status":
                _print(asdict(store.status()))
            elif args.command == "rollback":
                if not args.yes:
                    raise RuntimeError("回滚会切换活动索引，确认后请传入 --yes")
                _print(asdict(store.rollback()))
            elif args.command == "cleanup":
                if not args.yes:
                    raise RuntimeError("清理会删除历史集合，确认后请传入 --yes")
                removed = store.cleanup_generations(args.retain)
                _print({"status": "cleaned", "removed": removed, "retain": args.retain})
        finally:
            store.close()
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"错误：知识库存储操作失败（{type(exc).__name__}）", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
