"""工具定义与执行包装。

每个工具携带 JSON Schema。执行前强制经治理层 schema 校验（红线3），
并校验外部内容只作数据不作指令（红线4）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from packages.governance.schema_validator import validate_tool_args


@dataclass
class Tool:
    """一个 MCP 工具：名称 + 入参 schema + 执行函数。"""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Any]

    def invoke(self, args: dict[str, Any]) -> Any:
        """校验入参后执行。所有工具调用必经此入口（红线3）。"""
        validate_tool_args(args, self.input_schema)
        return self.handler(args)
