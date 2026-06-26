"""结构化日志。

CLAUDE 第6节：日志结构化，含 trace 信息（模型、工具、耗时、token、成本）。
本模块封装 structlog 初始化，业务侧通过 `get_logger(name)` 获取 logger。
"""

from __future__ import annotations

from typing import Any


def configure_logging(config_path: str = "config/logging.yaml") -> None:
    """初始化结构化日志（读取 logging.yaml + structlog processors）。

    Args:
        config_path: 日志配置文件路径。
    """
    raise NotImplementedError("TODO: 加载 logging.yaml 并配置 structlog processors")


def get_logger(name: str) -> Any:
    """获取带上下文绑定能力的结构化 logger。

    Args:
        name: logger 名称，建议用模块路径。

    Returns:
        structlog BoundLogger。
    """
    raise NotImplementedError("TODO: 返回 structlog.get_logger(name)")
