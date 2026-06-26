"""上下文压缩：超 token 阈值时对早期轮次做 LLM 滚动摘要。

保留最近 N 轮原文 + 全局摘要 + 活跃数据引用（设计文档 5.2.2）。
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
    raise NotImplementedError("TODO: 估算 token；超阈值则 LLM 摘要早期轮次写入 global_summary")
