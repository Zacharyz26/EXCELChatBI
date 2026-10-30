"""Same independent two-group oracle through generic statistic and count syntax."""

import dataclasses

import pytest

from tests.test_r1_group_count_aliases import check, offline, source

__all__ = ["offline"]


@pytest.mark.parametrize(
    "text",
    [
        "共有9个频道。",
        "共有九个分组。",
        "分组数为9。",
        "总分组数为9。",
    ],
)
def test_other_consumer_syntax_cannot_read_unsealed_group_count(text):
    e, iv = source(
        "available", {"path": "$.other.group_total", "value": "9", "source_kind": "number"}
    )
    result, refs = check(e, iv, text)
    assert refs and any(r.get("normalized") == "9" or r.get("token") == "九" for r in refs), refs
    assert not result.passed, (refs, dataclasses.asdict(result))


@pytest.mark.parametrize(
    "text",
    [
        "共有2个频道。",
        "共有两个分组。",
        "分组数为2。",
        "总分组数为2。",
    ],
)
def test_same_syntax_accepts_the_actual_canonical_count(text):
    e, iv = source("available")
    result, refs = check(e, iv, text)
    assert result.passed, (refs, dataclasses.asdict(result))
