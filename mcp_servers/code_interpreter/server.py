"""独立 Code Interpreter 安全项目的服务占位。

当前不注册 run_code，也不属于可用 Agent 工具。只有沙箱威胁建模、隔离实现、
资源/输出限制、取消、审计和对抗测试全部通过后才能启用。
"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer


def build_server() -> MCPServer:
    """构建空服务；安全项目验收前不得注册 run_code。"""
    server = MCPServer(name="code_interpreter", port=8105)
    # 独立安全项目验收后才可注入 Sandbox 并注册 run_code。
    return server


if __name__ == "__main__":
    build_server().run()
