"""Stage-5A HTTP authorization and conflict-resolution contracts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from apps.api.deps import session_store_dep, settings_dep
from apps.api.main import app
from fastapi.testclient import TestClient
from packages.common.config import Settings
from packages.session.store import SessionStore

_ALICE_TOKEN = "domain-alice-token-0000000000000001"
_BOB_TOKEN = "domain-bob-token-000000000000000002"
_OTHER_TOKEN = "domain-other-token-0000000000000003"
_DATASET_REF = "a" * 32


@pytest.fixture
def domain_api(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, TestClient, TestClient, SessionStore, str]]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project(
        "领域定义 API", owner_user_id="alice", tenant_id="tenant-a"
    )
    store.register_dataset(
        ref=_DATASET_REF,
        project_id=project.id,
        filename="anonymous.xlsx",
        profile={
            "columns": [
                {"name": "bucket", "dtype": "string"},
                {"name": "value", "dtype": "number"},
            ]
        },
    )
    with sqlite3.connect(store.db_path) as connection:
        connection.execute(
            """
            INSERT INTO project_memberships(
                project_id, user_id, tenant_id, role, created_at
            ) VALUES (?, 'bob', 'tenant-a', 'viewer', ?)
            """,
            (project.id, project.created_at),
        )
    settings = Settings(
        auth_mode="bearer",
        auth_tokens_json=json.dumps(
            {
                _ALICE_TOKEN: {
                    "user_id": "alice",
                    "tenant_id": "tenant-a",
                    "roles": [],
                },
                _BOB_TOKEN: {
                    "user_id": "bob",
                    "tenant_id": "tenant-a",
                    "roles": [],
                },
                _OTHER_TOKEN: {
                    "user_id": "alice",
                    "tenant_id": "tenant-b",
                    "roles": [],
                },
            }
        ),
        chat_db_path=str(store.db_path),
        report_dir=str(tmp_path / "reports"),
    )
    app.dependency_overrides[session_store_dep] = lambda: store
    app.dependency_overrides[settings_dep] = lambda: settings
    try:
        yield (
            _client(_ALICE_TOKEN),
            _client(_BOB_TOKEN),
            _client(_OTHER_TOKEN),
            store,
            project.id,
        )
    finally:
        app.dependency_overrides.clear()


def test_api_publishes_maps_and_compiles_without_private_fields(
    domain_api: tuple[TestClient, TestClient, TestClient, SessionStore, str],
) -> None:
    alice, bob, other, _, project_id = domain_api
    created = alice.post(
        f"/projects/{project_id}/domain-definitions",
        headers={"Idempotency-Key": "api-definition-v1"},
        json=_definition_payload(),
    )
    assert created.status_code == 200
    body = created.json()
    assert body["outcome"] == "created"
    definition = body["definition"]
    assert definition["semantic_key"] == "metric.grouped_measure"
    assert definition["resource_uri"].startswith("chatbi://domain-definitions/")
    serialized = json.dumps(body, ensure_ascii=False)
    for private in ("tenant_id", "created_by_user_id", "idempotency_key", "request_hash"):
        assert private not in serialized

    for concept, field in (
        ("dimension.bucket", "bucket"),
        ("measure.value", "value"),
    ):
        response = alice.post(
            f"/projects/{project_id}/domain-definitions/field-mappings",
            json={
                "dataset_ref": _DATASET_REF,
                "concept_key": concept,
                "field_name": field,
                "source_ref": f"urn:api-map:{concept}",
            },
        )
        assert response.status_code == 200

    resolved = bob.get(
        f"/projects/{project_id}/domain-definitions/resolve",
        params={
            "semantic_key": "metric.grouped_measure",
            "as_of": "2026-06-01T00:00:00Z",
            "dataset_ref": _DATASET_REF,
        },
    )
    assert resolved.status_code == 200
    result = resolved.json()
    assert result["status"] == "resolved"
    assert result["requires_clarification"] is False
    assert result["compilation_status"] == "ready"
    assert result["compiled_invocation"]["tool_name"] == "aggregate_preview"
    assert result["compiled_invocation"]["arguments"]["group_col"] == "bucket"

    assert other.get(
        f"/projects/{project_id}/domain-definitions"
    ).status_code == 404
    assert bob.post(
        f"/projects/{project_id}/domain-definitions",
        headers={"Idempotency-Key": "viewer-denied"},
        json=_definition_payload(version=2),
    ).status_code == 404


def test_api_conflict_has_no_selected_winner_and_replays_safely(
    domain_api: tuple[TestClient, TestClient, TestClient, SessionStore, str],
) -> None:
    alice, _, _, _, project_id = domain_api
    first = alice.post(
        f"/projects/{project_id}/domain-definitions",
        headers={"Idempotency-Key": "api-definition-v1"},
        json=_definition_payload(),
    )
    replay = alice.post(
        f"/projects/{project_id}/domain-definitions",
        headers={"Idempotency-Key": "api-definition-v1"},
        json=_definition_payload(),
    )
    assert first.status_code == 200
    assert replay.json()["outcome"] == "replayed"
    assert replay.json()["definition"]["definition_id"] == first.json()["definition"][
        "definition_id"
    ]

    second_payload = _definition_payload(version=2)
    second_payload["effective_from"] = "2026-06-01T00:00:00Z"
    second_payload["formula"]["arguments"]["agg"] = "mean"
    conflict_write = alice.post(
        f"/projects/{project_id}/domain-definitions",
        headers={"Idempotency-Key": "api-definition-v2"},
        json=second_payload,
    )
    assert conflict_write.status_code == 200
    assert conflict_write.json()["outcome"] == "conflict"

    resolution = alice.get(
        f"/projects/{project_id}/domain-definitions/resolve",
        params={
            "semantic_key": "metric.grouped_measure",
            "as_of": "2026-07-01T00:00:00Z",
        },
    ).json()
    assert resolution["status"] == "conflict"
    assert resolution["requires_clarification"] is True
    assert resolution["compiled_invocation"] is None
    assert [item["version"] for item in resolution["candidates"]] == [1, 2]


def _definition_payload(*, version: int = 1) -> dict[str, Any]:
    return {
        "semantic_key": "metric.grouped_measure",
        "version": version,
        "title": "匿名分组度量",
        "description": "按匿名分组汇总匿名度量。",
        "formula": {
            "tool": "aggregate_preview",
            "arguments": {
                "group_concept": "dimension.bucket",
                "value_concept": "measure.value",
                "agg": "sum",
                "sort": "group",
            },
        },
        "grain": ["dimension.bucket"],
        "scope": {"dataset_type": "tabular"},
        "owner": "domain-owner",
        "source_ref": f"urn:domain-api:definition:v{version}",
        "effective_from": "2026-01-01T00:00:00Z",
    }


def _client(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})
