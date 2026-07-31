"""v2.5 阶段 3E 血缘 HTTP 契约与身份隔离测试。"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from apps.api.deps import session_store_dep, settings_dep
from apps.api.main import app
from fastapi.testclient import TestClient
from packages.common.config import Settings
from packages.session.store import SessionStore

_ALICE_TOKEN = "lineage-alice-token-000000000000001"
_BOB_TOKEN = "lineage-bob-token-00000000000000002"
_OTHER_TOKEN = "lineage-other-token-0000000000000003"
_DATASET_REF = "d" * 32


@pytest.fixture
def lineage_api(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, TestClient, TestClient, SessionStore, str, str]]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project(
        "血缘 API",
        owner_user_id="alice",
        tenant_id="tenant-a",
    )
    conversation = store.create_conversation(project.id)
    message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="画像完成",
    )
    store.register_dataset(
        ref=_DATASET_REF,
        project_id=project.id,
        filename="objects.xlsx",
        profile={"row_count": 1},
    )
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="profile",
        payload={"row_count": 1, "private_sample": ["不应进入血缘"]},
        source_tool="get_data_profile",
        params={"analysis_id": "profile-v1", "secret_param": "不应进入血缘"},
        dataset_ref=_DATASET_REF,
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
        chat_db_path=str(tmp_path / "chatbi.db"),
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
            conversation.id,
        )
    finally:
        app.dependency_overrides.clear()


def test_lineage_api_is_safe_and_viewer_readable(
    lineage_api: tuple[
        TestClient,
        TestClient,
        TestClient,
        SessionStore,
        str,
        str,
    ],
) -> None:
    alice, bob, other, _, project_id, conversation_id = lineage_api
    response = alice.get(f"/projects/{project_id}/lineage")
    assert response.status_code == 200
    body = response.json()
    assert body["integrity_status"] == "ok"
    assert body["truncated"] is False
    assert len(body["graph_hash"]) == 64
    assert {node["node_type"] for node in body["nodes"]} == {
        "dataset",
        "artifact",
    }
    assert [edge["relation"] for edge in body["edges"]] == ["profiled_as"]
    serialized = json.dumps(body, ensure_ascii=False)
    assert "private_sample" not in serialized
    assert "secret_param" not in serialized
    assert "payload" not in serialized
    assert "file_ref" not in serialized

    assert bob.get(f"/projects/{project_id}/lineage").status_code == 200
    assert other.get(f"/projects/{project_id}/lineage").status_code == 404
    assert alice.get(
        f"/projects/{project_id}/lineage",
        params={"conversation_id": conversation_id},
    ).status_code == 200


def test_lineage_api_rejects_cross_project_conversation(
    lineage_api: tuple[
        TestClient,
        TestClient,
        TestClient,
        SessionStore,
        str,
        str,
    ],
) -> None:
    alice, _, _, store, project_id, _ = lineage_api
    other_project = store.create_project(
        "其他项目",
        owner_user_id="alice",
        tenant_id="tenant-a",
    )
    other_conversation = store.create_conversation(other_project.id)

    response = alice.get(
        f"/projects/{project_id}/lineage",
        params={"conversation_id": other_conversation.id},
    )
    assert response.status_code == 404


def _client(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})
