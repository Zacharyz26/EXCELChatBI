"""I07/I08: numeric omissions are failures; percentage scaling needs typed ratios."""

from dataclasses import replace

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_numeric_claims
from apps.orchestrator.control.report_claims import validate_report_narratives
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.session.models import Artifact

from tests import test_profile_subject_boundaries as original


@pytest.fixture(scope="module")
def profile():
    return original.actual_profile.__wrapped__()


UNKNOWN = ["⅓", "½", "①", "¹⁰%", "₂", "Ⅳ", "0.25e", "0.25E+", "0.25e-", "0.25efoo", "三", "二十五"]


@pytest.mark.parametrize("value", UNKNOWN)
@pytest.mark.parametrize("context", ["plain", "mixed", "table"])
def test_unrecognized_value_cannot_disappear(profile, value, context):
    text = f"收入（元）空值率{value}。"
    if context == "mixed":
        text = "收入（元）最大3。" + text
    elif context == "table":
        text = f"| 字段 | 空值率 |\n|---|---|\n| 收入（元） | {value} |"
    result = original.check(text, [original.record(profile)])
    assert not result["verification"]["passed"], result
    assert any(
        ref.get("reason") == "unrecognized_numeric_expression" for ref in result["refs"]
    ), result
    artifact = Artifact(
        "profile",
        "c",
        "m",
        "profile",
        profile,
        None,
        "get_data_profile",
        {"dataset_ref": "d" * 32},
        "d" * 32,
        "now",
    )
    with pytest.raises(ValueError):
        validate_report_narratives([artifact], {"title": "画像", "insights": text})


@pytest.mark.parametrize("name", ["指标①", "字段⅓", "温度℃", "指数²", "归档\u200b值", "甲／乙"])
def test_source_identifier_preserved(name):
    result = {
        "profile": infer_schema(
            {"dataset_ref": save_dataframe(pd.DataFrame({name: [1.0, 2.0, 3.0, None]}))}
        ).to_dict()
    }
    original.test_semantics(result, name, f"{name}空值率25％。", True)
    refs = original.check(f"{name}空值率25％。", [original.record(result)])["refs"]
    assert refs[0]["semantic_binding"]["metric"] == name


@pytest.mark.parametrize(
    "text",
    [
        "报告编号为 report-1。",
        "文件 dataset2026.csv 已准备。",
        "示例日期为2026-01至2026-05。",
        "示例日期为2026-01-31。",
        "版本 v2.1 已准备。",
    ],
)
def test_non_measurement_identifiers_and_dates(text):
    assert extract_numeric_claims(final_text=text, goal="", evidence=[]) == []


@pytest.mark.parametrize("value", ["2026-01", "0.25e", "e25"])
def test_identifier_or_calendar_spelling_in_value_position_is_not_exempt(profile, value):
    original.test_semantics(profile, value, f"收入（元）空值率{value}。", False)


@pytest.mark.parametrize("field", ["金额（元）", "时长（分钟）"])
@pytest.mark.parametrize("statistic", ["最大", "最小", "均值", "中位数"])
@pytest.mark.parametrize("sign", ["%", "％", " ％"])
def test_equal_raw_value_and_ratio_cannot_exchange_units(field, statistic, sign):
    result = {
        "profile": infer_schema(
            {"dataset_ref": save_dataframe(pd.DataFrame({field: [0.25, 0.25, 0.25, None]}))}
        ).to_dict()
    }
    # Both the raw statistic and null_ratio are .25; magnitude cannot identify unit/role.
    original.test_semantics(result, "ratio", f"{field}空值率25{sign}。", True)
    original.test_semantics(result, "raw", f"{field}{statistic}0.25。", True)
    original.test_semantics(result, "wrong-unit", f"{field}{statistic}25{sign}。", False)


@pytest.mark.parametrize("sign", ["%", "％", " ％"])
def test_count_cannot_use_percentage_scale(profile, sign):
    original.test_semantics(profile, "count", f"收入（元）空值数100{sign}。", False)


@pytest.mark.parametrize(
    "tool,kind,payload,args,text,expected",
    [
        (
            "aggregate_preview",
            "tool_result",
            {"total_value": 0.25},
            {"value_col": "时长（分钟）", "agg": "sum"},
            "时长（分钟）总计25％。",
            False,
        ),
        (
            "aggregate_preview",
            "tool_result",
            {"total_value": 0.25},
            {"value_col": "时长（分钟）", "agg": "sum"},
            "时长（分钟）总计0.25。",
            True,
        ),
        (
            "get_data_profile",
            "tool_result",
            {"rate": 0.25, "row_count": 0.25, "amount": 0.25},
            {},
            "转化率25％。",
            True,
        ),
        (
            "get_data_profile",
            "tool_result",
            {"rate": 0.25, "row_count": 0.25, "amount": 0.25},
            {},
            "总行数25％。",
            False,
        ),
        (
            "get_data_profile",
            "tool_result",
            {"rate": 0.25, "amount": 0.25},
            {},
            "销售额总计25％。",
            False,
        ),
        ("get_data_profile", "tool_result", {"amount": 0.25}, {}, "转化率25％。", False),
        ("get_data_profile", "tool_result", {"amount": 0.25}, {}, "金额25％。", False),
    ],
)
def test_percentage_gate_applies_to_all_sources(profile, tool, kind, payload, args, text, expected):
    evidence = replace(
        original.record(profile),
        kind=kind,
        source={"tool": tool},
        summary=build_evidence_summary(
            summary="", result=payload, artifact_id=None, arguments=args
        ),
    )
    result = original.check(text, [evidence])
    assert result["verification"]["passed"] is expected, result
    if expected and "%" in text.replace("％", "%"):
        assert all(r["semantic_binding"]["unit"] == "ratio" for r in result["refs"])


HOST_CASES = [
    ("unicode_fraction", "空值率约 1.33%", "空值率约 ⅓"),
    ("circled_count", "有 1 个缺失值", "有 ② 个缺失值"),
    ("dangling_scientific", "空值率约 1.33%", "空值率约 0.0133e"),
    ("raw_ascii_percent", "中位数 37.5", "中位数 3750%"),
    ("raw_wide_percent", "中位数 37.5", "中位数 3750％"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,before,after", HOST_CASES, ids=[c[0] for c in HOST_CASES])
async def test_actual_host_numeric_guard(tmp_path, label, before, after):
    # Same complete original answer with one erroneous substitution; real tool,
    # upload, persistence, chat and report verification. No output repair shortcut.
    await original.test_actual_host(tmp_path, "numeric-guard-" + label, before, after, False)
