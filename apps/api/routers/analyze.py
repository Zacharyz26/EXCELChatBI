"""分析出图接口：dataset_ref → DeepSeek 规划 → gen_chart → ECharts 配置。

链路：infer_schema(画像) → chart_planner(仅画像喂模型，红线1) → gen_chart(真实数据聚合，红线2)。
可靠性：模型规划无效（非法 JSON / 选了不存在的列 / 非法枚举）时，把错误回传模型
带错重规划一次；仍失败才 422。模型不可用/网络问题直接 502。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from mcp_servers.common.base_server import MCPServer
from packages.common.logging import get_logger
from packages.governance.schema_validator import SchemaValidationError
from packages.models.gateway import ModelGateway

from apps.api.deps import chart_tools_dep, excel_tools_dep, model_gateway_dep
from apps.api.schemas import AnalyzeRequest, ChartResponse
from apps.orchestrator.chart_planner import plan_chart

router = APIRouter(prefix="/analyze", tags=["analyze"])

_log = get_logger("api.analyze")
_MAX_ATTEMPTS = 2  # 初次 + 带错重规划一次


class _PlanFailure(Exception):
    """可重试的规划失败（模型输出/选列无效），非模型不可用。"""


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

    feedback: str | None = None
    last: _PlanFailure | None = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            result = await _plan_and_generate(profile, chart, gateway, feedback)
            return ChartResponse(**result)
        except _PlanFailure as exc:
            last = exc
            feedback = str(exc)
            _log.warning("analyze.replan", attempt=attempt + 1, reason=feedback)
        except RuntimeError as exc:  # 模型不可用/网络（所有候选失败）
            raise HTTPException(
                status_code=502,
                detail=f"模型规划失败（检查 DEEPSEEK_API_KEY 与网络）：{exc}",
            ) from exc

    raise HTTPException(status_code=422, detail=f"两次规划仍无法生成有效图表：{last}")


async def _plan_and_generate(
    profile: object, chart: MCPServer, gateway: ModelGateway, feedback: str | None
) -> dict:
    """规划一次并出图；规划/入参无效抛 _PlanFailure（可重试）。"""
    try:
        gen_args = await plan_chart(profile, gateway, feedback=feedback)  # type: ignore[arg-type]
    except ValueError as exc:  # 模型未吐合法 JSON
        raise _PlanFailure(f"模型未返回合法 JSON 规划：{exc}") from exc

    # 红线2/3：经 Tool.invoke 校验入参后，用真实数据聚合出图
    try:
        return chart._tools["gen_chart"].invoke(gen_args)
    except (SchemaValidationError, ValueError) as exc:
        enc = gen_args.get("encoding", {})
        raise _PlanFailure(
            f"图表入参无效(chart_type={gen_args.get('chart_type')}, "
            f"x={enc.get('x')}, y={enc.get('y')}): {exc}"
        ) from exc
