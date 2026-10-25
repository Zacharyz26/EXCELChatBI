"""数量表达式与分句共享边界；引号标签不能借出其值槽。"""

from __future__ import annotations

import pytest
from apps.orchestrator.control.claim_structure import delimited_spans, split_claim_statements
from apps.orchestrator.control.claims import _measurement_labels
from apps.orchestrator.control.profile_claims import profile_statistic_labels
from apps.orchestrator.control.statistic_statements import numeric_value_owner

from tests.test_manual06_followup_ratio_lexical import FIELD, _record, _refs, _report_passes
from tests.test_profile_subject_boundaries import check


def labels(text: str) -> set[str]:
    return _measurement_labels() | profile_statistic_labels(text)


@pytest.mark.parametrize(
    ("text", "token"),
    [
        ("共有三\n项重复记录。", "三"),
        ("另有五\n笔重复记录。", "五"),
        ("重复记录增加三\n倍。", "三"),
        ("共有「三\n」项重复记录。", "三"),
        ('共有"三\n"项重复记录。', "三"),
        ("重复记录增加半\n倍。", "半"),
        ("共有三\r\n项重复记录。", "三"),
        ("共有三\n  项重复记录。", "三"),
        ("共有\n三项重复记录。", "三"),
        ("共有三项\n重复记录。", "三"),
        ("重复记录增加三\r\n  倍。", "三"),
        ('重复记录增加"三\n"倍。', "三"),
    ],
)
def test_true_quantity_keeps_expression_and_original_span(text: str, token: str) -> None:
    assert split_claim_statements(text, labels(text)) == [text]
    result = check(text, [_record()])
    assert not result["verification"]["passed"]
    assert not _report_passes(text)
    refs = _refs(result)
    assert [ref["token"] for ref in refs] == [token]
    assert refs[0]["reason"] == "unrecognized_numeric_expression"
    assert refs[0]["statement_span"] == [text.index(token), text.index(token) + len(token)]


@pytest.mark.parametrize("quote", ['"', "'", "`", "“”"])
def test_adjacent_quoted_labels_never_lend_direct_owner(quote: str) -> None:
    opening, closing = quote[0], quote[-1]
    text = f"备注中的{opening}重复组数{closing}{opening}三成{closing}均为标签。"
    result = check(text, [_record()])
    assert result["verification"]["passed"]
    assert _report_passes(text)
    assert _refs(result) == []


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_symmetric_quoted_true_value_still_rejects(quote: str) -> None:
    text = f"{FIELD}缺失比例{quote}三成{quote}。"
    result = check(text, [_record()])
    assert not result["verification"]["passed"]
    assert not _report_passes(text)
    refs = _refs(result)
    assert [ref["token"] for ref in refs] == ["三"]
    assert refs[0]["statement_span"] == [text.index("三"), text.index("三") + 1]


@pytest.mark.parametrize("quote", ['"', "'", "`"])
def test_quoted_label_explicit_assignment_is_not_a_sibling_label(quote: str) -> None:
    text = f"{quote}重复组数{quote}为{quote}三{quote}。"
    result = check(text, [_record()])
    assert not result["verification"]["passed"]
    assert not _report_passes(text)
    assert [ref["token"] for ref in _refs(result)] == ["三"]


@pytest.mark.parametrize("separator", ["\n\n", "\r\n\r\n", "\n \n", "\n- ", "\n## ", "。\n"])
def test_hard_boundary_does_not_continue_a_partial_quantity(separator: str) -> None:
    text = "共有三" + separator + "项重复记录。"
    statements = split_claim_statements(text, labels(text))
    assert "".join(statements) == text
    assert len(statements) > 1
    assert not any("三" in statement and "项重复记录" in statement for statement in statements)


def test_multiple_statements_keep_distinct_original_quantity_spans() -> None:
    text = "前置说明。\n共有三\n项重复记录\n另有五笔重复记录。"
    statements = split_claim_statements(text, labels(text))
    assert "".join(statements) == text
    assert len([statement for statement in statements if "三\n项" in statement]) == 1
    assert not any("三" in statement and "五" in statement for statement in statements)
    result = check(text, [_record()])
    assert not result["verification"]["passed"] and not _report_passes(text)
    assert [ref["token"] for ref in _refs(result)] == ["三", "五"]
    for claim in result["claims"]:
        for ref in claim["value_refs"]:
            left, right = ref["statement_span"]
            assert claim["statement"][left:right] == ref["token"]


def test_completed_multiplier_does_not_consume_next_ratio_statement() -> None:
    first = "重复记录增加三倍\n"
    second = f"{FIELD}缺失比例0.0133（1.33%）。"
    text = first + second
    assert split_claim_statements(text, labels(text)) == [first, second]
    position = first.index("三")
    owner = numeric_value_owner(text, position, position + 1, labels(text))
    assert owner is not None and owner.origin == "relative_quantity"
    assert owner.owner_span == (0, first.index("倍") + 1)
    result = check(text, [_record()])
    assert not result["verification"]["passed"] and not _report_passes(text)
    refs = _refs(result)
    assert [(ref["token"], ref["supported"]) for ref in refs] == [
        ("三", False),
        ("0.0133", True),
        ("1.33%", True),
    ]
    for key in ("evidence_id", "path", "semantic_binding"):
        assert refs[1][key] == refs[2][key]


@pytest.mark.parametrize(
    "prefix",
    [
        r"前注\"不启引号，",
        "O'Reilly说明，",
        r'"前注\"仍在引号内"，',
    ],
)
def test_escaped_quote_and_word_apostrophe_do_not_invert_value_opening(prefix: str) -> None:
    text = prefix + f'{FIELD}缺失比例"三成"。'
    result = check(text, [_record()])
    assert not result["verification"]["passed"] and not _report_passes(text)
    refs = _refs(result)
    assert [ref["token"] for ref in refs] == ["三"]
    for claim in result["claims"]:
        for ref in claim["value_refs"]:
            left, right = ref["statement_span"]
            assert claim["statement"][left:right] == "三"


def test_quoted_english_word_apostrophe_is_not_a_closing_quote() -> None:
    text = "'O'Reilly的备注'"
    assert delimited_spans(text) == [(0, len(text))]
