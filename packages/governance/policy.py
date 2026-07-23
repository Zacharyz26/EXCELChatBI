"""Minimal central policy gateway for v2.4 stage 1.

The gateway is deterministic and runs before every tool execution. It does not
trust model-controlled arguments as identity or authorization context.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass

from packages.governance.audit import AuditEvent, record
from packages.governance.permissions import (
    PermissionError_,
    Principal,
    check_tool_allowed,
)
from packages.session.models import JsonObject

POLICY_VERSION = "tool-policy-v1"

DEFAULT_AGENT_TOOL_ALLOWLIST = frozenset(
    {
        "get_data_profile",
        "trend_analysis",
        "anomaly_detect",
        "regression",
        "correlation",
        "gen_chart",
        "chart_screenshot",
        "transform_dataset",
        "aggregate_preview",
        "kb_search",
        "generate_report",
    }
)


@dataclass(frozen=True, slots=True)
class ToolPolicyRequest:
    principal: Principal
    project_id: str
    conversation_id: str
    run_id: str
    tool_name: str
    arguments: JsonObject
    calls_used: int
    max_tool_calls: int
    resource_project_id: str | None = None


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    allowed: bool
    code: str
    reason: str
    policy_version: str
    permission_snapshot_id: str
    arguments_hash: str

    def to_event_payload(self) -> JsonObject:
        return {
            "allowed": self.allowed,
            "code": self.code,
            "reason": self.reason,
            "policy_version": self.policy_version,
            "permission_snapshot_id": self.permission_snapshot_id,
            "arguments_hash": self.arguments_hash,
        }


class ToolPolicyGateway:
    """Apply static capability, project-scope and budget policy fail-closed."""

    def __init__(
        self,
        *,
        allowed_tools: frozenset[str] = DEFAULT_AGENT_TOOL_ALLOWLIST,
        audit_recorder: Callable[[AuditEvent], None] = record,
    ) -> None:
        self._allowed_tools = allowed_tools
        self._audit_recorder = audit_recorder

    def authorize(self, request: ToolPolicyRequest) -> PolicyDecision:
        arguments_hash = _hash_json(request.arguments)
        snapshot_id = _permission_snapshot_id(request)
        code = "policy_allowed"
        reason = "工具、项目范围和预算检查通过。"
        allowed = True
        try:
            _validate_context(request)
            check_tool_allowed(
                request.principal,
                request.tool_name,
                allowed_tools=self._allowed_tools,
            )
            if request.calls_used >= request.max_tool_calls:
                allowed = False
                code = "tool_budget_exhausted"
                reason = f"工具调用已达到上限（{request.max_tool_calls} 次）。"
            elif (
                request.resource_project_id is not None
                and request.resource_project_id != request.project_id
            ):
                allowed = False
                code = "cross_project_resource_denied"
                reason = "工具参数引用了其他项目的资源。"
        except (PermissionError_, ValueError) as exc:
            allowed = False
            code = (
                "tool_not_allowlisted"
                if isinstance(exc, PermissionError_)
                else "invalid_policy_context"
            )
            reason = str(exc)

        decision = PolicyDecision(
            allowed=allowed,
            code=code,
            reason=reason,
            policy_version=POLICY_VERSION,
            permission_snapshot_id=snapshot_id,
            arguments_hash=arguments_hash,
        )
        self._audit_recorder(
            AuditEvent(
                actor=request.principal.user_id,
                tenant_id=request.principal.tenant_id,
                action="tool.authorize",
                resource=request.tool_name,
                outcome="allowed" if allowed else "denied",
                project_id=request.project_id,
                run_id=request.run_id,
                detail={
                    "policy_version": POLICY_VERSION,
                    "decision_code": code,
                    "permission_snapshot_id": snapshot_id,
                    "arguments_hash": arguments_hash,
                },
            )
        )
        return decision


def _validate_context(request: ToolPolicyRequest) -> None:
    required = {
        "project_id": request.project_id,
        "conversation_id": request.conversation_id,
        "run_id": request.run_id,
        "tool_name": request.tool_name,
    }
    missing = [name for name, value in required.items() if not value.strip()]
    if missing:
        raise ValueError(f"策略上下文缺少字段: {', '.join(missing)}")
    if request.calls_used < 0 or request.max_tool_calls < 1:
        raise ValueError("工具预算上下文非法")


def _hash_json(value: JsonObject) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _permission_snapshot_id(request: ToolPolicyRequest) -> str:
    material = "\x1f".join(
        (
            POLICY_VERSION,
            request.principal.user_id,
            request.principal.tenant_id or "",
            request.project_id,
        )
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()
