"""Actual tool/SQLite policy oracle; first and other orders differ by fact."""

import json
from dataclasses import asdict

import pytest

from tests.test_r2_operation_completion import RealTrace, no_network

__all__ = ["no_network"]


@pytest.mark.parametrize("policy,expected", [("首条", True), ("末条", False), ("第三条", False)])
def test_compound_retention_policy_remains_an_evidence_obligation(tmp_path, policy, expected):
    trace = RealTrace(tmp_path, "按整行去重。")
    result = trace.execute({"drop_duplicates": []})
    assert result["row_changes"]["steps"][0]["retention_policy"] == "first"
    text = "去重前5行，去重后4行，每组保留" + policy + "。"
    verdict = trace.verify(text)
    (tmp_path / "compound-policy.json").write_text(
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
