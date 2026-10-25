"""MANUAL-05: one bounded profile-stat header grammar across chat and reports."""

from __future__ import annotations

import copy
from dataclasses import dataclass, fields, replace

import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

from tests.test_manual03_contract import CAPTURE, FIELD
from tests.test_profile_subject_boundaries import check

REAL_STATISTICS = (
    "播出时长（分钟）的统计（单位即分钟，列名已标注）："
    "最小值为 10.0，最大值为 999.0，均值为 50.121622，中位数为 37.5，"
    "不同值数量为 37，精确空值数为 1，非空数为 74，"
    "缺失比例为 0.0133（约 1.33%）。"
)

REAL_FULL_CANDIDATE = (
    "已取得画像结果，下面按你的要求逐项说明。\n\n"
    "数据规模与列名：共 75 行、5 列，列名依次为「月份」「频道」「状态」"
    "「播出时长（分钟）」「记录ID」。\n\n"
    + REAL_STATISTICS
    + "\n\n整行重复情况：工具返回 duplicate_rows 为 1，即存在 1 行整行重复"
    "（重复比例 0.0133）。据此，去重会删除的多余记录数为 1，"
    "参与重复的记录数为 2，重复组数为 1 组。需要说明的是，工具只给出了"
    "「重复行数」这一口径，我未对「参与重复的记录数」和「重复组数」做额外推算。\n\n"
    "记录ID 重复情况：记录ID 非空数为 75，不同值数量为 74，说明存在至少 1 个重复值"
    "（工具标注 duplicate_values_at_least 为 1）。工具未直接给出参与数或组数。\n\n"
    "另外两点提示：记录ID 被识别为标识列但并非完全唯一（唯一比例 0.9867）；"
    "播出时长的 1 个空值建议先确认缺失机制。"
)


@dataclass(frozen=True)
class HeaderCase:
    name: str
    text: str
    expected: bool
    field_override: str | None = None


CASES = [
    # A. Frozen real response and established minimal controls: 8.
    HeaderCase("a01_real_statistics", REAL_STATISTICS, True),
    HeaderCase("a02_real_full_candidate", REAL_FULL_CANDIDATE, False),
    HeaderCase(
        "a03_real_minimal_header",
        f"{FIELD}的统计（单位即分钟，列名已标注）：最大值为999。",
        True,
    ),
    HeaderCase("a04_unit_only_header", f"{FIELD}的统计（单位即分钟）：最大值为999。", True),
    HeaderCase("a05_bare_statistics_header", f"{FIELD}的统计：最大值为999。", True),
    HeaderCase("a06_field_label_contract", f"{FIELD}字段：单位为分钟。最大值999。", True),
    HeaderCase("a07_explicit_full_field", f"{FIELD}最大值999。", True),
    HeaderCase("a08_markdown_field_heading", f"**{FIELD}的统计**\n\n该列最大值999。", True),
    # B. One bounded header grammar: 16.
    HeaderCase(
        "b01_ascii_parentheses_and_comma",
        f"{FIELD}的统计(单位即分钟,列名已标注):最大值为999。",
        True,
    ),
    HeaderCase(
        "b02_metadata_order_reversed",
        f"{FIELD}的统计（列名已标注，单位即分钟）：最大值为999。",
        True,
    ),
    HeaderCase(
        "b03_quotes_and_spaces",
        f"{FIELD} 的统计（单位即 '分钟' , 列名已注明） ： 最大值为 999。",
        True,
    ),
    HeaderCase(
        "b04_trusted_base_alias",
        "播出时长的统计（单位即分钟，列名已标注）：最大值为999。",
        True,
    ),
    HeaderCase(
        "b05_column_name_unit_declaration",
        f"{FIELD}的统计（列名已标注为「分钟」）：最大值为999。",
        True,
    ),
    HeaderCase(
        "b06_unknown_unit_full_field",
        "温度（摄氏度）的统计：最大值为999。",
        True,
        "温度（摄氏度）",
    ),
    HeaderCase(
        "b07_wrong_metadata_unit",
        f"{FIELD}的统计（单位即小时，列名已标注）：最大值为999。",
        False,
    ),
    HeaderCase(
        "b08_conflicting_units",
        f"{FIELD}的统计（单位即分钟，单位即小时）：最大值为999。",
        False,
    ),
    HeaderCase("b09_non_unit_qualifier", f"{FIELD}的统计（含税）：最大值为999。", False),
    HeaderCase("b10_unknown_metadata", f"{FIELD}的统计（来自旧报表）：最大值为999。", False),
    HeaderCase("b11_unclosed_metadata", f"{FIELD}的统计（单位即分钟：最大值为999。", False),
    HeaderCase(
        "b12_nested_metadata",
        f"{FIELD}的统计（单位即（分钟），列名已标注）：最大值为999。",
        False,
    ),
    HeaderCase(
        "b13_multiple_fields",
        f"{FIELD}与记录ID的统计（单位即分钟）：最大值为999。",
        False,
    ),
    HeaderCase(
        "b14_similar_field_suffix",
        f"{FIELD}预算的统计（单位即分钟）：最大值为999。",
        False,
    ),
    HeaderCase("b15_unknown_field", "不存在字段的统计（单位即分钟）：最大值为999。", False),
    HeaderCase(
        "b16_unknown_unit_base_alias",
        "温度的统计（单位即摄氏度）：最大值为999。",
        False,
        "温度（摄氏度）",
    ),
    # C. Statement and structural boundaries: 12.
    HeaderCase("c01_same_statement", f"{FIELD}的统计（单位即分钟）：最大值999。", True),
    HeaderCase(
        "c02_comma_stat_list",
        f"{FIELD}的统计（单位即分钟）：最小值10，最大值999。",
        True,
    ),
    HeaderCase(
        "c03_semicolon_stat_list",
        f"{FIELD}的统计（单位即分钟）：最大值999；最小值10。",
        True,
    ),
    HeaderCase(
        "c04_period_second_hop",
        f"{FIELD}的统计（单位即分钟）：最大值999。最小值10。",
        False,
    ),
    HeaderCase("c05_lf_paragraph", f"{FIELD}字段：单位为分钟。\n\n最大值999。", False),
    HeaderCase(
        "c06_crlf_paragraph",
        f"{FIELD}字段：单位为分钟。\r\n\r\n最大值999。",
        False,
    ),
    HeaderCase("c07_new_list_item", f"{FIELD}字段：单位为分钟。\n- 最大值999。", False),
    HeaderCase("c08_blockquote", f"{FIELD}字段：单位为分钟。\n> 最大值999。", False),
    HeaderCase(
        "c09_new_heading",
        f"{FIELD}字段：单位为分钟。\n### 统计\n\n最大值999。",
        False,
    ),
    HeaderCase(
        "c10_table_boundary",
        f"{FIELD}字段：单位为分钟。\n| 统计 | 数值 |\n|---|---|\n| 最大值 | 999 |",
        False,
    ),
    HeaderCase(
        "c11_version_switch",
        f"{FIELD}的统计（单位即分钟）：版本 old，最大值999。",
        False,
    ),
    HeaderCase(
        "c12_filter_switch",
        f"{FIELD}的统计（单位即分钟）：仅限频道A，最大值999。",
        False,
    ),
    # D. Typed profile facts and unsupported roles: 16.
    HeaderCase("d01_min", f"{FIELD}的统计（单位即分钟）：最小值10。", True),
    HeaderCase("d02_max", f"{FIELD}的统计（单位即分钟）：最大值999。", True),
    HeaderCase("d03_mean", f"{FIELD}的统计（单位即分钟）：均值50.121622。", True),
    HeaderCase("d04_median", f"{FIELD}的统计（单位即分钟）：中位数37.5。", True),
    HeaderCase("d05_distinct", f"{FIELD}的统计（单位即分钟）：不同值数量37。", True),
    HeaderCase("d06_null_count", f"{FIELD}的统计（单位即分钟）：精确空值数1。", True),
    HeaderCase("d07_non_null", f"{FIELD}的统计（单位即分钟）：非空数74。", True),
    HeaderCase(
        "d08_null_ratio_restatement",
        f"{FIELD}的统计（单位即分钟）：缺失比例0.0133（约1.33%）。",
        True,
    ),
    HeaderCase("d09_wrong_value", f"{FIELD}的统计（单位即分钟）：最大值998。", False),
    HeaderCase("d10_wrong_role", f"{FIELD}的统计（单位即分钟）：最小值999。", False),
    HeaderCase("d11_wrong_physical_unit", f"{FIELD}的统计（单位即分钟）：最大值999小时。", False),
    HeaderCase("d12_row_duplicate_excess", "整行重复多余记录数为1。", True),
    HeaderCase("d13_id_duplicate_excess", "记录ID重复多余记录数为1。", True),
    HeaderCase(
        "d14_unsupported_participants_groups",
        "整行参与重复记录数为2，重复组数为1。",
        False,
    ),
    HeaderCase("d15_unsupported_confidence", f"{FIELD}角色置信度0.95。", False),
    HeaderCase("d16_unsupported_unique_ratio", "记录ID唯一率0.9867。", False),
]

REPORT_CASE_IDS = {
    "a01_real_statistics",
    "a02_real_full_candidate",
    "a03_real_minimal_header",
    "a04_unit_only_header",
    "a05_bare_statistics_header",
    "b02_metadata_order_reversed",
    "b07_wrong_metadata_unit",
    "b08_conflicting_units",
    "b10_unknown_metadata",
    "c05_lf_paragraph",
    "c11_version_switch",
    "d14_unsupported_participants_groups",
}

assert len(CASES) == 52
assert len(REPORT_CASE_IDS) == 12

LABEL_CONFIRMATION_POLARITY_CASES = [
    ("bare_column_marked", "列名标注", True),
    ("bare_field_noted_before", "字段名注明过", True),
    ("yi_column_marked", "列名已标注", True),
    ("yi_field_noted_after", "字段名已注明了", True),
    ("yijing_column_marked", "列名已经标注", True),
    ("yijing_field_noted", "字段名已经注明", True),
    ("self_yijing_marked_after", "列名本身已经标注了", True),
    ("self_yi_noted_before", "字段名本身已注明过", True),
    ("negative_unmarked", "列名未标注", False),
    ("negative_not_yet_noted", "字段名尚未注明", False),
    ("negative_still_unmarked", "列名仍未标注", False),
    ("negative_never_noted", "字段名没有注明", False),
    ("negative_emphatic_unmarked", "列名并未标注", False),
    ("uncertain_whether_marked", "列名是否标注", False),
    ("uncertain_maybe_noted", "字段名可能注明", False),
    ("uncertain_perhaps_marked", "列名也许标注", False),
    ("uncertain_seems_noted", "字段名似乎注明", False),
    ("uncertain_reportedly_marked", "列名据说标注", False),
]

assert len(LABEL_CONFIRMATION_POLARITY_CASES) == 18


def _record(field_override: str | None = None) -> EvidenceRecord:
    source = EvidenceRecord(**CAPTURE["evidence"])
    if field_override is None:
        return source
    summary = copy.deepcopy(source.summary)
    for item in summary["value_index"]:
        if item.get("metric") == FIELD:
            item["metric"] = field_override
        if item.get("profile_column") == FIELD:
            item["profile_column"] = field_override
    return replace(source, evidence_id=f"manual05-{field_override}", summary=summary)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_manual05_chat_matrix(case: HeaderCase) -> None:
    record = _record(case.field_override)
    result = check(case.text, [record])
    assert result["refs"], result
    assert result["verification"]["passed"] is case.expected, result
    if case.name == "a01_real_statistics":
        claim = next(item for item in result["claims"] if item["statement"].startswith(FIELD))
        assert len(claim["value_refs"]) == 9
        assert all(ref["supported"] for ref in claim["value_refs"])
        assert set(claim["evidence_ids"]) == {record.evidence_id}
        assert {ref["semantic_binding"]["metric"] for ref in claim["value_refs"]} == {FIELD}
        assert {ref["semantic_binding"]["statistic"] for ref in claim["value_refs"]} == {
            "min",
            "max",
            "mean",
            "median",
            "distinct_count",
            "null_count",
            "non_null_count",
            "null_ratio",
        }
    if case.name == "a02_real_full_candidate":
        claim = next(item for item in result["claims"] if item["statement"].startswith(FIELD))
        assert all(ref["supported"] for ref in claim["value_refs"]), result
        assert any(
            not ref["supported"]
            for item in result["claims"]
            for ref in item["value_refs"]
            if any(label in item["statement"] for label in ("参与重复", "重复组", "唯一比例"))
        ), result


@pytest.mark.parametrize(
    "case",
    [case for case in CASES if case.name in REPORT_CASE_IDS],
    ids=lambda case: case.name,
)
def test_manual05_report_matrix(case: HeaderCase) -> None:
    artifact = Artifact(
        **{field.name: CAPTURE["artifact"].get(field.name) for field in fields(Artifact)}
    )
    try:
        result = validate_report_narratives([artifact], {"title": "画像", "insights": case.text})
    except ValueError:
        passed = False
    else:
        passed = result["status"] == "passed"
    assert passed is case.expected


@pytest.mark.parametrize(
    ("_name", "atom", "expected"),
    LABEL_CONFIRMATION_POLARITY_CASES,
    ids=[case[0] for case in LABEL_CONFIRMATION_POLARITY_CASES],
)
def test_manual05_label_confirmation_polarity_in_chat_and_report(
    _name: str, atom: str, expected: bool
) -> None:
    text = f"{FIELD}的统计（{atom}，单位即分钟）：最大值999。"
    record = _record()
    chat = check(text, [record])
    assert chat["refs"], chat
    assert chat["verification"]["passed"] is expected, chat

    artifact = Artifact(
        **{field.name: CAPTURE["artifact"].get(field.name) for field in fields(Artifact)}
    )
    try:
        report = validate_report_narratives([artifact], {"title": "画像", "insights": text})
    except ValueError:
        report_passed = False
    else:
        report_passed = report["status"] == "passed"
    assert report_passed is expected
