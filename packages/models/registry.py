"""Model registry：从 config/models.yaml 加载模型与路由配置。

支持按场景取主选 / 备选模型，便于灰度切换、A/B、按租户配置。
模型名只在配置中出现，不进入业务代码（CLAUDE 第4节）。配置中的 ${ENV} 占位
从环境变量 / .env 注入，密钥不落代码。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # 存量：yaml 无内置 stubs（装 types-PyYAML 可移除）
from dotenv import dotenv_values

from packages.models.types import Scenario

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


@dataclass
class ModelSpec:
    """单个模型的接入规格（解析自 registry）。

    drop_params: 该模型不支持的调用参数名（如推理型不支持 response_format），
    网关分发时会剥掉这些参数，保证降级到该模型时调用仍可用。

    supports_tools: 该模型是否支持 function calling。工具与普通参数不同，
    **不可静默剥掉**——带工具的请求降级到不支持的模型会变成"不会用工具的
    聊天"（比报错更糟，决策10）。网关对带 tools 的请求直接**跳过**不支持
    的候选，而非剥参数。
    """

    name: str
    provider: str
    model: str
    api_base: str
    api_key: str
    drop_params: list[str] = field(default_factory=list)
    supports_tools: bool = True


@dataclass
class RouteSpec:
    """某场景的路由规格：主选 + 备选 + 采样参数。"""

    primary: str
    fallback: list[str] = field(default_factory=list)
    temperature: float = 0.3


@dataclass
class Defaults:
    """统一调用策略默认值。"""

    timeout_seconds: int = 60
    max_retries: int = 2


class ModelRegistry:
    """模型注册表。负责加载配置并按场景解析路由。"""

    def __init__(self, registry_path: str) -> None:
        self._registry_path = registry_path
        self._models: dict[str, ModelSpec] = {}
        self._routes: dict[str, RouteSpec] = {}
        self._defaults = Defaults()
        self._loaded = False

    def load(self) -> None:
        """读取 yaml、展开 ${ENV} 占位，构建 models / routes 索引。"""
        raw = yaml.safe_load(Path(self._registry_path).read_text(encoding="utf-8"))
        env = self._env_mapping()

        providers: dict[str, dict[str, str]] = {}
        for name, cfg in (raw.get("providers") or {}).items():
            providers[name] = {
                "api_base": self._expand(cfg.get("api_base", ""), env),
                "api_key": self._expand(cfg.get("api_key", ""), env),
            }

        for name, cfg in (raw.get("models") or {}).items():
            provider = cfg["provider"]
            pcfg = providers.get(provider, {"api_base": "", "api_key": ""})
            self._models[name] = ModelSpec(
                name=name,
                provider=provider,
                model=cfg["model"],
                api_base=pcfg["api_base"],
                api_key=pcfg["api_key"],
                drop_params=[str(p) for p in (cfg.get("drop_params") or [])],
                supports_tools=bool(cfg.get("supports_tools", True)),
            )

        for scenario, cfg in (raw.get("routes") or {}).items():
            self._routes[scenario] = RouteSpec(
                primary=cfg["primary"],
                fallback=list(cfg.get("fallback") or []),
                temperature=float(cfg.get("temperature", 0.3)),
            )

        defaults = raw.get("defaults") or {}
        self._defaults = Defaults(
            timeout_seconds=int(defaults.get("timeout_seconds", 60)),
            max_retries=int(defaults.get("max_retries", 2)),
        )
        self._loaded = True

    def resolve(self, scenario: Scenario) -> RouteSpec:
        """返回某场景的路由规格。"""
        self._ensure_loaded()
        try:
            return self._routes[scenario.value]
        except KeyError as exc:
            raise KeyError(f"registry 未配置场景路由: {scenario.value}") from exc

    def get_model(self, name: str) -> ModelSpec:
        """按模型名返回接入规格。"""
        self._ensure_loaded()
        try:
            return self._models[name]
        except KeyError as exc:
            raise KeyError(f"registry 未配置模型: {name}") from exc

    @property
    def defaults(self) -> Defaults:
        """统一调用策略默认值。"""
        self._ensure_loaded()
        return self._defaults

    # ── 内部 ──

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    @staticmethod
    def _env_mapping() -> dict[str, str]:
        """合并 .env 与进程环境（进程环境优先）。"""
        merged: dict[str, Any] = {**dotenv_values(".env"), **os.environ}
        return {k: str(v) for k, v in merged.items() if v is not None}

    @staticmethod
    def _expand(value: str, env: dict[str, str]) -> str:
        """展开字符串中的 ${VAR} 占位，缺失则替换为空串。"""
        return _ENV_PATTERN.sub(lambda m: env.get(m.group(1), ""), value)
