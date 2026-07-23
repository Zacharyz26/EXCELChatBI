"""Run the isolated v2.4 SQLite v1 -> v2 -> v1 migration rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from packages.session.migrations import downgrade_v2_to_v1, migrate_database, v2
from packages.session.store import _SCHEMA_V1

ROOT = Path(__file__).resolve().parent.parent
LIVE_DB = (ROOT / ".data" / "chatbi.db").resolve()
LEGACY_TABLES = (
    "projects",
    "datasets",
    "conversations",
    "messages",
    "artifacts",
)


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _version(path: Path) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _read_only_version(path: Path) -> int:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        row = connection.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row else 0


def _legacy_bytes(path: Path) -> bytes:
    """Canonical byte representation of every legacy column and row."""
    snapshot: list[dict[str, Any]] = []
    with sqlite3.connect(path) as connection:
        for table in LEGACY_TABLES:
            columns = [
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{table}")')
            ]
            rows = connection.execute(
                f'SELECT * FROM "{table}" ORDER BY rowid'
            ).fetchall()
            snapshot.append(
                {
                    "table": table,
                    "columns": columns,
                    "rows": [list(row) for row in rows],
                }
            )
    return json.dumps(
        snapshot,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _legacy_hash(path: Path) -> str:
    return hashlib.sha256(_legacy_bytes(path)).hexdigest()


def _table_names(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def _integrity_ok(path: Path) -> bool:
    with sqlite3.connect(path) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return row is not None and row[0] == "ok"


def _migrate(path: Path) -> None:
    with sqlite3.connect(path, timeout=5) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        migrate_database(connection, path, create_v1=_SCHEMA_V1)


def _backup_database(source: Path, destination: Path) -> None:
    source_uri = f"{source.resolve().as_uri()}?mode=ro"
    with (
        sqlite3.connect(source_uri, uri=True) as source_connection,
        sqlite3.connect(destination) as destination_connection,
    ):
        source_connection.backup(destination_connection)


def _create_synthetic_v1(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA_V1)
        connection.execute(
            "INSERT INTO projects(id, name, created_at) VALUES (?, ?, ?)",
            ("legacy-project", "Legacy project", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            """
            INSERT INTO datasets(
                ref, project_id, filename, profile_json, parent_ref,
                transform_json, created_at
            ) VALUES (?, ?, ?, ?, NULL, NULL, ?)
            """,
            (
                "legacy-dataset",
                "legacy-project",
                "legacy.xlsx",
                '{"rows":3,"columns":["region","amount"]}',
                "2026-01-01T00:00:01Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO conversations(id, project_id, title, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                "legacy-conversation",
                "legacy-project",
                "Legacy conversation",
                "2026-01-01T00:00:02Z",
                "2026-01-01T00:00:02Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO messages(
                id, conversation_id, role, content, tool_calls_json, created_at
            ) VALUES (?, ?, 'user', ?, NULL, ?)
            """,
            (
                "legacy-message",
                "legacy-conversation",
                "Generate a report",
                "2026-01-01T00:00:03Z",
            ),
        )
        connection.execute(
            """
            INSERT INTO artifacts(
                id, conversation_id, message_id, type, payload_json, file_ref,
                source_tool, params_json, dataset_ref, created_at
            ) VALUES (?, ?, ?, 'table', ?, NULL, 'aggregate_preview', ?, ?, ?)
            """,
            (
                "legacy-artifact",
                "legacy-conversation",
                "legacy-message",
                '{"rows":[{"group":"east","value":25}]}',
                '{"agg":"sum"}',
                "legacy-dataset",
                "2026-01-01T00:00:04Z",
            ),
        )
        connection.commit()


def _ensure_rollback_fixture(path: Path) -> tuple[str, str, str]:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT OR IGNORE INTO projects(id, name, created_at)
            VALUES ('rehearsal-project', 'Rehearsal project', '2026-01-02T00:00:00Z')
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO conversations(
                id, project_id, title, created_at, updated_at
            ) VALUES (
                'rehearsal-conversation', 'rehearsal-project', 'Rehearsal',
                '2026-01-02T00:00:01Z', '2026-01-02T00:00:01Z'
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO messages(
                id, conversation_id, role, content, tool_calls_json, created_at
            ) VALUES (
                'rehearsal-message', 'rehearsal-conversation', 'user',
                'Rehearsal', NULL, '2026-01-02T00:00:02Z'
            )
            """
        )
        connection.commit()
    return "rehearsal-project", "rehearsal-conversation", "rehearsal-message"


def _expect_runtime(action: Any, contains: str) -> bool:
    try:
        action()
    except RuntimeError as exc:
        return contains in str(exc)
    return False


def _validate_workspace(workspace: Path, source_v1: Path | None) -> Path:
    resolved = workspace.resolve()
    if LIVE_DB == resolved or LIVE_DB.is_relative_to(resolved):
        raise RuntimeError("演练目录不得是 live DB 或其祖先目录")
    if source_v1 is not None and source_v1.resolve() == LIVE_DB:
        raise RuntimeError("演练禁止读取或复制 .data/chatbi.db")
    resolved.mkdir(parents=True, exist_ok=True)
    if any(resolved.iterdir()):
        raise RuntimeError("迁移演练目录必须为空，避免覆盖既有证据")
    return resolved


def run_rehearsal(
    workspace: Path,
    *,
    source_v1: Path | None = None,
) -> dict[str, Any]:
    workspace = _validate_workspace(workspace, source_v1)
    source = workspace / "source-v1.sqlite3"
    source_kind = "provided-copy" if source_v1 is not None else "synthetic"
    external_hash_before: str | None = None
    if source_v1 is None:
        _create_synthetic_v1(source)
    else:
        if not source_v1.is_file():
            raise FileNotFoundError(source_v1)
        external_hash_before = _sha256_file(source_v1)
        if _read_only_version(source_v1) != 1:
            raise RuntimeError("提供的 source-v1 必须是 schema v1")
        _backup_database(source_v1, source)
    if _version(source) != 1 or not _integrity_ok(source):
        raise RuntimeError("source-v1 副本无效")

    source_file_hash = _sha256_file(source)
    source_legacy_hash = _legacy_hash(source)

    empty = workspace / "empty.sqlite3"
    _migrate(empty)

    migrated = workspace / "migrated-v2.sqlite3"
    _backup_database(source, migrated)
    _migrate(migrated)
    with sqlite3.connect(migrated) as connection:
        migration_row = connection.execute(
            """
            SELECT backup_path, source_sha256
            FROM schema_migrations WHERE version = 2
            """
        ).fetchone()
    if migration_row is None:
        raise RuntimeError("v2 migration record missing")
    backup_path = Path(str(migration_row[0]))
    recorded_backup_hash = str(migration_row[1])
    initial_backup_count = len(
        list(workspace.glob("migrated-v2.sqlite3.v1-backup.*.sqlite3"))
    )
    _migrate(migrated)
    repeated_backup_count = len(
        list(workspace.glob("migrated-v2.sqlite3.v1-backup.*.sqlite3"))
    )

    interrupted = workspace / "interrupted.sqlite3"
    _backup_database(source, interrupted)
    injected_ddl = v2.DDL + "\nSELECT chatbi_injected_migration_failure();"

    def interrupt_migration() -> None:
        with patch.object(v2, "DDL", injected_ddl):
            _migrate(interrupted)

    interruption_rejected = False
    try:
        interrupt_migration()
    except sqlite3.Error:
        interruption_rejected = True

    unknown = workspace / "unknown-version.sqlite3"
    _backup_database(source, unknown)
    with sqlite3.connect(unknown) as connection:
        connection.execute("PRAGMA user_version = 99")
    unknown_rejected = _expect_runtime(lambda: _migrate(unknown), "不支持")

    tampered = workspace / "tampered-checksum.sqlite3"
    _backup_database(migrated, tampered)
    with sqlite3.connect(tampered) as connection:
        connection.execute(
            "UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 2"
        )
    checksum_rejected = _expect_runtime(
        lambda: _migrate(tampered), "checksum 不匹配"
    )

    rollback = workspace / "rollback.sqlite3"
    _backup_database(migrated, rollback)
    project_id, conversation_id, message_id = _ensure_rollback_fixture(rollback)
    rollback_legacy_hash = _legacy_hash(rollback)
    with sqlite3.connect(rollback) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            """
            INSERT INTO task_runs(
                run_id, project_id, conversation_id, user_message_id,
                parent_run_id, goal, status, state_version, plan_version,
                budget_json, usage_json, terminal_reason, created_at,
                updated_at, finished_at
            ) VALUES (
                'rehearsal-run', ?, ?, ?, NULL, 'Rehearsal migration',
                'running', 1, 0, '{}', '{}', NULL,
                '2026-01-02T00:00:03Z', '2026-01-02T00:00:03Z', NULL
            )
            """,
            (project_id, conversation_id, message_id),
        )
        connection.commit()
    active_run_rejected = _expect_runtime(
        lambda: downgrade_v2_to_v1(rollback), "未终止"
    )
    with sqlite3.connect(rollback) as connection:
        connection.execute(
            """
            UPDATE task_runs
            SET status = 'completed',
                finished_at = '2026-01-02T00:00:04Z'
            WHERE run_id = 'rehearsal-run'
            """
        )
        connection.commit()
    export_path = downgrade_v2_to_v1(rollback)
    with sqlite3.connect(export_path) as connection:
        exported_status = connection.execute(
            "SELECT status FROM task_runs WHERE run_id = 'rehearsal-run'"
        ).fetchone()

    restored = workspace / "restored-v1.sqlite3"
    shutil.copy2(backup_path, restored)

    added_after_interrupt = _table_names(interrupted) & set(v2.ADDED_TABLES)
    checks = {
        "empty_to_v2": _version(empty) == 2 and _integrity_ok(empty),
        "source_copy_is_v1": _version(source) == 1,
        "source_copy_untouched": _sha256_file(source) == source_file_hash,
        "provided_source_untouched": (
            source_v1 is None or _sha256_file(source_v1) == external_hash_before
        ),
        "v1_to_v2": _version(migrated) == 2 and _integrity_ok(migrated),
        "legacy_bytes_unchanged_after_upgrade": (
            _legacy_hash(migrated) == source_legacy_hash
        ),
        "backup_exists_and_hash_matches": (
            backup_path.is_file()
            and _sha256_file(backup_path) == recorded_backup_hash
        ),
        "repeat_is_idempotent": (
            initial_backup_count == repeated_backup_count == 1
            and _legacy_hash(migrated) == source_legacy_hash
        ),
        "interruption_rejected": interruption_rejected,
        "interruption_rolled_back": (
            _version(interrupted) == 1
            and not added_after_interrupt
            and _legacy_hash(interrupted) == source_legacy_hash
        ),
        "unknown_version_rejected": unknown_rejected,
        "checksum_tamper_rejected": checksum_rejected,
        "active_run_blocks_rollback": active_run_rejected,
        "v2_to_v1": (
            _version(rollback) == 1
            and not (_table_names(rollback) & set(v2.ADDED_TABLES))
            and _integrity_ok(rollback)
        ),
        "legacy_bytes_unchanged_after_rollback": (
            _legacy_hash(rollback) == rollback_legacy_hash
        ),
        "control_data_exported": (
            export_path.is_file()
            and exported_status is not None
            and exported_status[0] == "completed"
        ),
        "backup_restore_exact": (
            _version(restored) == 1
            and _sha256_file(restored) == recorded_backup_hash
            and _legacy_hash(restored) == source_legacy_hash
        ),
    }
    if not all(checks.values()):
        failed = [name for name, passed in checks.items() if not passed]
        raise RuntimeError(f"迁移演练失败: {', '.join(failed)}")

    report = {
        "schema": "chatbi-sqlite-migration-rehearsal-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "workspace": str(workspace),
        "live_db_touched": False,
        "source_kind": source_kind,
        "source_v1_file_sha256": source_file_hash,
        "legacy_row_bytes_sha256": source_legacy_hash,
        "migration_checksum": v2.CHECKSUM,
        "backup_sha256": recorded_backup_hash,
        "versions": {
            "empty_after": _version(empty),
            "source": _version(source),
            "migrated_after": _version(migrated),
            "rollback_after": _version(rollback),
            "restored": _version(restored),
        },
        "checks": checks,
        "raw_rows_in_report": False,
    }
    report_path = workspace / "migration-rehearsal.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return report


def _default_workspace() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return Path(".data/evaluations/v2.4") / f"migration-rehearsal-{stamp}"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=_default_workspace())
    parser.add_argument("--source-v1", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    report = run_rehearsal(
        args.workspace,
        source_v1=args.source_v1,
    )
    print(
        json.dumps(
            {
                "report": str(Path(report["workspace"]) / "migration-rehearsal.json"),
                "checks_passed": sum(report["checks"].values()),
                "live_db_touched": report["live_db_touched"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
