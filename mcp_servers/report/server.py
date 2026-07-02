"""报告生成 MCP 服务入口：`python -m mcp_servers.report.server`。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
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
        )
    )
    server.register(
        Tool(
            "insight_summary",
            "生成中文洞察解读",
            schemas.INSIGHT_SUMMARY_SCHEMA,
            tools.insight_summary,
        )
    )
    server.register(
        Tool("export_pdf", "导出 PDF", schemas.EXPORT_PDF_SCHEMA, tools.export_pdf)
    )
    return server


if __name__ == "__main__":
    build_server().run()
