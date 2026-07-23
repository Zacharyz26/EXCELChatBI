"""已废弃的 Dify 原型，占位留档且不参与运行。

编排主体已经确定为自研；不得在 v2.4 Agent 控制面中恢复此 A 轨。
"""

from __future__ import annotations

from collections.abc import AsyncIterator


class DifyTrack:
    """历史接口，仅用于识别旧引用；调用始终报未实现。"""

    def __init__(self, api_base: str, api_key: str) -> None:
        self._api_base = api_base
        self._api_key = api_key

    async def run(self, session_id: str, query: str) -> AsyncIterator[str]:
        """拒绝调用已废弃的 Dify 路径。"""
        raise NotImplementedError("Dify 路径已废弃；请使用统一自研 Agent 控制面")
        yield ""  # pragma: no cover
