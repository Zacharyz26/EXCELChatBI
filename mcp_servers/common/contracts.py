"""Transport-neutral contracts shared by ChatBI MCP clients and servers.

The official SDK owns JSON-RPC and transport framing.  This module owns the
ChatBI-specific capability metadata, request context and stable error/result
shape so stdio, Streamable HTTP and the migration-time in-process adapter do
not grow separate schemas.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, cast

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

JsonObject = dict[str, Any]
RiskLevel = Literal["low", "medium", "high"]

CHATBI_META_PREFIX = "com.chatbi/"
CHATBI_CONTEXT_KEY = f"{CHATBI_META_PREFIX}context"
MCP_CONTRACT_VERSION = "chatbi-mcp-tool-v1"
POSTCONDITIONS_VERSION = "artifact-postconditions-v1"
GENERIC_OBJECT_OUTPUT_SCHEMA: JsonObject = {"type": "object"}


class MCPProtocolError(Exception):
    """A stable gateway/server error that is safe to expose to the caller."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ToolCapabilityMetadata:
    """Governed metadata emitted in MCP Tool ``_meta`` and annotations."""

    capabilities: tuple[str, ...]
    tool_version: str = "1.0.0"
    risk_level: RiskLevel = "low"
    required_permissions: tuple[str, ...] = ("analysis:execute",)
    artifact_types: tuple[str, ...] = ()
    idempotent: bool = True
    read_only: bool = True
    destructive: bool = False
    open_world: bool = False
    postconditions_version: str = POSTCONDITIONS_VERSION

    def to_meta(self) -> JsonObject:
        """Return namespaced metadata without any request- or user-controlled data."""
        return {
            f"{CHATBI_META_PREFIX}contract-version": MCP_CONTRACT_VERSION,
            f"{CHATBI_META_PREFIX}capabilities": list(self.capabilities),
            f"{CHATBI_META_PREFIX}tool-version": self.tool_version,
            f"{CHATBI_META_PREFIX}risk-level": self.risk_level,
            f"{CHATBI_META_PREFIX}required-permissions": list(self.required_permissions),
            f"{CHATBI_META_PREFIX}artifact-types": list(self.artifact_types),
            f"{CHATBI_META_PREFIX}idempotent": self.idempotent,
            f"{CHATBI_META_PREFIX}postconditions-version": self.postconditions_version,
        }

    @classmethod
    def from_meta(
        cls,
        meta: JsonObject,
        *,
        read_only: bool,
        destructive: bool,
        idempotent: bool,
        open_world: bool,
    ) -> ToolCapabilityMetadata:
        """Parse metadata discovered from a remote MCP server, failing closed."""
        if meta.get(f"{CHATBI_META_PREFIX}contract-version") != MCP_CONTRACT_VERSION:
            raise MCPProtocolError("incompatible_contract", "MCP Tool 契约版本不受支持")
        capabilities = _string_tuple(meta, f"{CHATBI_META_PREFIX}capabilities", required=True)
        permissions = _string_tuple(
            meta, f"{CHATBI_META_PREFIX}required-permissions", required=True
        )
        artifact_types = _string_tuple(
            meta, f"{CHATBI_META_PREFIX}artifact-types", required=False
        )
        risk = meta.get(f"{CHATBI_META_PREFIX}risk-level")
        if risk not in {"low", "medium", "high"}:
            raise MCPProtocolError("invalid_tool_metadata", "MCP Tool 风险等级无效")
        tool_version = meta.get(f"{CHATBI_META_PREFIX}tool-version")
        postconditions_version = meta.get(
            f"{CHATBI_META_PREFIX}postconditions-version"
        )
        declared_idempotent = meta.get(f"{CHATBI_META_PREFIX}idempotent")
        if not isinstance(tool_version, str) or not tool_version:
            raise MCPProtocolError("invalid_tool_metadata", "MCP Tool 版本缺失")
        if postconditions_version != POSTCONDITIONS_VERSION:
            raise MCPProtocolError("incompatible_postconditions", "Artifact 后置条件版本不兼容")
        if declared_idempotent is not idempotent:
            raise MCPProtocolError("invalid_tool_metadata", "MCP Tool 幂等声明不一致")
        return cls(
            capabilities=capabilities,
            tool_version=tool_version,
            risk_level=cast(RiskLevel, risk),
            required_permissions=permissions,
            artifact_types=artifact_types,
            idempotent=idempotent,
            read_only=read_only,
            destructive=destructive,
            open_world=open_world,
            postconditions_version=postconditions_version,
        )


@dataclass(frozen=True, slots=True)
class MCPToolDescriptor:
    """Canonical Tool definition used for discovery and contract hashing."""

    name: str
    description: str
    input_schema: JsonObject
    output_schema: JsonObject
    metadata: ToolCapabilityMetadata

    @property
    def contract_hash(self) -> str:
        """Hash all executable and governance-relevant parts of the contract."""
        return stable_hash(
            {
                "name": self.name,
                "description": self.description,
                "input_schema": self.input_schema,
                "output_schema": self.output_schema,
                "metadata": self.metadata.to_meta(),
                "annotations": self.annotations,
            }
        )

    @property
    def annotations(self) -> JsonObject:
        return {
            "readOnlyHint": self.metadata.read_only,
            "destructiveHint": self.metadata.destructive,
            "idempotentHint": self.metadata.idempotent,
            "openWorldHint": self.metadata.open_world,
        }

    def to_protocol_dict(self) -> JsonObject:
        """Return the MCP wire-name representation used by SDK adapters/tests."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
            "outputSchema": self.output_schema,
            "annotations": self.annotations,
            "_meta": self.metadata.to_meta(),
        }


@dataclass(frozen=True, slots=True)
class MCPRequestContext:
    """Host-owned context carried in MCP request metadata, never tool arguments."""

    subject_id: str
    project_id: str
    conversation_id: str
    run_id: str
    plan_version: int
    step_id: str
    invocation_id: str
    idempotency_key: str
    permission_snapshot_id: str
    trace_id: str
    deadline_at: str

    def validate(self, *, now: datetime | None = None) -> None:
        string_fields = (
            self.subject_id,
            self.project_id,
            self.conversation_id,
            self.run_id,
            self.step_id,
            self.invocation_id,
            self.idempotency_key,
            self.permission_snapshot_id,
            self.trace_id,
            self.deadline_at,
        )
        if any(not item.strip() for item in string_fields) or self.plan_version < 0:
            raise MCPProtocolError("invalid_request_context", "MCP 请求上下文不完整")
        try:
            deadline = datetime.fromisoformat(self.deadline_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MCPProtocolError("invalid_request_context", "MCP deadline 格式无效") from exc
        if deadline.tzinfo is None:
            raise MCPProtocolError("invalid_request_context", "MCP deadline 必须包含时区")
        current = now or datetime.now(UTC)
        if deadline <= current:
            raise MCPProtocolError("deadline_exceeded", "MCP 工具调用已超过截止时间")

    def to_dict(self) -> JsonObject:
        return asdict(self)

    def to_request_meta(self) -> JsonObject:
        return {CHATBI_CONTEXT_KEY: self.to_dict()}

    @classmethod
    def from_request_meta(cls, meta: JsonObject) -> MCPRequestContext:
        raw = meta.get(CHATBI_CONTEXT_KEY)
        if not isinstance(raw, dict):
            raise MCPProtocolError("invalid_request_context", "缺少 ChatBI MCP 请求上下文")
        try:
            context = cls(
                subject_id=_required_string(raw, "subject_id"),
                project_id=_required_string(raw, "project_id"),
                conversation_id=_required_string(raw, "conversation_id"),
                run_id=_required_string(raw, "run_id"),
                plan_version=_required_nonnegative_int(raw, "plan_version"),
                step_id=_required_string(raw, "step_id"),
                invocation_id=_required_string(raw, "invocation_id"),
                idempotency_key=_required_string(raw, "idempotency_key"),
                permission_snapshot_id=_required_string(raw, "permission_snapshot_id"),
                trace_id=_required_string(raw, "trace_id"),
                deadline_at=_required_string(raw, "deadline_at"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MCPProtocolError("invalid_request_context", "MCP 请求上下文字段无效") from exc
        context.validate()
        return context


@dataclass(frozen=True, slots=True)
class MCPCallResult:
    """Transport-neutral representation of an MCP tools/call response."""

    tool_name: str
    structured_content: JsonObject | None
    text: str
    is_error: bool = False
    error_code: str | None = None
    retryable: bool = False
    result_hash: str | None = None

    @classmethod
    def success(cls, tool_name: str, result: JsonObject) -> MCPCallResult:
        return cls(
            tool_name=tool_name,
            structured_content=result,
            text=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            result_hash=stable_hash(result),
        )

    @classmethod
    def failure(
        cls, tool_name: str, error: MCPProtocolError
    ) -> MCPCallResult:
        return cls(
            tool_name=tool_name,
            structured_content=None,
            text=error.message,
            is_error=True,
            error_code=error.code,
            retryable=error.retryable,
        )


def normalize_structured_result(value: Any) -> JsonObject:
    """Normalize supported Python result objects into JSON-compatible mappings."""
    normalized = _normalize_json(value)
    if not isinstance(normalized, dict):
        raise MCPProtocolError("invalid_tool_output", "工具输出必须是 JSON 对象")
    return normalized


def validate_json(value: Any, schema: JsonObject, *, code: str, label: str) -> None:
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.path))
    if not errors:
        return
    primary = best_match(errors)
    path = ".".join(str(part) for part in primary.path) or "<root>"
    raise MCPProtocolError(code, f"{label}校验失败 @ {path}: {primary.message}")


def stable_hash(value: Any) -> str:
    encoded = json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _normalize_json(value: Any) -> Any:
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _normalize_json(asdict(value))
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _normalize_json(to_dict())
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _normalize_json(model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(key): _normalize_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_json(item) for item in value]
    raise MCPProtocolError(
        "invalid_tool_output", f"工具输出包含不可序列化类型: {type(value).__name__}"
    )


def _string_tuple(meta: JsonObject, key: str, *, required: bool) -> tuple[str, ...]:
    raw = meta.get(key)
    if raw is None and not required:
        return ()
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise MCPProtocolError("invalid_tool_metadata", f"MCP Tool metadata 字段无效: {key}")
    if required and not raw:
        raise MCPProtocolError("invalid_tool_metadata", f"MCP Tool metadata 字段为空: {key}")
    return tuple(raw)


def _required_string(raw: JsonObject, key: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(key)
    return value


def _required_nonnegative_int(raw: JsonObject, key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(key)
    return cast(int, value)
