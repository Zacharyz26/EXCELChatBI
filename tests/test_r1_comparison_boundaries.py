"""Equivalent explicit comparisons share the same frozen filter contract."""

from tests.test_r1_post_review_contracts import dataclasses, pytest, verdict
from tests.test_r1_post_review_contracts import offline_review as offline_review
from tests.test_r1_stage_and_operation_scope import evidence_for


@pytest.mark.parametrize(
    "text,expected",
    [
        ("频道=A且状态=完成筛选后38行。", True),
        ("频道=A并且状态=完成筛选后38行。", True),
        ("频道为A且状态为完成筛选后38行。", True),
        ('"频道"="A"且"状态"="完成"筛选后38行。', True),
        ("“频道”=“A”且“状态”=“完成”筛选后38行。", True),
        ('"客户"="VIP"筛选后38行。', False),
        ("“客户”=“VIP”筛选后38行。", False),
    ],
)
def test_comparison_structure_preserves_field_identity(text, expected):
    filters = [
        {"column": "频道", "op": "==", "value": "A"},
        {"column": "状态", "op": "==", "value": "完成"},
    ]
    e, iv = evidence_for(
        {"filters": filters},
        75,
        38,
        [
            {
                "operation": "filters",
                "rows_before": 75,
                "rows_after": 38,
                "removed_count": 37,
                "count_basis": "removed_rows",
                "columns": None,
            }
        ],
    )
    v, refs = verdict(text, [e], [iv])
    assert v.passed is expected, (refs, dataclasses.asdict(v))
