"""模型路由网关 + registry。

业务层只按"场景"（Scenario）请求模型，由网关按 model registry 路由到具体模型，
并统一封装重试、超时、降级。模型名只存在于 registry 配置，业务代码禁止硬编码
（CLAUDE 第4节）。
"""

from packages.models.gateway import ModelGateway
from packages.models.types import Scenario

__all__ = ["ModelGateway", "Scenario"]
