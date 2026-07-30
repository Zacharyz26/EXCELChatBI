"""Add the v2.5 memory control-plane schema."""

from __future__ import annotations

import hashlib

VERSION = 4
NAME = "memory_control_plane"

# Keep this text immutable after release. The runner records its checksum and
# refuses to open a v4 database whose recorded migration differs from the code.
DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversations_id_project
    ON conversations(id, project_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_id_project
    ON task_runs(run_id, project_id);

CREATE TABLE IF NOT EXISTS memory_records (
    memory_id TEXT PRIMARY KEY
        CHECK (
            length(memory_id) = 32
            AND memory_id = lower(memory_id)
            AND memory_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    scope TEXT NOT NULL CHECK (scope IN ('conversation', 'project', 'subject')),
    scope_key TEXT NOT NULL CHECK (length(trim(scope_key)) > 0),
    conversation_id TEXT,
    subject_user_id TEXT,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'field_alias', 'user_preference', 'confirmed_decision',
            'entity_mapping', 'conversation_summary'
        )
    ),
    semantic_key TEXT NOT NULL
        CHECK (length(trim(semantic_key)) BETWEEN 1 AND 200),
    content_summary TEXT NOT NULL
        CHECK (length(trim(content_summary)) BETWEEN 1 AND 4000),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('message', 'user_confirmation', 'artifact', 'evidence', 'invocation')
    ),
    source_ref TEXT NOT NULL CHECK (length(trim(source_ref)) BETWEEN 1 AND 200),
    source_hash TEXT NOT NULL
        CHECK (
            length(source_hash) = 64
            AND source_hash = lower(source_hash)
            AND source_hash NOT GLOB '*[^0-9a-f]*'
        ),
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    valid_from TEXT NOT NULL,
    expires_at TEXT,
    version INTEGER NOT NULL CHECK (version > 0),
    status TEXT NOT NULL CHECK (
        status IN ('active', 'conflict', 'superseded', 'deleted')
    ),
    supersedes_id TEXT,
    conflicts_with_id TEXT,
    created_by_user_id TEXT NOT NULL CHECK (length(trim(created_by_user_id)) > 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT,
    CHECK (
        (scope = 'conversation' AND conversation_id IS NOT NULL
            AND subject_user_id IS NULL AND scope_key = conversation_id)
        OR (scope = 'project' AND conversation_id IS NULL
            AND subject_user_id IS NULL AND scope_key = 'project')
        OR (scope = 'subject' AND conversation_id IS NULL
            AND subject_user_id IS NOT NULL AND scope_key = subject_user_id)
    ),
    CHECK (expires_at IS NULL OR expires_at > valid_from),
    CHECK (
        (status = 'deleted' AND deleted_at IS NOT NULL)
        OR (status != 'deleted' AND deleted_at IS NULL)
    ),
    UNIQUE (memory_id, version),
    UNIQUE (project_id, scope, scope_key, semantic_key, version),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id, project_id)
        REFERENCES conversations(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, subject_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE CASCADE,
    FOREIGN KEY (project_id, created_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_id) REFERENCES memory_records(memory_id) ON DELETE SET NULL,
    FOREIGN KEY (conflicts_with_id)
        REFERENCES memory_records(memory_id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_records_active_semantic
    ON memory_records(project_id, scope, scope_key, semantic_key)
    WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_memory_records_project_scope_status
    ON memory_records(project_id, scope, scope_key, status, valid_from);
CREATE INDEX IF NOT EXISTS idx_memory_records_expiry
    ON memory_records(project_id, expires_at)
    WHERE status = 'active' AND expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_memory_records_source
    ON memory_records(project_id, source_type, source_ref);

CREATE TABLE IF NOT EXISTS memory_operations (
    operation_id TEXT PRIMARY KEY
        CHECK (
            length(operation_id) = 32
            AND operation_id = lower(operation_id)
            AND operation_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    actor_user_id TEXT NOT NULL CHECK (length(trim(actor_user_id)) > 0),
    idempotency_key TEXT NOT NULL
        CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 200),
    operation_type TEXT NOT NULL CHECK (
        operation_type IN ('remember', 'revise', 'delete')
    ),
    request_hash TEXT NOT NULL
        CHECK (
            length(request_hash) = 64
            AND request_hash = lower(request_hash)
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    result_ref TEXT NOT NULL CHECK (length(trim(result_ref)) > 0),
    result_version INTEGER NOT NULL CHECK (result_version > 0),
    created_at TEXT NOT NULL,
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, actor_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_memory_operations_actor_created
    ON memory_operations(tenant_id, actor_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_snapshots (
    memory_snapshot_id TEXT PRIMARY KEY
        CHECK (
            length(memory_snapshot_id) = 32
            AND memory_snapshot_id = lower(memory_snapshot_id)
            AND memory_snapshot_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    subject_user_id TEXT NOT NULL CHECK (length(trim(subject_user_id)) > 0),
    conversation_id TEXT,
    run_id TEXT UNIQUE,
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
    selection_hash TEXT NOT NULL
        CHECK (
            length(selection_hash) = 64
            AND selection_hash = lower(selection_hash)
            AND selection_hash NOT GLOB '*[^0-9a-f]*'
        ),
    content_hash TEXT NOT NULL
        CHECK (
            length(content_hash) = 64
            AND content_hash = lower(content_hash)
            AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
    record_count INTEGER NOT NULL CHECK (record_count >= 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, subject_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE CASCADE,
    FOREIGN KEY (conversation_id, project_id)
        REFERENCES conversations(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, project_id)
        REFERENCES task_runs(run_id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_memory_snapshots_project_created
    ON memory_snapshots(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_snapshots_subject_created
    ON memory_snapshots(tenant_id, subject_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_snapshot_items (
    memory_snapshot_id TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    memory_version INTEGER NOT NULL CHECK (memory_version > 0),
    position INTEGER NOT NULL CHECK (position >= 0),
    record_json TEXT NOT NULL CHECK (length(trim(record_json)) > 1),
    PRIMARY KEY (memory_snapshot_id, memory_id),
    UNIQUE (memory_snapshot_id, position),
    FOREIGN KEY (memory_snapshot_id)
        REFERENCES memory_snapshots(memory_snapshot_id) ON DELETE CASCADE,
    FOREIGN KEY (memory_id, memory_version)
        REFERENCES memory_records(memory_id, version) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS memory_links (
    link_id TEXT PRIMARY KEY
        CHECK (
            length(link_id) = 32
            AND link_id = lower(link_id)
            AND link_id NOT GLOB '*[^0-9a-f]*'
        ),
    memory_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    target_type TEXT NOT NULL CHECK (
        target_type IN (
            'conversation', 'message', 'task_run', 'dataset',
            'artifact', 'claim', 'evidence', 'invocation'
        )
    ),
    target_ref TEXT NOT NULL CHECK (length(trim(target_ref)) BETWEEN 1 AND 200),
    created_by_user_id TEXT NOT NULL CHECK (length(trim(created_by_user_id)) > 0),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE (memory_id, target_type, target_ref),
    FOREIGN KEY (memory_id) REFERENCES memory_records(memory_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, created_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_memory_links_target
    ON memory_links(project_id, target_type, target_ref);
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "memory_links",
    "memory_snapshot_items",
    "memory_snapshots",
    "memory_operations",
    "memory_records",
)

ADDED_INDEXES_ON_LEGACY_TABLES = (
    "idx_conversations_id_project",
    "idx_task_runs_id_project",
)
