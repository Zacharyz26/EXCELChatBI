"""API 请求 / 响应模型（Pydantic）。"""

from __future__ import annotations

from pydantic import BaseModel


class ChatRequest(BaseModel):
    """对话请求。"""

    session_id: str
    message: str
    image_refs: list[str] = []     # 多模态：图像引用（设计文档 F3）


class ChatChunk(BaseModel):
    """SSE 流式返回的增量片段。"""

    type: str                      # token | step | chart | error
    data: str


class UploadResponse(BaseModel):
    """Excel 上传响应：返回数据集引用，前端据此展示画像供确认。"""

    dataset_ref: str
