"""OpenAI 兼容适配器（DeepSeek / Qwen-VL 等均经此接入）。"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from packages.models.adapters.base import ModelAdapter
from packages.models.registry import ModelSpec
from packages.models.types import Message, ModelResponse, ToolCall


def _to_wire(messages: list[Message]) -> list[dict[str, Any]]:
    """把内部 Message 转为 OpenAI 兼容的消息字典。

    - assistant 消息若带 tool_calls，按 function calling 线格式展开
      （content 允许为 null，历史回填时保持原样）；
    - tool 消息带 tool_call_id，对应模型发起的那次调用。
    """
    wire: list[dict[str, Any]] = []
    for m in messages:
        item: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            item["content"] = m.content or None
            item["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in m.tool_calls
            ]
        if m.tool_call_id:
            item["tool_call_id"] = m.tool_call_id
        wire.append(item)
    return wire


def _parse_tool_calls(message: Any) -> list[ToolCall]:
    """从接口返回的 message 解析 tool_calls；arguments 保持原样 JSON 字符串。

    解析与 schema 校验（红线3）由 Agent 编排层负责，此处不做有损转换。
    """
    calls = getattr(message, "tool_calls", None) or []
    out: list[ToolCall] = []
    for tc in calls:
        fn = getattr(tc, "function", None)
        if fn is None:
            continue
        out.append(
            ToolCall(
                id=str(getattr(tc, "id", "") or ""),
                name=str(getattr(fn, "name", "") or ""),
                arguments=str(getattr(fn, "arguments", "") or ""),
            )
        )
    return out


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
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]] | None = None,
        **params: object,
    ) -> ModelResponse:
        """同步补全，返回内容、tool_calls 与可观测元数据。"""
        started = time.perf_counter()
        kwargs: dict[str, Any] = dict(params)
        if tools is not None:
            kwargs["tools"] = tools
        resp = await self._client.chat.completions.create(
            model=self._spec.model,
            # 线格式在 _to_wire 构造并有测试兜底；cast 适配 SDK 的 TypedDict 入参
            messages=cast(list[ChatCompletionMessageParam], _to_wire(messages)),
            **kwargs,
        )
        latency_ms = (time.perf_counter() - started) * 1000
        usage = resp.usage
        message = resp.choices[0].message
        return ModelResponse(
            content=message.content or "",
            model=self._spec.model,
            prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
            completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
            latency_ms=latency_ms,
            cost=0.0,  # TODO: 按 registry 价目表计算成本
            raw={},
            tool_calls=_parse_tool_calls(message),
        )

    async def stream(
        self, messages: list[Message], **params: object
    ) -> AsyncIterator[str]:
        """流式补全，逐段 yield 文本增量（不支持 tools，见基类说明）。"""
        kwargs: dict[str, Any] = dict(params)
        stream = await self._client.chat.completions.create(
            model=self._spec.model,
            # 线格式在 _to_wire 构造并有测试兜底；cast 适配 SDK 的 TypedDict 入参
            messages=cast(list[ChatCompletionMessageParam], _to_wire(messages)),
            stream=True,
            **kwargs,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue  # 部分供应商的末尾 usage chunk 无 choices
            delta = chunk.choices[0].delta
            piece = getattr(delta, "content", None)
            if piece:
                yield piece
