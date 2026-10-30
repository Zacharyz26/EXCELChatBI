"""Unsupported natural-language filter identities cannot disappear."""

from tests.test_r1_post_review_contracts import dataclasses, projected, pytest, verdict
from tests.test_r1_post_review_contracts import offline_review as offline_review


@pytest.mark.parametrize("text", ["客户为VIP筛选后38行。", "客户是VIP筛选后38行。"])
def test_unknown_natural_filter_remains_an_obligation(text):
    e, iv = projected()
    v, refs = verdict(text, [e], [iv])
    assert not v.passed, (refs, dataclasses.asdict(v))
