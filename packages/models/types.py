"""模型层公共类型：场景枚举、消息、调用结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Scenario(str, Enum):
    """模型路由场景（对应 config/models.yaml 的 routes）。"""

    CORE_REASONING = "core_reasoning"        # 核心推理：意图/规划/解读/代码生成
    COMPLEX_REASONING = "complex_reasoning"  # 复杂多步（B轨，MVP 暂不启用）
    VISION = "vision"                        # 多模态识图
    LIGHTWEIGHT = "lightweight"              # 轻量：改写/分类/指代消解
    AGENT = "agent"                          # 对话式 Agent（function-calling，14 章；决策10）


@dataclass
class ToolCall:
    """模型发起的一次工具调用（OpenAI 兼容 function calling）。

    arguments 保持接口返回的**原样 JSON 字符串**，不在模型层解析——
    解析与 schema 校验（红线3）由 Agent 编排层在 Tool.invoke 前完成，
    模型层不做有损转换。
    """

    id: str
    name: str
    arguments: str


@dataclass
class Message:
    """对话消息。role ∈ {system, user, assistant, tool}。

    - assistant 消息可携带 tool_calls（模型发起的调用，回填历史时原样带上）；
    - tool 消息须携带 tool_call_id（对应哪次调用的执行结果）。
    """

    role: str
    content: str
    tool_calls: list[ToolCall] | None = None   # 仅 assistant 消息使用
    tool_call_id: str | None = None            # 仅 tool 消息使用


@dataclass
class ModelResponse:
    """一次模型调用的结果与可观测元数据。"""

    content: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0
    cost: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolCall] = field(default_factory=list)  # 模型要求调用的工具
