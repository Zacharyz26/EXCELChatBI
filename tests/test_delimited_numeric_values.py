"""I07: delimiters cannot hide unsupported values inside a statistic slot."""

import json

import pandas as pd
import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.session.models import Artifact

from tests import test_profile_subject_boundaries as original


@pytest.fixture(scope="module")
def profile():
    return original.actual_profile.__wrapped__()


def evaluate(profile, text):
    result = original.check(text, [original.record(profile)])
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
    try:
        result["report"] = validate_report_narratives(
            [artifact], {"title": "画像", "insights": text}
        )
    except ValueError as error:
        result["report"] = {"status": "rejected", "error": str(error)}
    return result


def assert_rejected(profile, text, tmp_path):
    result = evaluate(profile, text)
    (tmp_path / "claim-witness.json").write_text(
        json.dumps({"text": text, **result}, ensure_ascii=False, indent=2)
    )
    assert not result["verification"]["passed"] and result["report"]["status"] == "rejected", result
    assert any(
        ref.get("reason") == "unrecognized_numeric_expression" for ref in result["refs"]
    ), result


VALUES = ["a25", "2024-02-29", "三"]
WRAPPERS = [
    ("(", ")"),
    ("（", "）"),
    ("「", "」"),
    ("『", "』"),
    ("[", "]"),
    ("【", "】"),
    ("“", "”"),
    ('"', '"'),
]


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("opening,closing", WRAPPERS)
def test_wrapped_statistic_values(profile, value, opening, closing, tmp_path):
    assert_rejected(profile, f"收入（元）空值率{opening}{value}{closing}。", tmp_path)


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize(
    "template",
    [
        "收入（元）「空值率」为（「{value}」）。",
        "收入（元）空值率 ： 约 （ “{value}” ）。",
        "收入（元）最大3；收入（元）空值率（{value}）。",
        "| 字段 | 空值率 |\n|---|---|\n| 收入（元） | （{value}） |",
    ],
)
def test_nested_mixed_and_table_values(profile, value, template, tmp_path):
    assert_rejected(profile, template.format(value=value), tmp_path)


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize(
    "template",
    [
        "收入（元）有（{value}）个缺失值。",
        "收入（元）有「（{value}）」个不同取值。",
        "收入（元）有（{value}）个（空值）。",
    ],
)
def test_postfix_count_slots(profile, value, template, tmp_path):
    assert_rejected(profile, template.format(value=value), tmp_path)


@pytest.mark.parametrize(
    "text",
    [
        "报告编号为（a25）。",
        "报告编号「a25」已生成。",
        "示例日期为（2024-02-29）。",
        "示例月份「2024-02」。",
        "以下（三）个提示供参考。",
        "备注「三」是标签。",
        "报告（a25）附日期「2024-02-29」和三个提示。",
        "收入（元）空值率25％。",
        "（收入（元）空值率25％）。",
    ],
)
def test_labels_units_dates_and_supported_claims(profile, text):
    result = evaluate(profile, text)
    assert result["verification"]["passed"] and result["report"]["status"] == "passed", result


@pytest.mark.parametrize(
    "field",
    [
        "收入（a25）",
        "收入（2024-02-29）",
        "收入（三）",
        "金额（万元）",
        "指标「三」",
        "空值率（三）",
    ],
)
def test_actual_source_field_identity_is_preserved(field):
    profile = {
        "profile": infer_schema(
            {"dataset_ref": save_dataframe(pd.DataFrame({field: [1.0, 2.0, 3.0, None]}))}
        ).to_dict()
    }
    result = evaluate(profile, f"{field}空值率25％。")
    assert result["verification"]["passed"] and result["report"]["status"] == "passed", result
    assert result["refs"] and all(
        ref["semantic_binding"]["metric"] == field for ref in result["refs"]
    )


HOST_CASES = [
    ("identifier", "空值率约 1.33%", "空值率约（a25）"),
    ("date", "空值率约 1.33%", "空值率约（2024-02-29）"),
    ("ideograph", "空值率约 1.33%", "空值率约（三）"),
    ("count", "有 1 个缺失值", "有（三）个缺失值"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,before,after", HOST_CASES, ids=[c[0] for c in HOST_CASES])
async def test_actual_host_delimited_values(tmp_path, label, before, after):
    await original.test_actual_host(tmp_path, "delimited-value-" + label, before, after, False)
