"""Add the v2.5 stage-3B conversation compaction control plane."""

from __future__ import annotations

import hashlib

VERSION = 5
NAME = "conversation_compaction"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_id_conversation
    ON messages(id, conversation_id);

CREATE TABLE IF NOT EXISTS conversation_compactions (
    compaction_id TEXT PRIMARY KEY
        CHECK (
            length(compaction_id) = 32
            AND compaction_id = lower(compaction_id)
            AND compaction_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    policy_version TEXT NOT NULL CHECK (length(trim(policy_version)) > 0),
    strategy TEXT NOT NULL CHECK (strategy = 'extractive-v1'),
    trigger_chars INTEGER NOT NULL
        CHECK (trigger_chars BETWEEN 100 AND 2000000),
    keep_recent INTEGER NOT NULL CHECK (keep_recent BETWEEN 1 AND 100),
    summary_max_chars INTEGER NOT NULL
        CHECK (summary_max_chars BETWEEN 256 AND 12000),
    per_message_max_chars INTEGER NOT NULL
        CHECK (per_message_max_chars BETWEEN 40 AND 2000),
    covered_through_message_id TEXT NOT NULL,
    source_message_count INTEGER NOT NULL CHECK (source_message_count > 0),
    source_hash TEXT NOT NULL
        CHECK (
            length(source_hash) = 64
            AND source_hash = lower(source_hash)
            AND source_hash NOT GLOB '*[^0-9a-f]*'
        ),
    summary_text TEXT NOT NULL
        CHECK (length(trim(summary_text)) BETWEEN 1 AND 12000),
    summary_hash TEXT NOT NULL
        CHECK (
            length(summary_hash) = 64
            AND summary_hash = lower(summary_hash)
            AND summary_hash NOT GLOB '*[^0-9a-f]*'
        ),
    redaction_count INTEGER NOT NULL CHECK (redaction_count >= 0),
    omitted_message_count INTEGER NOT NULL CHECK (omitted_message_count >= 0),
    supersedes_id TEXT,
    created_by_user_id TEXT NOT NULL CHECK (length(trim(created_by_user_id)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE (compaction_id, conversation_id),
    UNIQUE (conversation_id, version),
    UNIQUE (conversation_id, source_hash),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (conversation_id, project_id)
        REFERENCES conversations(id, project_id) ON DELETE CASCADE,
    FOREIGN KEY (covered_through_message_id, conversation_id)
        REFERENCES messages(id, conversation_id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, created_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (supersedes_id)
        REFERENCES conversation_compactions(compaction_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_compactions_project_created
    ON conversation_compactions(project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_compactions_conversation_version
    ON conversation_compactions(conversation_id, version DESC);

CREATE TABLE IF NOT EXISTS conversation_compaction_items (
    compaction_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content_hash TEXT NOT NULL
        CHECK (
            length(content_hash) = 64
            AND content_hash = lower(content_hash)
            AND content_hash NOT GLOB '*[^0-9a-f]*'
        ),
    PRIMARY KEY (compaction_id, message_id),
    UNIQUE (compaction_id, position),
    FOREIGN KEY (compaction_id, conversation_id)
        REFERENCES conversation_compactions(compaction_id, conversation_id)
        ON DELETE CASCADE,
    FOREIGN KEY (message_id, conversation_id)
        REFERENCES messages(id, conversation_id) ON DELETE CASCADE
);

ALTER TABLE memory_snapshots
    ADD COLUMN compaction_id TEXT
        REFERENCES conversation_compactions(compaction_id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_memory_snapshots_compaction
    ON memory_snapshots(compaction_id);
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "conversation_compaction_items",
    "conversation_compactions",
)

ADDED_INDEXES = (
    "idx_messages_id_conversation",
    "idx_compactions_project_created",
    "idx_compactions_conversation_version",
    "idx_memory_snapshots_compaction",
)
