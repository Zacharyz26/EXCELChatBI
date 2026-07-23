"""v2.5 阶段 3 上下文压缩占位：超阈值时对早期轮次做滚动摘要。

保留最近 N 轮原文 + 全局摘要 + 活跃数据引用。摘要只能用于上下文导航，
不能替代工具 Evidence 或作为数值 Claim 来源。
"""

from __future__ import annotations

from packages.session.state import SessionState


def compact_if_needed(
    state: SessionState, token_threshold: int, keep_recent: int = 6
) -> SessionState:
    """必要时压缩历史：早期轮次摘要化，保留最近 keep_recent 轮原文。

    Args:
        state: 当前会话状态。
        token_threshold: 触发压缩的 token 阈值。
        keep_recent: 保留原文的最近轮数。
    """
    raise NotImplementedError("v2.5 阶段 3：实现 token 预算、滚动摘要及 Evidence 隔离")
