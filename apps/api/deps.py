"""FastAPI 依赖注入：配置、模型网关、会话存储、编排器。"""

from __future__ import annotations

from functools import lru_cache

from packages.common.config import Settings, get_settings


def settings_dep() -> Settings:
    """注入全局配置。"""
    return get_settings()


@lru_cache
def model_gateway_dep() -> object:
    """注入模型路由网关单例。"""
    raise NotImplementedError("TODO: 构造 ModelRegistry(load) → ModelGateway 并缓存")


@lru_cache
def session_store_dep() -> object:
    """注入会话存储单例。"""
    raise NotImplementedError("TODO: 用 Settings 构造 SessionStore 并缓存")
