"""ChatBI SQLite schema migration runner.

Migrations are intentionally append-only.  Application repositories never create
control-plane tables ad hoc; every database reaches the current schema through
``migrate_database`` so an existing v1 workspace and a fresh installation follow
the same path.
"""

from packages.session.migrations.runner import (
    CURRENT_SCHEMA_VERSION,
    downgrade_v2_to_v1,
    migrate_database,
)

__all__ = ["CURRENT_SCHEMA_VERSION", "downgrade_v2_to_v1", "migrate_database"]
