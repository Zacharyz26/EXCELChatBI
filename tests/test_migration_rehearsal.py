"""Operational v1 -> v2 -> v1 migration rehearsal tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from packages.session.store import _SCHEMA_V1
from scripts.migration_rehearsal import LIVE_DB, run_rehearsal


def test_migration_rehearsal_matrix_is_all_green(tmp_path: Path) -> None:
    workspace = tmp_path / "rehearsal"
    report = run_rehearsal(workspace)

    assert all(report["checks"].values())
    assert report["live_db_touched"] is False
    assert report["raw_rows_in_report"] is False
    saved = json.loads(
        (workspace / "migration-rehearsal.json").read_text(encoding="utf-8")
    )
    assert saved["checks"] == report["checks"]
    assert saved["legacy_row_bytes_sha256"]


def test_rehearsal_uses_read_only_copy_of_provided_v1(tmp_path: Path) -> None:
    source = tmp_path / "provided-v1.sqlite3"
    with sqlite3.connect(source) as connection:
        connection.executescript(_SCHEMA_V1)
        connection.execute(
            """
            INSERT INTO projects(id, name, created_at)
            VALUES ('provided-project', 'Provided', '2026-01-01T00:00:00Z')
            """
        )
        connection.commit()
    before = source.read_bytes()

    report = run_rehearsal(tmp_path / "provided-rehearsal", source_v1=source)

    assert report["source_kind"] == "provided-copy"
    assert report["checks"]["provided_source_untouched"] is True
    assert source.read_bytes() == before


def test_rehearsal_refuses_live_database_and_nonempty_workspace(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="chatbi.db"):
        run_rehearsal(tmp_path / "live-source", source_v1=LIVE_DB)

    workspace = tmp_path / "not-empty"
    workspace.mkdir()
    (workspace / "keep.txt").write_text("owned by user", encoding="utf-8")
    with pytest.raises(RuntimeError, match="必须为空"):
        run_rehearsal(workspace)
