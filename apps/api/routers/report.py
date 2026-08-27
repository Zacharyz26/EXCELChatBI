"""对话式 Agent 生成报告后的受权下载接口。"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from packages.common.config import Settings
from packages.common.identifiers import validate_report_id
from packages.governance.permissions import Principal
from packages.session.store import SessionStore

from apps.api.auth import current_principal_dep
from apps.api.authz import require_project_access
from apps.api.deps import session_store_dep, settings_dep

router = APIRouter(prefix="/analyze/report", tags=["report"])


@router.get("/{report_id}.pdf")
def download_pdf(
    report_id: str,
    settings: Settings = Depends(settings_dep),
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> FileResponse:
    """下载当前用户有权访问的 PDF 报告。"""
    _require_report_access(store, report_id, principal)
    return _file_response(settings, report_id, "pdf", "application/pdf")


@router.get("/{report_id}.md")
def download_md(
    report_id: str,
    settings: Settings = Depends(settings_dep),
    store: SessionStore = Depends(session_store_dep),
    principal: Principal = Depends(current_principal_dep),
) -> FileResponse:
    """下载当前用户有权访问的 Markdown 报告。"""
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


def _file_response(
    settings: Settings,
    report_id: str,
    ext: str,
    media_type: str,
) -> FileResponse:
    """按受校验的 report_id 定位落盘文件并返回下载响应。"""
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
