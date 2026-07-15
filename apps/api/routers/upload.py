"""Excel 上传接口：上传 → 触发数据画像（设计文档 5.1 / 6.1）。"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, cast

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from mcp_servers.common.base_server import MCPServer
from mcp_servers.excel_parser.tools import TableTooLargeError
from packages.common.config import Settings
from packages.common.dataset_store import save_metadata
from packages.governance.data_boundary import parse_policy_override
from packages.session.store import SessionStore

from apps.api.deps import excel_tools_dep, session_store_dep, settings_dep
from apps.api.schemas import ArtifactResponse, MessageResponse, UploadResponse

router = APIRouter(prefix="/upload", tags=["upload"])


@router.post("/excel", response_model=UploadResponse, response_model_exclude_unset=True)
async def upload_excel(
    file: UploadFile,
    settings: Settings = Depends(settings_dep),
    excel: MCPServer = Depends(excel_tools_dep),
    store: SessionStore = Depends(session_store_dep),
    policy: str | None = Form(default=None),
    project_id: str | None = Form(default=None),
    conversation_id: str | None = Form(default=None),
) -> UploadResponse:
    """接收 Excel，落盘后经 parse_excel/infer_schema 生成画像供用户确认。

    默认仅返回数据画像与 dataset_ref；同时传入 project_id/conversation_id 时，
    原子登记数据集、上传消息与画像工件。原始整表不进入 LLM（红线1）。
    可选 `policy`（JSON 字符串）指定该数据集的安全策略，落为 sidecar 元数据；
    不传则用默认（宽松）策略。
    """
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xls 文件")

    override = _validate_policy(policy)
    workspace_link = await run_in_threadpool(
        _validate_workspace_link,
        store,
        project_id,
        conversation_id,
    )

    upload_dir = Path(settings.upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    # 只取 basename，避免用户传入 ../ 造成路径穿越（uuid 前缀保证唯一）
    safe_name = Path(file.filename or "upload.xlsx").name
    saved = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
    await _save_within_limit(file, saved, settings.max_upload_mb)

    # 经 Tool.invoke 走 schema 校验（红线3）；解析是阻塞重活 → 线程池，不卡事件循环
    try:
        parsed = await run_in_threadpool(
            excel._tools["parse_excel"].invoke, {"file_ref": str(saved)}
        )
    except TableTooLargeError as exc:
        saved.unlink(missing_ok=True)  # 拒绝的文件不留盘
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except ValueError as exc:  # 格式无法解析/工作表不存在等 → 可读 422 而非 500
        saved.unlink(missing_ok=True)
        raise HTTPException(status_code=422, detail=f"Excel 解析失败：{exc}") from exc
    dataset_ref = parsed["dataset_ref"]
    if override is not None:
        # 落数据集级安全策略，infer_schema 脱敏时读取
        save_metadata(dataset_ref, {"policy": override})

    profile = await run_in_threadpool(
        excel._tools["infer_schema"].invoke, {"dataset_ref": dataset_ref}
    )
    profile_data = profile.to_dict()

    if workspace_link is None:
        # 经典模式兼容：不传项目/对话时保持原响应结构与行为。
        return UploadResponse(dataset_ref=dataset_ref, profile=profile_data)

    linked_project_id, linked_conversation_id = workspace_link
    try:
        _, messages, artifact = await run_in_threadpool(
            store.record_profile_upload,
            ref=dataset_ref,
            project_id=linked_project_id,
            conversation_id=linked_conversation_id,
            filename=safe_name,
            profile=profile_data,
            user_content=f"上传了文件：{safe_name}",
            assistant_content=_profile_message(safe_name, profile_data),
        )
    except (sqlite3.IntegrityError, ValueError) as exc:
        # 文件解析已成功，但关联目标可能被并发删除；数据库事务会完整回滚。
        raise HTTPException(status_code=409, detail=f"数据集关联失败：{exc}") from exc

    return UploadResponse(
        dataset_ref=dataset_ref,
        profile=profile_data,
        messages=[MessageResponse.model_validate(message) for message in messages],
        artifact=ArtifactResponse.model_validate(artifact),
    )


async def _save_within_limit(file: UploadFile, dest: Path, max_mb: int) -> None:
    """分块流式写盘，累计超过 max_mb 即 413 并清理半成品（防内存 DoS）。"""
    max_bytes = max_mb * 1024 * 1024
    # 有可信 Content-Length 时先快速拒绝
    if file.size is not None and file.size > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件过大（上限 {max_mb} MB）")

    written = 0
    try:
        with dest.open("wb") as out:
            while chunk := await file.read(1 << 20):  # 1 MB/块
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(status_code=413, detail=f"文件过大（上限 {max_mb} MB）")
                out.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)  # 清理半成品，避免残留超大文件
        raise


def _validate_policy(policy: str | None) -> dict[str, Any] | None:
    """解析并校验上传的策略 JSON；非法则 400。返回原始 dict（供 sidecar 存储）。"""
    if not policy:
        return None
    try:
        data = json.loads(policy)
        if not isinstance(data, dict):
            raise ValueError("策略必须是 JSON 对象")
        parse_policy_override(data)  # 仅做校验，非法枚举等会抛错
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"非法安全策略: {exc}") from exc
    return cast(dict[str, Any], data)


def _validate_workspace_link(
    store: SessionStore,
    project_id: str | None,
    conversation_id: str | None,
) -> tuple[str, str] | None:
    """校验上传关联；两个 ID 必须同时提供，均不提供则走经典模式。"""
    clean_project_id = project_id.strip() if project_id and project_id.strip() else None
    clean_conversation_id = (
        conversation_id.strip() if conversation_id and conversation_id.strip() else None
    )
    if clean_project_id is None and clean_conversation_id is None:
        return None
    if clean_project_id is None or clean_conversation_id is None:
        raise HTTPException(
            status_code=422,
            detail="project_id 和 conversation_id 必须同时提供",
        )
    if store.get_project(clean_project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    conversation = store.get_conversation(clean_conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    if conversation.project_id != clean_project_id:
        raise HTTPException(status_code=422, detail="对话不属于指定项目")
    return clean_project_id, clean_conversation_id


def _profile_message(filename: str, profile: dict[str, object]) -> str:
    """生成不经过模型的确定性画像说明，数值直接来自 infer_schema。"""
    row_count = profile.get("row_count")
    column_count = profile.get("column_count")
    if isinstance(row_count, int) and isinstance(column_count, int):
        return f"已完成“{filename}”的数据画像，共 {row_count} 行、{column_count} 列。"
    return f"已完成“{filename}”的数据画像。"
