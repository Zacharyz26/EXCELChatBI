"""模型路由网关。

业务唯一入口：按场景分发到具体模型，统一封装重试、超时、降级（主模型不可用
切备选），并记录调用的模型、token、耗时、成本供治理层观测（设计文档 3.2 / 第7节）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

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
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelResponse:
        """同步补全。按场景路由 → 调用适配器 → 失败按 fallback 降级。

        Args:
            scenario: 路由场景。
            messages: 对话消息列表。
            params: 透传给底层接口的额外参数（如 response_format）。
                模型不支持的参数按 registry 中该模型的 drop_params 自动剥掉
                （如推理型 fallback 不支持 response_format），保证降级可用。
            tools: OpenAI 兼容工具定义；None 表示不启用。工具是能力要求而非
                普通参数：带 tools 的请求**跳过** supports_tools=False 的候选，
                而非剥掉 tools 静默降级成"不会用工具的聊天"（决策10）。

        Raises:
            RuntimeError: 主选与所有备选模型均失败（或均不支持工具调用）。
        """
        route = self._registry.resolve(scenario)
        candidates = [route.primary, *route.fallback]
        extra = params or {}

        # 红线1 可观测：记录发往模型的 payload 概况（不含原始数据，仅角色与长度）
        _log.info(
            "model.request",
            scenario=scenario.value,
            candidates=candidates,
            with_tools=tools is not None,
            message_roles=[m.role for m in messages],
            payload_chars=sum(len(m.content) for m in messages),
        )

        last_error: Exception | None = None
        for name in candidates:
            spec = self._registry.get_model(name)
            if tools is not None and not spec.supports_tools:
                # 决策10：不支持 function calling 的候选直接跳过，绝不静默丢工具
                _log.warning("model.skip_no_tools", model=name, scenario=scenario.value)
                continue
            # 按模型能力过滤参数（drop_params 来自 registry 配置）：
            # 主选支持的参数（如 response_format）备选未必支持，不剥掉则降级必失败。
            call_params: dict[str, object] = {"temperature": route.temperature, **extra}
            call_params = {k: v for k, v in call_params.items() if k not in spec.drop_params}
            try:
                adapter = self._get_adapter(spec)
                resp = await adapter.complete(messages, tools=tools, **call_params)
                _log.info(
                    "model.response",
                    model=resp.model,
                    prompt_tokens=resp.prompt_tokens,
                    completion_tokens=resp.completion_tokens,
                    latency_ms=round(resp.latency_ms, 1),
                    tool_call_count=len(resp.tool_calls),
                )
                return resp
            # 只把"模型不可用"降级到下一候选：OpenAIError 覆盖 API/网络/超时/限流，
            # ValueError 为 adapter 构造时 API key 缺失（保持"没配 key → 友好 502"）。
            # TypeError/KeyError 等编程错误不吞，正常抛出暴露 bug。
            except (OpenAIError, ValueError) as exc:
                last_error = exc
                _log.warning("model.fallback", model=name, error=str(exc))

        if last_error is None and tools is not None:
            raise RuntimeError(
                f"场景 {scenario.value} 的候选模型（{candidates}）均不支持工具调用；"
                "请在 config/models.yaml 为该场景配置 supports_tools 的模型（决策10）"
            )
        raise RuntimeError(f"所有候选模型均失败（{candidates}）: {last_error}")

    async def stream(
        self,
        scenario: Scenario,
        messages: list[Message],
        *,
        params: dict[str, object] | None = None,
    ) -> AsyncIterator[str]:
        """流式补全（SSE 用），逐段产出文本增量。

        降级语义：**首个增量产出之前**失败（连接/鉴权/限流）→ 切下一候选；
        已开始产出后中途失败 → 如实上抛，不换模型重来（避免用户看到
        两个模型拼接的答案）。不支持 tools（Agent 工具轮走 complete）。

        Raises:
            RuntimeError: 所有候选模型均在开流前失败。
        """
        route = self._registry.resolve(scenario)
        candidates = [route.primary, *route.fallback]
        extra = params or {}

        _log.info(
            "model.stream.request",
            scenario=scenario.value,
            candidates=candidates,
            message_roles=[m.role for m in messages],
            payload_chars=sum(len(m.content) for m in messages),
        )

        last_error: Exception | None = None
        for name in candidates:
            spec = self._registry.get_model(name)
            call_params: dict[str, object] = {"temperature": route.temperature, **extra}
            call_params = {k: v for k, v in call_params.items() if k not in spec.drop_params}
            try:
                adapter = self._get_adapter(spec)
                chunks = aiter(adapter.stream(messages, **call_params))
                first = await anext(chunks, None)
            except (OpenAIError, ValueError) as exc:
                last_error = exc
                _log.warning("model.stream.fallback", model=name, error=str(exc))
                continue
            # 已成功开流（含空流）：之后的错误如实上抛，不再降级
            if first is not None:
                yield first
                async for piece in chunks:
                    yield piece
            _log.info("model.stream.done", model=spec.model)
            return

        raise RuntimeError(f"所有候选模型均失败（{candidates}）: {last_error}")
