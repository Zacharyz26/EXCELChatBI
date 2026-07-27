"""Add project ownership plus report publication ownership."""

from __future__ import annotations

import hashlib

VERSION = 3
NAME = "project_membership_and_report_ownership"

DDL = """
CREATE TABLE IF NOT EXISTS project_memberships (
    project_id TEXT NOT NULL,
    user_id TEXT NOT NULL CHECK (length(trim(user_id)) > 0),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    role TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, user_id, tenant_id),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
);

INSERT OR IGNORE INTO project_memberships(
    project_id, user_id, tenant_id, role, created_at
)
SELECT id, 'local-user', 'local', 'owner', created_at
FROM projects;

CREATE INDEX IF NOT EXISTS idx_project_memberships_subject
    ON project_memberships(tenant_id, user_id, project_id);

CREATE TABLE IF NOT EXISTS report_publications (
    report_id TEXT PRIMARY KEY
        CHECK (
            length(report_id) = 32
            AND report_id = lower(report_id)
            AND report_id NOT GLOB '*[^0-9a-f]*'
        ),
    project_id TEXT NOT NULL,
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_report_publications_project
    ON report_publications(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_report_publications_conversation
    ON report_publications(conversation_id, created_at);
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = ("report_publications", "project_memberships")
