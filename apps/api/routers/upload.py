"""Excel 上传接口：上传 → 触发数据画像（设计文档 5.1 / 6.1）。"""

from __future__ import annotations

from fastapi import APIRouter, UploadFile

router = APIRouter(prefix="/upload", tags=["upload"])


@router.post("/excel")
async def upload_excel(file: UploadFile) -> object:
    """接收 Excel，存 MinIO，触发 parse_excel/infer_schema 生成画像供用户确认。

    注意：仅返回数据画像引用；原始整表不进入 LLM（红线1）。
    """
    raise NotImplementedError("TODO: 存 MinIO → 调 excel_parser → 返回 dataset_ref + 画像")
