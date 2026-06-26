"""代码执行沙箱（红线5）。

Code Interpreter 的代码必须在沙箱内执行：禁网络、限文件系统、限 CPU/内存、强制超时。
**具体实现选型（Docker / gVisor / 限权进程）属 CLAUDE 第9节"待确认"，此处只定义抽象
接口，不选定实现。** 待选型确认后再补具体 Sandbox 子类。
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
