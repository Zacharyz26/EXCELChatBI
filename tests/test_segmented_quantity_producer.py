"""Actual transform, temporary SQLite, sealed Evidence and complete Verifier."""

import hashlib
import json
from dataclasses import asdict

import pytest
from apps.orchestrator.control.quantity_presentation import prepare_candidate

from tests.test_r2_operation_completion import RealTrace, no_network

__all__ = ["no_network"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("清洗前5行，清洗后4行，删除1条空值记录。列结构仍为“频道、时长、备注”3列。", True),
        ("清洗前5行，清洗后4行，删除1条空值记录。列结构仍为“频道、时长、备注”4列。", False),
        ("清洗前5行，清洗后4行，删除1条空值记录。列结构仍为“频道、利润、备注”3列。", False),
        ("清洗前5行，清洗后4行，删除1条空值记录。删除3列。", False),
    ],
)
def test_actual_column_count_producer_and_whole_verifier(tmp_path, text, expected):
    trace = RealTrace(tmp_path, "删除时长为空的记录。")
    assert trace.admit({"drop_nulls": ["时长"]}) is None
    result = trace.execute({"drop_nulls": ["时长"]})
    evidence = trace.state()[1][0]
    assert result["rows_before"] == 5 and result["rows_after"] == 4
    assert result["columns"] == ["频道", "时长", "备注"]
    verdict = trace.verify(text)
    (tmp_path / "column-count.json").write_text(
        json.dumps(
            {
                "result": result,
                "evidence": asdict(evidence),
                "text": text,
                "verdict": asdict(verdict),
            },
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    assert verdict.passed is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("去重前5行，去重后4行，共移除1条完全重复的记录（每组保留首条）。", True),
        ("去重前5行，去重后4行，共移除1条完全重复的记录（每组保留末条）。", False),
        ("去重前5行，去重后4行，共移除1条完全重复的记录（每组保留两条）。", False),
        ("去重前5行，去重后4行，参与整行重复的记录数为1。", False),
    ],
)
def test_actual_retention_policy_producer_and_whole_verifier(tmp_path, text, expected):
    trace = RealTrace(tmp_path, "按整行去重。")
    assert trace.admit({"drop_duplicates": []}) is None
    result = trace.execute({"drop_duplicates": []})
    evidence = trace.state()[1][0]
    assert result["rows_before"] == 5 and result["rows_after"] == 4
    verdict = trace.verify(text)
    (tmp_path / "retention-policy.json").write_text(
        json.dumps(
            {
                "result": result,
                "evidence": asdict(evidence),
                "text": text,
                "verdict": asdict(verdict),
            },
            ensure_ascii=False,
            default=str,
            indent=2,
        )
    )
    assert verdict.passed is expected


def test_missing_policy_projection_does_not_certify_first(tmp_path):
    trace = RealTrace(tmp_path, "按整行去重。")
    trace.execute({"drop_duplicates": []})
    evidence = trace.state()[1][0]
    projection = evidence.summary["fact_projection"]
    projection.pop("policies", None)
    envelope = {k: v for k, v in projection.items() if k != "projection_hash"}
    facts = [
        item for item in evidence.summary["value_index"] if item["path"] in projection["paths"]
    ]
    projection["projection_hash"] = hashlib.sha256(
        json.dumps(
            {"envelope": envelope, "facts": facts},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert not prepare_candidate("每组保留首条。", [evidence], goal="说明去重结果").valid
