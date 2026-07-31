"""v2.5 阶段 3D 项目记忆治理 API 的权限、并发与快照回归。"""

from __future__ import annotations

import hashlib
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
from packages.governance.permissions import Principal
from packages.session.memory_models import MemoryDraft
from packages.session.memory_store import MemoryStore
from packages.session.store import SessionStore

_ALICE = Principal(user_id="alice", tenant_id="tenant-a")
_ALICE_TOKEN = "memory-alice-token-0000000000000001"
_BOB_TOKEN = "memory-bob-token-000000000000000002"
_OTHER_TENANT_TOKEN = "memory-other-token-0000000000000003"


@pytest.fixture
def memory_api(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, TestClient, TestClient, SessionStore, dict[str, Any]]]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    project = store.create_project(
        "记忆治理项目",
        owner_user_id=_ALICE.user_id,
        tenant_id=_ALICE.tenant_scope,
    )
    conversation = store.create_conversation(project.id, "口径确认")
    message = store.append_message(
        conversation_id=conversation.id,
        role="user",
        content="以后将客户编号称为客户 ID",
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

    memories = MemoryStore(store, audit_recorder=lambda _event: None)
    project_memory = memories.remember(
        project_id=project.id,
        principal=_ALICE,
        draft=_draft(
            message.id,
            message.content,
            scope="project",
            semantic_key="field-alias.customer-id",
        ),
        idempotency_key="seed-project-memory",
    ).record
    conversation_memory = memories.remember(
        project_id=project.id,
        principal=_ALICE,
        draft=_draft(
            message.id,
            message.content,
            scope="conversation",
            conversation_id=conversation.id,
            semantic_key="conversation.customer-id",
        ),
        idempotency_key="seed-conversation-memory",
    ).record
    subject_memory = memories.remember(
        project_id=project.id,
        principal=_ALICE,
        draft=_draft(
            message.id,
            message.content,
            scope="subject",
            semantic_key="subject.customer-id",
        ),
        idempotency_key="seed-subject-memory",
    ).record
    memories.add_link(
        project_memory.memory_id,
        project_id=project.id,
        principal=_ALICE,
        target_type="message",
        target_ref=message.id,
    )

    records = {
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
        _OTHER_TENANT_TOKEN: {
            "user_id": "alice",
            "tenant_id": "tenant-b",
            "roles": [],
        },
    }
    settings = Settings(
        auth_mode="bearer",
        auth_tokens_json=json.dumps(records),
        chat_db_path=str(tmp_path / "chatbi.db"),
        report_dir=str(tmp_path / "reports"),
    )
    app.dependency_overrides[session_store_dep] = lambda: store
    app.dependency_overrides[settings_dep] = lambda: settings
    seeded = {
        "project_id": project.id,
        "conversation_id": conversation.id,
        "message_id": message.id,
        "project_memory": project_memory,
        "conversation_memory": conversation_memory,
        "subject_memory": subject_memory,
    }
    try:
        yield (
            _client(_ALICE_TOKEN),
            _client(_BOB_TOKEN),
            _client(_OTHER_TENANT_TOKEN),
            store,
            seeded,
        )
    finally:
        app.dependency_overrides.clear()


def test_governance_list_includes_all_conversations_and_hides_private_fields(
    memory_api: tuple[TestClient, TestClient, TestClient, SessionStore, dict[str, Any]],
) -> None:
    alice, bob, other_tenant, store, seeded = memory_api
    project_id = seeded["project_id"]

    response = alice.get(f"/projects/{project_id}/memories")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert {
        item["memory_id"] for item in body["items"]
    } == {
        seeded["project_memory"].memory_id,
        seeded["conversation_memory"].memory_id,
        seeded["subject_memory"].memory_id,
    }
    project_item = next(
        item
        for item in body["items"]
        if item["memory_id"] == seeded["project_memory"].memory_id
    )
    assert project_item["links"] == [
        {"target_type": "message", "target_ref": seeded["message_id"]}
    ]
    for private_field in (
        "tenant_id",
        "subject_user_id",
        "scope_key",
        "semantic_key",
        "source_ref",
        "source_hash",
        "created_by_user_id",
    ):
        assert private_field not in project_item

    viewer = bob.get(f"/projects/{project_id}/memories").json()
    assert viewer["total"] == 2
    assert seeded["subject_memory"].memory_id not in {
        item["memory_id"] for item in viewer["items"]
    }
    assert other_tenant.get(f"/projects/{project_id}/memories").status_code == 404

    conversation_only = alice.get(
        f"/projects/{project_id}/memories",
        params={"conversation_id": seeded["conversation_id"], "scope": "conversation"},
    ).json()
    assert conversation_only["total"] == 1
    assert conversation_only["items"][0]["memory_id"] == (
        seeded["conversation_memory"].memory_id
    )
    other_project = store.create_project(
        "其他项目",
        owner_user_id=_ALICE.user_id,
        tenant_id=_ALICE.tenant_scope,
    )
    other_conversation = store.create_conversation(other_project.id)
    assert alice.get(
        f"/projects/{project_id}/memories",
        params={"conversation_id": other_conversation.id},
    ).status_code == 404


def test_revision_is_immutable_idempotent_and_preserves_links_and_snapshot(
    memory_api: tuple[TestClient, TestClient, TestClient, SessionStore, dict[str, Any]],
) -> None:
    alice, bob, _, store, seeded = memory_api
    project_id = seeded["project_id"]
    current = seeded["project_memory"]
    memories = MemoryStore(store, audit_recorder=lambda _event: None)
    snapshot, frozen_records = memories.create_snapshot(
        project_id=project_id,
        principal=_ALICE,
        conversation_id=seeded["conversation_id"],
    )
    payload = {
        "expected_version": current.version,
        "content_summary": "客户编号统一展示为客户标识",
        "confidence": 0.98,
        "expires_at": "2099-01-01T00:00:00Z",
    }
    headers = {"Idempotency-Key": "api-revise-project-memory"}

    assert bob.patch(
        f"/projects/{project_id}/memories/{current.memory_id}",
        json=payload,
        headers=headers,
    ).status_code == 404

    response = alice.patch(
        f"/projects/{project_id}/memories/{current.memory_id}",
        json=payload,
        headers=headers,
    )
    assert response.status_code == 200
    result = response.json()
    assert result["outcome"] == "revised"
    revised = result["memory"]
    assert revised["memory_id"] != current.memory_id
    assert revised["version"] == 2
    assert revised["supersedes_id"] == current.memory_id
    assert revised["links"] == [
        {"target_type": "message", "target_ref": seeded["message_id"]}
    ]

    replay = alice.patch(
        f"/projects/{project_id}/memories/{current.memory_id}",
        json=payload,
        headers=headers,
    )
    assert replay.status_code == 200
    assert replay.json()["outcome"] == "replayed"
    assert replay.json()["memory"]["memory_id"] == revised["memory_id"]

    changed_payload = {**payload, "content_summary": "不同请求"}
    assert alice.patch(
        f"/projects/{project_id}/memories/{current.memory_id}",
        json=changed_payload,
        headers=headers,
    ).status_code == 409
    assert alice.patch(
        f"/projects/{project_id}/memories/{current.memory_id}",
        json=payload,
        headers={"Idempotency-Key": "api-stale-revision"},
    ).status_code == 409

    old = alice.get(
        f"/projects/{project_id}/memories/{current.memory_id}"
    ).json()
    assert old["status"] == "superseded"
    frozen_after = memories.get_snapshot(snapshot.memory_snapshot_id, principal=_ALICE)
    assert frozen_after == (snapshot, frozen_records)
    assert any(
        record.memory_id == current.memory_id
        and record.content_summary == current.content_summary
        for record in frozen_after[1]
    )


def test_delete_replays_and_revision_policy_rejects_secrets(
    memory_api: tuple[TestClient, TestClient, TestClient, SessionStore, dict[str, Any]],
) -> None:
    alice, _, _, _, seeded = memory_api
    project_id = seeded["project_id"]
    current = seeded["project_memory"]
    secret_payload = {
        "expected_version": current.version,
        "content_summary": "Authorization: Bearer top-secret-token",
        "confidence": 0.9,
        "expires_at": None,
    }
    assert alice.patch(
        f"/projects/{project_id}/memories/{current.memory_id}",
        json=secret_payload,
        headers={"Idempotency-Key": "api-secret-revision"},
    ).status_code == 422

    path = f"/projects/{project_id}/memories/{current.memory_id}"
    params = {"expected_version": current.version}
    headers = {"Idempotency-Key": "api-delete-project-memory"}
    deleted = alice.delete(path, params=params, headers=headers)
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    replayed = alice.delete(path, params=params, headers=headers)
    assert replayed.status_code == 200
    assert replayed.json() == deleted.json()
    assert alice.delete(
        path,
        params=params,
        headers={"Idempotency-Key": "api-stale-delete"},
    ).status_code == 409


def _client(token: str) -> TestClient:
    return TestClient(app, headers={"Authorization": f"Bearer {token}"})


def _draft(
    source_ref: str,
    source_content: str,
    *,
    scope: str,
    semantic_key: str,
    conversation_id: str | None = None,
) -> MemoryDraft:
    return MemoryDraft(
        scope=scope,  # type: ignore[arg-type]
        kind="field_alias",
        semantic_key=semantic_key,
        content_summary="客户编号的展示名称是客户 ID",
        source_type="user_confirmation",
        source_ref=source_ref,
        source_hash=hashlib.sha256(source_content.encode("utf-8")).hexdigest(),
        confidence=0.95,
        conversation_id=conversation_id,
    )
