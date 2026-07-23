"""统计工具注册入口；``MCPServer.run`` 提供官方 SDK stdio 服务。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.catalog import tool_metadata, tool_output_schema
from mcp_servers.common.tool import Tool
from mcp_servers.stats import schemas, tools


def build_server() -> MCPServer:
    """构建并注册统计分析工具。"""
    server = MCPServer(name="stats", port=8102)
    server.register(
        Tool(
            "trend_analysis", "趋势分析", schemas.TREND_ANALYSIS_SCHEMA,
            tools.trend_analysis, output_schema=tool_output_schema("trend_analysis"),
            metadata=tool_metadata("stats.trend", "stats")
        )
    )
    server.register(
        Tool(
            "anomaly_detect", "异常检测", schemas.ANOMALY_DETECT_SCHEMA,
            tools.anomaly_detect, output_schema=tool_output_schema("anomaly_detect"),
            metadata=tool_metadata("stats.anomaly", "stats")
        )
    )
    server.register(
        Tool(
            "regression", "回归分析", schemas.REGRESSION_SCHEMA,
            tools.regression, output_schema=tool_output_schema("regression"),
            metadata=tool_metadata("stats.regression", "stats")
        )
    )
    server.register(
        Tool(
            "correlation", "相关性分析", schemas.CORRELATION_SCHEMA,
            tools.correlation, output_schema=tool_output_schema("correlation"),
            metadata=tool_metadata("stats.correlation", "stats")
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
