"""权限与白名单（红线7）。

工具走白名单；内部数据接入按用户 / 租户权限过滤。MVP 仅预留接口，
多租户隔离的完整实现属阶段三（CLAUDE 第7节）。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Principal:
    """调用主体：用户与租户身份，用于权限过滤。"""

    user_id: str
    tenant_id: str | None = None


class PermissionError_(Exception):
    """权限校验未通过（避免遮蔽内建 PermissionError，故加下划线）。"""


def check_tool_allowed(principal: Principal, tool_name: str) -> None:
    """校验主体是否被允许调用某工具（白名单）。

    Raises:
        PermissionError_: 不在白名单或无权限。
    """
    raise NotImplementedError("TODO: 校验工具白名单 + 主体权限")
