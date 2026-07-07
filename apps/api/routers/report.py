"""报告导出接口：dataset_ref → 重跑分析 → 组装 Markdown/PDF → 下载。

编排层职责（红线归属清晰）：
- 重跑工具拿**真实结果**（红线2）：infer_schema / gen_chart / chart_screenshot / stats 工具。
- 中文解读**唯一**在此经 `interpret_stats`（已门控出口）产出；report 工具零 LLM（铁律）。
- 所有工具经 `Tool.invoke` 校验（红线3）；chart_screenshot 与 export_pdf 是同步阻塞
  （无头浏览器 / WeasyPrint），放到线程池执行，避免阻塞事件循环、并规避 sync playwright
  不能在事件循环内运行的限制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from mcp_servers.common.base_server import MCPServer
from packages.common.config import Settings
from packages.common.logging import get_logger
from packages.governance.schema_validator import SchemaValidationError
from packages.models.gateway import ModelGateway

from apps.api.deps import (
    chart_tools_dep,
    excel_tools_dep,
    model_gateway_dep,
    report_tools_dep,
    settings_dep,
    stats_tools_dep,
)
from apps.api.schemas import ReportRequest, ReportResponse
from apps.orchestrator.stats_interpreter import interpret_stats

router = APIRouter(prefix="/analyze/report", tags=["report"])

_log = get_logger("api.report")

_STATS_TOOLS = {"trend": "trend_analysis", "anomaly": "anomaly_detect", "regression": "regression"}
_KIND_LABEL = {"trend": "趋势分析", "anomaly": "异常检测", "regression": "回归分析"}


@router.post("", response_model=ReportResponse)
async def create_report(
    req: ReportRequest,
    excel: MCPServer = Depends(excel_tools_dep),
    chart: MCPServer = Depends(chart_tools_dep),
    stats: MCPServer = Depends(stats_tools_dep),
    report: MCPServer = Depends(report_tools_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
) -> ReportResponse:
    """基于 dataset_ref 重跑分析并组装成可下载报告（Markdown + PDF）。"""
    _log.info(
        "report.request",
        dataset_ref=req.dataset_ref,
        charts=len(req.charts),
        stats=len(req.stats),
        interpret=req.interpret,
    )
    try:
        profile = excel._tools["infer_schema"].invoke({"dataset_ref": req.dataset_ref}).to_dict()
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    chart_sections = await _build_charts(req, chart)
    stat_sections, insight_items = await _build_stats(req, stats, gateway)

    insights_md = None
    if insight_items:
        insight = report._tools["insight_summary"].invoke({"items": insight_items})
        insights_md = insight["summary_md"]

    md = report._tools["gen_report_md"].invoke(
        {
            "title": req.title,
            "profile": profile,
            "charts": chart_sections,
            "stats": stat_sections,
            "insights": insights_md,
        }
    )
    report_id = md["report_id"]
    # WeasyPrint 阻塞 → 线程池
    await run_in_threadpool(report._tools["export_pdf"].invoke, {"report_id": report_id})

    return ReportResponse(
        report_id=report_id,
        md_url=f"/analyze/report/{report_id}.md",
        pdf_url=f"/analyze/report/{report_id}.pdf",
    )


async def _build_charts(req: ReportRequest, chart: MCPServer) -> list[dict[str, Any]]:
    """每个图表 spec：gen_chart（真实数据）→ chart_screenshot（线程池，出 PNG）。"""
    sections: list[dict[str, Any]] = []
    for spec in req.charts:
        try:
            res = chart._tools["gen_chart"].invoke(
                {
                    "dataset_ref": req.dataset_ref,
                    "chart_type": spec.chart_type,
                    "encoding": spec.encoding,
                }
            )
        except (SchemaValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"图表参数无效：{exc}") from exc
        # sync playwright 不能在事件循环内 → 线程池
        img = await run_in_threadpool(
            chart._tools["chart_screenshot"].invoke, {"option": res["option"]}
        )
        sections.append(
            {"caption": spec.caption or f"{res['chart_type']} 图", "image_path": img["image_path"]}
        )
    return sections


async def _build_stats(
    req: ReportRequest, stats: MCPServer, gateway: ModelGateway
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """每个统计 spec：stats 工具（真实结果）+（interpret 时）interpret_stats（唯一 LLM 出口）。"""
    sections: list[dict[str, Any]] = []
    insight_items: list[dict[str, Any]] = []
    for spec in req.stats:
        tool = _STATS_TOOLS.get(spec.kind)
        if tool is None:
            raise HTTPException(status_code=422, detail=f"不支持的统计类型: {spec.kind}")
        try:
            result = stats._tools[tool].invoke({"dataset_ref": req.dataset_ref, **spec.params})
        except (SchemaValidationError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=f"统计参数无效：{exc}") from exc

        interp = None
        if req.interpret:
            # 报告里的解读只来自这条已门控的唯一出口；report 工具不碰 LLM
            interp = await interpret_stats(spec.kind, result, gateway, req.dataset_ref, spec.params)

        label = spec.caption or _KIND_LABEL.get(spec.kind, spec.kind)
        sections.append(
            {"kind": spec.kind, "caption": spec.caption, "result": result, "interpretation": interp}
        )
        if interp:
            insight_items.append({"label": label, "text": interp})
    return sections, insight_items


@router.get("/{report_id}.pdf")
def download_pdf(
    report_id: str, settings: Settings = Depends(settings_dep)
) -> FileResponse:
    """下载报告 PDF。"""
    return _file_response(settings, report_id, "pdf", "application/pdf")


@router.get("/{report_id}.md")
def download_md(
    report_id: str, settings: Settings = Depends(settings_dep)
) -> FileResponse:
    """下载报告 Markdown。"""
    return _file_response(settings, report_id, "md", "text/markdown; charset=utf-8")


def _file_response(settings: Settings, report_id: str, ext: str, media_type: str) -> FileResponse:
    """按 report_id 定位落盘文件并返回下载响应。"""
    # report_id 只应为十六进制；拒绝任何路径分隔，防穿越
    if not report_id.isalnum():
        raise HTTPException(status_code=400, detail="非法 report_id")
    path = Path(settings.report_dir) / f"{report_id}.{ext}"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"报告不存在: {report_id}.{ext}")
    return FileResponse(path, media_type=media_type, filename=f"report_{report_id}.{ext}")
