"""Bearer 认证与项目资源隔离回归测试。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from apps.api.deps import session_store_dep, settings_dep
from apps.api.main import app
from apps.orchestrator.control.contracts import build_minimal_contract
from fastapi.testclient import TestClient
from packages.common.config import Settings
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

_ALICE_TOKEN = "alice-secret-token-0000000000000001"
_BOB_TOKEN = "bob-secret-token-0000000000000000002"
_OTHER_TENANT_TOKEN = "alice-other-tenant-0000000000000003"
_DATASET_REF = "a" * 32


@pytest.fixture
def auth_clients(
    tmp_path: Path,
) -> Iterator[tuple[TestClient, TestClient, TestClient, SessionStore]]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    records = {
        _ALICE_TOKEN: {
            "user_id": "alice",
            "tenant_id": "tenant-a",
            "roles": ["kb_admin"],
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
    try:
        yield (
            TestClient(app, headers={"Authorization": f"Bearer {_ALICE_TOKEN}"}),
            TestClient(app, headers={"Authorization": f"Bearer {_BOB_TOKEN}"}),
            TestClient(
                app,
                headers={"Authorization": f"Bearer {_OTHER_TENANT_TOKEN}"},
            ),
            store,
        )
    finally:
        app.dependency_overrides.clear()


def test_bearer_mode_rejects_missing_and_invalid_tokens(
    auth_clients: tuple[TestClient, TestClient, TestClient, SessionStore],
) -> None:
    unauthenticated = TestClient(app)
    invalid = TestClient(app, headers={"Authorization": "Bearer invalid-token-value"})

    assert unauthenticated.get("/projects").status_code == 401
    response = invalid.get("/projects")
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert unauthenticated.get("/auth/config").json() == {"mode": "bearer"}


def test_project_lists_are_scoped_by_user_and_tenant(
    auth_clients: tuple[TestClient, TestClient, TestClient, SessionStore],
) -> None:
    alice, bob, same_user_other_tenant, _ = auth_clients
    alice_project = alice.post("/projects", json={"name": "Alice"}).json()
    bob_project = bob.post("/projects", json={"name": "Bob"}).json()

    assert [item["id"] for item in alice.get("/projects").json()] == [
        alice_project["id"]
    ]
    assert [item["id"] for item in bob.get("/projects").json()] == [bob_project["id"]]
    assert same_user_other_tenant.get("/projects").json() == []

    assert alice.get(f"/projects/{bob_project['id']}").status_code == 404
    assert bob.patch(
        f"/projects/{alice_project['id']}",
        json={"name": "越权修改"},
    ).status_code == 404


def test_conversation_and_dataset_cannot_cross_project_boundary(
    auth_clients: tuple[TestClient, TestClient, TestClient, SessionStore],
) -> None:
    alice, bob, _, store = auth_clients
    project = alice.post("/projects", json={"name": "Alice"}).json()
    conversation = alice.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "私有对话"},
    ).json()
    store.register_dataset(
        ref=_DATASET_REF,
        project_id=project["id"],
        filename="private.xlsx",
        profile={"row_count": 1, "column_count": 1, "columns": []},
    )

    assert bob.get(f"/conversations/{conversation['id']}").status_code == 404
    assert bob.patch(
        f"/datasets/{_DATASET_REF}",
        json={"filename": "stolen.xlsx"},
    ).status_code == 404
    assert bob.get(f"/projects/{project['id']}/datasets").status_code == 404

    assert alice.get(f"/conversations/{conversation['id']}").status_code == 200
    assert alice.patch(
        f"/datasets/{_DATASET_REF}",
        json={"filename": "owned.xlsx"},
    ).status_code == 200


def test_report_download_is_scoped_to_owning_project(
    auth_clients: tuple[TestClient, TestClient, TestClient, SessionStore],
    tmp_path: Path,
) -> None:
    alice, bob, _, store = auth_clients
    project = alice.post("/projects", json={"name": "Alice"}).json()
    report_id = "e" * 32
    report_dir = tmp_path / "reports"
    report_dir.mkdir()
    (report_dir / f"{report_id}.md").write_text("# private", encoding="utf-8")
    store.register_report_publication(
        report_id=report_id,
        project_id=project["id"],
    )

    assert bob.get(f"/analyze/report/{report_id}.md").status_code == 404
    response = alice.get(f"/analyze/report/{report_id}.md")
    assert response.status_code == 200
    assert response.text == "# private"


def test_task_control_write_is_project_scoped_and_idempotent(
    auth_clients: tuple[TestClient, TestClient, TestClient, SessionStore],
) -> None:
    alice, bob, _, store = auth_clients
    project = alice.post("/projects", json={"name": "Alice"}).json()
    conversation = alice.post(
        f"/projects/{project['id']}/conversations",
        json={"title": "受控任务"},
    ).json()
    _, message = store.start_user_turn(
        conversation_id=conversation["id"],
        content="分析数据",
        suggested_title="分析数据",
    )
    contract = build_minimal_contract(
        run_id="private-controlled-run",
        user_text="分析数据",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    tasks = TaskStore(store.db_path)
    run, _ = tasks.create_run(
        project_id=project["id"],
        conversation_id=conversation["id"],
        user_message_id=message.id,
        contract=contract,
        budget={"max_tool_calls": 2},
    )
    detail_path = f"/agent/runs/{run.run_id}"
    detail = alice.get(detail_path)
    assert detail.status_code == 200
    assert detail.json()["tool_audits"] == []
    assert bob.get(detail_path).status_code == 404
    headers = {
        "Idempotency-Key": "cancel-private-run",
        "If-Match": str(run.state_version),
    }

    assert bob.post(
        f"/agent/runs/{run.run_id}/cancel",
        headers=headers,
    ).status_code == 404
    cancelled = alice.post(
        f"/agent/runs/{run.run_id}/cancel",
        headers=headers,
    )
    replayed = alice.post(
        f"/agent/runs/{run.run_id}/cancel",
        headers=headers,
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["run"]["status"] == "cancelled"
    assert cancelled.json()["replayed"] is False
    assert replayed.status_code == 200
    assert replayed.json()["replayed"] is True
    assert len(
        [
            event
            for event in tasks.list_events(run.run_id)
            if event.event_type == "run.cancelled"
        ]
    ) == 1

    feedback_path = f"/agent/runs/{run.run_id}/feedback"
    feedback_headers = {
        "Idempotency-Key": "feedback-private-run",
        "If-Match": str(cancelled.json()["run"]["state_version"]),
    }
    feedback_body = {
        "rating": "helpful",
        "comment": "结果符合预期",
        "evidence_ids": [],
        "artifact_ids": [],
    }
    assert bob.post(
        feedback_path,
        headers=feedback_headers,
        json=feedback_body,
    ).status_code == 404
    feedback = alice.post(
        feedback_path,
        headers=feedback_headers,
        json=feedback_body,
    )
    feedback_replay = alice.post(
        feedback_path,
        headers=feedback_headers,
        json=feedback_body,
    )
    assert feedback.status_code == 200
    assert feedback.json()["feedback"]["rating"] == "helpful"
    assert feedback.json()["replayed"] is False
    assert feedback_replay.status_code == 200
    assert feedback_replay.json()["replayed"] is True
    refreshed_detail = alice.get(detail_path).json()
    assert refreshed_detail["feedback"] == [feedback.json()["feedback"]]
    assert refreshed_detail["related_runs"][0]["run_id"] == run.run_id

    latest_path = f"/agent/runs/by-conversation/{conversation['id']}/latest"
    latest = alice.get(latest_path)
    assert latest.status_code == 200
    assert latest.json()["run"]["run_id"] == run.run_id
    assert bob.get(latest_path).status_code == 404

    reconnect_path = f"/agent/runs/{run.run_id}/stream"
    assert bob.get(reconnect_path).status_code == 404
    reconnect = alice.get(
        reconnect_path,
        headers={"Last-Event-ID": f"{run.run_id}:1"},
    )
    assert reconnect.status_code == 200
    assert reconnect.text.count("event: run.cancelled") == 1
    assert reconnect.text.count("event: done") == 1
    assert alice.get(
        reconnect_path,
        headers={"Last-Event-ID": "another-run:1"},
    ).status_code == 400
