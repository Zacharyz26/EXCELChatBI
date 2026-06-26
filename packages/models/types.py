"""模型层公共类型：场景枚举、消息、调用结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class Scenario(str, Enum):
    """模型路由场景（对应 config/models.yaml 的 routes）。"""

    CORE_REASONING = "core_reasoning"        # 核心推理：意图/规划/解读/代码生成
    COMPLEX_REASONING = "complex_reasoning"  # 复杂多步（B轨，MVP 暂不启用）
    VISION = "vision"                        # 多模态识图
    LIGHTWEIGHT = "lightweight"              # 轻量：改写/分类/指代消解


@dataclass
class Message:
    """对话消息。role ∈ {system, user, assistant, tool}。"""

    role: str
    content: str


@dataclass
class ModelResponse:
    """一次模型调用的结果与可观测元数据。"""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost: float = 0.0
    raw: dict = field(default_factory=dict)
