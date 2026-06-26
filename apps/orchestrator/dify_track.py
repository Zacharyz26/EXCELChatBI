"""A 轨：Dify Chatflow / Workflow 客户端骨架（RAG 问答 + 简单分析出图）。"""

from __future__ import annotations

from collections.abc import AsyncIterator


class DifyTrack:
    """调用 Dify Chatflow 完成 A 轨流程，流式返回。"""

    def __init__(self, api_base: str, api_key: str) -> None:
        self._api_base = api_base
        self._api_key = api_key

    async def run(self, session_id: str, query: str) -> AsyncIterator[str]:
        """调用 Dify，流式产出结果增量。"""
        raise NotImplementedError("TODO: 调 Dify Chatflow API，流式转发")
        yield ""  # pragma: no cover
