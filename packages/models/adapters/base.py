"""模型适配器抽象基类。"""

from __future__ import annotations

import abc
from collections.abc import AsyncIterator

from packages.models.registry import ModelSpec
from packages.models.types import Message, ModelResponse


class ModelAdapter(abc.ABC):
    """所有模型适配器的统一接口。"""

    def __init__(self, spec: ModelSpec) -> None:
        self._spec = spec

    @abc.abstractmethod
    async def complete(
        self, messages: list[Message], **params: object
    ) -> ModelResponse:
        """同步补全。"""

    @abc.abstractmethod
    def stream(
        self, messages: list[Message], **params: object
    ) -> AsyncIterator[str]:
        """流式补全。"""
