"""Additive v1 -> v2 Agent control-plane schema."""

from __future__ import annotations

import hashlib

VERSION = 2
NAME = "agent_control_plane"

# Keep this text immutable after release.  The runner records its checksum and
# refuses to open a v2 database whose recorded migration differs from the code.
DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    backup_path TEXT,
    source_sha256 TEXT,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    user_message_id TEXT NOT NULL,
    parent_run_id TEXT,
    goal TEXT NOT NULL CHECK (length(trim(goal)) > 0),
    status TEXT NOT NULL CHECK (
        status IN (
            'planning', 'waiting_user', 'running', 'verifying', 'paused',
            'completed', 'blocked', 'failed', 'cancelled'
        )
    ),
    state_version INTEGER NOT NULL DEFAULT 1 CHECK (state_version > 0),
    plan_version INTEGER NOT NULL DEFAULT 0 CHECK (plan_version >= 0),
    budget_json TEXT NOT NULL,
    usage_json TEXT NOT NULL,
    terminal_reason TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,
    FOREIGN KEY (user_message_id) REFERENCES messages(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_run_id) REFERENCES task_runs(run_id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS task_contracts (
    run_id TEXT PRIMARY KEY,
    contract_json TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_plans (
    plan_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    reason TEXT,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, version),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_steps (
    step_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    status TEXT NOT NULL,
    step_json TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    UNIQUE (plan_id, position),
    FOREIGN KEY (plan_id) REFERENCES task_plans(plan_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_events (
    event_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    event_type TEXT NOT NULL CHECK (length(trim(event_type)) > 0),
    payload_json TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS task_snapshots (
    run_id TEXT PRIMARY KEY,
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS tool_invocations (
    invocation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    step_id TEXT,
    tool_call_id TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    args_hash TEXT NOT NULL,
    args_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('running', 'succeeded', 'failed', 'unknown')),
    result_hash TEXT,
    error_text TEXT,
    artifact_id TEXT,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (run_id, idempotency_key),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (step_id) REFERENCES task_steps(step_id) ON DELETE SET NULL,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    invocation_id TEXT NOT NULL,
    artifact_id TEXT,
    kind TEXT NOT NULL,
    source_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (invocation_id) REFERENCES tool_invocations(invocation_id) ON DELETE CASCADE,
    FOREIGN KEY (artifact_id) REFERENCES artifacts(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    statement TEXT NOT NULL,
    claim_kind TEXT NOT NULL,
    value_refs_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS claim_evidence (
    claim_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    PRIMARY KEY (claim_id, evidence_id),
    FOREIGN KEY (claim_id) REFERENCES claims(claim_id) ON DELETE CASCADE,
    FOREIGN KEY (evidence_id) REFERENCES evidence(evidence_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    state_version INTEGER NOT NULL CHECK (state_version > 0),
    state_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES task_runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_runs_conversation_created
    ON task_runs(conversation_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_status_updated
    ON task_runs(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_events_run_sequence
    ON task_events(run_id, sequence);
CREATE INDEX IF NOT EXISTS idx_task_steps_run_status
    ON task_steps(run_id, status);
CREATE INDEX IF NOT EXISTS idx_tool_invocations_run_started
    ON tool_invocations(run_id, started_at);
CREATE INDEX IF NOT EXISTS idx_evidence_run_created
    ON evidence(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_claims_run_created
    ON claims(run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_checkpoints_run_sequence
    ON checkpoints(run_id, sequence DESC);
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "claim_evidence",
    "claims",
    "checkpoints",
    "evidence",
    "tool_invocations",
    "task_snapshots",
    "task_events",
    "task_steps",
    "task_plans",
    "task_contracts",
    "task_runs",
    "schema_migrations",
)
