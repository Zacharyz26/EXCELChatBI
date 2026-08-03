"""Add the stage-5A versioned domain definition control plane."""

from __future__ import annotations

import hashlib

VERSION = 8
NAME = "versioned_domain_definitions"

# Released migration text is immutable. Startup verifies the recorded checksum.
DDL = """
CREATE TABLE IF NOT EXISTS domain_definitions (
    definition_id TEXT PRIMARY KEY
        CHECK (
            length(definition_id) = 32
            AND definition_id = lower(definition_id)
            AND definition_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    semantic_key TEXT NOT NULL
        CHECK (
            length(semantic_key) BETWEEN 1 AND 100
            AND semantic_key = lower(semantic_key)
            AND substr(semantic_key, 1, 1) GLOB '[a-z]'
            AND semantic_key NOT GLOB '*[^a-z0-9_.-]*'
        ),
    definition_kind TEXT NOT NULL CHECK (definition_kind = 'metric'),
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) BETWEEN 1 AND 200),
    description TEXT NOT NULL CHECK (length(description) <= 4000),
    formula_json TEXT NOT NULL CHECK (json_valid(formula_json)),
    formula_hash TEXT NOT NULL
        CHECK (
            length(formula_hash) = 64
            AND formula_hash = lower(formula_hash)
            AND formula_hash NOT GLOB '*[^0-9a-f]*'
        ),
    grain_json TEXT NOT NULL CHECK (json_valid(grain_json)),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    owner TEXT NOT NULL CHECK (length(trim(owner)) BETWEEN 1 AND 200),
    source_ref TEXT NOT NULL
        CHECK (
            length(trim(source_ref)) BETWEEN 1 AND 1000
            AND (
                source_ref GLOB 'https://*'
                OR source_ref GLOB 'urn:*'
                OR source_ref GLOB 'chatbi://*'
            )
        ),
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    resource_uri TEXT NOT NULL UNIQUE
        CHECK (resource_uri = 'chatbi://domain-definitions/' || definition_id),
    created_by_user_id TEXT NOT NULL
        CHECK (length(trim(created_by_user_id)) > 0),
    idempotency_key TEXT NOT NULL
        CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 200),
    request_hash TEXT NOT NULL
        CHECK (
            length(request_hash) = 64
            AND request_hash = lower(request_hash)
            AND request_hash NOT GLOB '*[^0-9a-f]*'
        ),
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, project_id, semantic_key, version),
    UNIQUE (tenant_id, project_id, idempotency_key),
    CHECK (effective_to IS NULL OR effective_to > effective_from),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (project_id, created_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_domain_definitions_resolution
    ON domain_definitions(
        tenant_id, project_id, semantic_key, effective_from, effective_to
    );
CREATE INDEX IF NOT EXISTS idx_domain_definitions_formula
    ON domain_definitions(formula_hash);

CREATE TABLE IF NOT EXISTS domain_field_mappings (
    mapping_id TEXT PRIMARY KEY
        CHECK (
            length(mapping_id) = 32
            AND mapping_id = lower(mapping_id)
            AND mapping_id NOT GLOB '*[^0-9a-f]*'
        ),
    tenant_id TEXT NOT NULL CHECK (length(trim(tenant_id)) > 0),
    project_id TEXT NOT NULL,
    dataset_ref TEXT NOT NULL,
    concept_key TEXT NOT NULL
        CHECK (
            length(concept_key) BETWEEN 1 AND 100
            AND concept_key = lower(concept_key)
            AND substr(concept_key, 1, 1) GLOB '[a-z]'
            AND concept_key NOT GLOB '*[^a-z0-9_.-]*'
        ),
    field_name TEXT NOT NULL CHECK (length(trim(field_name)) BETWEEN 1 AND 255),
    source_ref TEXT NOT NULL
        CHECK (
            length(trim(source_ref)) BETWEEN 1 AND 1000
            AND (
                source_ref GLOB 'https://*'
                OR source_ref GLOB 'urn:*'
                OR source_ref GLOB 'chatbi://*'
            )
        ),
    created_by_user_id TEXT NOT NULL
        CHECK (length(trim(created_by_user_id)) > 0),
    created_at TEXT NOT NULL,
    UNIQUE (tenant_id, project_id, dataset_ref, concept_key),
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
    FOREIGN KEY (dataset_ref) REFERENCES datasets(ref) ON DELETE CASCADE,
    FOREIGN KEY (project_id, created_by_user_id, tenant_id)
        REFERENCES project_memberships(project_id, user_id, tenant_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_domain_field_mappings_dataset
    ON domain_field_mappings(tenant_id, project_id, dataset_ref, concept_key);

CREATE TRIGGER IF NOT EXISTS trg_domain_definition_immutable
BEFORE UPDATE ON domain_definitions
BEGIN
    SELECT RAISE(ABORT, 'domain definition is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_domain_field_mapping_immutable
BEFORE UPDATE ON domain_field_mappings
BEGIN
    SELECT RAISE(ABORT, 'domain field mapping is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_domain_field_mapping_project_guard
BEFORE INSERT ON domain_field_mappings
WHEN NOT EXISTS (
    SELECT 1 FROM datasets
    WHERE ref = NEW.dataset_ref AND project_id = NEW.project_id
)
BEGIN
    SELECT RAISE(ABORT, 'domain field mapping crosses project boundary');
END;
""".strip()

CHECKSUM = hashlib.sha256(DDL.encode("utf-8")).hexdigest()

ADDED_TABLES = (
    "domain_field_mappings",
    "domain_definitions",
)

ADDED_INDEXES = (
    "idx_domain_definitions_resolution",
    "idx_domain_definitions_formula",
    "idx_domain_field_mappings_dataset",
)

ADDED_TRIGGERS = (
    "trg_domain_definition_immutable",
    "trg_domain_field_mapping_immutable",
    "trg_domain_field_mapping_project_guard",
)
