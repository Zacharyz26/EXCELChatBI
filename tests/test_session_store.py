"""阶段 1 SQLite 会话持久层与 LRU 热缓存测试。"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from packages.session import ConversationCache, SessionStore


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    return SessionStore(str(tmp_path / "chatbi.db"), cache_size=2)


def test_schema_initializes_and_reopens(tmp_path: Path) -> None:
    db_path = tmp_path / "nested" / "chatbi.db"
    first = SessionStore(str(db_path))
    project = first.create_project("中文销售项目")

    second = SessionStore(str(db_path))

    assert db_path.exists()
    assert second.schema_version == 2
    assert second.get_project(project.id) == project
    with sqlite3.connect(db_path) as connection:
        journal_mode = connection.execute("PRAGMA journal_mode").fetchone()
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert journal_mode is not None and journal_mode[0] == "wal"
    assert {
        "projects",
        "datasets",
        "conversations",
        "messages",
        "artifacts",
        "schema_migrations",
        "task_runs",
        "task_contracts",
        "task_events",
        "task_snapshots",
        "tool_invocations",
        "evidence",
        "checkpoints",
    } <= tables


def test_unknown_schema_version_is_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "future.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA user_version = 99")

    with pytest.raises(RuntimeError, match="不支持的 ChatBI 数据库版本 99"):
        SessionStore(str(db_path))


def test_project_crud_and_validation(store: SessionStore) -> None:
    first = store.create_project("  销售分析  ")
    second = store.create_project("运营分析")

    assert first.name == "销售分析"
    assert store.list_projects() == [first, second]
    updated = store.update_project(first.id, "全国销售")
    assert updated is not None and updated.name == "全国销售"
    assert store.update_project("missing", "无效") is None
    with pytest.raises(ValueError, match="项目名称不能为空"):
        store.create_project("  ")
    assert store.delete_project(second.id) is True
    assert store.delete_project(second.id) is False


def test_dataset_json_lineage_and_project_guard(store: SessionStore) -> None:
    project = store.create_project("销售")
    other_project = store.create_project("运营")
    profile = {
        "row_count": 3,
        "columns": [{"name": "销售额", "dtype": "number"}],
        "summary": "中文画像",
    }
    parent = store.register_dataset(
        ref="sales-v1",
        project_id=project.id,
        filename="销售数据.xlsx",
        profile=profile,
    )
    child = store.register_dataset(
        ref="sales-v2",
        project_id=project.id,
        filename="销售数据-清洗.xlsx",
        profile=profile,
        parent_ref=parent.ref,
        transform={"drop_nulls": ["销售额"]},
    )

    assert store.get_dataset(parent.ref) == parent
    assert store.get_dataset(child.ref) == child
    assert store.list_datasets(project.id) == [parent, child]
    with pytest.raises(ValueError, match="必须属于同一项目"):
        store.register_dataset(
            ref="wrong-child",
            project_id=other_project.id,
            filename="wrong.xlsx",
            profile={},
            parent_ref=parent.ref,
        )
    with pytest.raises(sqlite3.IntegrityError):
        store.register_dataset(
            ref="orphan",
            project_id="missing",
            filename="orphan.xlsx",
            profile={},
        )


def test_deleting_parent_dataset_clears_lineage_reference(
    store: SessionStore, tmp_path: Path
) -> None:
    project = store.create_project("血缘")
    store.register_dataset(
        ref="parent", project_id=project.id, filename="a.xlsx", profile={}
    )
    store.register_dataset(
        ref="child",
        project_id=project.id,
        filename="b.xlsx",
        profile={},
        parent_ref="parent",
        transform={"deduplicate": True},
    )

    with sqlite3.connect(tmp_path / "chatbi.db") as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("DELETE FROM datasets WHERE ref = 'parent'")

    child = store.get_dataset("child")
    assert child is not None and child.parent_ref is None


def test_conversation_messages_artifacts_roundtrip_and_cache(store: SessionStore) -> None:
    project = store.create_project("销售")
    dataset = store.register_dataset(
        ref="sales", project_id=project.id, filename="销售.xlsx", profile={"rows": 20}
    )
    conversation = store.create_conversation(project.id, "数据概览")
    user_message = store.append_message(
        conversation_id=conversation.id,
        role="user",
        content="请介绍数据",
    )
    assistant_message = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="已生成画像。",
        tool_calls=[{"id": "call-1", "name": "get_data_profile"}],
    )
    artifact = store.create_artifact(
        conversation_id=conversation.id,
        message_id=assistant_message.id,
        type="profile",
        payload={"字段": ["地区", "销售额"], "空值率": 0.0},
        source_tool="infer_schema",
        params={"dataset_ref": dataset.ref},
        dataset_ref=dataset.ref,
    )

    assert store.list_messages(conversation.id) == [user_message, assistant_message]
    assert store.list_artifacts(conversation.id) == [artifact]
    first_context = store.load_conversation_context(conversation.id)
    assert first_context is not None
    assert first_context.messages == (user_message, assistant_message)
    assert first_context.artifacts == (artifact,)
    assert store.load_conversation_context(conversation.id) is first_context

    next_message = store.append_message(
        conversation_id=conversation.id, role="user", content="继续"
    )
    refreshed_context = store.load_conversation_context(conversation.id)
    assert refreshed_context is not None and refreshed_context is not first_context
    assert refreshed_context.messages[-1] == next_message


def test_record_profile_upload_is_atomic_and_rebuilds_context(store: SessionStore) -> None:
    project = store.create_project("上传项目")
    conversation = store.create_conversation(project.id)
    profile = {"row_count": 2, "column_count": 1, "columns": [{"name": "销售额"}]}

    dataset, messages, artifact = store.record_profile_upload(
        ref="uploaded-dataset",
        project_id=project.id,
        conversation_id=conversation.id,
        filename="销售.xlsx",
        profile=profile,
        user_content="上传了文件：销售.xlsx",
        assistant_content="已完成数据画像。",
    )

    assert store.get_dataset(dataset.ref) == dataset
    assert [message.role for message in messages] == ["user", "assistant"]
    assert store.list_messages(conversation.id) == list(messages)
    assert store.list_artifacts(conversation.id) == [artifact]
    assert artifact.message_id == messages[1].id
    assert artifact.payload == profile
    context = store.load_conversation_context(conversation.id)
    assert context is not None
    assert context.conversation.title == "销售.xlsx"
    assert context.messages == messages
    assert context.artifacts == (artifact,)

    other_project = store.create_project("其他项目")
    with pytest.raises(ValueError, match="对话不属于指定项目"):
        store.record_profile_upload(
            ref="must-rollback",
            project_id=other_project.id,
            conversation_id=conversation.id,
            filename="错误.xlsx",
            profile={},
            user_content="上传",
            assistant_content="画像",
        )
    assert store.get_dataset("must-rollback") is None


def test_start_user_turn_sets_title_once_and_accepts_assistant_id(store: SessionStore) -> None:
    project = store.create_project("聊天")
    conversation = store.create_conversation(project.id)

    updated, first = store.start_user_turn(
        conversation_id=conversation.id,
        content="  请分析各地区销售趋势  ",
        suggested_title="请分析各地区销售趋势",
    )
    assert first.content == "请分析各地区销售趋势"
    assert updated.title == "请分析各地区销售趋势"

    still_updated, second = store.start_user_turn(
        conversation_id=conversation.id,
        content="再看看异常值",
        suggested_title="不应覆盖原标题",
    )
    assert still_updated.title == updated.title
    assistant = store.append_message(
        conversation_id=conversation.id,
        role="assistant",
        content="好的。",
        message_id="assistant-fixed-id",
    )
    assert assistant.id == "assistant-fixed-id"
    assert store.list_messages(conversation.id) == [first, second, assistant]


def test_file_artifact_roundtrip(store: SessionStore) -> None:
    project = store.create_project("报告")
    conversation = store.create_conversation(project.id)
    message = store.append_message(
        conversation_id=conversation.id, role="assistant", content="报告已生成"
    )
    artifact = store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="report",
        file_ref=".data/reports/report.pdf",
    )

    assert artifact.payload is None
    assert store.list_artifacts(conversation.id) == [artifact]
    with pytest.raises(ValueError, match="必须包含 payload 或 file_ref"):
        store.create_artifact(
            conversation_id=conversation.id,
            message_id=message.id,
            type="empty",
        )


def test_artifact_ownership_guards(store: SessionStore) -> None:
    first_project = store.create_project("项目一")
    second_project = store.create_project("项目二")
    first_conversation = store.create_conversation(first_project.id)
    second_conversation = store.create_conversation(first_project.id)
    message = store.append_message(
        conversation_id=first_conversation.id, role="assistant", content="完成"
    )
    foreign_dataset = store.register_dataset(
        ref="foreign", project_id=second_project.id, filename="foreign.xlsx", profile={}
    )

    with pytest.raises(ValueError, match="消息不属于指定对话"):
        store.create_artifact(
            conversation_id=second_conversation.id,
            message_id=message.id,
            type="profile",
            payload={},
        )
    with pytest.raises(ValueError, match="必须属于同一项目"):
        store.create_artifact(
            conversation_id=first_conversation.id,
            message_id=message.id,
            type="profile",
            payload={},
            dataset_ref=foreign_dataset.ref,
        )


def test_conversation_order_and_cascade(store: SessionStore) -> None:
    project = store.create_project("排序")
    first = store.create_conversation(project.id, "第一条")
    second = store.create_conversation(project.id, "第二条")
    assert store.list_conversations(project.id) == [second, first]

    message = store.append_message(
        conversation_id=first.id, role="user", content="更新第一条"
    )
    store.create_artifact(
        conversation_id=first.id,
        message_id=message.id,
        type="profile",
        payload={"ok": True},
    )
    assert store.list_conversations(project.id)[0].id == first.id
    assert store.delete_conversation(first.id) is True
    assert store.get_conversation(first.id) is None
    assert store.list_messages(first.id) == []
    assert store.list_artifacts(first.id) == []
    assert store.load_conversation_context(first.id) is None


def test_project_delete_cascades_database_records(store: SessionStore) -> None:
    project = store.create_project("级联")
    dataset = store.register_dataset(
        ref="cascade", project_id=project.id, filename="cascade.xlsx", profile={}
    )
    conversation = store.create_conversation(project.id)
    message = store.append_message(
        conversation_id=conversation.id, role="assistant", content="画像"
    )
    store.create_artifact(
        conversation_id=conversation.id,
        message_id=message.id,
        type="profile",
        payload={"ok": True},
        dataset_ref=dataset.ref,
    )
    assert store.load_conversation_context(conversation.id) is not None

    assert store.delete_project(project.id) is True
    assert store.get_dataset(dataset.ref) is None
    assert store.get_conversation(conversation.id) is None
    assert store.list_messages(conversation.id) == []
    assert store.list_artifacts(conversation.id) == []


def test_lru_eviction_and_explicit_invalidation(store: SessionStore) -> None:
    project = store.create_project("缓存")
    first = store.create_conversation(project.id, "一")
    second = store.create_conversation(project.id, "二")
    third = store.create_conversation(project.id, "三")

    first_context = store.load_conversation_context(first.id)
    assert first_context is not None
    assert store.load_conversation_context(second.id) is not None
    assert store.load_conversation_context(third.id) is not None
    assert store.load_conversation_context(first.id) is not first_context

    cache = ConversationCache(capacity=1)
    assert cache.capacity == 1
    cache.put(first_context)
    assert cache.get(first.id) is first_context
    cache.invalidate(first.id)
    assert cache.get(first.id) is None
    with pytest.raises(ValueError, match="容量必须大于 0"):
        ConversationCache(capacity=0)


def test_two_store_instances_can_append_concurrently(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent.db"
    first_store = SessionStore(str(db_path))
    project = first_store.create_project("并发")
    conversation = first_store.create_conversation(project.id)
    second_store = SessionStore(str(db_path))

    def append(index: int) -> str:
        active_store = first_store if index % 2 else second_store
        return active_store.append_message(
            conversation_id=conversation.id,
            role="user",
            content=f"消息 {index}",
        ).id

    with ThreadPoolExecutor(max_workers=4) as pool:
        message_ids = list(pool.map(append, range(20)))

    assert len(set(message_ids)) == 20
    assert len(first_store.list_messages(conversation.id)) == 20


def test_invalid_conversation_and_message_role_are_rejected(store: SessionStore) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        store.create_conversation("missing")

    project = store.create_project("角色")
    conversation = store.create_conversation(project.id)
    with pytest.raises(ValueError, match="不支持的消息角色"):
        store.append_message(
            conversation_id=conversation.id,
            role="invalid",
            content="无效",
        )
