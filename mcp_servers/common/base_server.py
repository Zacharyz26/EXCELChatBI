"""MCP 服务基类（HTTP / SSE Transport）。

每个 MCP 工具服务继承此类，注册工具后以独立进程启动
（如 `python -m mcp_servers.stats.server`）。
"""

from __future__ import annotations

from mcp_servers.common.tool import Tool


class MCPServer:
    """MCP 服务基类。负责工具注册与启动。"""

    def __init__(self, name: str, port: int) -> None:
        self.name = name
        self.port = port
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """注册一个工具。"""
        self._tools[tool.name] = tool

    def run(self) -> None:
        """启动 MCP 服务（HTTP / SSE transport），阻塞运行。"""
        raise NotImplementedError("TODO: 用 mcp SDK 暴露已注册工具并监听 self.port")
