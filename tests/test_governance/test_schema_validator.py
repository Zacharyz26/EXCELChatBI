"""治理层 schema 校验测试（红线3）。

合法入参通过；非法入参抛 SchemaValidationError。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mcp_servers.chart.schemas import GEN_CHART_SCHEMA  # noqa: E402
from packages.governance.schema_validator import (  # noqa: E402
    SchemaValidationError,
    validate_tool_args,
)


class SchemaValidatorTest(unittest.TestCase):
    def test_legal_args_pass(self) -> None:
        validate_tool_args(
            {
                "dataset_ref": "abc",
                "chart_type": "bar",
                "encoding": {"x": "区域", "y": "销售额", "agg": "sum"},
            },
            GEN_CHART_SCHEMA,
        )

    def test_illegal_chart_type_blocked(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_tool_args(
                {"dataset_ref": "abc", "chart_type": "donut", "encoding": {"x": "a", "y": "b"}},
                GEN_CHART_SCHEMA,
            )

    def test_missing_required_blocked(self) -> None:
        with self.assertRaises(SchemaValidationError):
            validate_tool_args(
                {"dataset_ref": "abc", "chart_type": "bar", "encoding": {"x": "a"}},
                GEN_CHART_SCHEMA,
            )


if __name__ == "__main__":
    unittest.main()
