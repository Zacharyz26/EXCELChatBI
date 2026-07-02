"""FastAPI 依赖注入：配置、模型网关、MCP 工具服务（进程内）。

本切片 MCP 工具走进程内 `Tool.invoke`（仍经 schema 校验挂载点，红线3），
不起独立 HTTP 进程；真·MCP-over-HTTP 留后续切片。
"""

from __future__ import annotations

from functools import lru_cache

from mcp_servers.chart.server import build_server as build_chart_server
from mcp_servers.common.base_server import MCPServer
from mcp_servers.excel_parser.server import build_server as build_excel_server
from packages.common.config import Settings, get_settings
from packages.models.gateway import ModelGateway
from packages.models.registry import ModelRegistry


def settings_dep() -> Settings:
    """注入全局配置。"""
    return get_settings()


@lru_cache
def model_gateway_dep() -> ModelGateway:
    """注入模型路由网关单例（registry 从配置文件加载）。"""
    registry = ModelRegistry(get_settings().model_registry_path)
    registry.load()
    return ModelGateway(registry)


@lru_cache
def excel_tools_dep() -> MCPServer:
    """注入 Excel 解析工具服务（进程内）。"""
    return build_excel_server()


@lru_cache
def chart_tools_dep() -> MCPServer:
    """注入图表工具服务（进程内）。"""
    return build_chart_server()
