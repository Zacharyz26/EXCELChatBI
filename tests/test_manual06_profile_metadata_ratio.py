"""MANUAL-06: typed header metadata and bounded ratio restatements."""

from __future__ import annotations

import copy
from dataclasses import fields

import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

from tests.test_manual03_contract import CAPTURE, FIELD
from tests.test_profile_subject_boundaries import check

REAL_STATISTICS = (
    f"{FIELD}的统计（单位即分钟，来自列名）：最小值 10.0，最大值 999.0，"
    "均值 50.121622，中位数 37.5，不同值数量 37，精确空值数 1，非空数 74，"
    "缺失比例 0.0133（即 1.33%）。"
)

REAL_FULL_CANDIDATE = (
    "已取得画像结果，下面按你要求逐项复核说明。\n\n"
    "数据规模与列名：共 75 行、5 列，列名依次为「月份」「频道」「状态」"
    "「播出时长（分钟）」「记录ID」。\n\n"
    + REAL_STATISTICS
    + "\n\n整行重复情况：工具返回 duplicate_rows 为 1，即存在 1 组整行完全相同的重复"
    "（duplicate_ratio 0.0133）。工具只给出了‘重复行数=1’这一口径，未进一步拆分"
    "‘去重会删除的多余记录数’‘参与重复的记录数’‘重复组数’这三个细分指标。"
)


HEADER_CASES = [
    ("real_column_source", f"{FIELD}的统计（单位即分钟，来自列名）：最大值999。", True),
    ("source_first", f"{FIELD}的统计（来自列名，单位即分钟）：最大值999。", True),
    ("ascii_punctuation", f"{FIELD}的统计(单位即分钟,来自列名):最大值999。", True),
    ("field_name_source", f"{FIELD}的统计（单位为分钟、来自字段名）：最大值999。", True),
    ("source_from_column", f"{FIELD}的统计（来源于列名，单位为分钟）：最大值999。", True),
    ("source_is_field", f"{FIELD}的统计（来源为字段名，单位即分钟）：最大值999。", True),
    ("source_equals_column", f"{FIELD}的统计（来源是列名，单位即分钟）：最大值999。", True),
    ("confirmation_control", f"{FIELD}的统计（单位即分钟，列名已经注明）：最大值999。", True),
    ("unit_only_control", f"{FIELD}的统计（单位即分钟）：最大值999。", True),
    ("negative_not_from", f"{FIELD}的统计（单位即分钟，并非来自列名）：最大值999。", False),
    ("negative_is_not_from", f"{FIELD}的统计（单位即分钟，不是来自列名）：最大值999。", False),
    ("negative_bare_not_from", f"{FIELD}的统计（单位即分钟，不来自列名）：最大值999。", False),
    ("negative_source", f"{FIELD}的统计（单位即分钟，来源并非列名）：最大值999。", False),
    ("uncertain_maybe", f"{FIELD}的统计（单位即分钟，可能来自列名）：最大值999。", False),
    ("uncertain_whether", f"{FIELD}的统计（单位即分钟，是否来自列名）：最大值999。", False),
    ("uncertain_seems", f"{FIELD}的统计（单位即分钟，似乎来自列名）：最大值999。", False),
    ("uncertain_reportedly", f"{FIELD}的统计（单位即分钟，据说来自列名）：最大值999。", False),
    ("wrong_report_source", f"{FIELD}的统计（单位即分钟，来自旧报表）：最大值999。", False),
    ("wrong_other_field", f"{FIELD}的统计（单位即分钟，来源为其他字段）：最大值999。", False),
    ("wrong_unit", f"{FIELD}的统计（单位即小时，来自列名）：最大值999。", False),
    ("duplicate_provenance", f"{FIELD}的统计（来自列名，来源为字段名）：最大值999。", False),
    ("wrong_value", f"{FIELD}的统计（单位即分钟，来自列名）：最大值998。", False),
    ("wrong_role", f"{FIELD}的统计（单位即分钟，来自列名）：最小值999。", False),
    ("version_metadata", f"{FIELD}的统计（单位即分钟，来自列名，版本old）：最大值999。", False),
    ("paragraph_boundary", f"{FIELD}的统计（单位即分钟，来自列名）：\n\n最大值999。", False),
]


RATIO_CASES = [
    ("real_equivalent", f"{FIELD}缺失比例0.0133（即1.33%）。", True),
    ("approx_control", f"{FIELD}缺失比例0.0133（约1.33%）。", True),
    ("ascii_parentheses", f"{FIELD}缺失比例0.0133(即1.33%)。", True),
    ("fullwidth_percent", f"{FIELD}缺失比例0.0133（即1.33％）。", True),
    ("spaced_equivalent", f"{FIELD}缺失比例 0.0133 （ 即 1.33 % ）。", True),
    ("extra_precision", f"{FIELD}缺失比例0.01330（即1.330%）。", True),
    ("wrong_percent", f"{FIELD}缺失比例0.0133（即13.3%）。", False),
    ("wrong_percent_approx", f"{FIELD}缺失比例0.0133（约13.3%）。", False),
    ("wrong_raw", f"{FIELD}缺失比例0.133（即1.33%）。", False),
    ("wrong_raw_approx", f"{FIELD}缺失比例0.133（约1.33%）。", False),
    ("wrong_denominator", f"{FIELD}缺失比例0.0133（按1/74计算，即1.35%）。", False),
    ("unsupported_correct_denominator", f"{FIELD}缺失比例0.0133（按1/75计算，即1.33%）。", False),
    ("cross_field", "记录ID缺失比例0.0133（即1.33%）。", False),
    ("inside_other_field", f"{FIELD}缺失比例0.0133（其他列即1.33%）。", False),
    ("physical_unit", f"{FIELD}缺失比例0.0133（即1.33%小时）。", False),
    ("double_percent", f"{FIELD}缺失比例0.0133（即1.33%%）。", False),
    ("extra_number", f"{FIELD}缺失比例0.0133（即1.33%，参考2%）。", False),
    ("version_switch", f"{FIELD}缺失比例0.0133（版本old，即1.33%）。", False),
    ("filter_switch", f"{FIELD}缺失比例0.0133（仅限频道A，即1.33%）。", False),
    ("implicit_connector", f"{FIELD}缺失比例0.0133（1.33%）。", True),
    ("negative_connector", f"{FIELD}缺失比例0.0133（并非1.33%）。", False),
    ("uncertain_connector", f"{FIELD}缺失比例0.0133（可能为1.33%）。", False),
]

assert len(HEADER_CASES) == 25
assert len(RATIO_CASES) == 22


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
    ("_name", "text", "expected"), HEADER_CASES, ids=[case[0] for case in HEADER_CASES]
)
def test_manual06_typed_header_metadata(_name: str, text: str, expected: bool) -> None:
    result = check(text, [_record()])
    assert result["refs"], result
    assert result["verification"]["passed"] is expected, result
    assert _report_passes(text) is expected


@pytest.mark.parametrize(
    ("_name", "text", "expected"), RATIO_CASES, ids=[case[0] for case in RATIO_CASES]
)
def test_manual06_bounded_ratio_restatement(_name: str, text: str, expected: bool) -> None:
    record = _record()
    result = check(text, [record])
    refs = _refs(result)
    assert refs, result
    assert result["verification"]["passed"] is expected, result
    assert _report_passes(text) is expected
    if expected:
        assert len(refs) == 2, refs
        assert all(ref["supported"] for ref in refs), refs
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


def test_manual06_real_statistics_bind_all_nine_refs() -> None:
    record = _record()
    result = check(REAL_STATISTICS, [record])
    refs = _refs(result)
    assert result["verification"]["passed"] is True, result
    assert _report_passes(REAL_STATISTICS)
    assert len(refs) == 9
    assert all(ref["supported"] for ref in refs)
    assert {ref["evidence_id"] for ref in refs} == {record.evidence_id}


def test_manual06_full_candidate_stays_blocked_by_unsupported_group_count() -> None:
    result = check(REAL_FULL_CANDIDATE, [_record()])
    assert result["verification"]["passed"] is False, result
    profile_claim = next(
        claim for claim in result["claims"] if claim["statement"].startswith(FIELD)
    )
    assert len(profile_claim["value_refs"]) == 9
    assert all(ref["supported"] for ref in profile_claim["value_refs"])
    assert any(
        not ref["supported"]
        for claim in result["claims"]
        if "重复组" in claim["statement"] or "存在 1 组" in claim["statement"]
        for ref in claim["value_refs"]
    ), result
    assert not _report_passes(REAL_FULL_CANDIDATE)
