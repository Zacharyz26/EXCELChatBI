"""Principal and deterministic tool permission checks."""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Principal:
    """Authenticated subject snapshot used by the policy gateway."""

    user_id: str
    tenant_id: str | None = None


class PermissionError_(Exception):
    """Permission check failed (named to avoid shadowing the builtin)."""


def check_tool_allowed(
    principal: Principal,
    tool_name: str,
    *,
    allowed_tools: Set[str],
) -> None:
    """Fail closed unless both the subject and tool allowlist are valid."""
    if not principal.user_id.strip():
        raise PermissionError_("调用主体不能为空")
    if not tool_name.strip() or tool_name not in allowed_tools:
        raise PermissionError_(f"工具未进入静态 allowlist: {tool_name or '<empty>'}")
