"""Numeric ownership must track the true value, never an unrelated prose glyph."""

from __future__ import annotations

import pytest
from apps.orchestrator.control.numeric_display import numeric_text_context
from apps.orchestrator.control.profile_claims import profile_statistic_labels

from tests.test_manual06_followup_ratio_lexical import FIELD, _record, _refs, _report_passes
from tests.test_profile_subject_boundaries import check


@pytest.mark.parametrize(
    ("text", "targets"),
    [
        (f"{FIELD}缺失比例三成。", ["三"]),
        (f"{FIELD}缺失比例一半。", ["一"]),
        (f"{FIELD}缺失比例半。", ["半"]),
        (f"{FIELD}缺失比例为三成。", ["三"]),
        (f"{FIELD}最大值三火星单位。", ["三"]),
        (f"{FIELD}缺失比例「三成」。", ["三"]),
        ("共有三项重复记录。", ["三"]),
        ("另有三笔重复记录。", ["三"]),
        ("共有三沓重复记录。", ["三"]),
        ("存在三组重复。", ["三"]),
        ("重复记录增加三倍。", ["三"]),
        ("重复记录减少三倍。", ["三"]),
        ("参与重复的记录数尚未确认，但重复组数为三。", ["三"]),
        ("重复是否指向同一条记录尚不明确，但存在三组重复。", ["三"]),
        ("请检查任一重复记录，但共有壹条重复记录。", ["壹"]),
        ("共有三项重复记录，另有五笔重复记录，重复记录增加七倍。", ["三", "五", "七"]),
    ],
)
def test_unknown_values_reject_at_each_true_original_position(
    text: str, targets: list[str]
) -> None:
    result = check(text, [_record()])
    refs = _refs(result)
    assert not result["verification"]["passed"], result
    assert not _report_passes(text)
    assert [ref["token"] for ref in refs] == targets, refs
    for ref, target in zip(refs, targets, strict=True):
        assert not ref["supported"]
        assert ref["statement_span"] == [text.index(target), text.index(target) + len(target)]


@pytest.mark.parametrize(
    "text",
    [
        "任一条记录都需进一步核对。",
        "每一条记录是否重复仍未确认。",
        "某一条记录的来源尚未核实。",
        "请检查任一重复记录。",
        "一旦记录重复就需核对。",
        "一切记录是否重复仍需核对。",
        "“重复组数”“参与重复的记录数”尚未确认。",
        "另有参与核对的记录。",
        "重复记录增加前，请参考同一条说明。",
    ],
)
def test_ordinary_words_have_no_numeric_claim(text: str) -> None:
    result = check(text, [_record()])
    assert result["verification"]["passed"], result
    assert result["claims"] == []
    assert _report_passes(text)


@pytest.mark.parametrize(
    ("text", "token", "origin", "state"),
    [
        ("缺失比例三成", "三", "direct_statistic", "unsupported_or_ambiguous_numeric"),
        ("缺失比例为一半", "一", "statistic_assignment", "unsupported_or_ambiguous_numeric"),
        ("缺失比例0.0133", "0.0133", "direct_statistic", "parsed_numeric"),
        ("共有三项重复记录", "三", "count", "unsupported_or_ambiguous_numeric"),
        ("重复记录增加三倍", "三", "relative_quantity", "unsupported_or_ambiguous_numeric"),
        ("任一条记录", "一", None, "ordinary_text"),
        ("参与重复的记录数", "参", None, "ordinary_text"),
    ],
)
def test_origin_is_separate_from_lexical_classification(
    text: str, token: str, origin: str | None, state: str
) -> None:
    left = text.index(token)
    context = numeric_text_context(text, left, left + len(token), {"缺失比例", "重复记录"})
    assert context.classification == state
    assert (context.owner.origin if context.owner else None) == origin
    assert context.value_span == (left, left + len(token))


def test_quantity_word_inside_statistic_label_is_not_its_value() -> None:
    text = "重复多余记录数为1"
    left = text.index("多余")
    labels = profile_statistic_labels(text) | {"重复"}
    assert "重复多余记录数" in labels
    context = numeric_text_context(text, left, left + 2, labels)
    assert context.classification == "ordinary_text"
    assert context.owner is None


@pytest.mark.parametrize("value", ["三", "半"])
def test_complete_statistic_label_does_not_hide_its_unknown_value(value: str) -> None:
    text = f"重复多余记录数为{value}。"
    result = check(text, [_record()])
    refs = _refs(result)
    assert not result["verification"]["passed"]
    assert not _report_passes(text)
    assert [ref["token"] for ref in refs] == [value]
    assert refs[0]["statement_span"] == [text.index(value), text.index(value) + 1]


def test_supported_ratio_keeps_fact_identity_beside_unsupported_count() -> None:
    text = f"{FIELD}缺失比例0.0133（1.33%），但存在三组重复记录。"
    result = check(text, [_record()])
    refs = _refs(result)
    assert not result["verification"]["passed"]
    assert not _report_passes(text)
    unsupported = [ref for ref in refs if not ref["supported"]]
    assert [ref["token"] for ref in unsupported] == ["三"]
    pair = [ref for ref in refs if ref["supported"]]
    assert [ref["token"] for ref in pair] == ["0.0133", "1.33%"]
    for key in ("evidence_id", "path", "semantic_binding"):
        assert pair[0][key] == pair[1][key]


# Frozen complementary design cases A01-A12 from auxiliary-static-design.md.
@pytest.mark.parametrize(
    ("case", "text", "target", "ratio"),
    [
        ("A01", "另有一致适用于重复记录的规则。", None, False),
        ("A02", f"{FIELD}最大值参考原表核对。", None, False),
        ("A03", "字段说明中的“重复组数：”“参与重复的记录数”均为标签。", None, False),
        ("A04", "另有参与核对的记录。", None, False),
        ("A05", "重复记录增加前，请参考同一条说明。", None, False),
        ("A06", "共有三项重复记录，请参照同一条说明。", "三", False),
        ("A07", "同一批次的重复组数为一；按同一口径复核。", "为一", False),
        ("A08", "📌重复组数为三。", "三", False),
        ("A09", "重复组数不能确认，并不表示没有重复；另有三笔重复记录。", "三", False),
        ("A10", f"重复记录增加三倍；{FIELD}缺失比例0.0133（1.33%）。", "三", True),
        ("A11", f"{FIELD}缺失比例\n“三成”。", "三", False),
        ("A12", f"{FIELD}缺失比例**三成**。", "三", False),
    ],
    ids=lambda item: item if isinstance(item, str) and item.startswith("A") else None,
)
def test_complementary_frozen_boundaries(
    case: str, text: str, target: str | None, ratio: bool
) -> None:
    result = check(text, [_record()])
    assert result["verification"]["passed"] is (target is None), result
    assert _report_passes(text) is (target is None)
    refs = _refs(result)
    if target is None:
        assert refs == []
        return
    unsupported = [ref for ref in refs if not ref["supported"]]
    assert [ref["token"] for ref in unsupported] == [target[-1]]
    for claim in result["claims"]:
        for ref in claim["value_refs"]:
            if not ref["supported"]:
                left, right = ref["statement_span"]
                assert claim["statement"][left:right] == target[-1]
                assert left == claim["statement"].index(target) + len(target) - 1
    if ratio:
        pair = [ref for ref in refs if ref["supported"]]
        assert [ref["token"] for ref in pair] == ["0.0133", "1.33%"]
        for key in ("evidence_id", "path", "semantic_binding"):
            assert pair[0][key] == pair[1][key]
