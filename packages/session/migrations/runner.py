"""Versioned SQLite migration and guarded rollback helpers."""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from packages.session.migrations import v2

CURRENT_SCHEMA_VERSION = v2.VERSION


def migrate_database(
    connection: sqlite3.Connection,
    db_path: Path,
    *,
    create_v1: str,
) -> None:
    """Bring a database to the current schema and validate applied checksums."""
    row = connection.execute("PRAGMA user_version").fetchone()
    version = int(row[0]) if row is not None else 0
    if version == 0:
        existing = connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            LIMIT 1
            """
        ).fetchone()
        if existing is not None:
            raise RuntimeError("检测到未标版本的非空数据库，拒绝自动初始化")
        connection.executescript(create_v1)
        _apply_v2(connection, source_version=1, backup=None)
        return
    if version == 1:
        backup = _backup_v1(connection, db_path)
        _apply_v2(connection, source_version=1, backup=backup)
        return
    if version == CURRENT_SCHEMA_VERSION:
        _validate_v2_checksum(connection)
        return
    raise RuntimeError(
        f"不支持的 ChatBI 数据库版本 {version}，当前代码仅支持 0、1、"
        f"{CURRENT_SCHEMA_VERSION}"
    )


def downgrade_v2_to_v1(db_path: str | Path) -> Path:
    """Export v2 control data then remove additive tables when no run is active.

    The JSON export is intentionally a SQLite backup: it preserves all task rows
    without inventing a second serialization format and can be inspected/restored
    with standard SQLite tooling.
    """
    path = Path(db_path)
    export_path = _timestamped_path(path, "v2-task-export")
    with _connect(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version != 2:
            raise RuntimeError(f"只能从 schema v2 回滚，当前版本为 {version}")
        active = connection.execute(
            """
            SELECT COUNT(*) FROM task_runs
            WHERE status IN ('planning', 'waiting_user', 'running', 'verifying', 'paused')
            """
        ).fetchone()
        if active is not None and int(active[0]) > 0:
            raise RuntimeError("存在未终止的 TaskRun，禁止回滚 schema")
        with sqlite3.connect(export_path) as export_connection:
            connection.backup(export_connection)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for table in v2.ADDED_TABLES:
                connection.execute(f'DROP TABLE IF EXISTS "{table}"')
            connection.execute("PRAGMA user_version = 1")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
    return export_path


def _apply_v2(
    connection: sqlite3.Connection,
    *,
    source_version: int,
    backup: tuple[Path, str] | None,
) -> None:
    backup_path = str(backup[0]) if backup is not None else None
    source_sha256 = backup[1] if backup is not None else None
    try:
        connection.executescript(f"BEGIN IMMEDIATE;\n{v2.DDL}\n")
        connection.execute(
            """
            INSERT INTO schema_migrations(
                version, name, checksum, source_version, backup_path,
                source_sha256, applied_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                v2.VERSION,
                v2.NAME,
                v2.CHECKSUM,
                source_version,
                backup_path,
                source_sha256,
                _utc_now(),
            ),
        )
        connection.execute(f"PRAGMA user_version = {v2.VERSION}")
        connection.commit()
    except Exception:
        connection.rollback()
        raise


def _validate_v2_checksum(connection: sqlite3.Connection) -> None:
    try:
        row = connection.execute(
            "SELECT name, checksum FROM schema_migrations WHERE version = ?",
            (v2.VERSION,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError("schema v2 缺少迁移登记，拒绝启动") from exc
    if row is None:
        raise RuntimeError("schema v2 缺少迁移登记，拒绝启动")
    if str(row[0]) != v2.NAME or str(row[1]) != v2.CHECKSUM:
        raise RuntimeError("schema v2 迁移 checksum 不匹配，拒绝启动")


def _backup_v1(connection: sqlite3.Connection, db_path: Path) -> tuple[Path, str]:
    # Flush committed WAL pages so the recorded source-file hash is meaningful.
    connection.execute("PRAGMA wal_checkpoint(FULL)")
    backup_path = _timestamped_path(db_path, "v1-backup")
    with sqlite3.connect(backup_path) as backup_connection:
        connection.backup(backup_connection)
    digest = hashlib.sha256(backup_path.read_bytes()).hexdigest()
    return backup_path, digest


def _timestamped_path(db_path: Path, label: str) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return db_path.with_name(f"{db_path.name}.{label}.{stamp}.sqlite3")


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA busy_timeout=5000")
    return connection


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
