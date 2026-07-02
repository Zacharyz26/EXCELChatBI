"""OpenAI 兼容适配器（DeepSeek / Qwen-VL 等均经此接入）。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from packages.models.adapters.base import ModelAdapter
from packages.models.registry import ModelSpec
from packages.models.types import Message, ModelResponse


class OpenAICompatibleAdapter(ModelAdapter):
    """通过 OpenAI 兼容 HTTP 接口调用模型。"""

    def __init__(
        self, spec: ModelSpec, timeout_seconds: int = 60, max_retries: int = 2
    ) -> None:
        super().__init__(spec)
        if not spec.api_key:
            # 不硬编码 key；缺失时显式报错，提示去 .env 配置
            raise ValueError(
                f"模型 {spec.name} 缺少 API key，请在 .env 配置对应变量后重试"
            )
        self._client = AsyncOpenAI(
            base_url=spec.api_base or None,
            api_key=spec.api_key,
            timeout=timeout_seconds,
            max_retries=max_retries,
        )

    async def complete(
        self, messages: list[Message], **params: object
    ) -> ModelResponse:
        """同步补全，返回内容与可观测元数据。"""
        started = time.perf_counter()
        resp = await self._client.chat.completions.create(
            model=self._spec.model,
            messages=[{"role": m.role, "content": m.content} for m in messages],
            **params,  # 透传 temperature 等
        )
        latency_ms = (time.perf_counter() - started) * 1000
        usage = resp.usage
        return ModelResponse(
            content=resp.choices[0].message.content or "",
            model=self._spec.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
            cost=0.0,  # TODO: 按 registry 价目表计算成本
            raw={},
        )

    async def stream(
        self, messages: list[Message], **params: object
    ) -> AsyncIterator[str]:
        raise NotImplementedError("TODO: stream=True，逐 chunk yield 增量内容")
        yield ""  # pragma: no cover
