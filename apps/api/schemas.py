"""API 请求 / 响应模型（Pydantic）。"""

from __future__ import annotations

from typing import Any

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
    """Excel 上传响应：数据集引用 + 数据画像（供前端展示并确认）。

    注意：返回的是画像，原始整表只在服务端以 dataset_ref 引用（红线1）。
    """

    dataset_ref: str
    profile: dict[str, Any]


class AnalyzeRequest(BaseModel):
    """分析请求：基于已上传数据集出图。"""

    dataset_ref: str


class ChartResponse(BaseModel):
    """出图响应：ECharts 配置（数值来自真实数据，红线2）。"""

    chart_id: str
    chart_type: str
    option: dict[str, Any]


class IngestRequest(BaseModel):
    """知识库摄入请求：路径（文件/目录）或内联文本，二选一。"""

    path: str | None = None
    text: str | None = None
    source: str | None = None    # 内联文本时的来源标注


class IngestResponse(BaseModel):
    """摄入统计。"""

    ingested_docs: int
    chunks: int
    total_chunks: int            # 库内片段总数


class KBQueryRequest(BaseModel):
    """知识库问答请求（单轮中文提问）。"""

    question: str
    top_k: int = 5


class Citation(BaseModel):
    """引用来源（红线6）。"""

    source: str
    snippet: str
    section: str | None = None


class KBQueryResponse(BaseModel):
    """问答响应：答案 + 引用；无结果时如实告知。"""

    answer: str
    citations: list[Citation]
    is_empty: bool
