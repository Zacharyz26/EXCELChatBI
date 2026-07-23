"""工具定义与执行包装。

每个工具携带 JSON Schema。执行前强制经治理层 schema 校验（红线3），
并校验外部内容只作数据不作指令（红线4）。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from packages.governance.schema_validator import validate_tool_args

from mcp_servers.common.adapter import MCPToolBinding
from mcp_servers.common.contracts import (
    GENERIC_OBJECT_OUTPUT_SCHEMA,
    MCPToolDescriptor,
    ToolCapabilityMetadata,
)


@dataclass
class Tool:
    """一个确定性工具定义，也是标准 MCP Tool 的唯一 schema 来源。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]
    output_schema: dict[str, Any] = field(
        default_factory=lambda: dict(GENERIC_OBJECT_OUTPUT_SCHEMA)
    )
    metadata: ToolCapabilityMetadata | None = None

    def invoke(self, args: dict[str, Any]) -> Any:
        """校验入参后执行。所有工具调用必经此入口（红线3）。"""
        validate_tool_args(args, self.input_schema)
        return self.handler(args)

    def descriptor(self) -> MCPToolDescriptor:
        """Build the canonical MCP descriptor without copying either schema."""
        metadata = self.metadata or ToolCapabilityMetadata(capabilities=(self.name,))
        return MCPToolDescriptor(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            output_schema=self.output_schema,
            metadata=metadata,
        )

    def mcp_binding(self) -> MCPToolBinding:
        return MCPToolBinding(descriptor=self.descriptor(), handler=self.invoke)
