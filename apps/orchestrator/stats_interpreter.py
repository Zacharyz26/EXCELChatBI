"""统计结果 → LLM 中文解读：摘要提取（红线1）+ 网关调用（红线2）。

红线1 的唯一出口：本模块是统计结果进入模型的唯一通道，集中守门——
`extract_summary` 用**白名单**从完整结果里抽出摘要（只挑允许字段重建新 dict，
而非从结果里删字段），因此 trend 的逐行分量、anomaly 的逐点原始 value 等明细
绝无可能进入喂给模型的 payload；将来工具即使新增明细字段也默认不外泄。
红线2：解读只是把工具算出的真实数字翻译成中文，禁止模型自行计算或编造统计量。
降级：模型不可用/超时时返回 None，统计结果照常返回，不因解读失败拖垮接口。
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from packages.common.logging import get_logger
from packages.models.types import Message, ModelResponse, Scenario

_log = get_logger("orchestrator.stats_interpreter")


class _Gateway(Protocol):
    """模型网关最小接口（便于测试替身）。"""

    async def complete(
        self, scenario: Scenario, messages: list[Message], *, params: dict[str, object] | None = ...
    ) -> ModelResponse: ...


_KIND_LABEL = {"trend": "趋势分析", "anomaly": "异常检测", "regression": "回归分析"}

_SYSTEM_PROMPT = """你是一名 BI 数据解读助手。你只会收到一次统计分析的**结果摘要**\
（不含任何逐行明细或原始数据）。请用简洁、通顺的中文，写出对业务有意义的洞察解读。

规则：
- 只解读所给摘要中的数字，**禁止自己计算或编造任何新的数字/统计量**；\
你引用的每个数值都必须能在摘要里找到。
- 抓住关键结论：趋势的方向与预测、异常的规模与占比、回归中显著的驱动因素等。
- 直接输出中文段落，不要输出 JSON、代码块或列表标记。"""


def extract_summary(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """从完整统计结果里**白名单**抽取可喂给 LLM 的摘要（红线1）。

    绝不纳入：trend 的 points.{trend,seasonal,resid} 逐行数组与 time 全数组、
    anomaly 的 anomalies[] 逐点 index/value/score。只保留聚合量与结论。

    Args:
        kind: trend | anomaly | regression。
        result: 统计工具的完整输出（含明细）。

    Returns:
        仅含摘要字段的新 dict。

    Raises:
        ValueError: 未知统计类型。
    """
    if kind == "trend":
        time = result.get("time") or []
        return {
            "method": result.get("method"),
            "direction": result.get("direction"),
            "slope": result.get("slope"),
            "seasonality_strength": result.get("seasonality_strength"),
            "ma_window": result.get("ma_window"),
            "n": result.get("n"),
            "forecast": result.get("forecast"),
            # 只取首末时间点，不发逐行时间数组
            "time_start": time[0] if time else None,
            "time_end": time[-1] if time else None,
        }

    if kind == "anomaly":
        n_total = result.get("n_total") or 0
        n_anom = result.get("n_anomalies") or 0
        anomalies = result.get("anomalies") or []
        # 只对异常值做聚合描述（范围/均值/数量），绝不逐个下发原始 value
        vals = [a["value"] for a in anomalies if isinstance(a.get("value"), int | float)]
        scores = [a["score"] for a in anomalies if isinstance(a.get("score"), int | float)]
        summary: dict[str, Any] = {
            "method": result.get("method"),
            "n_total": n_total,
            "n_anomalies": n_anom,
            "anomaly_rate": round(n_anom / n_total, 4) if n_total else None,
            "max_score": round(max(scores), 6) if scores else None,
        }
        if vals:
            summary["anomaly_value_summary"] = {
                "min": min(vals),
                "max": max(vals),
                "mean": round(sum(vals) / len(vals), 6),
            }
        return summary

    if kind == "regression":
        # 回归结果本就是聚合量（系数表 / R² / p 值），无逐行明细
        return {
            "kind": result.get("kind"),
            "r_squared": result.get("r_squared"),
            "adj_r_squared": result.get("adj_r_squared"),
            "n_obs": result.get("n_obs"),
            "model_pvalue": result.get("model_pvalue"),
            "coefficients": result.get("coefficients"),
        }

    raise ValueError(f"未知统计类型: {kind}")


def build_messages(kind: str, summary: dict[str, Any]) -> list[Message]:
    """构造发往模型的消息：system 约束 + 仅含摘要的 user。

    红线1 可观测：把发出的摘要与其字段名整体打进日志（sends_detail=False），
    因为发出去的就是白名单摘要本身，日志摊开即可证明不含 points/异常原值。
    """
    _log.info(
        "stats.interpret.payload",
        kind=kind,
        summary_keys=sorted(summary.keys()),
        summary=summary,
        sends_detail=False,
    )
    user = (
        f"分析类型：{_KIND_LABEL.get(kind, kind)}\n"
        "结果摘要（JSON）：\n" + json.dumps(summary, ensure_ascii=False)
    )
    return [
        Message(role="system", content=_SYSTEM_PROMPT),
        Message(role="user", content=user),
    ]


async def interpret_stats(
    kind: str, result: dict[str, Any], gateway: _Gateway
) -> str | None:
    """把统计结果摘要交给模型，生成中文解读；失败降级返回 None。

    Args:
        kind: 统计类型。
        result: 统计工具的完整输出（本函数内经 extract_summary 收敛后才喂模型）。
        gateway: 模型网关（不硬编码模型名，按 CORE_REASONING 场景路由）。

    Returns:
        中文解读文本；模型不可用/超时时返回 None（红线：解读失败不拖垮统计接口）。
    """
    summary = extract_summary(kind, result)
    messages = build_messages(kind, summary)
    try:
        resp = await gateway.complete(Scenario.CORE_REASONING, messages)
    except RuntimeError as exc:  # 主选与所有备选均失败（模型不可用/网络/超时）
        _log.warning("stats.interpret.degraded", kind=kind, reason=str(exc))
        return None
    text = resp.content.strip()
    return text or None
