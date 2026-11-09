"""An enum policy keeps its parent's real deduplication-key scope."""

import json
from dataclasses import asdict

import pytest

from tests.test_r2_operation_completion import RealTrace, no_network

__all__ = ["no_network"]


@pytest.mark.parametrize("parent,expected", [("按整行去重。", True), ("按频道去重。", False)])
def test_policy_parent_operation_is_bound(tmp_path, parent, expected):
    trace = RealTrace(tmp_path, "按整行去重。")
    result = trace.execute({"drop_duplicates": []})
    assert result["row_changes"]["steps"][0]["retention_policy"] == "first"
    text = parent + "\n每组保留首条。"
    verdict = trace.verify(text)
    (tmp_path / "policy-parent-operation.json").write_text(
        json.dumps(
            {
                "text": text,
                "expected": expected,
                "result": result,
                "evidence": [asdict(e) for e in trace.state()[1]],
                "verdict": asdict(verdict),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )
    assert verdict.passed is expected
