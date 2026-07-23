"""报告工具注册入口；``MCPServer.run`` 提供官方 SDK stdio 服务。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.catalog import tool_metadata, tool_output_schema
from mcp_servers.common.tool import Tool
from mcp_servers.report import schemas, tools


def build_server() -> MCPServer:
    """构建并注册报告工具。"""
    server = MCPServer(name="report", port=8104)
    server.register(
        Tool(
            "gen_report_md",
            "生成 Markdown 报告",
            schemas.GEN_REPORT_MD_SCHEMA,
            tools.gen_report_md,
            output_schema=tool_output_schema("gen_report_md"),
            metadata=tool_metadata(
                "report.markdown",
                "report",
                read_only=False,
                idempotent=False,
                risk_level="medium",
            ),
        )
    )
    server.register(
        Tool(
            "insight_summary",
            "生成中文洞察解读",
            schemas.INSIGHT_SUMMARY_SCHEMA,
            tools.insight_summary,
            output_schema=tool_output_schema("insight_summary"),
            metadata=tool_metadata("report.summary"),
        )
    )
    server.register(
        Tool(
            "export_pdf", "导出 PDF", schemas.EXPORT_PDF_SCHEMA, tools.export_pdf,
            output_schema=tool_output_schema("export_pdf"),
            metadata=tool_metadata(
                "report.pdf", "report", read_only=False, idempotent=False,
                risk_level="medium"
            ),
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
