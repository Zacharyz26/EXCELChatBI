"""入参 JSON Schema 校验（红线3）。

LLM 生成的 MCP 工具入参，进入执行前强制 JSON Schema 校验，拦截非法 / 越权参数。
校验失败抛 `SchemaValidationError`，由调用方走"带错误回退重试"（设计文档第7节）。
"""

from __future__ import annotations

from typing import Any


class SchemaValidationError(Exception):
    """工具入参未通过 schema 校验。"""


def validate_tool_args(args: dict[str, Any], schema: dict[str, Any]) -> None:
    """对工具入参做 JSON Schema 校验。

    Args:
        args: LLM 生成的工具入参。
        schema: 该工具的 JSON Schema。

    Raises:
        SchemaValidationError: 校验不通过。
    """
    raise NotImplementedError("TODO: 用 jsonschema 校验 args，失败转 SchemaValidationError")
