"""图表工具注册入口；``MCPServer.run`` 提供官方 SDK stdio 服务。"""

from __future__ import annotations

from mcp_servers.chart import schemas, tools
from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.catalog import tool_metadata, tool_output_schema
from mcp_servers.common.tool import Tool


def build_server() -> MCPServer:
    """构建并注册图表工具。"""
    server = MCPServer(name="chart", port=8103)
    server.register(
        Tool(
            "gen_chart", "生成 ECharts 配置", schemas.GEN_CHART_SCHEMA,
            tools.gen_chart, output_schema=tool_output_schema("gen_chart"),
            metadata=tool_metadata("visualization.chart", "chart")
        )
    )
    server.register(
        Tool(
            "chart_screenshot",
            "图表服务端截图",
            schemas.CHART_SCREENSHOT_SCHEMA,
            tools.chart_screenshot,
            output_schema=tool_output_schema("chart_screenshot"),
            metadata=tool_metadata(
                "visualization.screenshot",
                read_only=False,
                idempotent=False,
                risk_level="medium",
            ),
        )
    )
    server.register(
        Tool(
            "multi_layout", "多图布局", schemas.MULTI_LAYOUT_SCHEMA,
            tools.multi_layout, output_schema=tool_output_schema("multi_layout"),
            metadata=tool_metadata("visualization.layout")
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
