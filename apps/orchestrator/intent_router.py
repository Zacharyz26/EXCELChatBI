"""意图路由 + 双轨判定（设计文档 5.2.1）。

判定顺序：意图分类 → 多步信号 → 工具数量预估 → 追问上下文 → 默认 A 轨。
**判定阈值（步骤数/工具数）属 CLAUDE 第9节"待确认"。** MVP 阶段先全部走 A 轨，
B 轨随阶段二/三引入；此处仅留判定骨架，不写死阈值。
"""

from __future__ import annotations

from enum import Enum


class Track(str, Enum):
    """编排轨道。"""

    A = "dify"          # 低代码：RAG 问答 / 单步分析
    B = "langgraph"     # 复杂多步（MVP 暂不启用）


def route(query: str) -> Track:
    """判定 query 应走的轨道。MVP 默认返回 A 轨。

    Args:
        query: 用户输入（已做指代消解）。
    """
    raise NotImplementedError(
        "TODO: 实现 5.2.1 判定规则；阈值待确认前 MVP 固定返回 Track.A"
    )
