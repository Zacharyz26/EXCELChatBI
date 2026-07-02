"""图表规划：把"数据画像"喂给 DeepSeek，得到一个图表规划（不含任何数值）。

红线1 的唯一出口：本模块是 DataProfile 进入模型的唯一通道，集中守门——
喂给模型的 payload 只有画像，绝无原始整表数据。
红线2：模型只产出 chart_type + 列映射，数值由 gen_chart 读真实数据计算。
"""

from __future__ import annotations

import json
from typing import Any

from mcp_servers.excel_parser.profile import DataProfile
from packages.common.logging import get_logger
from packages.models.gateway import ModelGateway
from packages.models.types import Message, Scenario

_log = get_logger("orchestrator.chart_planner")

_SYSTEM_PROMPT = """你是一名 BI 图表规划助手。你只会收到一张表的"数据画像"（列名、类型、\
空值率、统计摘要、少量样本行），绝不会收到完整原始数据。

请基于画像，为这张表选出**最合适的一张图**，并只输出严格 JSON（不要解释、不要代码块）：
{
  "chart_type": "line|bar|pie|scatter",
  "x": "用作维度/时间轴的列名",
  "y": "用作度量的列名",
  "agg": "sum|mean|count|none",
  "reason": "一句中文理由"
}

规则：
- 时间趋势优先 line；类目对比用 bar；占比用 pie；两数值相关用 scatter（agg=none）。
- x/y 必须是画像中真实存在的列名。
- 你**不要**输出任何具体数值或聚合结果，数值一律由后续工具基于真实数据计算。"""


def build_messages(profile: DataProfile) -> list[Message]:
    """构造发往模型的消息：system + 仅含画像的 user。"""
    payload = profile.to_dict()
    # 红线1 可观测：明确记录"只发画像"，便于审计/演示验证
    _log.info(
        "planner.payload",
        dataset_ref=profile.dataset_ref,
        columns=[c["name"] for c in payload["columns"]],
        sample_rows=len(payload["sample_rows"]),
        sends_raw_data=False,
    )
    user = "数据画像如下（JSON）：\n" + json.dumps(payload, ensure_ascii=False)
    return [Message(role="system", content=_SYSTEM_PROMPT), Message(role="user", content=user)]


async def plan_chart(profile: DataProfile, gateway: ModelGateway) -> dict[str, Any]:
    """调用模型规划图表，返回可直接喂给 gen_chart 的入参。

    Returns:
        {dataset_ref, chart_type, encoding:{x, y, agg}}。
    """
    messages = build_messages(profile)
    resp = await gateway.complete(Scenario.CORE_REASONING, messages)
    plan = _parse_plan(resp.content)
    return {
        "dataset_ref": profile.dataset_ref,
        "chart_type": plan["chart_type"],
        "encoding": {"x": plan["x"], "y": plan["y"], "agg": plan.get("agg", "sum")},
    }


def _parse_plan(content: str) -> dict[str, Any]:
    """从模型输出中解析 JSON 规划，容忍 ```json 代码块包裹。"""
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"模型未返回有效 JSON 规划: {content!r}")
    plan = json.loads(text[start : end + 1])
    for key in ("chart_type", "x", "y"):
        if key not in plan:
            raise ValueError(f"图表规划缺少字段 {key}: {plan}")
    return plan
