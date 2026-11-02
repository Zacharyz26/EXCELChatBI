"""Frozen R4 event/reopen holdouts; NOT_RUN, root owns execution.

Real upload/Host/tools/SQLite; the model alone is scripted. No invented Evidence
or completed state. The timestamp tie uses a declared test clock for two actual
uploads, not a fabricated source event. These are not real-model/browser tests.
"""

import sqlite3
from dataclasses import asdict

import pytest
from packages.session.store import SessionStore
from packages.session.task_store import TaskStore

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401
from tests.test_r4_local_chain_draft import api_request, conversation_detail


def snapshot(path):
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return "\n".join(connection.iterdump())


async def actual_upload(store, project, conversation, name):
    response = await api_request(
        store,
        "POST",
        "/upload/excel",
        data={"project_id": project.id, "conversation_id": conversation.id},
        files={"file": (name, support.OTHER, "text/csv")},
    )
    assert response.status_code == 200, response.text
    return response.json()["dataset_ref"]


@pytest.mark.asyncio
async def test_later_same_conversation_upload_supersedes_proven_child(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    _, child, first = await support.filtered_source(store, project, conversation)
    latest = await actual_upload(store, project, conversation, "later-source.csv")
    reopened = SessionStore(store.db_path)
    before = snapshot(store.db_path)
    detail = await conversation_detail(reopened, conversation)
    resolved = support.resolve(reopened, project, conversation, "查看当前数据集。")
    projection = support.execution_projection(detail)
    support.write_witness(
        tmp_path,
        "later-upload",
        first=first,
        child=child,
        latest=latest,
        detail=detail,
        resolved=asdict(resolved),
    )
    assert latest != child
    assert projection["dataset_ref"] == latest and projection["source_kind"] == "upload"
    assert resolved.status == "resolved" and support.ref_set(resolved) == {latest}
    assert TaskStore(reopened.db_path).get_run(first["run"]["run_id"]).status == "completed"
    assert snapshot(store.db_path) == before


@pytest.mark.asyncio
async def test_deleted_later_upload_is_unavailable_not_previous_proven_child(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    original, child, first = await support.filtered_source(store, project, conversation)
    latest = await actual_upload(store, project, conversation, "deleted-later-source.csv")
    assert store.delete_dataset(latest)
    reopened = SessionStore(store.db_path)
    before = snapshot(store.db_path)
    detail = await conversation_detail(reopened, conversation)
    resolved = support.resolve(reopened, project, conversation, "查看当前数据集。")
    projection = support.execution_projection(detail)
    support.write_witness(
        tmp_path,
        "deleted-later-upload",
        original=original,
        child=child,
        deleted=latest,
        first=first,
        detail=detail,
        resolved=asdict(resolved),
    )
    assert projection["availability"] == "unavailable" and projection["dataset_ref"] is None
    assert resolved.status in {"unresolved", "ambiguous"} and not support.ref_set(resolved)
    assert store.get_dataset(child) is not None  # a valid parent cannot hide latest-source loss
    assert snapshot(store.db_path) == before


@pytest.mark.asyncio
async def test_historical_run_get_keeps_requested_run_and_separate_latest_context(tmp_path):
    store, project, conversation = support.workspace(tmp_path)
    _, child, first = await support.filtered_source(store, project, conversation)
    second = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel([("get_data_profile", {"dataset_ref": child})], "已完成画像。"),
        "查看当前数据集的画像。",
    )
    assert second["run"]["status"] == "completed", second
    reopened = SessionStore(store.db_path)
    before = snapshot(store.db_path)
    response = await api_request(reopened, "GET", f"/agent/runs/{first['run']['run_id']}")
    assert response.status_code == 200, response.text
    detail = response.json()
    latest_detail = await conversation_detail(reopened, conversation)
    support.write_witness(
        tmp_path,
        "historical-run",
        first=first,
        second=second,
        detail=detail,
        conversation_detail=latest_detail,
    )
    assert detail["run"]["run_id"] == first["run"]["run_id"]
    assert detail["run"]["status"] == first["run"]["status"]
    projection = support.execution_projection(latest_detail)
    assert projection["run_id"] == second["run"]["run_id"]
    assert projection["run_status"] == "completed" and projection["dataset_ref"] == child
    if "execution_context" in detail:
        assert detail["execution_context"] == projection
    assert snapshot(store.db_path) == before


@pytest.mark.asyncio
async def test_equal_time_distinct_actual_uploads_remain_ambiguous(tmp_path, monkeypatch):
    store, project, conversation = support.workspace(tmp_path)
    monkeypatch.setattr("packages.session.store._utc_now", lambda: "2026-11-01T12:00:00Z")
    first = await actual_upload(store, project, conversation, "tie-one.csv")
    second = await actual_upload(store, project, conversation, "tie-two.csv")
    assert first != second
    reopened = SessionStore(store.db_path)
    before = snapshot(store.db_path)
    detail = await conversation_detail(reopened, conversation)
    projection = support.execution_projection(detail)
    resolved = support.resolve(reopened, project, conversation, "查看当前数据集。")
    support.write_witness(
        tmp_path,
        "equal-event-time",
        first=first,
        second=second,
        detail=detail,
        resolved=asdict(resolved),
        fixture="declared equal clock; actual uploads",
    )
    assert projection["availability"] == "ambiguous" and projection["dataset_ref"] is None
    assert resolved.status in {"ambiguous", "unresolved"} and not support.ref_set(resolved)
    assert snapshot(store.db_path) == before
