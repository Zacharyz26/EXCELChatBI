"""报告导出接口：dataset_ref → 重跑分析 → 组装 Markdown/PDF → 下载。

编排层职责（红线归属清晰）：
- 重跑工具拿**真实结果**（红线2）：infer_schema / gen_chart / chart_screenshot / stats 工具。
- 中文解读**唯一**在此经 `interpret_stats`（已门控出口）产出；report 工具零 LLM（铁律）。
- 所有工具经 `Tool.invoke` 校验（红线3），且一律放线程池执行：既避免阻塞事件循环
  （无头浏览器 / WeasyPrint / DuckDB / statsmodels 都是同步重活），也规避 sync
  playwright 不能在事件循环内运行的限制。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from mcp_servers.common.base_server import MCPServer
from packages.common.config import Settings
from packages.common.identifiers import validate_report_id
from packages.common.logging import get_logger
from packages.governance.permissions import Principal
from packages.governance.schema_validator import SchemaValidationError
from packages.models.gateway import ModelGateway
from packages.session.file_lifecycle import delete_chart_file, delete_report_files
from packages.session.store import SessionStore

from apps.api.auth import current_principal_dep
from apps.api.authz import require_dataset_access, require_project_access
from apps.api.deps import (
    chart_tools_dep,
    excel_tools_dep,
    model_gateway_dep,
    report_tools_dep,
    session_store_dep,
    settings_dep,
    stats_tools_dep,
)
from apps.api.schemas import ReportRequest, ReportResponse
from apps.orchestrator.stats_interpreter import interpret_stats

router = APIRouter(prefix="/analyze/report", tags=["report"])

_log = get_logger("api.report")

_STATS_TOOLS = {
    "trend": "trend_analysis",
    "anomaly": "anomaly_detect",
    "regression": "regression",
    "correlation": "correlation",
}
_KIND_LABEL = {
    "trend": "趋势分析",
    "anomaly": "异常检测",
    "regression": "回归分析",
    "correlation": "相关性分析",
}


@router.post("", response_model=ReportResponse)
async def create_report(
    req: ReportRequest,
    excel: MCPServer = Depends(excel_tools_dep),
    chart: MCPServer = Depends(chart_tools_dep),
    stats: MCPServer = Depends(stats_tools_dep),
    report: MCPServer = Depends(report_tools_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
    settings: Settings = Depends(settings_dep),
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> ReportResponse:
    """基于 dataset_ref 重跑分析并组装成可下载报告（Markdown + PDF）。"""
    dataset = require_dataset_access(store, req.dataset_ref, principal)
    assert dataset is not None
    _log.info(
        "report.request",
        dataset_ref=req.dataset_ref,
        charts=len(req.charts),
        stats=len(req.stats),
        interpret=req.interpret,
    )
    try:
        # 阻塞的读盘/画像计算 → 线程池（本路由所有 Tool.invoke 同此，不卡事件循环）
        profile_obj = await run_in_threadpool(
            excel._tools["infer_schema"].invoke, {"dataset_ref": req.dataset_ref}
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    profile = profile_obj.to_dict()

    chart_sections: list[dict[str, Any]] = []
    report_id: str | None = None
    try:
        chart_sections = await _build_charts(req, chart, settings=settings)
        stat_sections, insight_items = await _build_stats(req, stats, gateway)

        insights_md = None
        if insight_items:
            insight = await run_in_threadpool(
                report._tools["insight_summary"].invoke, {"items": insight_items}
            )
            insights_md = insight["summary_md"]

        md = await run_in_threadpool(
            report._tools["gen_report_md"].invoke,
            {
                "title": req.title,
                "profile": profile,
                "charts": chart_sections,
                "stats": stat_sections,
                "insights": insights_md,
            },
        )
        report_id = str(md["report_id"])
        await run_in_threadpool(
            report._tools["export_pdf"].invoke, {"report_id": report_id}
        )
        await run_in_threadpool(
            store.register_report_publication,
            report_id=report_id,
            project_id=dataset.project_id,
        )
    except Exception:
        if report_id is not None:
            delete_report_files(report_id, settings.report_dir)
        raise
    finally:
        for section in chart_sections:
            delete_chart_file(section.get("image_path"), settings.report_dir)

    assert report_id is not None
    return ReportResponse(
        report_id=report_id,
        md_url=f"/analyze/report/{report_id}.md",
        pdf_url=f"/analyze/report/{report_id}.pdf",
    )


async def _build_charts(
    req: ReportRequest,
    chart: MCPServer,
    *,
    settings: Settings,
) -> list[dict[str, Any]]:
    """每个图表 spec：gen_chart（真实数据）→ chart_screenshot（线程池，出 PNG）。"""
    sections: list[dict[str, Any]] = []
    try:
        for spec in req.charts:
            res = await run_in_threadpool(
                chart._tools["gen_chart"].invoke,
                {
                    "dataset_ref": req.dataset_ref,
                    "chart_type": spec.chart_type,
                    "encoding": spec.encoding,
                },
            )
            img = await run_in_threadpool(
                chart._tools["chart_screenshot"].invoke, {"option": res["option"]}
            )
            sections.append(
                {
                    "caption": spec.caption or f"{res['chart_type']} 图",
                    "image_path": img["image_path"],
                }
            )
    except (SchemaValidationError, ValueError) as exc:
        for section in sections:
            delete_chart_file(section.get("image_path"), settings.report_dir)
        raise HTTPException(status_code=422, detail=f"图表参数无效：{exc}") from exc
    except Exception:
        for section in sections:
            delete_chart_file(section.get("image_path"), settings.report_dir)
        raise
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
            result = await run_in_threadpool(
                stats._tools[tool].invoke, {"dataset_ref": req.dataset_ref, **spec.params}
            )
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
    report_id: str,
    settings: Settings = Depends(settings_dep),
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> FileResponse:
    """下载报告 PDF。"""
    _require_report_access(store, report_id, principal)
    return _file_response(settings, report_id, "pdf", "application/pdf")


@router.get("/{report_id}.md")
def download_md(
    report_id: str,
    settings: Settings = Depends(settings_dep),
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> FileResponse:
    """下载报告 Markdown。"""
    _require_report_access(store, report_id, principal)
    return _file_response(settings, report_id, "md", "text/markdown; charset=utf-8")


def _require_report_access(
    store: SessionStore,
    report_id: str,
    principal: Principal,
) -> None:
    try:
        project_id = store.report_project_id(report_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法 report_id") from exc
    if project_id is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    require_project_access(store, project_id, principal)


def _file_response(settings: Settings, report_id: str, ext: str, media_type: str) -> FileResponse:
    """按 report_id 定位落盘文件并返回下载响应。"""
    try:
        clean_report_id = validate_report_id(report_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="非法 report_id") from exc
    root = Path(settings.report_dir).resolve()
    path = (root / f"{clean_report_id}.{ext}").resolve()
    if path.parent != root:
        raise HTTPException(status_code=400, detail="非法 report_id")
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"报告不存在: {clean_report_id}.{ext}")
    return FileResponse(
        path,
        media_type=media_type,
        filename=f"report_{clean_report_id}.{ext}",
    )
