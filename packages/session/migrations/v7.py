"""Add the v2.5 stage-4A collaboration and approval control plane."""

from __future__ import annotations

import hashlib

VERSION = 7
NAME = "collaboration_approvals"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE TABLE IF NOT EXISTS approval_records (
    approval_id TEXT PRIMARY KEY
        CHECK (
            length(approval_id) = 32
            AND approval_id = lower(approval_id)
            AND approval_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_version INTEGER NOT NULL CHECK (plan_version > 0),
    task_step_id TEXT NOT NULL,
    step_logical_id TEXT NOT NULL
        CHECK (length(trim(step_logical_id)) BETWEEN 1 AND 100),
    subject_user_id TEXT NOT NULL CHECK (length(trim(subject_user_id)) > 0),
    requested_by_user_id TEXT NOT NULL
        CHECK (length(trim(requested_by_user_id)) > 0),
    tool_name TEXT NOT NULL CHECK (length(trim(tool_name)) BETWEEN 1 AND 200),
    tool_schema_hash TEXT NOT NULL
        CHECK (
            length(tool_schema_hash) = 64
            AND tool_schema_hash = lower(tool_schema_hash)
            AND tool_schema_hash NOT GLOB '*[^0-9a-f]*'
        ),
    parameter_summary_hash TEXT NOT NULL
        CHECK (
            length(parameter_summary_hash) = 64
            AND parameter_summary_hash = lower(parameter_summary_hash)
            AND parameter_summary_hash NOT GLOB '*[^0-9a-f]*'
        ),
    risk_level TEXT NOT NULL CHECK (risk_level IN ('high', 'critical')),
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'approved', 'denied', 'consumed', 'revoked')),
    version INTEGER NOT NULL CHECK (version > 0),
    expires_at TEXT NOT NULL,
    decision_reason TEXT,
    decided_by_user_id TEXT,
    requested_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    decided_at TEXT,
    consumed_at TEXT,
    idempotency_key TEXT NOT NULL
        CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 200),
    request_hash TEXT NOT NULL
        CHECK (
            length(request_hash) = 64
            AND request_hash = lower(request_hash)
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    request_event_id TEXT NOT NULL,
    UNIQUE (run_id, idempotency_key),
    CHECK (expires_at > requested_at),
    CHECK (
        (status = 'pending'
            AND decision_reason IS NULL
            AND decided_by_user_id IS NULL
            AND decided_at IS NULL
            AND consumed_at IS NULL)
        OR (status IN ('approved', 'denied', 'revoked')
            AND decision_reason IS NOT NULL
            AND decided_by_user_id IS NOT NULL
            AND decided_at IS NOT NULL
            AND consumed_at IS NULL)
        OR (status = 'consumed'
            AND decision_reason IS NOT NULL
            AND decided_by_user_id IS NOT NULL
            AND decided_at IS NOT NULL
            AND consumed_at IS NOT NULL)
    ),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, project_id)
        REFERENCES task_runs(run_id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (plan_id) REFERENCES task_plans(plan_id) ON DELETE CASCADE,
    FOREIGN KEY (task_step_id) REFERENCES task_steps(step_id) ON DELETE CASCADE,
    FOREIGN KEY (request_event_id) REFERENCES task_events(event_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, subject_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (project_id, requested_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (project_id, decided_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_approval_records_run_status
    ON approval_records(run_id, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_records_subject_status
    ON approval_records(tenant_id, subject_user_id, status, requested_at DESC);
CREATE INDEX IF NOT EXISTS idx_approval_records_expiry
    ON approval_records(status, expires_at)
    WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS approval_operations (
    operation_id TEXT PRIMARY KEY
        CHECK (
            length(operation_id) = 32
            AND operation_id = lower(operation_id)
            AND operation_id NOT GLOB '*[^0-9a-f]*'
        ),
    approval_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    actor_user_id TEXT NOT NULL CHECK (length(trim(actor_user_id)) > 0),
    idempotency_key TEXT NOT NULL
        CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 200),
    operation_type TEXT NOT NULL CHECK (operation_type IN ('decide', 'consume', 'revoke')),
    request_hash TEXT NOT NULL
        CHECK (
            length(request_hash) = 64
            AND request_hash = lower(request_hash)
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    result_status TEXT NOT NULL
        CHECK (result_status IN ('approved', 'denied', 'consumed', 'revoked')),
    result_version INTEGER NOT NULL CHECK (result_version > 0),
    event_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, idempotency_key),
    FOREIGN KEY (approval_id) REFERENCES approval_records(approval_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES task_events(event_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, actor_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_approval_operations_approval_created
    ON approval_operations(approval_id, created_at);

CREATE TRIGGER IF NOT EXISTS trg_approval_binding_immutable
BEFORE UPDATE ON approval_records
WHEN
    NEW.tenant_id != OLD.tenant_id
    OR NEW.project_id != OLD.project_id
    OR NEW.run_id != OLD.run_id
    OR NEW.plan_id != OLD.plan_id
    OR NEW.plan_version != OLD.plan_version
    OR NEW.task_step_id != OLD.task_step_id
    OR NEW.step_logical_id != OLD.step_logical_id
    OR NEW.subject_user_id != OLD.subject_user_id
    OR NEW.requested_by_user_id != OLD.requested_by_user_id
    OR NEW.tool_name != OLD.tool_name
    OR NEW.tool_schema_hash != OLD.tool_schema_hash
    OR NEW.parameter_summary_hash != OLD.parameter_summary_hash
    OR NEW.risk_level != OLD.risk_level
    OR NEW.expires_at != OLD.expires_at
    OR NEW.requested_at != OLD.requested_at
    OR NEW.idempotency_key != OLD.idempotency_key
    OR NEW.request_hash != OLD.request_hash
    OR NEW.request_event_id != OLD.request_event_id
BEGIN
    SELECT RAISE(ABORT, 'approval binding is immutable');
END;
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "approval_operations",
    "approval_records",
)

ADDED_INDEXES = (
    "idx_approval_records_run_status",
    "idx_approval_records_subject_status",
    "idx_approval_records_expiry",
    "idx_approval_operations_approval_created",
)

ADDED_TRIGGERS = ("trg_approval_binding_immutable",)
