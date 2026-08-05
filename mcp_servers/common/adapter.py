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
    MCPResourceContents,
    MCPResourceDescriptor,
    MCPResourceProvider,
    MCPResourceSubscriptionSnapshot,
    MCPToolDescriptor,
    normalize_structured_result,
    stable_hash,
    validate_json,
    validate_tool_approval,
)


@dataclass(frozen=True, slots=True)
class MCPToolBinding:
    """One canonical MCP descriptor bound to the existing deterministic runner."""

    descriptor: MCPToolDescriptor
    handler: Callable[[dict[str, Any]], Any] | None = None
    context_handler: Callable[[dict[str, Any], MCPRequestContext], Any] | None = None

    def __post_init__(self) -> None:
        if (self.handler is None) == (self.context_handler is None):
            raise ValueError("MCP binding 必须且只能配置一个 handler")


class MCPServerAdapter:
    """Implements tools/list and tools/call semantics without owning a transport."""

    def __init__(
        self,
        name: str,
        bindings: Iterable[MCPToolBinding],
        *,
        resource_provider: MCPResourceProvider | None = None,
    ) -> None:
        self.name = name
        self._bindings = {binding.descriptor.name: binding for binding in bindings}
        self._resource_provider = resource_provider
        if not self._bindings:
            # Empty services such as code-interpreter are intentionally allowed but
            # still have a valid adapter/catalog.
            self._bindings = {}

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._bindings)

    def list_tools(self) -> tuple[MCPToolDescriptor, ...]:
        return tuple(binding.descriptor for binding in self._bindings.values())

    @property
    def has_resources(self) -> bool:
        return self._resource_provider is not None

    def list_resources(self, context: MCPRequestContext) -> tuple[MCPResourceDescriptor, ...]:
        """List only resources visible to the signed Host subject and project."""
        try:
            context.validate()
            if self._resource_provider is None:
                return ()
            return self._resource_provider.list_resources(context)
        except MCPProtocolError:
            raise
        except PermissionError as exc:
            raise MCPProtocolError("resource_not_found", "MCP Resource 项目不存在") from exc
        except Exception as exc:
            raise MCPProtocolError(
                "resource_internal_error",
                f"Resource 列表内部错误: {type(exc).__name__}",
                retryable=True,
            ) from exc

    def read_resource(self, uri: str, context: MCPRequestContext) -> MCPResourceContents:
        """Read one opaque URI after the same signed-context authorization."""
        try:
            context.validate()
            if self._resource_provider is None:
                raise FileNotFoundError("MCP Resource 不存在")
            contents = self._resource_provider.read_resource(uri, context)
            if contents.uri != uri:
                raise MCPProtocolError("invalid_resource_output", "MCP Resource URI 与请求不一致")
            return contents
        except MCPProtocolError:
            raise
        except (FileNotFoundError, PermissionError) as exc:
            raise MCPProtocolError("resource_not_found", "MCP Resource 不存在") from exc
        except ValueError as exc:
            raise MCPProtocolError("invalid_resource_uri", "MCP Resource URI 无效") from exc
        except Exception as exc:
            raise MCPProtocolError(
                "resource_internal_error",
                f"Resource 读取内部错误: {type(exc).__name__}",
                retryable=True,
            ) from exc

    def resource_catalog_version(self, context: MCPRequestContext) -> str:
        """Hash the complete authorized directory, never a transport page."""
        resources = self.list_resources(context)
        catalog_versions = {
            value
            for item in resources
            if (item.metadata or {}).get("com.chatbi/resource-kind")
            == "domain-definition-catalog"
            if isinstance(
                (value := (item.metadata or {}).get("com.chatbi/catalog-version")),
                str,
            )
            and len(value) == 64
        }
        if len(catalog_versions) == 1:
            return next(iter(catalog_versions))
        return stable_hash([item.to_protocol_dict() for item in resources])

    def resource_subscription_snapshot(
        self,
        uri: str,
        context: MCPRequestContext,
    ) -> MCPResourceSubscriptionSnapshot:
        """Authorize one URI and capture both content and directory versions."""
        contents = self.read_resource(uri, context)
        return MCPResourceSubscriptionSnapshot(
            uri=contents.uri,
            catalog_version=self.resource_catalog_version(context),
            content_hash=stable_hash(
                {
                    "text": contents.text,
                    "mime_type": contents.mime_type,
                    "metadata": contents.metadata or {},
                }
            ),
        )

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
            validate_tool_approval(binding.descriptor, arguments, context)
            if binding.context_handler is not None:
                raw_result = binding.context_handler(arguments, context)
            else:
                assert binding.handler is not None
                raw_result = binding.handler(arguments)
            result = normalize_structured_result(raw_result)
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
            return MCPCallResult.failure(name, MCPProtocolError("resource_not_found", str(exc)))
        except TimeoutError as exc:
            return MCPCallResult.failure(
                name, MCPProtocolError("tool_timeout", str(exc), retryable=True)
            )
        except ValueError as exc:
            return MCPCallResult.failure(name, MCPProtocolError("tool_business_error", str(exc)))
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
