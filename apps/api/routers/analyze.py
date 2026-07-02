"""分析出图接口：dataset_ref → DeepSeek 规划 → gen_chart → ECharts 配置。

链路：infer_schema(画像) → chart_planner(仅画像喂模型，红线1) → gen_chart(真实数据聚合，红线2)。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mcp_servers.common.base_server import MCPServer
from packages.governance.schema_validator import SchemaValidationError
from packages.models.gateway import ModelGateway

from apps.api.deps import chart_tools_dep, excel_tools_dep, model_gateway_dep
from apps.api.schemas import AnalyzeRequest, ChartResponse
from apps.orchestrator.chart_planner import plan_chart

router = APIRouter(prefix="/analyze", tags=["analyze"])


@router.post("", response_model=ChartResponse)
async def analyze(
    req: AnalyzeRequest,
    excel: MCPServer = Depends(excel_tools_dep),
    chart: MCPServer = Depends(chart_tools_dep),
    gateway: ModelGateway = Depends(model_gateway_dep),
) -> ChartResponse:
    """基于已上传数据集，自动规划并生成一张 ECharts 图。"""
    try:
        profile = excel._tools["infer_schema"].invoke({"dataset_ref": req.dataset_ref})
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # 红线1：plan_chart 内部只把画像喂模型
    gen_args = await _safe_plan(profile, gateway)

    # 红线2/3：经 Tool.invoke 校验入参后，用真实数据聚合出图
    try:
        result = chart._tools["gen_chart"].invoke(gen_args)
    except SchemaValidationError as exc:
        raise HTTPException(status_code=422, detail=f"图表入参非法: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    return ChartResponse(**result)


async def _safe_plan(profile: object, gateway: ModelGateway) -> dict:
    """调用规划，模型不可用时返回友好错误（设计文档第7节降级）。"""
    try:
        return await plan_chart(profile, gateway)  # type: ignore[arg-type]
    except Exception as exc:
        raise HTTPException(
            status_code=502, detail=f"模型规划失败（检查 DEEPSEEK_API_KEY 与网络）：{exc}"
        ) from exc
