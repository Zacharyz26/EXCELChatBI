"""独立后审组合：标题内事实不能被结构遮罩；回指不能吞掉赋值槽。"""

from __future__ import annotations

import json
from dataclasses import asdict

import pytest
from apps.orchestrator.control.quantity_presentation import validate_candidate

from tests import test_delimited_numeric_values as consumers
from tests import test_profile_subject_boundaries as source
from tests.test_r1_core_quantity_gate import broadcast_profile

BODY = (
    "记录ID重复多余记录数为1，记录ID distinct为74，"
    "播出时长（分钟）有1个缺失值，整行重复多余记录数为1。"
)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("顺带提示三个数据质量点，供后续参考：" + BODY, True),
        ("播出时长（分钟）最大888，顺带提示三个数据质量点：" + BODY, False),
        ("顺带提示三个数据质量点，播出时长（分钟）最大888，供后续参考：" + BODY, False),
        ("顺带提示三个数据质量点，八个异常均已核实，供后续参考：" + BODY, False),
        ("顺带提示三个数据质量点，播出时长（分钟）最大999，供后续参考：" + BODY, True),
    ],
)
def test_diagnostic_heading_preserves_every_fact(text, expected, tmp_path):
    profile = broadcast_profile.__wrapped__()
    records = [source.record(profile)]
    candidate = validate_candidate(text, records, goal="复核画像")
    results = consumers.evaluate(profile, text)
    witness = {
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "checks": results,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert results["verification"]["passed"] is expected, witness
    assert (results["report"]["status"] == "passed") is expected, witness
    if "最大888" in text:
        assert any(q["token"] == "888" for q in candidate.quantities), "标题不能吞掉真实统计数值"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("收入（元）最大这一元。", False),
        ("收入（元）有这一个。", False),
        ("收入（元）最大\n这一元。", False),
        ("收入（元）有\n这一个。", False),
        ("收入（元）最大\n那一元。", False),
        ("收入（元）最大3。画像印证了这一点。", True),
        ("收入（元）最大3。这一点说明八个异常均已核实。", False),
    ],
)
def test_deictic_phrase_cannot_own_business_assignment(text, expected, tmp_path):
    profile = source.actual_profile.__wrapped__()
    records = [source.record(profile)]
    candidate = validate_candidate(text, records, goal="复核画像")
    results = consumers.evaluate(profile, text)
    witness = {
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "checks": results,
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert results["verification"]["passed"] is expected, witness
    assert (results["report"]["status"] == "passed") is expected, witness


@pytest.mark.parametrize("operator", ["为", "是", "就是", "等于", "="])
@pytest.mark.parametrize("gap", ["", "\n"], ids=["inline", "softline"])
def test_general_assignment_keeps_unknown_business_quantity(operator, gap, tmp_path):
    """未知业务赋值须保留数量；不能因缺少统计角色反而没有Claim即通过。"""
    profile = source.actual_profile.__wrapped__()
    text = f"收入（元）{operator}{gap}这一元。"
    candidate = validate_candidate(text, [source.record(profile)], goal="复核画像")
    results = consumers.evaluate(profile, text)
    witness = {"text": text, "expected": False, "candidate": asdict(candidate), "checks": results}
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert not candidate.passed, witness
    assert not results["verification"]["passed"], witness
    assert results["report"]["status"] == "rejected", witness
    assert candidate.quantities, "未知赋值必须保留为数量义务"


@pytest.mark.parametrize(
    "text",
    [
        "收入（元）最大3元。画像印证了这一点。",
        "收入（元）最大3元。\n画像印证了这一点。",
    ],
)
def test_bound_business_fact_then_deictic_proposition_remains_valid(text, tmp_path):
    profile = source.actual_profile.__wrapped__()
    candidate = validate_candidate(text, [source.record(profile)], goal="复核画像")
    results = consumers.evaluate(profile, text)
    witness = {"text": text, "expected": True, "candidate": asdict(candidate), "checks": results}
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed and results["verification"]["passed"], witness
    assert results["report"]["status"] == "passed", witness
    assert all(q["token"] != "一" for q in candidate.quantities)
