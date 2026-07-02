"""入参 JSON Schema 校验（红线3）。

LLM 生成的 MCP 工具入参，进入执行前强制 JSON Schema 校验，拦截非法 / 越权参数。
校验失败抛 `SchemaValidationError`，由调用方走"带错误回退重试"（设计文档第7节）。
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match


class SchemaValidationError(Exception):
    """工具入参未通过 schema 校验。"""


def validate_tool_args(args: dict[str, Any], schema: dict[str, Any]) -> None:
    """对工具入参做 JSON Schema 校验（红线3）。

    所有 MCP 工具入参在执行前都必须经过本函数（经 Tool.invoke 挂载），
    拦截非法 / 越权参数。

    Args:
        args: LLM 生成的工具入参。
        schema: 该工具的 JSON Schema。

    Raises:
        SchemaValidationError: 校验不通过，message 为最贴切的错误描述。
    """
    validator = Draft202012Validator(schema)
    errors = sorted(validator.iter_errors(args), key=lambda e: list(e.path))
    if errors:
        primary = best_match(errors)
        path = ".".join(str(p) for p in primary.path) or "<root>"
        raise SchemaValidationError(f"入参校验失败 @ {path}: {primary.message}")
