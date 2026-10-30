"""Independent R1 adjacency and partial-projection contracts; no model IO."""

import dataclasses
import hashlib
import json

import pytest

from tests.test_r1_post_review_contracts import (
    EvidenceRecord,
    ToolInvocation,
    build_evidence_summary,
    offline_review,
    projected,
    seal_count_facts,
    verdict,
)
from tests.test_r1_quantity_structure import aggregate_evidence

__all__ = ["offline_review"]


@pytest.mark.parametrize(
    "phrase,expected",
    [
        ("本表包含六个月份和八个频道", False),
        ("本表包含六个月份和八个月份", False),
        ("本表有六个月份且有八个月份", False),
        ("本表覆盖六个月份共八个月份", False),
        ("本表共六个月份以及八个月份", False),
        ("本表包含六个月份和六个分组", True),
    ],
)
def test_adjacent_counts_each_keep_their_own_value_and_object(phrase, expected):
    result, refs = verdict(phrase + "，播出时长（分钟）的总计为1350。", aggregate_evidence())
    assert len(refs) == 3, refs
    assert [r["token"] for r in refs] == ["六", "六" if expected else "八", "1350"], refs
    assert result.passed is expected, (refs, dataclasses.asdict(result))


def partial_aggregate(kind):
    args = {"dataset_ref": "a" * 32, "group_col": "月份", "value_col": "时长", "agg": "sum"}
    result = {"rows": [{"group": "一月", "value": 25}], "total_value": 25, "agg": "sum"}
    if kind != "missing_count":
        result["group_total"] = 1
    if kind == "complete":
        result.update(truncated=False, group_col="月份", value_col="时长")
    h = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    summary = build_evidence_summary(
        summary="",
        result=result,
        artifact_id=None,
        arguments={**args, "data_version_hash": "branch"},
    )
    summary = seal_count_facts(
        summary=summary,
        result=result,
        tool="aggregate_preview",
        arguments=args,
        run_id="run",
        invocation_id="iv",
        result_hash=h,
        branch_hash="branch",
    )
    evidence = EvidenceRecord(
        "ev", "run", "iv", None, "tool_result", {"tool": "aggregate_preview"}, h, summary, "now"
    )
    invocation = ToolInvocation(
        "iv",
        "run",
        None,
        "call",
        "aggregate_preview",
        "key",
        "args",
        args,
        "succeeded",
        h,
        None,
        None,
        "now",
        "now",
    )
    return evidence, invocation


@pytest.mark.parametrize("kind", ["missing_count", "missing_metadata", "complete"])
@pytest.mark.parametrize(
    "text,requires_count",
    [
        ("时长总计为25。", False),
        ("共有一个月份。", True),
        ("共有一个月份，时长总计为25。", True),
    ],
)
def test_partial_aggregate_cannot_borrow_count_but_retains_legacy_metric(
    kind, text, requires_count
):
    evidence, invocation = partial_aggregate(kind)
    result, refs = verdict(text, [evidence], [invocation])
    assert result.passed is (kind == "complete" or not requires_count), (
        refs,
        dataclasses.asdict(result),
    )
    if kind != "complete":
        assert not any(item["path"] == "$.group_total" for item in evidence.summary["value_index"])


@pytest.mark.parametrize("context", [None, 1, [], {}])
def test_malformed_numeric_context_is_explicit_rejection(context):
    evidence, invocation = projected()
    evidence.summary["numeric_context"] = context
    result, refs = verdict("筛选后38行。", [evidence], [invocation])
    assert not result.passed, (refs, dataclasses.asdict(result))
