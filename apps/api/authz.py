"""项目资源授权辅助函数；对无权资源统一返回 404 防止枚举。"""

from __future__ import annotations

from fastapi import HTTPException
from packages.governance.permissions import Principal
from packages.session.models import Conversation, Dataset, Project
from packages.session.store import SessionStore
from packages.session.task_models import TaskRun


def require_project_access(
    store: SessionStore,
    project_id: str,
    principal: Principal,
    *,
    write: bool = False,
) -> Project:
    """校验项目成员关系；写操作仅 owner/editor 可用。"""
    project = store.get_project(project_id)
    role = store.project_role(
        project_id,
        user_id=principal.user_id,
        tenant_id=principal.tenant_scope,
    )
    if project is None or role is None or (write and role not in {"owner", "editor"}):
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


def require_conversation_access(
    store: SessionStore,
    conversation_id: str,
    principal: Principal,
    *,
    write: bool = False,
) -> Conversation:
    """读取对话并校验其项目成员关系。"""
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    require_project_access(store, conversation.project_id, principal, write=write)
    return conversation


def require_dataset_access(
    store: SessionStore,
    dataset_ref: str,
    principal: Principal,
    *,
    write: bool = False,
    allow_unregistered: bool = False,
) -> Dataset | None:
    """读取已登记数据集并校验项目成员关系；未知引用默认拒绝。"""
    dataset = store.get_dataset(dataset_ref)
    if dataset is None:
        if allow_unregistered:
            return None
        raise HTTPException(status_code=404, detail="数据集不存在")
    require_project_access(store, dataset.project_id, principal, write=write)
    return dataset


def require_run_access(
    store: SessionStore,
    run: TaskRun,
    principal: Principal,
    *,
    write: bool = False,
) -> None:
    """校验 TaskRun 所属项目。"""
    require_project_access(store, run.project_id, principal, write=write)
