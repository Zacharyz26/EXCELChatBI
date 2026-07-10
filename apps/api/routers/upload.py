"""Excel 上传接口：上传 → 触发数据画像（设计文档 5.1 / 6.1）。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from mcp_servers.common.base_server import MCPServer
from mcp_servers.excel_parser.tools import TableTooLargeError
from packages.common.config import Settings
from packages.common.dataset_store import save_metadata
from packages.governance.data_boundary import parse_policy_override

from apps.api.deps import excel_tools_dep, settings_dep
from apps.api.schemas import UploadResponse

router = APIRouter(prefix="/upload", tags=["upload"])


@router.post("/excel", response_model=UploadResponse)
async def upload_excel(
    file: UploadFile,
    settings: Settings = Depends(settings_dep),
    excel: MCPServer = Depends(excel_tools_dep),
    policy: str | None = Form(default=None),
) -> UploadResponse:
    """接收 Excel，落盘后经 parse_excel/infer_schema 生成画像供用户确认。

    仅返回数据画像与 dataset_ref；原始整表不进入 LLM（红线1）。
    可选 `policy`（JSON 字符串）指定该数据集的安全策略，落为 sidecar 元数据；
    不传则用默认（宽松）策略。
    """
    if not (file.filename or "").lower().endswith((".xlsx", ".xls")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx / .xls 文件")

    override = _validate_policy(policy)

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
    return UploadResponse(dataset_ref=dataset_ref, profile=profile.to_dict())


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


def _validate_policy(policy: str | None) -> dict | None:
    """解析并校验上传的策略 JSON；非法则 400。返回原始 dict（供 sidecar 存储）。"""
    if not policy:
        return None
    try:
        data = json.loads(policy)
        parse_policy_override(data)  # 仅做校验，非法枚举等会抛错
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail=f"非法安全策略: {exc}") from exc
    return data
