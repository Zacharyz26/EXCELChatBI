"""冻结边界组合：真实数量不丢失，标签不产生伪声明。"""

from __future__ import annotations

import pytest
from apps.orchestrator.control.claim_structure import split_claim_statements
from apps.orchestrator.control.claims import _measurement_labels
from apps.orchestrator.control.profile_claims import profile_statistic_labels

from tests.test_manual06_followup_ratio_lexical import FIELD, _record, _refs, _report_passes
from tests.test_profile_subject_boundaries import check


@pytest.mark.parametrize("quote", ['"', "'", "`", "“”", "「」", "『』"])
@pytest.mark.parametrize("separator", [" ", "\n", "\r\n"])
def test_labels_beside_true_quantity(quote: str, separator: str) -> None:
    a, b = quote[0], quote[-1]
    text = f"备注中的{a}重复组数{b}{a}三成{b}均为标签；另有五{separator}笔重复记录。"
    result = check(text, [_record()])
    assert not result["verification"]["passed"] and not _report_passes(text)
    refs = _refs(result)
    assert [(r["token"], r["supported"]) for r in refs] == [("五", False)]
    for claim in result["claims"]:
        for ref in claim["value_refs"]:
            left, right = ref["statement_span"]
            assert claim["statement"][left:right] == "五"


@pytest.mark.parametrize("separator", ["\n\n", "\n \n", "\r\n\r\n", "\n- ", "\n## ", "。\n"])
def test_paragraph_and_item_cannot_share_quantity_owner(separator: str) -> None:
    text = "共有三" + separator + "项重复记录。"
    parts = split_claim_statements(text, _measurement_labels() | profile_statistic_labels(text))
    assert "".join(parts) == text
    assert not any("三" in s and "项重复记录" in s for s in parts)


@pytest.mark.parametrize("quantity", ["三", "壹", "半"])
def test_question_then_affirmative_quantity_and_ratio(quantity: str) -> None:
    text = (
        f"重复是否指向同一条记录尚未确认，但重复记录增加{quantity}\n倍；"
        f"{FIELD}缺失比例0.0133（即1.33%）。"
    )
    result = check(text, [_record()])
    assert not result["verification"]["passed"] and not _report_passes(text)
    refs = _refs(result)
    assert [r["token"] for r in refs if not r["supported"]] == [quantity]
    pair = [r for r in refs if r["supported"]]
    assert [r["token"] for r in pair] == ["0.0133", "1.33%"]
    for key in ("evidence_id", "path", "semantic_binding"):
        assert pair[0][key] == pair[1][key]


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_sibling_assignment_label_is_not_real_assignment(quote: str) -> None:
    text = f"备注中的{quote}重复组数：{quote}{quote}三成{quote}均为标签。"
    result = check(text, [_record()])
    assert result["verification"]["passed"] and _report_passes(text)
    assert _refs(result) == []
