"""脚手架冒烟测试。

用标准库 unittest 编写（pytest 亦可收集），仅依赖纯骨架模块，
不依赖尚未安装的第三方库，因此 `python3 -m unittest` 即可通过，
验证目录结构与模块可导入。
"""

from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class StructureTest(unittest.TestCase):
    """关键目录存在性。"""

    def test_top_level_dirs(self) -> None:
        for rel in ["apps", "mcp_servers", "packages", "config", "docs"]:
            self.assertTrue((ROOT / rel).is_dir(), f"缺少目录: {rel}")

    def test_key_config_files(self) -> None:
        for rel in ["pyproject.toml", ".env.example", "config/models.example.yaml"]:
            self.assertTrue((ROOT / rel).is_file(), f"缺少文件: {rel}")


class ImportTest(unittest.TestCase):
    """纯骨架模块可导入（不触发 NotImplementedError，仅导入）。"""

    PURE_MODULES = [
        "packages.models",
        "packages.models.types",
        "packages.models.gateway",
        "packages.governance.schema_validator",
        "packages.governance.sandbox",
        "packages.rag.retriever",
        "packages.session.state",
        "mcp_servers.common.tool",
        "mcp_servers.excel_parser.profile",
        "mcp_servers.excel_parser.server",
        "mcp_servers.stats.server",
    ]

    def test_import_pure_modules(self) -> None:
        for name in self.PURE_MODULES:
            with self.subTest(module=name):
                self.assertIsNotNone(importlib.import_module(name))


class SkeletonContractTest(unittest.TestCase):
    """骨架契约：未实现的函数应抛 NotImplementedError；可构造的对象应可构造。"""

    def test_scenario_enum(self) -> None:
        from packages.models.types import Scenario

        self.assertEqual(Scenario.CORE_REASONING.value, "core_reasoning")

    def test_unimplemented_raises(self) -> None:
        from packages.rag.tokenizer import tokenize

        with self.assertRaises(NotImplementedError):
            tokenize("销售额同比增长")

    def test_mcp_server_registers_tools(self) -> None:
        from mcp_servers.excel_parser.server import build_server

        server = build_server()
        self.assertEqual(server.name, "excel_parser")
        # parse_excel / infer_schema / data_preview
        self.assertEqual(len(server._tools), 3)


if __name__ == "__main__":
    unittest.main()
