"""数据集变换 MCP 服务入口：`python -m mcp_servers.dataset_ops.server`。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.tool import Tool
from mcp_servers.dataset_ops import schemas, tools


def build_server() -> MCPServer:
    """构建并注册数据集变换/聚合工具。"""
    server = MCPServer(name="dataset_ops", port=8106)
    server.register(
        Tool(
            "transform_dataset",
            "结构化变换产出衍生数据集（过滤/去空/去重/排序/排除行）",
            schemas.TRANSFORM_DATASET_SCHEMA,
            tools.transform_dataset,
        )
    )
    server.register(
        Tool(
            "aggregate_preview",
            "分组聚合出表格预览",
            schemas.AGGREGATE_PREVIEW_SCHEMA,
            tools.aggregate_preview,
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
