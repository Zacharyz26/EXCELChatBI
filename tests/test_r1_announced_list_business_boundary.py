"""A list preamble cannot reclassify measured anomaly points as document tips."""

import dataclasses

import pytest
from apps.orchestrator.control.statistic_statements import count_expressions

from tests.test_r1_post_review_contracts import offline_review, verdict
from tests.test_r1_quantity_structure import aggregate_evidence

__all__ = ["offline_review"]


@pytest.mark.parametrize(
    "text",
    [
        "提示八个异常数据点，供后续分步处理时参考：均已经核实。",
        "顺带提示八个异常点，供后续分步处理时参考：均已经核实。",
    ],
)
def test_list_preamble_does_not_hide_anomaly_observation_count(text):
    result, refs = verdict(text, aggregate_evidence())
    counts = count_expressions(text)
    assert any(text[slice(*c.value_span)] == "八" for c in counts), counts
    assert any(r["token"] == "八" and r.get("supported") is False for r in refs), refs
    assert not result.passed, dataclasses.asdict(result)
