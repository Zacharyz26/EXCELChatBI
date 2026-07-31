"""Preserve immutable Dataset anchors for the v2.5 stage-3E lineage graph."""

from __future__ import annotations

import hashlib

VERSION = 6
NAME = "immutable_lineage_anchors"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE TABLE IF NOT EXISTS dataset_lineage_anchors (
    ref TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    filename TEXT NOT NULL CHECK (length(trim(filename)) > 0),
    parent_ref TEXT,
    created_at TEXT NOT NULL,
    deleted_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

INSERT INTO dataset_lineage_anchors(
    ref, project_id, filename, parent_ref, created_at, deleted_at
)
SELECT ref, project_id, filename, parent_ref, created_at, NULL
FROM datasets;

ALTER TABLE datasets ADD COLUMN lineage_parent_ref TEXT;
UPDATE datasets SET lineage_parent_ref = parent_ref;

ALTER TABLE artifacts ADD COLUMN lineage_dataset_ref TEXT;
UPDATE artifacts SET lineage_dataset_ref = dataset_ref;

CREATE INDEX IF NOT EXISTS idx_datasets_lineage_parent
    ON datasets(project_id, lineage_parent_ref);
CREATE INDEX IF NOT EXISTS idx_artifacts_lineage_dataset
    ON artifacts(lineage_dataset_ref, conversation_id);
CREATE INDEX IF NOT EXISTS idx_dataset_lineage_anchors_project
    ON dataset_lineage_anchors(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_dataset_lineage_anchors_parent
    ON dataset_lineage_anchors(project_id, parent_ref);

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_record
AFTER INSERT ON datasets
BEGIN
    INSERT INTO dataset_lineage_anchors(
        ref, project_id, filename, parent_ref, created_at, deleted_at
    ) VALUES (
        NEW.ref, NEW.project_id, NEW.filename, NEW.parent_ref, NEW.created_at, NULL
    );
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_insert_guard
BEFORE INSERT ON dataset_lineage_anchors
WHEN NOT EXISTS (
    SELECT 1 FROM datasets
    WHERE ref = NEW.ref
      AND project_id = NEW.project_id
      AND filename = NEW.filename
      AND parent_ref IS NEW.parent_ref
      AND created_at = NEW.created_at
)
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage anchor must match a live dataset');
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_delete
AFTER DELETE ON datasets
BEGIN
    UPDATE dataset_lineage_anchors
    SET deleted_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
    WHERE ref = OLD.ref;
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_rename
AFTER UPDATE OF filename ON datasets
WHEN OLD.filename IS NOT NEW.filename
BEGIN
    UPDATE dataset_lineage_anchors
    SET filename = NEW.filename
    WHERE ref = NEW.ref;
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_identity_immutable
BEFORE UPDATE OF ref, project_id, parent_ref, created_at
ON dataset_lineage_anchors
WHEN OLD.ref IS NOT NEW.ref
  OR OLD.project_id IS NOT NEW.project_id
  OR OLD.parent_ref IS NOT NEW.parent_ref
  OR OLD.created_at IS NOT NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage anchor identity is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_filename_guard
BEFORE UPDATE OF filename ON dataset_lineage_anchors
WHEN NOT EXISTS (
    SELECT 1 FROM datasets
    WHERE ref = OLD.ref AND filename = NEW.filename
)
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage anchor label must match live dataset');
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_anchor_tombstone_immutable
BEFORE UPDATE OF deleted_at ON dataset_lineage_anchors
WHEN OLD.deleted_at IS NOT NULL
  OR NEW.deleted_at IS NULL
  OR EXISTS (SELECT 1 FROM datasets WHERE ref = OLD.ref)
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage anchor tombstone is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_datasets_lineage_anchor_insert
AFTER INSERT ON datasets
WHEN NEW.parent_ref IS NOT NULL AND NEW.lineage_parent_ref IS NULL
BEGIN
    UPDATE datasets
    SET lineage_parent_ref = NEW.parent_ref
    WHERE ref = NEW.ref;
END;

CREATE TRIGGER IF NOT EXISTS trg_artifacts_lineage_anchor_insert
AFTER INSERT ON artifacts
WHEN NEW.dataset_ref IS NOT NULL AND NEW.lineage_dataset_ref IS NULL
BEGIN
    UPDATE artifacts
    SET lineage_dataset_ref = NEW.dataset_ref
    WHERE id = NEW.id;
END;

CREATE TRIGGER IF NOT EXISTS trg_datasets_lineage_anchor_immutable
BEFORE UPDATE OF lineage_parent_ref ON datasets
WHEN OLD.lineage_parent_ref IS NOT NEW.lineage_parent_ref
  AND (
        OLD.lineage_parent_ref IS NOT NULL
        OR NEW.lineage_parent_ref IS NOT OLD.parent_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage anchor is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_artifacts_lineage_anchor_immutable
BEFORE UPDATE OF lineage_dataset_ref ON artifacts
WHEN OLD.lineage_dataset_ref IS NOT NEW.lineage_dataset_ref
  AND (
        OLD.lineage_dataset_ref IS NOT NULL
        OR NEW.lineage_dataset_ref IS NOT OLD.dataset_ref
  )
BEGIN
    SELECT RAISE(ABORT, 'artifact lineage anchor is immutable');
END;
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_INDEXES = (
    "idx_datasets_lineage_parent",
    "idx_artifacts_lineage_dataset",
    "idx_dataset_lineage_anchors_project",
    "idx_dataset_lineage_anchors_parent",
)

ADDED_TRIGGERS = (
    "trg_dataset_lineage_anchor_record",
    "trg_dataset_lineage_anchor_insert_guard",
    "trg_dataset_lineage_anchor_delete",
    "trg_dataset_lineage_anchor_rename",
    "trg_dataset_lineage_anchor_identity_immutable",
    "trg_dataset_lineage_anchor_filename_guard",
    "trg_dataset_lineage_anchor_tombstone_immutable",
    "trg_datasets_lineage_anchor_insert",
    "trg_artifacts_lineage_anchor_insert",
    "trg_datasets_lineage_anchor_immutable",
    "trg_artifacts_lineage_anchor_immutable",
)

ADDED_TABLES = ("dataset_lineage_anchors",)
