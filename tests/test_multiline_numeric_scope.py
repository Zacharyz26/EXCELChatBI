"""Formatting invariant statistics across complete multiline assertion scopes."""

import json

import pandas as pd
import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe
from packages.session.models import Artifact

from tests import test_delimited_numeric_values as checks
from tests import test_profile_subject_boundaries as original


@pytest.fixture(scope="module")
def profile():
    return original.actual_profile.__wrapped__()


UNKNOWN_LAYOUTS = [
    "收入（元）空值率为 {value}。",
    "收入（元）空值率为\n{value}。",
    "收入（元）空值率为（ “{value}” ）。",
    "收入（元）空值率为（\n“{value}”\n）。",
    "收入（元）空值率为\n（\n「{value}」\n）。",
    "收入（元）空值率为（\n【\n『{value}』\n】\n）。",
    "收入（元）空值率为（\r\n“{value}”\r\n）。",
    "收入（元）空值率为（\n\n“{value}”\n\n）。",
    "收入（元）空值率为（\n“{value}”。",
    "收入（元）有（\n{value}\n）个\n「缺失值」。",
    "收入（元）有\n{value}\n个缺失值。",
    "收入（元）最大3。\n收入（元）空值率为（\n{value}\n）。",
    "| 字段 | 最大 |\n|---|---|\n| 收入（元） | 3 |\n\n收入（元）空值率为（\n{value}\n）。",
]


@pytest.mark.parametrize("value", ["b17", "2026-10-16", "二十五"])
@pytest.mark.parametrize("layout", UNKNOWN_LAYOUTS)
def test_unknown_statistic_is_independent_of_line_layout(profile, value, layout, tmp_path):
    text = layout.format(value=value)
    result = checks.evaluate(profile, text)
    (tmp_path / "witness.json").write_text(
        json.dumps({"text": text, **result}, ensure_ascii=False, indent=2)
    )
    assert not result["verification"]["passed"] and result["report"]["status"] == "rejected", result
    assert any(
        r.get("reason") == "unrecognized_numeric_expression" and r["token"] == value
        for r in result["refs"]
    ), result


@pytest.mark.parametrize("value,expected", [(25, True), (35, False)])
@pytest.mark.parametrize(
    "layout",
    [
        "收入（元）空值率为 {value}％。",
        "收入（元）空值率为\n{value}％。",
        "（\n收入（元）空值率为\n{value}％\n）。",
        "（\n收入（元）空值率为 {value}％\n）\n\n报告日期为（2026-10-16）。",
    ],
)
def test_supported_and_wrong_ratios_keep_their_meaning(profile, value, expected, layout):
    result = checks.evaluate(profile, layout.format(value=value))
    assert result["verification"]["passed"] is expected, result
    assert (result["report"]["status"] == "passed") is expected, result
    if expected:
        assert result["refs"] and all(
            r["semantic_binding"]["unit"] == "ratio" for r in result["refs"]
        )


@pytest.mark.parametrize(
    "text",
    [
        "报告编号为（\n“b17”\n）。",
        "报告日期（\n2026-10-16\n）。",
        "备注（\n二十五\n）是标签。",
        "以下\n三个提示\n供参考。",
        "收入（元）最大3\n报告编号（b17）。",
        "收入（元）最大3\n\n报告日期（2026-10-16）。",
        "收入（元）有\n1\n个缺失值。",
        "1. 收入（元）空值率25％。\n2. 报告日期（2026-10-16）。",
    ],
)
def test_prose_dates_lists_and_typed_counts_remain_valid(profile, text):
    result = checks.evaluate(profile, text)
    assert result["verification"]["passed"] and result["report"]["status"] == "passed", result


@pytest.mark.parametrize(
    "field", ["净收入「三」", "收入（b17）", "金额（万元）", "收入（2026-10-16）"]
)
@pytest.mark.parametrize("value,expected", [(25, True), (35, False)])
def test_multiline_assertion_preserves_exact_source_field(field, value, expected):
    result_profile = {
        "profile": infer_schema(
            {"dataset_ref": save_dataframe(pd.DataFrame({field: [2.0, 4.0, 6.0, None]}))}
        ).to_dict()
    }
    result = checks.evaluate(result_profile, f"（\n{field}空值率\n{value}％\n）。")
    assert (
        result["verification"]["passed"] is expected
        and (result["report"]["status"] == "passed") is expected
    ), result
    if expected:
        assert all(r["semantic_binding"]["metric"] == field for r in result["refs"])


@pytest.mark.parametrize("title", ["987654. 画像", "画像\n## 987654. 统计"])
def test_report_numeric_heading_is_not_discarded_as_list_counter(profile, title):
    artifact = Artifact(
        "p",
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
        validate_report_narratives(
            [artifact], {"title": title, "insights": "收入（元）空值率25％。"}
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,value,expected",
    [
        ("id", "b17", False),
        ("date", "2026-10-16", False),
        ("cjk", "二十五", False),
        ("valid", "1.33%", True),
    ],
)
async def test_actual_host_multiline_scope(tmp_path, label, value, expected):
    replacement = f"空值率约（\n“{value}”\n）" if not expected else f"空值率约\n{value}"
    await original.test_actual_host(
        tmp_path, "multiline-scope-" + label, "空值率约 1.33%", replacement, expected
    )


def test_rendered_ordered_list_counter_remains_a_counter(profile):
    from apps.orchestrator.control.report_claims import _visible_text

    # The previously mistaken heading fixture renders its second line as an
    # ordered-list counter. Preserve that original input as a positive case.
    title = "画像\n987654. 统计"
    assert "987654" not in _visible_text(title, heading=True)
    artifact = Artifact(
        "p",
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
    assert (
        validate_report_narratives(
            [artifact], {"title": title, "insights": "收入（元）空值率25％。"}
        )["status"]
        == "passed"
    )
