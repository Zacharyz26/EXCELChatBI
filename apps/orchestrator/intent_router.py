"""已废弃的 Dify/LangGraph 双轨路由骨架。

v2.4 使用统一 TaskPlan schema 下的简单快速路径、模板路径和开放规划路径，
不会实现本模块。保留枚举只为识别历史引用。
"""

from __future__ import annotations

from enum import Enum


class Track(str, Enum):
    """编排轨道。"""

    A = "dify"          # 已废弃的历史值
    B = "langgraph"     # 已废弃的历史值


def route(query: str) -> Track:
    """拒绝使用已废弃的双轨路由。

    Args:
        query: 用户输入（已做指代消解）。
    """
    raise NotImplementedError("A/B 双轨已废弃；请使用 v2.4 统一 Planner 路径")
