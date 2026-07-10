"""统计分析 MCP 服务入口：`python -m mcp_servers.stats.server`。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.tool import Tool
from mcp_servers.stats import schemas, tools


def build_server() -> MCPServer:
    """构建并注册统计分析工具。"""
    server = MCPServer(name="stats", port=8102)
    server.register(
        Tool("trend_analysis", "趋势分析", schemas.TREND_ANALYSIS_SCHEMA, tools.trend_analysis)
    )
    server.register(
        Tool("anomaly_detect", "异常检测", schemas.ANOMALY_DETECT_SCHEMA, tools.anomaly_detect)
    )
    server.register(
        Tool("regression", "回归分析", schemas.REGRESSION_SCHEMA, tools.regression)
    )
    server.register(
        Tool("correlation", "相关性分析", schemas.CORRELATION_SCHEMA, tools.correlation)
    )
    return server


if __name__ == "__main__":
    build_server().run()
