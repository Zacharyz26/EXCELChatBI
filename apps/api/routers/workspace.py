"""对话工作区的项目、数据集与历史对话 API。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response, status
from packages.session.models import Project
from packages.session.store import SessionStore

from apps.api.deps import session_store_dep
from apps.api.schemas import (
    ArtifactResponse,
    ConversationCreate,
    ConversationDetailResponse,
    ConversationResponse,
    ConversationUpdate,
    DatasetResponse,
    MessageResponse,
    ProjectCreate,
    ProjectResponse,
    ProjectUpdate,
)

router = APIRouter(tags=["workspace"])


@router.post("/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
def create_project(
    req: ProjectCreate,
    store: SessionStore = Depends(session_store_dep),
) -> ProjectResponse:
    """创建一个对话工作区项目。"""
    return ProjectResponse.model_validate(store.create_project(req.name))


@router.get("/projects", response_model=list[ProjectResponse])
def list_projects(store: SessionStore = Depends(session_store_dep)) -> list[ProjectResponse]:
    """列出全部项目。"""
    return [ProjectResponse.model_validate(item) for item in store.list_projects()]


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> ProjectResponse:
    """读取一个项目。"""
    return ProjectResponse.model_validate(_require_project(store, project_id))


@router.patch("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: str,
    req: ProjectUpdate,
    store: SessionStore = Depends(session_store_dep),
) -> ProjectResponse:
    """重命名项目。"""
    project = store.update_project(project_id, req.name)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return ProjectResponse.model_validate(project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> Response:
    """删除项目及其数据库内的对话记录；不删除 parquet 文件。"""
    if not store.delete_project(project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/projects/{project_id}/datasets", response_model=list[DatasetResponse])
def list_project_datasets(
    project_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> list[DatasetResponse]:
    """列出项目登记的数据集。"""
    _require_project(store, project_id)
    return [DatasetResponse.model_validate(item) for item in store.list_datasets(project_id)]


@router.post(
    "/projects/{project_id}/conversations",
    response_model=ConversationResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_conversation(
    project_id: str,
    req: ConversationCreate,
    store: SessionStore = Depends(session_store_dep),
) -> ConversationResponse:
    """在指定项目中新建对话。"""
    _require_project(store, project_id)
    conversation = store.create_conversation(project_id, req.title)
    return ConversationResponse.model_validate(conversation)


@router.get(
    "/projects/{project_id}/conversations",
    response_model=list[ConversationResponse],
)
def list_project_conversations(
    project_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> list[ConversationResponse]:
    """按最近更新时间列出项目历史对话。"""
    _require_project(store, project_id)
    return [
        ConversationResponse.model_validate(item)
        for item in store.list_conversations(project_id)
    ]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetailResponse)
def get_conversation(
    conversation_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> ConversationDetailResponse:
    """读取对话及其持久化消息和工件。"""
    context = store.load_conversation_context(conversation_id)
    if context is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return ConversationDetailResponse(
        conversation=ConversationResponse.model_validate(context.conversation),
        messages=[MessageResponse.model_validate(item) for item in context.messages],
        artifacts=[ArtifactResponse.model_validate(item) for item in context.artifacts],
    )


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
def update_conversation(
    conversation_id: str,
    req: ConversationUpdate,
    store: SessionStore = Depends(session_store_dep),
) -> ConversationResponse:
    """修改历史对话标题。"""
    conversation = store.update_conversation(conversation_id, req.title)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return ConversationResponse.model_validate(conversation)


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_conversation(
    conversation_id: str,
    store: SessionStore = Depends(session_store_dep),
) -> Response:
    """删除对话及其消息、工件。"""
    if not store.delete_conversation(conversation_id):
        raise HTTPException(status_code=404, detail="对话不存在")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _require_project(store: SessionStore, project_id: str) -> Project:
    project = store.get_project(project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project
