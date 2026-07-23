"""Structured audit events for policy and sensitive execution decisions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from packages.common.logging import get_logger
from packages.session.models import JsonObject

AuditOutcome = Literal["allowed", "denied", "error"]
_log = get_logger("governance.audit")


@dataclass(frozen=True, slots=True)
class AuditEvent:
    """A bounded audit record; raw tool arguments and secrets are forbidden."""

    actor: str
    action: str
    resource: str
    outcome: AuditOutcome
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    tenant_id: str | None = None
    project_id: str | None = None
    run_id: str | None = None
    invocation_id: str | None = None
    detail: JsonObject = field(default_factory=dict)
    occurred_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )

    def to_dict(self) -> JsonObject:
        return {
            "schema_version": 1,
            "event_id": self.event_id,
            "actor": self.actor,
            "action": self.action,
            "resource": self.resource,
            "outcome": self.outcome,
            "tenant_id": self.tenant_id,
            "project_id": self.project_id,
            "run_id": self.run_id,
            "invocation_id": self.invocation_id,
            "detail": self.detail,
            "occurred_at": self.occurred_at,
        }


def record(event: AuditEvent) -> None:
    """Emit an audit event to the configured structured-log sink.

    v2.4 also persists the corresponding policy/Observation facts in TaskEvent.
    Enterprise append-only audit storage remains a v3.0 backend concern.
    """
    _log.info("audit.event", **event.to_dict())
