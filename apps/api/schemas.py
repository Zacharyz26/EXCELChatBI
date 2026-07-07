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


class StatsRequest(BaseModel):
    """统计分析请求：基于已上传数据集跑趋势/异常/回归。

    params 为工具专属入参（如 value_col/time_col/target/features），
    与 dataset_ref 合并后经 Tool.invoke 做 JSON Schema 校验（红线3）。
    """

    dataset_ref: str
    kind: str                      # trend | anomaly | regression
    params: dict[str, Any] = {}
    interpret: bool = False        # 是否附带 LLM 中文解读（默认关，不平白付模型成本）


class StatsResponse(BaseModel):
    """统计分析响应：结构化结果（数值来自工具，红线2）。

    result 内可能含明细级数组（STL 逐行分量、异常点原值），仅供前端渲染；
    interpretation 为可选的 LLM 中文解读——喂模型的只有摘要（红线1），
    模型不可用时为 None（降级，统计结果照常返回）。
    """

    kind: str
    result: dict[str, Any]
    interpretation: str | None = None


class ReportChartSpec(BaseModel):
    """报告中要包含的一张图（编排层重跑 gen_chart→chart_screenshot 出图片）。"""

    chart_type: str
    encoding: dict[str, Any]
    caption: str | None = None


class ReportStatSpec(BaseModel):
    """报告中要包含的一项统计（编排层重跑 stats 工具拿真实结果）。"""

    kind: str                      # trend | anomaly | regression
    params: dict[str, Any] = {}
    caption: str | None = None


class ReportRequest(BaseModel):
    """报告生成请求：基于 dataset_ref 重跑分析并组装成可下载报告。

    interpret=true 时，各统计段的中文解读由编排层调 stats_interpreter（已门控的
    唯一 LLM 出口）生成后传给 report 工具；report 工具本身不调 LLM（红线1/铁律）。
    """

    dataset_ref: str
    title: str = "分析报告"
    charts: list[ReportChartSpec] = []
    stats: list[ReportStatSpec] = []
    interpret: bool = False


class ReportResponse(BaseModel):
    """报告生成响应：报告 id 与下载链接。"""

    report_id: str
    md_url: str
    pdf_url: str


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
