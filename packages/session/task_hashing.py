"""Stable hashes shared by TaskStore and orchestration callers."""

from __future__ import annotations

import hashlib
import json

from packages.session.models import JsonObject


def canonical_json(value: object) -> str:
    """Serialize a value using TaskStore's persisted canonical JSON format."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def invocation_arguments_hash(arguments: JsonObject) -> str:
    """Return the canonical hash persisted for tool invocation arguments."""
    return hashlib.sha256(canonical_json(arguments).encode("utf-8")).hexdigest()


def invocation_idempotency_key(
    run_id: str,
    tool_call_id: str,
    tool_name: str,
    arguments: JsonObject,
) -> str:
    material = ":".join((run_id, tool_call_id, tool_name, canonical_json(arguments)))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
