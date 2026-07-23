"""Excel 解析工具注册入口；``MCPServer.run`` 提供官方 SDK stdio 服务。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.catalog import tool_metadata, tool_output_schema
from mcp_servers.common.tool import Tool
from mcp_servers.excel_parser import schemas, tools


def build_server() -> MCPServer:
    """构建并注册 Excel 解析工具。"""
    server = MCPServer(name="excel_parser", port=8101)
    server.register(
        Tool(
            "parse_excel", "解析 Excel 为数据集引用", schemas.PARSE_EXCEL_SCHEMA, tools.parse_excel,
            output_schema=tool_output_schema("parse_excel"),
            metadata=tool_metadata(
                "data.ingest", read_only=False, idempotent=False, risk_level="medium"
            ),
        )
    )
    server.register(
        Tool(
            "infer_schema",
            "推断 schema 生成数据画像",
            schemas.INFER_SCHEMA_SCHEMA,
            tools.infer_schema,
            output_schema=tool_output_schema("infer_schema"),
            metadata=tool_metadata("data.profile", "profile"),
        )
    )
    server.register(
        Tool(
            "data_preview", "返回样本行预览", schemas.DATA_PREVIEW_SCHEMA,
            tools.data_preview, output_schema=tool_output_schema("data_preview"),
            metadata=tool_metadata("data.preview")
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
