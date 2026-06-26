"""Code Interpreter MCP 服务入口：`python -m mcp_servers.code_interpreter.server`。

MVP 仅搭骨架；沙箱实现选型确认后再接入具体 Sandbox 子类。
"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer


def build_server() -> MCPServer:
    """构建 Code Interpreter 服务（沙箱实现待选型后注入）。"""
    server = MCPServer(name="code_interpreter", port=8105)
    # TODO: 选定沙箱实现后注册 run_code 工具（需注入 Sandbox 实例）
    return server


if __name__ == "__main__":
    build_server().run()
