"""Unsupported quantities must not borrow a valid suffix integer fact."""

import dataclasses

import pytest

from tests.test_r1_post_review_contracts import offline_review, verdict
from tests.test_r1_quantity_structure import aggregate_evidence

__all__ = ["offline_review"]


@pytest.mark.parametrize(
    "amount", ["六点六", "十点六", "六点零六", "一百零六点六", "6.6", "六分之六"]
)
def test_unsupported_quantity_keeps_maximal_expression(amount):
    result, refs = verdict(
        "本表包含" + amount + "个月份，播出时长（分钟）的总计为1350。", aggregate_evidence()
    )
    assert [r["token"] for r in refs] == [amount, "1350"], refs
    assert not result.passed, (refs, dataclasses.asdict(result))
