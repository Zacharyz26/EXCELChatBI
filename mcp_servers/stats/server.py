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
            metadata=tool_metadata("stats.trend", "stats", tool_version="1.1.0")
        )
    )
    server.register(
        Tool(
            "anomaly_detect", "异常检测", schemas.ANOMALY_DETECT_SCHEMA,
            tools.anomaly_detect, output_schema=tool_output_schema("anomaly_detect"),
            metadata=tool_metadata("stats.anomaly", "stats", tool_version="1.1.0")
        )
    )
    server.register(
        Tool(
            "regression", "回归分析", schemas.REGRESSION_SCHEMA,
            tools.regression, output_schema=tool_output_schema("regression"),
            metadata=tool_metadata("stats.regression", "stats", tool_version="1.1.0")
        )
    )
    server.register(
        Tool(
            "correlation", "相关性分析", schemas.CORRELATION_SCHEMA,
            tools.correlation, output_schema=tool_output_schema("correlation"),
            metadata=tool_metadata("stats.correlation", "stats", tool_version="1.1.0")
        )
    )
    server.register(
        Tool(
            "dimension_contribution",
            "维度贡献分析",
            schemas.DIMENSION_CONTRIBUTION_SCHEMA,
            tools.dimension_contribution,
            output_schema=tool_output_schema("dimension_contribution"),
            metadata=tool_metadata("stats.contribution", "stats"),
        )
    )
    server.register(
        Tool(
            "group_compare",
            "分群比较",
            schemas.GROUP_COMPARE_SCHEMA,
            tools.group_compare,
            output_schema=tool_output_schema("group_compare"),
            metadata=tool_metadata("stats.group_compare", "stats"),
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
