"""模型路由网关。

业务唯一入口：按场景分发到具体模型，统一封装重试、超时、降级（主模型不可用
切备选），并记录调用的模型、token、耗时、成本供治理层观测（设计文档 3.2 / 第7节）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from packages.common.logging import get_logger
from packages.models.adapters.openai_compatible import OpenAICompatibleAdapter
from packages.models.registry import ModelRegistry
from packages.models.types import Message, ModelResponse, Scenario

_log = get_logger("models.gateway")


class ModelGateway:
    """模型路由网关，对业务暴露 `complete` / `stream`。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> ModelResponse:
        """同步补全。按场景路由 → 调用适配器 → 失败按 fallback 降级。

        Args:
            scenario: 路由场景。
            messages: 对话消息列表。
            params: 透传给底层接口的额外参数（如 response_format）。
                注意：并非所有模型都支持（如推理型 fallback 可能不支持
                response_format），故仅在需要时由调用方按主模型能力传入。

        Raises:
            RuntimeError: 主选与所有备选模型均失败。
        """
        route = self._registry.resolve(scenario)
        defaults = self._registry.defaults
        candidates = [route.primary, *route.fallback]
        extra = params or {}

        # 红线1 可观测：记录发往模型的 payload 概况（不含原始数据，仅角色与长度）
        _log.info(
            "model.request",
            scenario=scenario.value,
            candidates=candidates,
            message_roles=[m.role for m in messages],
            payload_chars=sum(len(m.content) for m in messages),
        )

        last_error: Exception | None = None
        for name in candidates:
            spec = self._registry.get_model(name)
            try:
                adapter = OpenAICompatibleAdapter(
                    spec,
                    timeout_seconds=defaults.timeout_seconds,
                    max_retries=defaults.max_retries,
                )
                resp = await adapter.complete(
                    messages, temperature=route.temperature, **extra
                )
                _log.info(
                    "model.response",
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    latency_ms=round(resp.latency_ms, 1),
                )
                return resp
            except Exception as exc:  # 降级到下一个备选
                last_error = exc
                _log.warning("model.fallback", model=name, error=str(exc))

        raise RuntimeError(f"所有候选模型均失败（{candidates}）: {last_error}")

    async def stream(
        self,
        scenario: Scenario,
        messages: list[Message],
    ) -> AsyncIterator[str]:
        """流式补全（SSE 用），逐 token 产出。本切片暂未实现。"""
        raise NotImplementedError("TODO: 流式调用适配器并 yield 增量")
        yield ""  # pragma: no cover
