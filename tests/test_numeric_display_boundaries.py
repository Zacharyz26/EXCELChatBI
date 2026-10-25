"""Ratio measurements share a closed unit grammar in chat and report prose."""

from decimal import Decimal

import pandas as pd
import pytest
from apps.orchestrator.control.numeric_display import decimal_from_token, parse_numeric_display
from apps.orchestrator.control.report_claims import _visible_text
from mcp_servers.excel_parser.advisor import diagnose_data_quality, infer_data_roles
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import save_dataframe

from tests import test_profile_subject_boundaries as original


@pytest.fixture(scope="module")
def actual_profile():
    return original.actual_profile.__wrapped__()


RATIOS = [
    ("raw", "0.25", True),
    ("raw_precision", "0.2500", True),
    ("ascii", "25%", True),
    ("wide", "25％", True),
    ("wide_spaced", "25 ％", True),
    ("nbsp", "25\u00a0％", True),
    ("tab", "25\t%", True),
    ("bold", "**25％**", True),
    ("bold_value", "**25** ％", True),
    ("scientific", "2.5e1％", True),
    ("positive", "+25%", True),
    ("wide_wrong_scale", "0.25％", False),
    ("ascii_wrong_scale", "0.25%", False),
    ("raw_wrong_scale", "25", False),
    ("currency_yen", "0.25¥", False),
    ("currency_dollar", "0.25$", False),
    ("currency_euro", "0.25€", False),
    ("wide_brackets", "0.25【元】", False),
    ("wide_brackets_percent", "25%【元】", False),
    ("brackets", "0.25[kg]", False),
    ("celsius", "0.25℃", False),
    ("fahrenheit", "0.25℉", False),
    ("physical", "0.25千克", False),
    ("count", "0.25个", False),
    ("duplicate_spaced", "25 % %", False),
    ("duplicate_mixed", "25% ％", False),
    ("duplicate_wide", "25％ ％", False),
    ("duplicate_nbsp", "25％\u00a0%", False),
    ("percent_large", "25%万", False),
    ("large_percent", "0.25万%", False),
    ("tail_prose", "0.25额外单位", False),
    ("bold_unit", "25％**元**", False),
]


@pytest.mark.parametrize("name,ratio,expected", RATIOS, ids=[c[0] for c in RATIOS])
def test_chat_report_ratio_parity(actual_profile, name, ratio, expected):
    original.test_semantics(actual_profile, name, f"收入（元）空值率{ratio}。", expected)


@pytest.mark.parametrize(
    "token,suffix,factor,complete",
    [
        ("25%", "。", ".01", True),
        ("25％", "。", ".01", True),
        ("25", " ％。", ".01", True),
        ("0.25", "。", "1", True),
        ("0.25", "）。", "1", True),
        ("25%", " %。", None, False),
        ("25％", "\u00a0％。", None, False),
        ("25", " % %。", None, False),
        ("25%", "万", None, False),
        ("25", "万%", None, False),
        ("25", "万", "10000", False),
        ("0.25", "¥", "1", False),
        ("0.25", "【元】", "1", False),
        ("0.25", "℃", "1", False),
        ("0.25", "\u200b元", "1", False),
        ("25%%", "", None, False),
        ("①", "", None, False),
        ("⅓", "", None, False),
    ],
)
def test_scale_and_terminal_grammar(token, suffix, factor, complete):
    display = parse_numeric_display(token, suffix)
    assert display.factor == (None if factor is None else Decimal(factor))
    assert display.ratio_complete is complete


@pytest.mark.parametrize("sign", ["%", "％"])
def test_claim_retains_percent_token_and_scale(actual_profile, sign):
    result = original.check(f"收入（元）空值率25{sign}。", [original.record(actual_profile)])
    assert result["verification"]["passed"], result
    ref = result["refs"][0]
    assert ref["token"] == f"25{sign}"
    assert Decimal(str(ref["display_factor"])) == Decimal(".01")
    assert decimal_from_token(ref["token"]) == Decimal(25)
    assert ref["semantic_binding"]["metric"] == "收入（元）"


@pytest.mark.parametrize("name", ["指标①", "温度℃", "比率％"])
def test_unicode_field_identity_is_preserved(name):
    profile = infer_schema(
        {"dataset_ref": save_dataframe(pd.DataFrame({name: [1.0, 2.0, 3.0, None]}))}
    )
    roles = infer_data_roles(profile)
    result = {
        "profile": profile.to_dict(),
        "roles": roles,
        "quality": diagnose_data_quality(profile, roles, duplicate_rows=0),
    }
    text = f"{name}空值率25％。"
    assert name in _visible_text(text)
    original.test_semantics(result, name, text, True)
    checked = original.check(text, [original.record(result)])
    assert checked["refs"][0]["semantic_binding"]["metric"] == name


def test_report_rendering_does_not_rewrite_unicode_values_or_identifiers():
    text = "字段①；分数⅓；温度℃；比率25％；字段\u200b名。"
    assert text in _visible_text(text)


HOST_RATIOS = [
    ("fullwidth_correct", "1.33％", True),
    ("fullwidth_spaced", "1.33 ％", True),
    ("fullwidth_wrong", "0.0133％", False),
    ("currency_symbol", "0.0133¥", False),
    ("currency_dollar", "0.0133$", False),
    ("wide_brackets", "0.0133【元】", False),
    ("celsius", "0.0133℃", False),
    ("double_percent_space", "1.33 % %", False),
    ("double_mixed", "1.33％ %", False),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label,ratio,expected", HOST_RATIOS, ids=[c[0] for c in HOST_RATIOS])
async def test_actual_host_ratio_units(tmp_path, label, ratio, expected):
    # Real upload, get_data_profile, Evidence/Claim persistence and report verifier;
    # only the model text is fixed. The shared harness asserts both chat and report.
    await original.test_actual_host(
        tmp_path, label, "空值率约 1.33%", f"空值率约 {ratio}", expected
    )
