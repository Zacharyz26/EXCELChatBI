"""独立 Code Interpreter 安全项目占位；所有代码必须经 sandbox（红线5）。"""

from __future__ import annotations

from typing import Any

from packages.governance.sandbox import Sandbox, SandboxLimits, SandboxResult

RUN_CODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "description": "待执行代码（来自 LLM）"},
        "dataset_ref": {"type": "string", "description": "只读数据引用"},
    },
    "required": ["code"],
    "additionalProperties": False,
}


def run_code(args: dict[str, Any], sandbox: Sandbox) -> SandboxResult:
    """在沙箱中执行代码。超时强制 kill，错误回传供 LLM 修正（第7节）。

    Args:
        args: 含 code 与可选 dataset_ref。
        sandbox: 经独立安全评审的沙箱实现。
    """
    raise NotImplementedError("Code Interpreter 安全项目尚未验收，run_code 不可用")


def default_limits() -> SandboxLimits:
    """从配置构造默认沙箱限制（超时 / 内存 / 禁网）。"""
    raise NotImplementedError("Code Interpreter 安全项目尚未验收，限制配置未启用")
