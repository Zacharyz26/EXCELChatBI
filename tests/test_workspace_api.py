"""阶段 1 第二步：项目/对话 CRUD 与上传关联 API 测试。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pytest
from apps.api.deps import excel_tools_dep, session_store_dep, settings_dep
from apps.api.main import app
from fastapi.testclient import TestClient
from packages.common.config import Settings
from packages.session.store import SessionStore

_XLSX_CT = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_PROFILE = {
    "dataset_ref": "fake-dataset",
    "row_count": 3,
    "column_count": 2,
    "columns": [
        {"name": "地区", "dtype": "string"},
        {"name": "销售额", "dtype": "number"},
    ],
    "sample_rows": [],
}


class _StaticTool:
    def __init__(self, result: object) -> None:
        self._result = result

    def invoke(self, _args: dict[str, Any]) -> object:
        return self._result


class _Profile:
    def to_dict(self) -> dict[str, Any]:
        return dict(_PROFILE)


class _FakeExcelServer:
    def __init__(self) -> None:
        self._tools = {
            "parse_excel": _StaticTool({"dataset_ref": "fake-dataset"}),
            "infer_schema": _StaticTool(_Profile()),
        }


@pytest.fixture
def workspace_client(tmp_path: Path) -> Iterator[tuple[TestClient, SessionStore, Path]]:
    store = SessionStore(str(tmp_path / "chatbi.db"))
    upload_dir = tmp_path / "uploads"
    app.dependency_overrides[session_store_dep] = lambda: store
    app.dependency_overrides[settings_dep] = lambda: Settings(
        upload_dir=str(upload_dir),
        chat_db_path=str(tmp_path / "chatbi.db"),
    )
    app.dependency_overrides[excel_tools_dep] = _FakeExcelServer
    try:
        yield TestClient(app), store, upload_dir
    finally:
        app.dependency_overrides.clear()


def _create_project(client: TestClient, name: str = "销售项目") -> dict[str, Any]:
    response = client.post("/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def _create_conversation(
    client: TestClient,
    project_id: str,
    title: str = "新对话",
) -> dict[str, Any]:
    response = client.post(
        f"/projects/{project_id}/conversations",
        json={"title": title},
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


def test_project_and_conversation_crud(
    workspace_client: tuple[TestClient, SessionStore, Path],
) -> None:
    client, _, _ = workspace_client
    project = _create_project(client, "  全国销售  ")
    assert project["name"] == "全国销售"
    assert client.get(f"/projects/{project['id']}").json() == project
    assert client.get("/projects").json() == [project]

    renamed = client.patch(
        f"/projects/{project['id']}", json={"name": "区域销售"}
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "区域销售"

    first = _create_conversation(client, project["id"], "第一条")
    second = _create_conversation(client, project["id"], "第二条")
    conversations = client.get(f"/projects/{project['id']}/conversations")
    assert conversations.status_code == 200
    assert [item["id"] for item in conversations.json()] == [second["id"], first["id"]]

    detail = client.get(f"/conversations/{first['id']}")
    assert detail.status_code == 200
    assert detail.json() == {
        "conversation": first,
        "messages": [],
        "artifacts": [],
    }

    renamed_conversation = client.patch(
        f"/conversations/{first['id']}", json={"title": "月度趋势"}
    )
    assert renamed_conversation.status_code == 200
    assert renamed_conversation.json()["title"] == "月度趋势"

    deleted = client.delete(f"/conversations/{first['id']}")
    assert deleted.status_code == 204 and deleted.content == b""
    assert client.get(f"/conversations/{first['id']}").status_code == 404

    deleted_project = client.delete(f"/projects/{project['id']}")
    assert deleted_project.status_code == 204 and deleted_project.content == b""
    assert client.get(f"/projects/{project['id']}").status_code == 404
    assert client.get(f"/conversations/{second['id']}").status_code == 404


def test_workspace_crud_validation_and_missing_resources(
    workspace_client: tuple[TestClient, SessionStore, Path],
) -> None:
    client, _, _ = workspace_client
    assert client.post("/projects", json={"name": "   "}).status_code == 422
    assert client.patch("/projects/missing", json={"name": "项目"}).status_code == 404
    assert client.delete("/projects/missing").status_code == 404
    assert client.get("/projects/missing/datasets").status_code == 404
    assert client.get("/projects/missing/conversations").status_code == 404
    assert (
        client.post("/projects/missing/conversations", json={"title": "对话"}).status_code
        == 404
    )
    assert client.patch("/conversations/missing", json={"title": "对话"}).status_code == 404
    assert client.delete("/conversations/missing").status_code == 404


def test_linked_upload_persists_dataset_messages_and_profile_artifact(
    workspace_client: tuple[TestClient, SessionStore, Path],
) -> None:
    client, store, upload_dir = workspace_client
    project = _create_project(client)
    conversation = _create_conversation(client, project["id"], "上传分析")

    response = client.post(
        "/upload/excel",
        data={
            "project_id": project["id"],
            "conversation_id": conversation["id"],
        },
        files={"file": ("../../销售.xlsx", b"fake excel", _XLSX_CT)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset_ref"] == "fake-dataset"
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert body["messages"][0]["content"] == "上传了文件：销售.xlsx"
    assert "3 行、2 列" in body["messages"][1]["content"]
    assert body["artifact"]["type"] == "profile"
    assert body["artifact"]["payload"] == _PROFILE
    assert list(upload_dir.glob("*_销售.xlsx"))

    datasets = client.get(f"/projects/{project['id']}/datasets")
    assert datasets.status_code == 200
    assert datasets.json()[0]["filename"] == "销售.xlsx"
    assert datasets.json()[0]["profile"] == _PROFILE

    detail = client.get(f"/conversations/{conversation['id']}")
    assert detail.status_code == 200
    assert detail.json()["messages"] == body["messages"]
    assert detail.json()["artifacts"] == [body["artifact"]]
    assert store.get_dataset("fake-dataset") is not None


def test_legacy_upload_keeps_original_response_shape(
    workspace_client: tuple[TestClient, SessionStore, Path],
) -> None:
    client, store, _ = workspace_client

    response = client.post(
        "/upload/excel",
        files={"file": ("legacy.xlsx", b"fake excel", _XLSX_CT)},
    )

    assert response.status_code == 200, response.text
    assert set(response.json()) == {"dataset_ref", "profile"}
    assert store.get_dataset("fake-dataset") is None


def test_upload_link_requires_matching_project_and_conversation(
    workspace_client: tuple[TestClient, SessionStore, Path],
) -> None:
    client, store, upload_dir = workspace_client
    first_project = _create_project(client, "项目一")
    second_project = _create_project(client, "项目二")
    conversation = _create_conversation(client, second_project["id"])

    incomplete = client.post(
        "/upload/excel",
        data={"project_id": first_project["id"]},
        files={"file": ("incomplete.xlsx", b"fake", _XLSX_CT)},
    )
    assert incomplete.status_code == 422
    assert "必须同时提供" in incomplete.json()["detail"]

    mismatch = client.post(
        "/upload/excel",
        data={
            "project_id": first_project["id"],
            "conversation_id": conversation["id"],
        },
        files={"file": ("mismatch.xlsx", b"fake", _XLSX_CT)},
    )
    assert mismatch.status_code == 422
    assert mismatch.json()["detail"] == "对话不属于指定项目"
    assert not upload_dir.exists()
    assert store.get_dataset("fake-dataset") is None
