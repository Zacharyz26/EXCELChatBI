"""Independent R1 quantity obligations; frozen explicit six-group oracle."""

import dataclasses
import json
from pathlib import Path

import pytest

from tests.test_r1_post_review_contracts import EvidenceRecord, offline_review, verdict

__all__ = ["offline_review"]


def aggregate_evidence():
    raw = json.loads((Path(__file__).parent / "fixtures/r1_post_review_evidence.json").read_text())[
        "s5-chart"
    ]
    result = []
    for item in raw["evidence"]:
        item = dict(item)
        item["source"] = json.loads(item.pop("source_json"))
        item["summary"] = json.loads(item.pop("summary_json"))
        result.append(EvidenceRecord(**item))
    return result


@pytest.mark.parametrize("prefix", ["共", "涵盖", "统计", "合计", ""])
@pytest.mark.parametrize("amount,expected", [("六", True), ("八", False)])
def test_numeric_counter_noun_does_not_depend_on_predicate_dictionary(prefix, amount, expected):
    text = f"本表{prefix}{amount}个月份，播出时长（分钟）的总计为1350。"
    result, refs = verdict(text, aggregate_evidence())
    assert any(ref["token"] == amount for ref in refs), refs
    assert result.passed is expected, (refs, dataclasses.asdict(result))


@pytest.mark.parametrize(
    "text",
    [
        "这是七号方案，播出时长（分钟）的总计为1350。",
        "本段标题为“七号方案”，播出时长（分钟）的总计为1350。",
    ],
)
def test_ordinary_label_is_not_a_count_fact(text):
    result, refs = verdict(text, aggregate_evidence())
    assert [ref["token"] for ref in refs] == ["1350"], refs
    assert result.passed, (refs, dataclasses.asdict(result))
