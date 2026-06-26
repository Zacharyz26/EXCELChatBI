"""治理层 schema 校验占位测试（红线3）。

当前为骨架阶段：校验函数尚未实现，约定其抛 NotImplementedError。
实现后应改为：合法入参通过、非法入参抛 SchemaValidationError。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SchemaValidatorTest(unittest.TestCase):
    def test_validate_tool_args_not_implemented_yet(self) -> None:
        from packages.governance.schema_validator import validate_tool_args

        schema = {"type": "object", "properties": {"x": {"type": "string"}}}
        with self.assertRaises(NotImplementedError):
            validate_tool_args({"x": "a"}, schema)


if __name__ == "__main__":
    unittest.main()
