"""编排侧会话上下文封装：复用 packages.session（状态/压缩/指代消解）。"""

from __future__ import annotations

from packages.session.state import SessionState


def prepare_context(session_id: str, raw_query: str) -> tuple[SessionState, str]:
    """加载会话状态 + 指代消解 + 必要时压缩，返回 (状态, 改写后 query)。"""
    raise NotImplementedError(
        "TODO: SessionStore.load → coref.resolve_reference → compact_if_needed"
    )
