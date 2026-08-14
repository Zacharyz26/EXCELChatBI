"""数据集变换工具注册入口；``MCPServer.run`` 提供官方 SDK stdio 服务。"""

from __future__ import annotations

from mcp_servers.common.base_server import MCPServer
from mcp_servers.common.catalog import tool_metadata, tool_output_schema
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
            output_schema=tool_output_schema("transform_dataset"),
            metadata=tool_metadata(
                "dataset.transform", read_only=False, idempotent=False, risk_level="medium"
            ),
        )
    )
    server.register(
        Tool(
            "aggregate_preview",
            "分组聚合出表格预览",
            schemas.AGGREGATE_PREVIEW_SCHEMA,
            tools.aggregate_preview,
            output_schema=tool_output_schema("aggregate_preview"),
            metadata=tool_metadata("data.aggregate", "table"),
        )
    )
    server.register(
        Tool(
            "join_preflight",
            "只读评估两个数据集的 Join 基数、行数膨胀和安全风险（不执行 Join）",
            schemas.JOIN_PREFLIGHT_SCHEMA,
            tools.join_preflight,
            output_schema=tool_output_schema("join_preflight"),
            metadata=tool_metadata("dataset.join.preflight"),
        )
    )
    server.register(
        Tool(
            "join_datasets",
            "在预检与显式授权后执行固定等值 Join，并生成双父血缘衍生数据集",
            schemas.JOIN_DATASETS_SCHEMA,
            tools.join_datasets,
            output_schema=tool_output_schema("join_datasets"),
            metadata=tool_metadata(
                "dataset.join.execute",
                read_only=False,
                idempotent=False,
                risk_level="high",
            ),
        )
    )
    return server


if __name__ == "__main__":
    build_server().run()
