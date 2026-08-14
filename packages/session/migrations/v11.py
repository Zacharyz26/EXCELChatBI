"""Add immutable multi-parent dataset lineage for governed Join results."""

from __future__ import annotations

import hashlib

VERSION = 11
NAME = "multi_parent_dataset_lineage"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE TABLE IF NOT EXISTS dataset_lineage_edges (
    child_ref TEXT NOT NULL,
    parent_ref TEXT NOT NULL,
    parent_role TEXT NOT NULL CHECK (parent_role IN ('primary', 'secondary')),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    created_at TEXT NOT NULL,
    PRIMARY KEY (child_ref, parent_ref),
    UNIQUE (child_ref, ordinal),
    CHECK (
        (ordinal = 0 AND parent_role = 'primary')
        OR (ordinal > 0 AND parent_role = 'secondary')
    ),
    CHECK (child_ref != parent_ref),
    FOREIGN KEY (child_ref) REFERENCES dataset_lineage_anchors(ref) ON DELETE CASCADE,
    FOREIGN KEY (parent_ref) REFERENCES dataset_lineage_anchors(ref) ON DELETE CASCADE
);

INSERT INTO dataset_lineage_edges(
    child_ref, parent_ref, parent_role, ordinal, created_at
)
SELECT ref, parent_ref, 'primary', 0, created_at
FROM dataset_lineage_anchors
WHERE parent_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS task_dataset_binding_parents (
    run_id TEXT NOT NULL,
    dataset_ref TEXT NOT NULL,
    parent_ref TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    PRIMARY KEY (run_id, dataset_ref, parent_ref),
    UNIQUE (run_id, dataset_ref, ordinal),
    FOREIGN KEY (run_id, dataset_ref)
        REFERENCES task_dataset_bindings(run_id, dataset_ref) ON DELETE CASCADE,
    FOREIGN KEY (parent_ref) REFERENCES dataset_lineage_anchors(ref) ON DELETE RESTRICT
);

INSERT INTO task_dataset_binding_parents(run_id, dataset_ref, parent_ref, ordinal)
SELECT run_id, dataset_ref, parent_ref, 0
FROM task_dataset_bindings
WHERE parent_ref IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_dataset_lineage_edges_parent
    ON dataset_lineage_edges(parent_ref, child_ref);
CREATE INDEX IF NOT EXISTS idx_task_dataset_binding_parents_run
    ON task_dataset_binding_parents(run_id, dataset_ref, ordinal);

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_edge_from_legacy_parent
AFTER INSERT ON dataset_lineage_anchors
WHEN NEW.parent_ref IS NOT NULL
BEGIN
    INSERT INTO dataset_lineage_edges(
        child_ref, parent_ref, parent_role, ordinal, created_at
    ) VALUES (NEW.ref, NEW.parent_ref, 'primary', 0, NEW.created_at);
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_edge_insert_guard
BEFORE INSERT ON dataset_lineage_edges
WHEN NOT EXISTS (
        SELECT 1
        FROM dataset_lineage_anchors AS child
        JOIN dataset_lineage_anchors AS parent
          ON parent.ref = NEW.parent_ref
        WHERE child.ref = NEW.child_ref
          AND child.project_id = parent.project_id
    )
    OR (
        NEW.ordinal = 0
        AND NOT EXISTS (
            SELECT 1 FROM dataset_lineage_anchors
            WHERE ref = NEW.child_ref AND parent_ref = NEW.parent_ref
        )
    )
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage edge violates project or primary parent');
END;

CREATE TRIGGER IF NOT EXISTS trg_dataset_lineage_edge_immutable
BEFORE UPDATE ON dataset_lineage_edges
BEGIN
    SELECT RAISE(ABORT, 'dataset lineage edge is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_dataset_binding_parent_immutable
BEFORE UPDATE ON task_dataset_binding_parents
BEGIN
    SELECT RAISE(ABORT, 'task dataset binding parent is immutable');
END;
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "task_dataset_binding_parents",
    "dataset_lineage_edges",
)
ADDED_INDEXES = (
    "idx_task_dataset_binding_parents_run",
    "idx_dataset_lineage_edges_parent",
)
ADDED_TRIGGERS = (
    "trg_dataset_lineage_edge_from_legacy_parent",
    "trg_dataset_lineage_edge_insert_guard",
    "trg_dataset_lineage_edge_immutable",
    "trg_task_dataset_binding_parent_immutable",
)
