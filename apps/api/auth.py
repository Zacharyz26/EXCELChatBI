"""FastAPI Bearer 身份依赖。

当前单机版本使用由部署环境注入的静态 token registry；不会信任请求中的
user/tenant header。后续接企业 IdP 时只需替换本依赖，Principal 与授权层保持不变。
"""

from __future__ import annotations

import json
import secrets
from functools import lru_cache
from typing import Any

from fastapi import Depends, Header, HTTPException, status
from packages.common.config import Settings
from packages.governance.permissions import Principal

from apps.api.deps import settings_dep


def current_principal_dep(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(settings_dep),
) -> Principal:
    """认证请求并返回不可由客户端伪造的 Principal。"""
    if settings.auth_mode == "disabled":
        return Principal(
            user_id=settings.auth_default_user_id,
            tenant_id=settings.auth_default_tenant_id,
            roles=frozenset({"kb_admin"}),
        )

    token = _bearer_token(authorization)
    records = _parse_token_records(settings.auth_tokens_json)
    matched: Principal | None = None
    for candidate, principal in records:
        if secrets.compare_digest(token, candidate):
            matched = principal
    if matched is None:
        raise _unauthorized("Bearer token 无效")
    return matched


def require_kb_admin(principal: Principal = Depends(current_principal_dep)) -> Principal:
    """知识库写操作需要显式 kb_admin 全局角色。"""
    if not principal.has_role("kb_admin"):
        raise HTTPException(status_code=403, detail="缺少知识库管理权限")
    return principal


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise _unauthorized("缺少 Authorization Bearer token")
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("Authorization 必须使用 Bearer token")
    return token.strip()


@lru_cache(maxsize=16)
def _parse_token_records(raw: str) -> tuple[tuple[str, Principal], ...]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("AUTH_TOKENS_JSON 不是合法 JSON") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError("AUTH_TOKENS_JSON 必须是非空对象")

    records: list[tuple[str, Principal]] = []
    for token, item in parsed.items():
        if not isinstance(token, str) or len(token) < 16:
            raise RuntimeError("认证 token 至少需要 16 个字符")
        if not isinstance(item, dict):
            raise RuntimeError("认证主体记录必须是对象")
        records.append((token, _principal_from_record(item)))
    return tuple(records)


def _principal_from_record(item: dict[str, Any]) -> Principal:
    user_id = item.get("user_id")
    tenant_id = item.get("tenant_id")
    roles = item.get("roles", [])
    if not isinstance(user_id, str) or not user_id.strip():
        raise RuntimeError("认证主体缺少 user_id")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise RuntimeError("认证主体缺少 tenant_id")
    if not isinstance(roles, list) or not all(
        isinstance(role, str) and role.strip() for role in roles
    ):
        raise RuntimeError("认证主体 roles 必须是字符串数组")
    return Principal(
        user_id=user_id.strip(),
        tenant_id=tenant_id.strip(),
        roles=frozenset(role.strip() for role in roles),
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )
