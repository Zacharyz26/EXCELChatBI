"""Excel 解析 MCP 服务入口：`python -m mcp_servers.excel_parser.server`。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.tool import Tool
from mcp_servers.excel_parser import schemas, tools


def build_server() -> MCPServer:
    """构建并注册 Excel 解析工具。"""
    server = MCPServer(name="excel_parser", port=8101)
    server.register(
        Tool(
            "parse_excel", "解析 Excel 为数据集引用", schemas.PARSE_EXCEL_SCHEMA, tools.parse_excel
        )
    )
    server.register(
        Tool(
            "infer_schema",
            "推断 schema 生成数据画像",
            schemas.INFER_SCHEMA_SCHEMA,
            tools.infer_schema,
        )
    )
    server.register(
        Tool("data_preview", "返回样本行预览", schemas.DATA_PREVIEW_SCHEMA, tools.data_preview)
    )
    return server


if __name__ == "__main__":
    build_server().run()
