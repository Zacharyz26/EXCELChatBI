"""模型路由网关。

业务唯一入口：按场景分发到具体模型，统一封装重试、超时、降级（主模型不可用
切备选），并记录调用的模型、token、耗时、成本供治理层观测（设计文档 3.2 / 第7节）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from openai import OpenAIError

from packages.common.logging import get_logger
from packages.models.adapters.base import ModelAdapter
from packages.models.adapters.openai_compatible import OpenAICompatibleAdapter
from packages.models.registry import ModelRegistry, ModelSpec
from packages.models.types import Message, ModelResponse, Scenario

_log = get_logger("models.gateway")


class ModelGateway:
    """模型路由网关，对业务暴露 `complete` / `stream`。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry
        # 按模型名缓存 adapter（内含 httpx 连接池），跨请求复用、不泄漏连接
        self._adapters: dict[str, ModelAdapter] = {}

    def _get_adapter(self, spec: ModelSpec) -> ModelAdapter:
        """取该模型的 adapter，首次构造后缓存复用；构造失败（如 key 缺失）不缓存。"""
        adapter = self._adapters.get(spec.name)
        if adapter is None:
            defaults = self._registry.defaults
            adapter = OpenAICompatibleAdapter(
                spec,
                timeout_seconds=defaults.timeout_seconds,
                max_retries=defaults.max_retries,
            )
            self._adapters[spec.name] = adapter
        return adapter

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
                模型不支持的参数按 registry 中该模型的 drop_params 自动剥掉
                （如推理型 fallback 不支持 response_format），保证降级可用。

        Raises:
            RuntimeError: 主选与所有备选模型均失败。
        """
        route = self._registry.resolve(scenario)
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
            # 按模型能力过滤参数（drop_params 来自 registry 配置）：
            # 主选支持的参数（如 response_format）备选未必支持，不剥掉则降级必失败。
            call_params: dict[str, object] = {"temperature": route.temperature, **extra}
            call_params = {k: v for k, v in call_params.items() if k not in spec.drop_params}
            try:
                adapter = self._get_adapter(spec)
                resp = await adapter.complete(messages, **call_params)
                _log.info(
                    "model.response",
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    latency_ms=round(resp.latency_ms, 1),
                )
                return resp
            # 只把"模型不可用"降级到下一候选：OpenAIError 覆盖 API/网络/超时/限流，
            # ValueError 为 adapter 构造时 API key 缺失（保持"没配 key → 友好 502"）。
            # TypeError/KeyError 等编程错误不吞，正常抛出暴露 bug。
            except (OpenAIError, ValueError) as exc:
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
