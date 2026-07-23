"""MCP 工具注册基类 and official SDK entrypoint."""

from __future__ import annotations

from mcp_servers.common.adapter import MCPServerAdapter
from mcp_servers.common.tool import Tool


class MCPServer:
    """Deterministic registry that can be adapted to an official MCP server."""

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。"""
        self._tools[tool.name] = tool

    @property
    def tools(self) -> tuple[Tool, ...]:
        """Expose a stable, read-only view for schema adapters."""
        return tuple(self._tools.values())

    def as_mcp_adapter(self) -> MCPServerAdapter:
        """Create the transport-neutral tools/list + tools/call adapter."""
        return MCPServerAdapter(self.name, (tool.mcp_binding() for tool in self.tools))

    def run(self) -> None:
        """Start the verified official-SDK stdio entrypoint.

        The optional dependency is imported only here so the API's legacy
        in-process path does not require MCP at runtime.
        """
        try:
            from mcp_servers.common.sdk_adapter import run_adapter
        except ImportError as exc:  # pragma: no cover - packaging failure path
            raise RuntimeError("启动 MCP Server 需要安装项目的 mcp 可选依赖") from exc
        run_adapter(self.as_mcp_adapter())
