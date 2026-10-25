"""MANUAL-04: bounded field labels and exact unit declarations from a real response."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from packages.session.task_models import EvidenceRecord

from tests.test_manual03_contract import CAPTURE, FIELD, evaluate
from tests.test_profile_subject_boundaries import check

ORIGINAL_STATISTICS = (
    "播出时长（分钟）字段：单位即列名标注的「分钟」。"
    "最小值 10.0，最大值 999.0，均值 50.121622，中位数 37.5，"
    "不同值数量 37，精确空值数 1，非空数 74，"
    "缺失比例 0.0133（约 1.33%）。"
)

ORIGINAL_WITH_UNSUPPORTED_ADDITIONS = (
    "画像复核结果如下。\n\n"
    "数据规模与列名：共 75 行、5 列，列名依次为「月份」「频道」「状态」"
    "「播出时长（分钟）」「记录ID」。\n\n"
    + ORIGINAL_STATISTICS
    + "该字段被识别为指标（metric），置信度 0.95。\n\n"
    "重复情况：工具返回整行重复记录数为 1（duplicate_rows=1，重复比例 0.0133），"
    "即存在 1 条与另一条完全相同的整行重复记录。记录ID 字段非空数 75、"
    "不同值数量 74，说明有 1 个记录ID 值出现了重复"
    "（duplicate_values_at_least=1），该字段被识别为标识符（identifier），"
    "但唯一率仅 0.9867，并非完全唯一。"
)


def test_frozen_real_statistics_bind_to_one_exact_field(tmp_path):
    result = evaluate(ORIGINAL_STATISTICS, True, tmp_path)
    statistic = next(claim for claim in result["claims"] if claim["statement"].startswith("最小值"))
    assert len(statistic["value_refs"]) == 9
    assert all(ref["supported"] for ref in statistic["value_refs"])
    assert {ref["semantic_binding"]["metric"] for ref in statistic["value_refs"]} == {FIELD}


@pytest.mark.parametrize(
    "text",
    [
        f"{FIELD}字段：最大值999。",
        f"{FIELD}列：最大值999。",
        f"{FIELD}字段：单位为分钟。最大值999。",
        f"{FIELD}字段：单位即列名中的“分钟”。最大值999。",
        f"{FIELD}字段：单位即列名标注的「分钟」。最大值999。",
        "记录ID 字段非空数75、不同值数量74。",
        f"**{FIELD}的统计**\n\n单位为分钟。该列最大值999。",
    ],
)
def test_bounded_field_label_variants(text, tmp_path):
    evaluate(text, True, tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        f"{FIELD}字段：单位为分钟。最大值998。",
        f"{FIELD}字段：单位为分钟。最小值999。",
        f"{FIELD}字段：单位为小时。最大值999。",
        "月份字段：单位为分钟。最大值999。",
        f"预算{FIELD}字段：单位为分钟。最大值999。",
        f"{FIELD}和记录ID字段：单位为分钟。最大值999。",
        f"示例：{FIELD}字段：单位为分钟。最大值999。",
        f"{FIELD}字段：单位为分钟。仅限频道A。最大值999。",
        f"{FIELD}字段：单位为分钟。版本 old。最大值999。",
        f"{FIELD}字段：单位为分钟。记录ID最大值999。",
        f"{FIELD}字段：单位为分钟，筛选后最大值999。",
        "整行重复组数为1。",
        "整行参与重复记录数为2。",
        "整行重复多余记录数为2。",
    ],
)
def test_field_label_never_relaxes_identity_scope_or_truth(text, tmp_path):
    evaluate(text, False, tmp_path)


def test_full_real_candidate_still_rejects_unproven_additions(tmp_path):
    result = evaluate(ORIGINAL_WITH_UNSUPPORTED_ADDITIONS, False, tmp_path)
    statistic = next(claim for claim in result["claims"] if claim["statement"].startswith("最小值"))
    assert all(ref["supported"] for ref in statistic["value_refs"])
    assert any(
        not ref["supported"]
        for claim in result["claims"]
        if "置信度" in claim["statement"] or "重复" in claim["statement"]
        for ref in claim["value_refs"]
    )


@pytest.mark.parametrize(
    "text",
    [
        f"{FIELD}字段：单位为分钟。\n最大值999。",
        f"{FIELD}字段：单位为分钟。\n\n最大值999。",
        f"{FIELD}字段：单位为分钟。\n- 最大值999。",
        f"{FIELD}字段：单位为分钟。\n> 最大值999。",
        f"{FIELD}字段：单位为分钟。\n### 统计\n\n最大值999。",
        f"{FIELD}字段：单位为分钟。\n| 统计 | 数值 |\n|---|---|\n| 最大值 | 999 |",
        f"{FIELD}字段：单位为分钟。最大值999。最小值10。",
    ],
)
def test_field_label_context_stops_at_structural_boundary_or_one_statement(text, tmp_path):
    evaluate(text, False, tmp_path)


def _renamed_profile_record(field: str) -> EvidenceRecord:
    source = EvidenceRecord(**CAPTURE["evidence"])
    summary = copy.deepcopy(source.summary)
    for item in summary["value_index"]:
        if item.get("metric") == FIELD:
            item["metric"] = field
        if item.get("profile_column") == FIELD:
            item["profile_column"] = field
    return replace(source, evidence_id=f"renamed-{field}", summary=summary)


@pytest.mark.parametrize(
    ("field", "base", "unit"),
    [
        ("时长（分钟）", "时长", "分钟"),
        ("收入（元）", "收入", "元"),
        ("转化率（%）", "转化率", "%"),
        ("距离（公里）", "距离", "公里"),
        ("重量（千克）", "重量", "千克"),
    ],
)
def test_trusted_parenthetical_unit_can_create_exact_base_alias(field, base, unit):
    result = check(f"{base}字段：单位为{unit}。最大值999。", [_renamed_profile_record(field)])
    assert result["refs"], result
    assert result["verification"]["passed"] is True, result


@pytest.mark.parametrize("qualifier", ["含税", "未税", "调整后", "预测", "最新"])
def test_parenthetical_qualifier_never_creates_a_base_field_alias(qualifier):
    field = f"利润（{qualifier}）"
    evidence = [_renamed_profile_record(field)]
    result = check(f"利润字段：单位为{qualifier}。最大值999。", evidence)
    assert result["refs"], result
    assert result["verification"]["passed"] is False, result

    exact = check(f"{field}最大值999。", evidence)
    assert exact["refs"], exact
    assert exact["verification"]["passed"] is True, exact
