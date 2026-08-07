"""Add the durable control plane required for bounded parallel execution."""

from __future__ import annotations

import hashlib

VERSION = 10
NAME = "controlled_parallel_execution"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE TABLE IF NOT EXISTS task_execution_scopes (
    scope_id TEXT PRIMARY KEY
        CHECK (
            length(scope_id) = 32
            AND scope_id = lower(scope_id)
            AND scope_id NOT GLOB '*[^0-9a-f]*'
        ),
    run_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
    max_tool_calls INTEGER NOT NULL CHECK (max_tool_calls > 0),
    max_parallelism INTEGER NOT NULL CHECK (max_parallelism > 0),
    cancellation_root_id TEXT NOT NULL UNIQUE
        CHECK (
            length(cancellation_root_id) = 32
            AND cancellation_root_id = lower(cancellation_root_id)
            AND cancellation_root_id NOT GLOB '*[^0-9a-f]*'
        ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_dataset_bindings (
    run_id TEXT NOT NULL,
    dataset_ref TEXT NOT NULL,
    binding_kind TEXT NOT NULL CHECK (binding_kind IN ('initial', 'derived')),
    parent_ref TEXT,
    producing_invocation_id TEXT,
    dataset_created_at TEXT NOT NULL,
    bound_at TEXT NOT NULL,
    PRIMARY KEY (run_id, dataset_ref),
    CHECK (
        (binding_kind = 'initial' AND producing_invocation_id IS NULL)
        OR (binding_kind = 'derived' AND producing_invocation_id IS NOT NULL)
    ),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (dataset_ref) REFERENCES dataset_lineage_anchors(ref) ON DELETE RESTRICT,
    FOREIGN KEY (parent_ref) REFERENCES dataset_lineage_anchors(ref) ON DELETE RESTRICT,
    FOREIGN KEY (producing_invocation_id)
        REFERENCES tool_invocations(invocation_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS task_cancellation_nodes (
    node_id TEXT PRIMARY KEY
        CHECK (
            length(node_id) = 32
            AND node_id = lower(node_id)
            AND node_id NOT GLOB '*[^0-9a-f]*'
        ),
    run_id TEXT NOT NULL,
    parent_node_id TEXT,
    invocation_id TEXT UNIQUE,
    step_id TEXT,
    data_version_hash TEXT NOT NULL
        CHECK (
            length(data_version_hash) = 64
            AND data_version_hash = lower(data_version_hash)
            AND data_version_hash NOT GLOB '*[^0-9a-f]*'
        ),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'completed', 'cancel_requested')
    ),
    reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (parent_node_id IS NULL AND invocation_id IS NULL AND step_id IS NULL)
        OR (parent_node_id IS NOT NULL AND invocation_id IS NOT NULL)
    ),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_node_id)
        REFERENCES task_cancellation_nodes(node_id) ON DELETE CASCADE,
    FOREIGN KEY (invocation_id)
        REFERENCES tool_invocations(invocation_id) ON DELETE CASCADE,
    FOREIGN KEY (step_id) REFERENCES task_steps(step_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS evidence_ledger_entries (
    ledger_entry_id TEXT PRIMARY KEY
        CHECK (
            length(ledger_entry_id) = 32
            AND ledger_entry_id = lower(ledger_entry_id)
            AND ledger_entry_id NOT GLOB '*[^0-9a-f]*'
        ),
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    evidence_id TEXT NOT NULL UNIQUE,
    branch_node_id TEXT NOT NULL,
    data_version_hash TEXT NOT NULL
        CHECK (
            length(data_version_hash) = 64
            AND data_version_hash = lower(data_version_hash)
            AND data_version_hash NOT GLOB '*[^0-9a-f]*'
        ),
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id) ON DELETE CASCADE,
    FOREIGN KEY (branch_node_id)
        REFERENCES task_cancellation_nodes(node_id) ON DELETE RESTRICT
);

INSERT INTO task_execution_scopes(
    scope_id, run_id, schema_version, max_tool_calls, max_parallelism,
    cancellation_root_id, created_at
)
SELECT
    lower(hex(randomblob(16))), run_id, 1,
    CASE
        WHEN json_type(budget_json, '$.max_tool_calls') = 'integer'
             AND json_extract(budget_json, '$.max_tool_calls') > 0
        THEN json_extract(budget_json, '$.max_tool_calls')
        ELSE 12
    END,
    4,
    lower(hex(randomblob(16))),
    created_at
FROM task_runs;

INSERT INTO task_cancellation_nodes(
    node_id, run_id, parent_node_id, invocation_id, step_id, data_version_hash,
    status, reason, created_at, updated_at
)
SELECT
    cancellation_root_id, scope.run_id, NULL, NULL, NULL, lower(hex(zeroblob(32))),
    CASE
        WHEN run.status = 'cancelled' THEN 'cancel_requested'
        WHEN run.status IN ('completed', 'blocked', 'failed') THEN 'completed'
        ELSE 'active'
    END,
    CASE WHEN run.status = 'cancelled' THEN COALESCE(run.terminal_reason, 'cancelled') END,
    scope.created_at,
    run.updated_at
FROM task_execution_scopes AS scope
JOIN task_runs AS run ON run.run_id = scope.run_id;

INSERT OR IGNORE INTO task_dataset_bindings(
    run_id, dataset_ref, binding_kind, parent_ref, producing_invocation_id,
    dataset_created_at, bound_at
)
SELECT
    run.run_id, anchor.ref, 'initial', anchor.parent_ref, NULL,
    anchor.created_at, run.created_at
FROM task_runs AS run
JOIN dataset_lineage_anchors AS anchor
  ON anchor.project_id = run.project_id
 AND anchor.created_at <= run.created_at
 AND (anchor.deleted_at IS NULL OR anchor.deleted_at > run.created_at);

INSERT OR IGNORE INTO task_dataset_bindings(
    run_id, dataset_ref, binding_kind, parent_ref, producing_invocation_id,
    dataset_created_at, bound_at
)
SELECT
    invocation.run_id,
    json_extract(invocation.args_json, '$.dataset_ref'),
    'initial',
    anchor.parent_ref,
    NULL,
    anchor.created_at,
    invocation.started_at
FROM tool_invocations AS invocation
JOIN task_runs AS run ON run.run_id = invocation.run_id
JOIN dataset_lineage_anchors AS anchor
  ON anchor.ref = json_extract(invocation.args_json, '$.dataset_ref')
 AND anchor.project_id = run.project_id
WHERE json_type(invocation.args_json, '$.dataset_ref') = 'text';

INSERT INTO task_cancellation_nodes(
    node_id, run_id, parent_node_id, invocation_id, step_id, data_version_hash,
    status, reason, created_at, updated_at
)
SELECT
    lower(hex(randomblob(16))), invocation.run_id, scope.cancellation_root_id,
    invocation.invocation_id, invocation.step_id, lower(hex(zeroblob(32))),
    CASE
        WHEN run.status = 'cancelled' THEN 'cancel_requested'
        WHEN invocation.status = 'running' THEN 'active'
        ELSE 'completed'
    END,
    CASE
        WHEN run.status = 'cancelled' THEN COALESCE(run.terminal_reason, 'cancelled')
        ELSE invocation.error_text
    END,
    invocation.started_at,
    COALESCE(invocation.completed_at, run.updated_at)
FROM tool_invocations AS invocation
JOIN task_runs AS run ON run.run_id = invocation.run_id
JOIN task_execution_scopes AS scope ON scope.run_id = invocation.run_id;

INSERT INTO evidence_ledger_entries(
    ledger_entry_id, run_id, sequence, evidence_id, branch_node_id,
    data_version_hash, created_at
)
SELECT
    lower(hex(randomblob(16))), evidence.run_id,
    ROW_NUMBER() OVER (
        PARTITION BY evidence.run_id
        ORDER BY evidence.created_at, evidence.rowid
    ),
    evidence.evidence_id,
    branch.node_id,
    lower(hex(zeroblob(32))),
    evidence.created_at
FROM evidence
JOIN task_cancellation_nodes AS branch
  ON branch.invocation_id = evidence.invocation_id;

CREATE INDEX IF NOT EXISTS idx_task_dataset_bindings_run_kind
    ON task_dataset_bindings(run_id, binding_kind, bound_at);
CREATE INDEX IF NOT EXISTS idx_task_cancellation_nodes_run_status
    ON task_cancellation_nodes(run_id, status, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_ledger_run_sequence
    ON evidence_ledger_entries(run_id, sequence);
CREATE UNIQUE INDEX IF NOT EXISTS idx_evidence_one_per_invocation
    ON evidence(invocation_id);

CREATE TRIGGER IF NOT EXISTS trg_task_execution_scope_immutable
BEFORE UPDATE ON task_execution_scopes
BEGIN
    SELECT RAISE(ABORT, 'task execution scope is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_task_dataset_binding_immutable
BEFORE UPDATE ON task_dataset_bindings
BEGIN
    SELECT RAISE(ABORT, 'task dataset binding is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_evidence_ledger_entry_immutable
BEFORE UPDATE ON evidence_ledger_entries
BEGIN
    SELECT RAISE(ABORT, 'evidence ledger entry is immutable');
END;
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "evidence_ledger_entries",
    "task_cancellation_nodes",
    "task_dataset_bindings",
    "task_execution_scopes",
)
ADDED_INDEXES = (
    "idx_task_dataset_bindings_run_kind",
    "idx_task_cancellation_nodes_run_status",
    "idx_evidence_ledger_run_sequence",
    "idx_evidence_one_per_invocation",
)
ADDED_TRIGGERS = (
    "trg_task_execution_scope_immutable",
    "trg_task_dataset_binding_immutable",
    "trg_evidence_ledger_entry_immutable",
)
