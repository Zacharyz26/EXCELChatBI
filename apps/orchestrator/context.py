"""v2.5 阶段 3 上下文/记忆装配占位。

v2.4 控制面先定义 TaskContext 与 Evidence；本模块后续负责受作用域约束的记忆、
指代消解和压缩，摘要不得替代原始 Evidence。
"""

from __future__ import annotations

from packages.session.state import SessionState


def prepare_context(session_id: str, raw_query: str) -> tuple[SessionState, str]:
    """加载会话状态 + 指代消解 + 必要时压缩，返回 (状态, 改写后 query)。"""
    raise NotImplementedError(
        "v2.5 阶段 3：加载作用域记忆 → 指代消解 → 上下文压缩"
    )
