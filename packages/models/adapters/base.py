"""模型适配器抽象基类。"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator
from typing import Any

from packages.models.registry import ModelSpec
from packages.models.types import Message, ModelResponse


class ModelAdapter(abc.ABC):
    """所有模型适配器的统一接口。"""

    def __init__(self, spec: ModelSpec) -> None:
        self._spec = spec

    @abc.abstractmethod
    async def complete(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        **params: object,
    ) -> ModelResponse:
        """同步补全。

        Args:
            messages: 对话消息（含 assistant 的 tool_calls 与 tool 结果消息）。
            tools: OpenAI 兼容工具定义列表；None 表示本次不启用工具。
                工具是能力要求而非普通参数，故显式入参、不混入 params
                （params 走 drop_params 过滤，tools 走候选跳过，决策10）。
            **params: 透传的采样等参数（temperature / response_format …）。
        """

    @abc.abstractmethod
    def stream(
        self, messages: list[Message], **params: object
    ) -> AsyncIterator[str]:
        """流式补全，逐段产出文本增量。

        不支持 tools：Agent 循环的工具调用轮走 `complete`，仅最终答复轮
        用 stream 流式输出（第 14 章 14.5.1 的设计取舍）。
        """
