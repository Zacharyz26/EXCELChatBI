"""统计分析接口：dataset_ref → statsmodels/scikit-learn 出结构化统计结果。

与 /analyze（出图）并列，同为已上传 dataset_ref 的消费者；analyze.py 专注出图，
统计分析独立在此。链路：请求 → Tool.invoke(schema 校验，红线3) → 工具用真实数据
计算（红线2）→ 返回结构化结果。interpret=true 时再经编排层把**摘要**（红线1）
交给模型生成中文解读，解读失败降级为无解读、统计结果照常返回。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mcp_servers.common.base_server import MCPServer
from packages.common.logging import get_logger
from packages.governance.schema_validator import SchemaValidationError
from packages.models.gateway import ModelGateway

from apps.api.deps import model_gateway_dep, stats_tools_dep
from apps.api.schemas import StatsRequest, StatsResponse
from apps.orchestrator.stats_interpreter import interpret_stats

router = APIRouter(prefix="/analyze/stats", tags=["stats"])

_log = get_logger("api.stats")

# 请求 kind → stats 工具名
_TOOLS = {
    "trend": "trend_analysis",
    "anomaly": "anomaly_detect",
    "regression": "regression",
}


@router.post("", response_model=StatsResponse)
async def analyze_stats(
    req: StatsRequest,
    stats: MCPServer = Depends(stats_tools_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
) -> StatsResponse:
    """基于已上传数据集，跑趋势/异常/回归，返回结构化结果（可选中文解读）。"""
    tool_name = _TOOLS.get(req.kind)
    if tool_name is None:
        raise HTTPException(
            status_code=422,
            detail=f"不支持的统计类型: {req.kind}（可选 {'/'.join(_TOOLS)}）",
        )

    args = {"dataset_ref": req.dataset_ref, **req.params}
    _log.info(
        "stats.request",
        dataset_ref=req.dataset_ref,
        kind=req.kind,
        tool=tool_name,
        interpret=req.interpret,
    )
    try:
        result = stats._tools[tool_name].invoke(args)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except (SchemaValidationError, ValueError) as exc:
        # 入参不合法 / 列不存在 / 样本不足 —— 客户端可纠正
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 完整结果（含明细）返回前端；仅摘要喂模型生成解读（红线1），失败降级为 None
    interpretation = (
        await interpret_stats(req.kind, result, gateway, req.dataset_ref, req.params)
        if req.interpret
        else None
    )
    return StatsResponse(kind=req.kind, result=result, interpretation=interpretation)
