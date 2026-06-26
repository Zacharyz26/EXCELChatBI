"""Model registry：从 config/models.yaml 加载模型与路由配置。

支持按场景取主选 / 备选模型，便于灰度切换、A/B、按租户配置。
模型名只在配置中出现，不进入业务代码（CLAUDE 第4节）。
"""

from __future__ import annotations

from dataclasses import dataclass

from packages.models.types import Scenario


@dataclass
class ModelSpec:
    """单个模型的接入规格（解析自 registry）。"""

    name: str
    provider: str
    model: str
    api_base: str
    api_key: str


@dataclass
class RouteSpec:
    """某场景的路由规格：主选 + 备选 + 采样参数。"""

    primary: str
    fallback: list[str]
    temperature: float = 0.3


class ModelRegistry:
    """模型注册表。负责加载配置并按场景解析路由。"""

    def __init__(self, registry_path: str) -> None:
        self._registry_path = registry_path

    def load(self) -> None:
        """读取 yaml、展开 ${ENV} 占位，构建 models / routes 索引。"""
        raise NotImplementedError("TODO: 解析 models.yaml，env 变量注入 api_key/api_base")

    def resolve(self, scenario: Scenario) -> RouteSpec:
        """返回某场景的路由规格。"""
        raise NotImplementedError("TODO: 返回 routes[scenario]")

    def get_model(self, name: str) -> ModelSpec:
        """按模型名返回接入规格。"""
        raise NotImplementedError("TODO: 返回 models[name]")
