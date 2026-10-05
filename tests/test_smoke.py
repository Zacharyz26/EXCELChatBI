"""基础结构与轻依赖冒烟测试。

用标准库 unittest 编写（pytest 亦可收集），仅依赖轻量模块，
不依赖尚未安装的第三方库，因此 `python3 -m unittest` 即可通过，
验证目录结构与模块可导入。
"""

from __future__ import annotations

import builtins
import importlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
    """轻量模块可导入。"""

    PURE_MODULES = [
        "packages.models",
        "packages.models.types",
        "packages.models.gateway",
        "packages.governance.schema_validator",
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
    """核心轻量契约。"""

    def test_scenario_enum(self) -> None:
        from packages.models.types import Scenario

        self.assertEqual(Scenario.CORE_REASONING.value, "core_reasoning")

    def test_bge_failfast_without_rag_extra(self) -> None:
        """无论开发环境是否装了 rag extra，都验证缺依赖时 fail-fast。"""
        from packages.rag.embedding import BGEEmbedder

        real_import = builtins.__import__

        def import_without_flag_embedding(name: str, *args: object, **kwargs: object) -> object:
            if name == "FlagEmbedding" or name.startswith("FlagEmbedding."):
                raise ImportError("controlled missing FlagEmbedding")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=import_without_flag_embedding):
            with self.assertRaises(RuntimeError):
                BGEEmbedder("bge-m3")

    def test_mcp_server_registers_tools(self) -> None:
        from mcp_servers.excel_parser.server import build_server

        server = build_server()
        self.assertEqual(server.name, "excel_parser")
        # parse_excel / infer_schema / data_preview
        self.assertEqual(len(server._tools), 3)


if __name__ == "__main__":
    unittest.main()
