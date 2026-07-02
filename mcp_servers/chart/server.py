"""图表配置 MCP 服务入口：`python -m mcp_servers.chart.server`。"""

from __future__ import annotations

from mcp_servers.chart import schemas, tools
from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.tool import Tool


def build_server() -> MCPServer:
    """构建并注册图表工具。"""
    server = MCPServer(name="chart", port=8103)
    server.register(
        Tool("gen_chart", "生成 ECharts 配置", schemas.GEN_CHART_SCHEMA, tools.gen_chart)
    )
    server.register(
        Tool(
            "chart_screenshot",
            "图表服务端截图",
            schemas.CHART_SCREENSHOT_SCHEMA,
            tools.chart_screenshot,
        )
    )
    server.register(
        Tool("multi_layout", "多图布局", schemas.MULTI_LAYOUT_SCHEMA, tools.multi_layout)
    )
    return server


if __name__ == "__main__":
    build_server().run()
