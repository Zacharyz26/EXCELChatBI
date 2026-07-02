"""结构化日志。

CLAUDE 第6节：日志结构化，含 trace 信息（模型、工具、耗时、token、成本）。
本模块封装 structlog 初始化，业务侧通过 `get_logger(name)` 获取 logger。
"""

from __future__ import annotations

import logging
from typing import Any

import structlog

_configured = False


def configure_logging(level: str = "INFO") -> None:
    """初始化结构化日志（JSON 输出到 stdout，含时间戳与级别）。

    幂等：重复调用只生效一次。日志用于含 trace 信息的结构化输出
    （CLAUDE 第6节），本切片用于打印发往 DeepSeek 的 payload 以验证红线1。

    Args:
        level: 根日志级别。
    """
    global _configured
    if _configured:
        return
    logging.basicConfig(format="%(message)s", level=getattr(logging, level.upper(), logging.INFO))
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> Any:
    """获取带上下文绑定能力的结构化 logger。

    Args:
        name: logger 名称，建议用模块路径。

    Returns:
        structlog BoundLogger。
    """
    if not _configured:
        configure_logging()
    return structlog.get_logger(name)
