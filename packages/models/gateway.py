"""模型路由网关。

业务唯一入口：按场景分发到具体模型，统一封装重试、超时、降级（主模型不可用
切备选），并记录调用的模型、token、耗时、成本供治理层观测（设计文档 3.2 / 第7节）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from packages.models.registry import ModelRegistry
from packages.models.types import Message, ModelResponse, Scenario


class ModelGateway:
    """模型路由网关，对业务暴露 `complete` / `stream`。"""

    def __init__(self, registry: ModelRegistry) -> None:
        self._registry = registry

    async def complete(
        self,
        scenario: Scenario,
        messages: list[Message],
    ) -> ModelResponse:
        """同步补全。按场景路由 → 调用适配器 → 失败按 fallback 降级。

        Args:
            scenario: 路由场景。
            messages: 对话消息列表。
        """
        raise NotImplementedError(
            "TODO: registry.resolve(scenario) → 选适配器调用 → 重试/超时/降级 → 记录可观测"
        )

    async def stream(
        self,
        scenario: Scenario,
        messages: list[Message],
    ) -> AsyncIterator[str]:
        """流式补全（SSE 用），逐 token 产出。"""
        raise NotImplementedError("TODO: 流式调用适配器并 yield 增量")
        yield ""  # pragma: no cover  —— 标注此为异步生成器
