"""Excel 上传接口：上传 → 触发数据画像（设计文档 5.1 / 6.1）。"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile
from mcp_servers.common.base_server import MCPServer
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
    saved = upload_dir / f"{uuid.uuid4().hex}_{file.filename}"
    saved.write_bytes(await file.read())

    # 经 Tool.invoke 走 schema 校验（红线3）
    parsed = excel._tools["parse_excel"].invoke({"file_ref": str(saved)})
    dataset_ref = parsed["dataset_ref"]
    if override is not None:
        # 落数据集级安全策略，infer_schema 脱敏时读取
        save_metadata(dataset_ref, {"policy": override})

    profile = excel._tools["infer_schema"].invoke({"dataset_ref": dataset_ref})
    return UploadResponse(dataset_ref=dataset_ref, profile=profile.to_dict())


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
