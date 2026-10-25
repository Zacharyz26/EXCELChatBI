"""MANUAL-06 follow-up: implicit ratio restatements and Chinese lexical boundaries."""

from __future__ import annotations

import copy
from dataclasses import fields

import pytest
from apps.orchestrator.control.profile_claims import parse_ratio_restatement
from apps.orchestrator.control.report_claims import validate_report_narratives
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

from tests.test_manual03_contract import CAPTURE, FIELD
from tests.test_profile_subject_boundaries import check

IMPLICIT_RATIO_CASES = [
    ("real_no_connector", f"{FIELD}缺失比例0.0133（1.33%）。", True),
    ("ascii_parentheses", f"{FIELD}缺失比例0.0133(1.33%)。", True),
    ("fullwidth_percent", f"{FIELD}缺失比例0.0133（1.33％）。", True),
    ("spaced", f"{FIELD}缺失比例 0.0133 （ 1.33 % ）。", True),
    ("extra_precision", f"{FIELD}缺失比例0.01330（1.330%）。", True),
    ("wrong_percent", f"{FIELD}缺失比例0.0133（13.3%）。", False),
    ("wrong_raw", f"{FIELD}缺失比例0.133（1.33%）。", False),
    ("wrong_both", f"{FIELD}缺失比例0.133（13.3%）。", False),
    (
        "correct_denominator_still_unsupported",
        f"{FIELD}缺失比例0.0133（按1/75计算，1.33%）。",
        False,
    ),
    ("wrong_denominator", f"{FIELD}缺失比例0.0133（按1/74计算，1.35%）。", False),
    ("cross_field", "记录ID缺失比例0.0133（1.33%）。", False),
    ("physical_unit", f"{FIELD}缺失比例0.0133（1.33%小时）。", False),
    ("extra_number", f"{FIELD}缺失比例0.0133（1.33%，参考2%）。", False),
    ("version_switch", f"{FIELD}缺失比例0.0133（版本old，1.33%）。", False),
    ("filter_switch", f"{FIELD}缺失比例0.0133（仅频道A，1.33%）。", False),
    ("mismatched_parentheses", f"{FIELD}缺失比例0.0133（1.33%)。", False),
]

NONASSERTIVE_LEXICAL_CASES = [
    "工具未进一步拆分“去重会删除的多余记录数”“参与重复的记录数”"
    "“重复组数”这三个细分指标，因此我无法从工具结果中确认这些数字，不能自行推算。",
    "同时整行重复与记录ID 重复是否指向同一条记录，工具未直接说明，" "需结合原始数据进一步核对。",
    "参与重复的记录数和尚未计算的重复组数，均不能确认。",
    "该说明仅供参考，需参照原表核对。",
    "同时核对同一条记录与第一条记录的来源。",
    "一方面需要核对业务口径，另一方面不能推算。",
    "记录ID重复是否来自同一数据版本仍不确定。",
]

ASSERTIVE_NUMERIC_CASES = [
    "整行参与重复记录数为2。",
    "整行参与重复记录数为壹。",
    "重复组数是1。",
    "重复组数是壹。",
    "重复组数为三。",
    f"{FIELD}缺失比例三。",
    f"{FIELD}缺失比例二十五。",
    "共有一条重复记录。",
    "共有壹条重复记录。",
    "一条重复记录已确认。",
    "有参条重复记录。",
]

assert len(IMPLICIT_RATIO_CASES) == 16
assert len(NONASSERTIVE_LEXICAL_CASES) == 7
assert len(ASSERTIVE_NUMERIC_CASES) == 11


def _record() -> EvidenceRecord:
    return EvidenceRecord(**CAPTURE["evidence"])


def _report_passes(text: str) -> bool:
    artifact = Artifact(
        **{
            field.name: copy.deepcopy(CAPTURE["artifact"].get(field.name))
            for field in fields(Artifact)
        }
    )
    try:
        result = validate_report_narratives([artifact], {"title": "画像", "insights": text})
    except ValueError:
        return False
    return result["status"] == "passed"


def _refs(result: dict[str, object]) -> list[dict[str, object]]:
    return [ref for claim in result["claims"] for ref in claim["value_refs"]]


@pytest.mark.parametrize(
    ("_name", "text", "expected"),
    IMPLICIT_RATIO_CASES,
    ids=[case[0] for case in IMPLICIT_RATIO_CASES],
)
def test_implicit_ratio_restatement_is_atomic(_name: str, text: str, expected: bool) -> None:
    record = _record()
    result = check(text, [record])
    refs = _refs(result)
    assert refs, result
    assert result["verification"]["passed"] is expected, result
    assert _report_passes(text) is expected
    if expected:
        assert len(refs) == 2
        assert all(ref["supported"] for ref in refs)
        assert {ref["evidence_id"] for ref in refs} == {record.evidence_id}
        assert {ref["path"] for ref in refs} == {"$.profile.columns[3].null_ratio"}
        assert {
            (
                ref["semantic_binding"]["metric"],
                ref["semantic_binding"]["statistic"],
                ref["semantic_binding"]["dataset_ref"],
                ref["semantic_binding"]["dataset_version"],
                tuple(ref["semantic_binding"]["filters"]),
            )
            for ref in refs
        } == {
            (
                FIELD,
                "null_ratio",
                refs[0]["semantic_binding"]["dataset_ref"],
                refs[0]["semantic_binding"]["dataset_version"],
                (),
            )
        }
    else:
        assert not any(ref["supported"] for ref in refs), refs


def test_implicit_ratio_restatement_has_typed_connector() -> None:
    text = f"{FIELD}缺失比例0.0133（1.33%）。"
    parsed = parse_ratio_restatement(text, 0, len(text))
    assert parsed is not None
    assert parsed.connector == "implicit"
    assert parsed.raw_token == "0.0133"
    assert parsed.percent_token == "1.33"


@pytest.mark.parametrize("text", NONASSERTIVE_LEXICAL_CASES)
def test_nonassertive_chinese_words_do_not_create_numeric_claims(text: str) -> None:
    result = check(text, [_record()])
    assert result["verification"]["passed"] is True, result
    assert result["claims"] == []
    assert _report_passes(text)


@pytest.mark.parametrize("text", ASSERTIVE_NUMERIC_CASES)
def test_real_chinese_or_decimal_numeric_assertions_remain_guarded(text: str) -> None:
    result = check(text, [_record()])
    refs = _refs(result)
    assert result["verification"]["passed"] is False, result
    assert refs
    assert not any(ref["supported"] for ref in refs)
    assert _report_passes(text) is False


def test_original_lexical_fragments_are_clean_but_unsupported_group_count_remains() -> None:
    disclaimer = NONASSERTIVE_LEXICAL_CASES[0]
    scope_question = NONASSERTIVE_LEXICAL_CASES[1]
    assert check(disclaimer, [_record()])["claims"] == []
    assert check(scope_question, [_record()])["claims"] == []

    unsupported = check("整行重复情况：存在 1 组整行完全相同的重复。", [_record()])
    assert unsupported["verification"]["passed"] is False
    assert any(not ref["supported"] for ref in _refs(unsupported))
