"""代码执行沙箱（红线5）。

Code Interpreter 的代码必须在沙箱内执行：禁网络、限文件系统、限 CPU/内存、强制超时。
本模块只定义抽象接口；具体实现属于独立安全项目，需完成威胁建模、隔离、取消、审计和
对抗测试后再接入。
"""

from __future__ import annotations

import abc
from dataclasses import dataclass


@dataclass
class SandboxLimits:
    """沙箱资源限制。"""

    timeout_seconds: int
    max_memory_mb: int
    network_disabled: bool = True


@dataclass
class SandboxResult:
    """沙箱执行结果。"""

    stdout: str
    stderr: str
    returncode: int
    timed_out: bool = False


class Sandbox(abc.ABC):
    """沙箱抽象。实现需保证：禁网、限文件系统、限资源、超时 kill。"""

    def __init__(self, limits: SandboxLimits) -> None:
        self._limits = limits

    @abc.abstractmethod
    def run(self, code: str, data_refs: dict[str, str] | None = None) -> SandboxResult:
        """在隔离环境中执行代码。

        Args:
            code: 待执行代码（来自 LLM）。
            data_refs: 允许访问的数据引用（只读挂载），不暴露原始敏感路径。
        """
