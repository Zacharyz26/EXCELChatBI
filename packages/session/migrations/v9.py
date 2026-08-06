"""Freeze one immutable capability catalog snapshot for every TaskRun."""

from __future__ import annotations

import hashlib

VERSION = 9
NAME = "task_capability_catalog_snapshots"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE TABLE IF NOT EXISTS capability_catalog_snapshots (
    snapshot_id TEXT PRIMARY KEY
        CHECK (
            length(snapshot_id) = 32
            AND snapshot_id = lower(snapshot_id)
            AND snapshot_id NOT GLOB '*[^0-9a-f]*'
        ),
    run_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    catalog_json TEXT NOT NULL CHECK (json_valid(catalog_json)),
    content_hash TEXT NOT NULL
        CHECK (
            length(content_hash) = 64
            AND content_hash = lower(content_hash)
            AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_capability_catalog_snapshots_content_hash
    ON capability_catalog_snapshots(content_hash);

CREATE TRIGGER IF NOT EXISTS trg_capability_catalog_snapshot_immutable
BEFORE UPDATE ON capability_catalog_snapshots
BEGIN
    SELECT RAISE(ABORT, 'capability catalog snapshot is immutable');
END;
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = ("capability_catalog_snapshots",)
ADDED_INDEXES = ("idx_capability_catalog_snapshots_content_hash",)
ADDED_TRIGGERS = ("trg_capability_catalog_snapshot_immutable",)
