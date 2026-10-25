"""I09: grouping does not lend field/role evidence between complete statements."""

import json

import pytest

from tests import test_delimited_numeric_values as checks
from tests import test_profile_subject_boundaries as original


@pytest.fixture(scope="module")
def profile():
    return original.actual_profile.__wrapped__()


LAYOUTS = [
    ("", "。", ""),
    ("（", "。", "）"),
    ("（", "。\n", "）"),
    ("（\n", "\n", "\n）"),
    ("（\r\n", "。\r\n", "\r\n）"),
    ("【（\n", "；\n", "\n）】"),
    ("『（\n", "\n\n", "\n）』"),
    ('"（', "。", '）"'),
]

# Explicit oracle: income max3/min1/null_count1/ratio25%; profit max40/ratio0.
PAIRS = [
    ("收入（元）最大3", True, "收入（元）空值率25％", True),
    ("收入（元）最大3", True, "利润（元）最大40", True),
    ("收入（元）最小1", True, "收入（元）有1个缺失值", True),
    ("收入（元）最大3", True, "利润（元）最大3", False),
    ("利润（元）最大3", False, "收入（元）最大3", True),
    ("收入（元）最大3", True, "收入（元）最小3", False),
    ("收入（元）空值率25％", True, "利润（元）空值率25％", False),
]


def checked(profile, text, expected):
    result = checks.evaluate(profile, text)
    assert result["verification"]["passed"] is expected, result
    assert (result["report"]["status"] == "passed") is expected, result
    assert result["refs"], result
    return result


@pytest.mark.parametrize("opening,separator,closing", LAYOUTS)
@pytest.mark.parametrize("first,first_ok,second,second_ok", PAIRS)
def test_individual_and_joint_statements_keep_bindings(
    profile, opening, separator, closing, first, first_ok, second, second_ok, tmp_path, request
):
    singles = [
        checked(profile, text, valid) for text, valid in [(first, first_ok), (second, second_ok)]
    ]
    grouped = checks.evaluate(profile, opening + first + separator + second + closing + "。")
    (tmp_path / "witness.json").write_text(
        json.dumps({"independent": singles, "grouped": grouped}, ensure_ascii=False, indent=2)
    )
    expected = first_ok and second_ok
    if opening == '"（' and first == "收入（元）最大3" and second == "收入（元）空值率25％":
        # Frozen pre-I09 code rejects this same ASCII closing quote suffix.
        # Keep the positive assertion below. Only the exact known failure or
        # its complete repair receives the strict marker; other regressions fail.
        independent_refs = [r for s in singles for r in s["refs"]]
        known_rejection = (
            grouped["refs"]
            == [
                singles[0]["refs"][0],
                {
                    "token": "25％",
                    "normalized": "25",
                    "supported": False,
                    "statement_span": [8, 11],
                },
            ]
            # IR assertion tracking adds only this exact source location. Keep
            # the full ref comparison and verify the pointed-to original token;
            # unrelated rejection changes must not enter the known-xfail gate.
            and grouped["claims"][1]["statement"][8:11] == "25％"
            and not grouped["verification"]["passed"]
            and grouped["report"]["status"] == "rejected"
        )
        repaired = (
            grouped["refs"] == independent_refs
            and grouped["verification"]["passed"]
            and grouped["report"]["status"] == "passed"
        )
        if known_rejection or repaired:
            request.applymarker(
                pytest.mark.xfail(
                    strict=True,
                    reason="Pre-I09 ASCII quote ratio suffix; positive assertion retained",
                )
            )
    assert grouped["verification"]["passed"] is expected, grouped
    assert (grouped["report"]["status"] == "passed") is expected, grouped

    # Match identities and roles, not merely whether some source contains a number.
    def signatures(refs):
        return [
            (r["token"], r["supported"], r.get("path"), r.get("semantic_binding")) for r in refs
        ]

    assert signatures(grouped["refs"]) == signatures(
        [r for s in singles for r in s["refs"]]
    ), grouped


@pytest.mark.parametrize("value", ["b17", "2026-10-16", "二十五"])
@pytest.mark.parametrize("opening,separator,closing", [LAYOUTS[i] for i in (1, 2, 3, 6)])
def test_grouping_never_hides_unknown_second_value(
    profile, value, opening, separator, closing, tmp_path
):
    text = (
        opening
        + "收入（元）最大3"
        + separator
        + f"收入（元）空值率为（\n\n『{value}』\n）"
        + closing
        + "。"
    )
    result = checks.evaluate(profile, text)
    (tmp_path / "witness.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    assert not result["verification"]["passed"] and result["report"]["status"] == "rejected", result
    assert any(
        r.get("reason") == "unrecognized_numeric_expression" and r["token"] == value
        for r in result["refs"]
    ), result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,after,expected",
    [
        ("sentences", "（播出时长（分钟）有 1 个缺失值。\n播出时长（分钟）空值率约 1.33%）", True),
        (
            "nested-soft",
            "【（\n播出时长（分钟）有 1 个缺失值\n播出时长（分钟）空值率约\n1.33%\n）】",
            True,
        ),
        (
            "mixed-wrong",
            "（播出时长（分钟）有 1 个缺失值。\n播出时长（分钟）空值率约 13.3%）",
            False,
        ),
        (
            "unknown",
            "（播出时长（分钟）有 1 个缺失值。\n播出时长（分钟）空值率约（\nb17\n））",
            False,
        ),
    ],
)
async def test_actual_host_grouped_statements(tmp_path, label, after, expected):
    await original.test_actual_host(
        tmp_path,
        "wrapped-" + label,
        "播出时长（分钟）有 1 个缺失值（空值率约 1.33%）",
        after,
        expected,
    )


@pytest.mark.parametrize(
    "field",
    ["收入（元；税后）", "净收入「甲；乙」", "收入（b17；2026-10-16）", "收益（万元。含税）"],
)
@pytest.mark.parametrize("variant", ["correct", "wrong-ratio", "unknown-subject"])
def test_source_label_punctuation_is_not_a_sentence_boundary(field, variant, tmp_path):
    import pandas as pd
    from mcp_servers.excel_parser.tools import infer_schema
    from packages.common.dataset_store import save_dataframe

    profile = {
        "profile": infer_schema(
            {"dataset_ref": save_dataframe(pd.DataFrame({field: [2.0, 4.0, 6.0, None]}))}
        ).to_dict()
    }
    first = f"{field}最大6"
    subject = "未知" + field if variant == "unknown-subject" else field
    second = f"{subject}空值率{35 if variant == 'wrong-ratio' else 25}％"
    expected = variant == "correct"
    checked(profile, first, True)
    checked(profile, second, expected)
    results = []
    for text in [
        first + "。\n" + second + "。",
        "（" + first + "。\n" + second + "）。",
        "【（\n" + first + "\n" + second + "\n）】。",
    ]:
        result = checks.evaluate(profile, text)
        results.append(result)
    (tmp_path / "witness.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    for result in results:
        assert result["verification"]["passed"] is expected, result
        assert (result["report"]["status"] == "passed") is expected, result
        if expected:
            assert result["refs"] and all(
                r["semantic_binding"]["metric"] == field for r in result["refs"]
            ), result
