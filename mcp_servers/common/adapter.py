"""Transport-neutral MCP server adapter over deterministic ChatBI tools."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from packages.governance.schema_validator import SchemaValidationError, validate_tool_args

from mcp_servers.common.contracts import (
    MCPCallResult,
    MCPProtocolError,
    MCPRequestContext,
    MCPToolDescriptor,
    normalize_structured_result,
    validate_json,
)


@dataclass(frozen=True, slots=True)
class MCPToolBinding:
    """One canonical MCP descriptor bound to the existing deterministic runner."""

    descriptor: MCPToolDescriptor
    handler: Callable[[dict[str, Any]], Any]


class MCPServerAdapter:
    """Implements tools/list and tools/call semantics without owning a transport."""

    def __init__(self, name: str, bindings: Iterable[MCPToolBinding]) -> None:
        self.name = name
        self._bindings = {binding.descriptor.name: binding for binding in bindings}
        if not self._bindings:
            # Empty services such as code-interpreter are intentionally allowed but
            # still have a valid adapter/catalog.
            self._bindings = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return tuple(binding.descriptor for binding in self._bindings.values())

    def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: MCPRequestContext,
    ) -> MCPCallResult:
        """Validate host context/input/output and map failures to stable codes."""
        try:
            context.validate()
            binding = self._bindings.get(name)
            if binding is None:
                raise MCPProtocolError("tool_not_found", f"MCP 工具不存在: {name}")
            try:
                validate_tool_args(arguments, binding.descriptor.input_schema)
            except SchemaValidationError as exc:
                raise MCPProtocolError("invalid_arguments", str(exc)) from exc
            result = normalize_structured_result(binding.handler(arguments))
            validate_json(
                result,
                binding.descriptor.output_schema,
                code="invalid_tool_output",
                label="工具输出",
            )
            return MCPCallResult.success(name, result)
        except MCPProtocolError as exc:
            return MCPCallResult.failure(name, exc)
        except FileNotFoundError as exc:
            return MCPCallResult.failure(
                name, MCPProtocolError("resource_not_found", str(exc))
            )
        except TimeoutError as exc:
            return MCPCallResult.failure(
                name, MCPProtocolError("tool_timeout", str(exc), retryable=True)
            )
        except ValueError as exc:
            return MCPCallResult.failure(
                name, MCPProtocolError("tool_business_error", str(exc))
            )
        except Exception as exc:
            # Do not leak exception text from unexpected implementation failures.
            return MCPCallResult.failure(
                name,
                MCPProtocolError(
                    "tool_internal_error",
                    f"工具内部错误: {type(exc).__name__}",
                    retryable=True,
                ),
            )
