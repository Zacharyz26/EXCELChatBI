"""审计日志（红线7）。

内部数据接入与敏感操作留审计。MVP 仅预留接口。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class AuditEvent:
    """一条审计事件。"""

    actor: str
    action: str
    resource: str
    tenant_id: str | None = None
    detail: dict | None = None


def record(event: AuditEvent) -> None:
    """落审计事件（持久化到 PostgreSQL / 日志系统）。"""
    raise NotImplementedError("TODO: 持久化审计事件")
