"""Without an operation qualifier, a delivery refers to invocation totals."""

from tests.test_r1_post_review_contracts import dataclasses, pytest, verdict
from tests.test_r1_post_review_contracts import offline_review as offline_review
from tests.test_r1_stage_and_operation_scope import multi


@pytest.mark.parametrize(
    "text,expected",
    [("剩余38行。", False), ("剩余37行。", True), ("删除1行。", False), ("删除38行。", True)],
)
def test_unqualified_delivery_cannot_borrow_an_intermediate_step(text, expected):
    e, iv = multi()
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))
