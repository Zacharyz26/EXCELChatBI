"""SQLite、Dataset 与 Artifact 的离线一致备份/恢复。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from packages.session.lineage import inspect_lineage_connection
from packages.session.migrations import CURRENT_SCHEMA_VERSION, v4, v5, v6, v7, v8, v9

BACKUP_FORMAT = "chatbi-workspace-backup-v1"
_COUNTED_TABLES = (
    "projects",
    "project_memberships",
    "datasets",
    "dataset_lineage_anchors",
    "conversations",
    "messages",
    "artifacts",
    "task_runs",
    "task_plans",
    "task_steps",
    "task_events",
    "task_snapshots",
    "tool_invocations",
    "evidence",
    "claims",
    "claim_evidence",
    "checkpoints",
    "memory_records",
    "memory_operations",
    "memory_snapshots",
    "memory_snapshot_items",
    "memory_links",
    "conversation_compactions",
    "conversation_compaction_items",
    "approval_records",
    "approval_operations",
    "domain_definitions",
    "domain_field_mappings",
    "capability_catalog_snapshots",
)


def backup_workspace(
    *,
    db_path: str | Path,
    dataset_dir: str | Path,
    artifact_dir: str | Path,
    backup_root: str | Path,
    service_stopped: bool,
    output: str | Path | None = None,
) -> dict[str, object]:
    """在调用方确认写服务停止后，生成一个带 hash 与行数清单的工作区备份。"""
    _require_service_stopped(service_stopped)
    database = _existing_file(db_path, "SQLite 数据库")
    datasets = _safe_root(dataset_dir, "Dataset 目录", create=True)
    artifacts = _safe_root(artifact_dir, "Artifact 目录", create=True)
    root = _safe_root(backup_root, "工作区备份目录", create=True)
    _require_disjoint(database, datasets, artifacts, root)
    _inspect_database(database)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    destination = (
        _safe_output_path(output, root)
        if output is not None
        else root / f"workspace-{stamp}"
    )
    if destination.exists():
        raise RuntimeError(f"备份目标已存在: {destination}")
    temporary = destination.with_name(f".{destination.name}.tmp-{uuid.uuid4().hex}")
    temporary.mkdir(parents=True)
    try:
        database_copy = temporary / "database" / "chatbi.db"
        database_copy.parent.mkdir(parents=True)
        _sqlite_backup(database, database_copy)
        database_status = _inspect_database(database_copy)
        trees = {
            "datasets": _backup_tree(datasets, temporary / "datasets"),
            "artifacts": _backup_tree(artifacts, temporary / "artifacts"),
        }
        manifest: dict[str, object] = {
            "format": BACKUP_FORMAT,
            "created_at": _utc_now(),
            "database": {
                "path": "database/chatbi.db",
                "sha256": _sha256_file(database_copy),
                **database_status,
            },
            "trees": trees,
            "knowledge_store": {
                "included": False,
                "reason": "由 kb_admin / milvus-backup 独立管理",
            },
        }
        manifest_path = temporary / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                manifest,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(temporary, destination)
    except Exception:
        if temporary.exists():
            shutil.rmtree(temporary)
        raise
    verified = verify_workspace_backup(destination)
    return {
        "status": "backed_up",
        "path": str(destination),
        "format": verified["format"],
        "database": verified["database"],
        "trees": verified["trees"],
    }


def verify_workspace_backup(input_dir: str | Path) -> dict[str, Any]:
    """完整验证 manifest、SQLite schema/checksum/行数和每个文件 hash。"""
    source = _safe_root(input_dir, "工作区备份", create=False)
    if any(item.is_symlink() for item in source.rglob("*")):
        raise RuntimeError("工作区备份不允许符号链接")
    manifest_path = source / "manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("工作区备份缺少 manifest.json")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("工作区备份 manifest 无效") from exc
    if not isinstance(manifest, dict) or manifest.get("format") != BACKUP_FORMAT:
        raise RuntimeError("工作区备份格式不受支持")

    database_item = _mapping(manifest.get("database"), "database")
    database_relative = _safe_relative_file(database_item.get("path"), "database.path")
    database = source / database_relative
    if not database.is_file():
        raise RuntimeError("工作区备份缺少 SQLite 文件")
    _require_hash(database, database_item.get("sha256"))
    inspected = _inspect_database(database)
    for key in (
        "schema_version",
        "integrity",
        "migration_checksums",
        "table_counts",
        "lineage",
    ):
        if database_item.get(key) != inspected[key]:
            raise RuntimeError(f"工作区备份 SQLite {key} 与 manifest 不一致")

    trees_item = _mapping(manifest.get("trees"), "trees")
    expected_files = {"manifest.json", database_relative.as_posix()}
    normalized_trees: dict[str, list[dict[str, object]]] = {}
    for label in ("datasets", "artifacts"):
        raw_entries = trees_item.get(label)
        if not isinstance(raw_entries, list):
            raise RuntimeError(f"工作区备份 {label} 清单无效")
        normalized: list[dict[str, object]] = []
        seen: set[str] = set()
        for raw_entry in raw_entries:
            entry = _mapping(raw_entry, f"trees.{label}")
            relative = _safe_relative_file(entry.get("path"), f"trees.{label}.path")
            key = relative.as_posix()
            if key in seen:
                raise RuntimeError(f"工作区备份 {label} 存在重复路径")
            seen.add(key)
            file_path = source / label / relative
            if not file_path.is_file() or file_path.is_symlink():
                raise RuntimeError(f"工作区备份文件不存在或是符号链接: {label}/{key}")
            expected_size = entry.get("size")
            if isinstance(expected_size, bool) or not isinstance(expected_size, int):
                raise RuntimeError(f"工作区备份 {label} 文件大小无效")
            if file_path.stat().st_size != expected_size:
                raise RuntimeError(f"工作区备份文件大小不匹配: {label}/{key}")
            _require_hash(file_path, entry.get("sha256"))
            normalized.append(
                {
                    "path": key,
                    "size": expected_size,
                    "sha256": str(entry["sha256"]),
                }
            )
            expected_files.add(f"{label}/{key}")
        normalized_trees[label] = normalized

    actual_files = {
        item.relative_to(source).as_posix()
        for item in source.rglob("*")
        if item.is_file()
    }
    if actual_files != expected_files:
        raise RuntimeError("工作区备份包含未登记文件或缺少已登记文件")
    return {
        **manifest,
        "database": {**database_item, **inspected},
        "trees": normalized_trees,
    }


def restore_workspace(
    *,
    input_dir: str | Path,
    db_path: str | Path,
    dataset_dir: str | Path,
    artifact_dir: str | Path,
    backup_root: str | Path,
    service_stopped: bool,
    confirmed: bool,
    replace_files: bool,
) -> dict[str, object]:
    """验证完整备份后恢复；覆盖前把当前状态复制到可恢复 quarantine。"""
    _require_service_stopped(service_stopped)
    if not confirmed:
        raise RuntimeError("恢复会覆盖当前工作区，必须显式传入 --yes")
    if not replace_files:
        raise RuntimeError("一致恢复必须显式传入 --replace-files")
    source = _safe_root(input_dir, "工作区备份", create=False)
    manifest = verify_workspace_backup(source)
    database = _safe_file_target(db_path, "SQLite 数据库")
    datasets = _safe_root(dataset_dir, "Dataset 目录", create=True)
    artifacts = _safe_root(artifact_dir, "Artifact 目录", create=True)
    root = _safe_root(backup_root, "工作区备份目录", create=True)
    _require_disjoint(database, datasets, artifacts, root)
    if source == root or root not in source.parents:
        raise RuntimeError("恢复源必须位于 WORKSPACE_BACKUP_DIR 内")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    quarantine = root / f"pre-restore-{stamp}-{uuid.uuid4().hex[:8]}"
    quarantine.mkdir(parents=True)
    if database.exists():
        quarantine_database = quarantine / "database" / "chatbi.db"
        quarantine_database.parent.mkdir(parents=True)
        _sqlite_backup(database, quarantine_database)
    _copy_tree(datasets, quarantine / "datasets")
    _copy_tree(artifacts, quarantine / "artifacts")

    database_item = _mapping(manifest["database"], "database")
    database_source = source / _safe_relative_file(
        database_item["path"],
        "database.path",
    )
    temporary_database = database.with_name(
        f".{database.name}.restore-{uuid.uuid4().hex}.tmp"
    )
    database.parent.mkdir(parents=True, exist_ok=True)
    try:
        shutil.copy2(database_source, temporary_database)
        _inspect_database(temporary_database)
        os.replace(temporary_database, database)
        _remove_sqlite_sidecars(database)
        _replace_tree(
            source / "datasets",
            datasets,
            _tree_paths(manifest, "datasets"),
        )
        _replace_tree(
            source / "artifacts",
            artifacts,
            _tree_paths(manifest, "artifacts"),
        )
    except Exception as exc:
        temporary_database.unlink(missing_ok=True)
        raise RuntimeError(
            f"工作区恢复未完成；覆盖前副本保留在 {quarantine}"
        ) from exc
    restored = _inspect_database(database)
    return {
        "status": "restored",
        "format": BACKUP_FORMAT,
        "schema_version": restored["schema_version"],
        "table_counts": restored["table_counts"],
        "datasets": len(_tree_paths(manifest, "datasets")),
        "artifacts": len(_tree_paths(manifest, "artifacts")),
        "pre_restore_backup": str(quarantine),
    }


def _inspect_database(path: Path) -> dict[str, object]:
    try:
        connection = sqlite3.connect(
            f"{path.as_uri()}?mode=ro&immutable=1",
            uri=True,
            timeout=5.0,
        )
    except sqlite3.Error as exc:
        raise RuntimeError("无法以只读方式打开工作区 SQLite") from exc
    try:
        integrity = connection.execute("PRAGMA quick_check").fetchone()
        if integrity is None or str(integrity[0]) != "ok":
            raise RuntimeError("工作区 SQLite quick_check 未通过")
        version_row = connection.execute("PRAGMA user_version").fetchone()
        version = int(version_row[0]) if version_row is not None else 0
        if version != CURRENT_SCHEMA_VERSION:
            raise RuntimeError(
                f"工作区 SQLite schema 版本不兼容: {version}"
            )
        migrations = connection.execute(
            """
            SELECT version, name, checksum FROM schema_migrations
            WHERE version IN (?, ?, ?, ?, ?, ?)
            """,
            (
                v4.VERSION,
                v5.VERSION,
                v6.VERSION,
                v7.VERSION,
                v8.VERSION,
                v9.VERSION,
            ),
        ).fetchall()
        actual_migrations = {
            str(int(row[0])): {"name": str(row[1]), "checksum": str(row[2])}
            for row in migrations
        }
        expected_migrations = {
            str(v4.VERSION): {"name": v4.NAME, "checksum": v4.CHECKSUM},
            str(v5.VERSION): {"name": v5.NAME, "checksum": v5.CHECKSUM},
            str(v6.VERSION): {"name": v6.NAME, "checksum": v6.CHECKSUM},
            str(v7.VERSION): {"name": v7.NAME, "checksum": v7.CHECKSUM},
            str(v8.VERSION): {"name": v8.NAME, "checksum": v8.CHECKSUM},
            str(v9.VERSION): {"name": v9.NAME, "checksum": v9.CHECKSUM},
        }
        if actual_migrations != expected_migrations:
            raise RuntimeError("工作区 v2.5 migration checksum 不匹配")
        counts = {
            table: int(
                connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            )
            for table in _COUNTED_TABLES
        }
        lineage = inspect_lineage_connection(connection)
        if lineage["integrity"] != "ok":
            raise RuntimeError("工作区血缘完整性检查未通过")
    except sqlite3.Error as exc:
        raise RuntimeError("工作区 SQLite 结构不可读") from exc
    finally:
        connection.close()
    return {
        "schema_version": version,
        "integrity": "ok",
        "migration_checksums": expected_migrations,
        "table_counts": counts,
        "lineage": lineage,
    }


def _backup_tree(source: Path, destination: Path) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    for file_path, relative in _tree_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)
        entries.append(
            {
                "path": relative.as_posix(),
                "size": target.stat().st_size,
                "sha256": _sha256_file(target),
            }
        )
    return entries


def _copy_tree(source: Path, destination: Path) -> None:
    for file_path, relative in _tree_files(source):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, target)


def _replace_tree(source: Path, destination: Path, expected: set[str]) -> None:
    source_files = {relative.as_posix() for _, relative in _tree_files(source)}
    if source_files != expected:
        raise RuntimeError("恢复源文件集合与已验证 manifest 不一致")
    for file_path, _ in _tree_files(destination):
        file_path.unlink()
    for directory in sorted(
        (item for item in destination.rglob("*") if item.is_dir()),
        key=lambda item: len(item.parts),
        reverse=True,
    ):
        directory.rmdir()
    _copy_tree(source, destination)


def _tree_files(root: Path) -> list[tuple[Path, Path]]:
    files: list[tuple[Path, Path]] = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            raise RuntimeError(f"工作区目录不允许符号链接: {root.name}")
        if item.is_file():
            files.append((item, item.relative_to(root)))
    return files


def _tree_paths(manifest: dict[str, Any], label: str) -> set[str]:
    trees = _mapping(manifest["trees"], "trees")
    entries = trees.get(label)
    if not isinstance(entries, list):
        raise RuntimeError(f"工作区备份 {label} 清单无效")
    return {
        _safe_relative_file(_mapping(item, label)["path"], label).as_posix()
        for item in entries
    }


def _sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(
        f"{source.as_uri()}?mode=ro",
        uri=True,
        timeout=5.0,
    ) as source_connection:
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def _remove_sqlite_sidecars(database: Path) -> None:
    for suffix in ("-wal", "-shm"):
        database.with_name(database.name + suffix).unlink(missing_ok=True)


def _require_hash(path: Path, expected: object) -> None:
    if (
        not isinstance(expected, str)
        or len(expected) != 64
        or any(char not in "0123456789abcdef" for char in expected)
        or _sha256_file(path) != expected
    ):
        raise RuntimeError(f"工作区备份文件 hash 不匹配: {path.name}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _existing_file(value: str | Path, label: str) -> Path:
    path = _safe_file_target(value, label)
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"{label}不存在或不是普通文件")
    return path


def _safe_file_target(value: str | Path, label: str) -> Path:
    path = Path(value).expanduser().resolve()
    if path == Path("/") or len(path.parts) < 3:
        raise RuntimeError(f"{label}路径范围过大")
    return path


def _safe_root(value: str | Path, label: str, *, create: bool) -> Path:
    path = Path(value).expanduser().resolve()
    if path == Path("/") or len(path.parts) < 3:
        raise RuntimeError(f"{label}路径范围过大")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if not path.is_dir() or path.is_symlink():
        raise RuntimeError(f"{label}不存在、不是目录或是符号链接")
    return path


def _safe_output_path(value: str | Path, root: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if root not in resolved.parents:
        raise RuntimeError("备份输出必须位于 WORKSPACE_BACKUP_DIR 内")
    return resolved


def _safe_relative_file(value: object, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} 无效")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path == Path("."):
        raise RuntimeError(f"{label} 不是安全相对路径")
    return path


def _require_disjoint(*paths: Path) -> None:
    resolved = [path.resolve() for path in paths]
    for index, left in enumerate(resolved):
        for right in resolved[index + 1 :]:
            if left == right or left in right.parents or right in left.parents:
                raise RuntimeError("数据库、Dataset、Artifact 与备份目录必须彼此独立")


def _mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeError(f"工作区备份 {label} 无效")
    return value


def _require_service_stopped(value: bool) -> None:
    if not value:
        raise RuntimeError("一致备份/恢复前必须停止 API 与所有写服务")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
