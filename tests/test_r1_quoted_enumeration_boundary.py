"""Independent visible enumeration oracle, separate from business Evidence."""

import pytest

from tests.test_r1_document_quantities import profile_input
from tests.test_r1_document_quantities import test_textual_quantity_is_not_a_dataset_fact as check

__all__ = ["profile_input"]


@pytest.mark.parametrize(
    "name,text,expected,reject_token",
    [
        ("three_visible_names", "“指标甲”“指标乙”“指标丙”这三个细分指标。", True, None),
        ("two_names_do_not_prove_three", "“指标甲”“指标乙”这三个细分指标。", False, "三"),
        ("three_names_do_not_prove_eight", "“指标甲”“指标乙”“指标丙”这八个细分指标。", False, "八"),
        (
            "local_names_do_not_prove_table_metric_count",
            "“指标甲”“指标乙”“指标丙”。本表有三个指标。",
            False,
            "三",
        ),
        (
            "enumeration_followed_by_real_wrong_count",
            "“指标甲”“指标乙”“指标丙”这三个细分指标，存在八个重复组。",
            False,
            "八",
        ),
    ],
    ids=lambda x: x if isinstance(x, str) and len(x) < 60 else None,
)
def test_visible_names_have_local_cardinality_only(
    name, text, expected, reject_token, profile_input
):
    check(name, text, expected, reject_token, profile_input)
