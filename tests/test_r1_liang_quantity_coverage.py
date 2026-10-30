"""Non-Unicode-numeric 两 must survive quantity discovery before binding."""

import dataclasses

import pytest
from apps.orchestrator.control.statistic_statements import count_expressions

from tests.test_r1_post_review_contracts import offline_review, verdict
from tests.test_r1_quantity_structure import aggregate_evidence

__all__ = ["offline_review"]


@pytest.mark.parametrize("object_text", ["个月份", "组结果", "项未知指标", "张图表"])
def test_liang_is_preserved_for_known_and_unknown_count_objects(object_text):
    text = "本表有两" + object_text + "，播出时长（分钟）的总计为1350。"
    counts = count_expressions(text)
    assert any(text[slice(*c.value_span)] == "两" for c in counts), counts
    result, refs = verdict(text, aggregate_evidence())
    assert any(r["token"] == "两" and r.get("supported") is False for r in refs), refs
    assert not result.passed, dataclasses.asdict(result)


@pytest.mark.parametrize("prefix", ["两者一致", "两全其美"])
def test_ordinary_liang_word_is_not_a_quantity(prefix):
    text = prefix + "，播出时长（分钟）的总计为1350。"
    result, refs = verdict(text, aggregate_evidence())
    assert [r["token"] for r in refs] == ["1350"], refs
    assert result.passed, dataclasses.asdict(result)
