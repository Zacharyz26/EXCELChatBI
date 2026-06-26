"""OpenAI 兼容适配器（DeepSeek / Qwen-VL 等均经此接入）。"""

from __future__ import annotations

from collections.abc import AsyncIterator

from packages.models.adapters.base import ModelAdapter
from packages.models.types import Message, ModelResponse


class OpenAICompatibleAdapter(ModelAdapter):
    """通过 OpenAI 兼容 HTTP 接口调用模型。"""

    async def complete(
        self, messages: list[Message], **params: object
    ) -> ModelResponse:
        raise NotImplementedError(
            "TODO: 用 openai SDK 指向 spec.api_base，调用 chat.completions，组装 ModelResponse"
        )

    async def stream(
        self, messages: list[Message], **params: object
    ) -> AsyncIterator[str]:
        raise NotImplementedError("TODO: stream=True，逐 chunk yield 增量内容")
        yield ""  # pragma: no cover
