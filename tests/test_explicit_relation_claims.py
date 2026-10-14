"""真实两列单候选证据：显式成员不可退化为单成员省略。"""

from __future__ import annotations

import pytest
from apps.orchestrator.control.statistic_roles import mentioned_roles
from mcp_servers.stats import tools

from tests.test_statistical_object_identity import Source, check
from tests.test_statistical_object_identity import real_sources as make_real_sources


@pytest.fixture(scope="module")
def real_sources() -> dict[str, Source]:
    return make_real_sources.__wrapped__()


@pytest.fixture(scope="module")
def two_columns(real_sources: dict[str, Source]) -> Source:
    source = real_sources["correlation"]
    args = {**source[1], "columns": ["销售额", "利润"]}
    return "correlation", args, tools.correlation(args)


@pytest.mark.parametrize("role", ["corr", "p_value"])
@pytest.mark.parametrize(
    "variant",
    [
        "correct",
        "reversed",
        "unknown_first",
        "unknown_second",
        "duplicate",
        "self",
        "empty",
        "third",
    ],
)
@pytest.mark.parametrize("form", ["table", "与", "、", "/", " and ", " & "])
def test_complete_pair_members(two_columns: Source, role: str, variant: str, form: str) -> None:
    pair = two_columns[2]["top_pairs"][0]
    value = pair[role]
    members = ["销售额", "利润"]
    if variant == "reversed":
        members.reverse()
    elif variant == "unknown_first":
        members[0] = "广告费"
    elif variant == "unknown_second":
        members[1] = "广告费"
    elif variant in {"duplicate", "self"}:
        members[1] = "销售额"
        if variant == "self":
            value = 1.0
    elif variant == "empty":
        members[1] = ""
    elif variant == "third":
        members.append("广告费")
    expected = variant in {"correct", "reversed"} or (variant == "self" and role == "corr")
    if form == "table":
        headers = ["变量一", "变量二", "变量三"][: len(members)] + [role]
        text = "|" + "|".join(headers) + "|\n|" + "|".join(["---"] * len(headers)) + "|\n|"
        text += "|".join([*members, str(value)]) + "|"
    else:
        text = f"{form.join(members)}的{role}为{value}。"
    check([two_columns], text, expected)


@pytest.mark.parametrize(
    "subjects,expected",
    [
        ("销售额", True),
        ("利润", True),
        ("广告费", False),
        ("销售额：广告费", False),
        ("销售额~~广告费", False),
        ("销售额和利润和广告费", False),
        ("销售额广告费", False),
        ("销售额→利润", False),
    ],
)
def test_omission_has_selected_two_column_basis(
    two_columns: Source, subjects: str, expected: bool
) -> None:
    check(
        [two_columns], f"{subjects}的相关系数为{two_columns[2]['top_pairs'][0]['corr']}。", expected
    )


@pytest.mark.parametrize("variant", ["correct", "unknown", "reverse", "duplicate", "malformed"])
def test_multiple_measurements_keep_table_cells(two_columns: Source, variant: str) -> None:
    pair = two_columns[2]["top_pairs"][0]
    left, right = {
        "unknown": ("销售额", "广告费"),
        "reverse": ("利润", "销售额"),
        "duplicate": ("销售额", "销售额"),
    }.get(variant, ("销售额", "利润"))
    text = (
        "| 变量一 | 变量二 | corr | p_value |\n|---|---|---|---|\n"
        f"| **{left}** | `{right}` | {pair['corr']} | {pair['p_value']} |"
    )
    if variant == "malformed":
        text = text.replace("| `利润`", "| `利润` | 广告费")
    check([two_columns], text, variant in {"correct", "reverse"})


@pytest.mark.parametrize("alias", ["correlation", "CORRELATION", "Correlation", "相关性分析"])
@pytest.mark.parametrize("multi_source", [False, True])
def test_correlation_tool_alias_is_not_corr(
    real_sources: dict[str, Source], alias: str, multi_source: bool
) -> None:
    source = real_sources["correlation"]
    sources = list(real_sources.values()) if multi_source else [source]
    check(sources, f"{alias}的样本量为{source[2]['n_obs']}。", True)
    assert mentioned_roles(f"{alias}的样本量为24。", {"sample_size", "corr"}) == {"sample_size"}


@pytest.mark.parametrize(
    "subject", ["CORR", "correlation_extra", "mycorrelation", "趋势分析", "unknown_tool"]
)
def test_alias_normalization_does_not_bypass_source_or_role(
    two_columns: Source, subject: str
) -> None:
    check([two_columns], f"{subject}的样本量为{two_columns[2]['n_obs']}。", False)


@pytest.mark.parametrize("alias", ["corr", "CORR", "相关系数"])
def test_explicit_corr_role_still_checks_value(two_columns: Source, alias: str) -> None:
    value = two_columns[2]["top_pairs"][0]["corr"]
    check([two_columns], f"CORRELATION销售额与利润的{alias}为{value}。", True)
    check([two_columns], f"CORRELATION销售额与利润的{alias}为{two_columns[2]['n_obs']}。", False)


@pytest.mark.parametrize(
    "variant",
    [
        "correct",
        "reverse_arrow",
        "wrong_direction",
        "unknown",
        "unordered",
        "table",
        "table_wrong_roles",
        "table_unknown",
    ],
)
def test_regression_relation_keeps_direction(real_sources: dict[str, Source], variant: str) -> None:
    source = real_sources["regression"]
    value = next(c["coef"] for c in source[2]["coefficients"] if c["name"] == "利润")
    subject = {
        "correct": "利润→销售额",
        "reverse_arrow": "销售额←利润",
        "wrong_direction": "销售额→利润",
        "unknown": "利润→广告费",
        "unordered": "利润与销售额",
    }.get(variant)
    if subject:
        text = f"{subject}的回归系数为{value}。"
    else:
        target, feature = {
            "table_wrong_roles": ("利润", "销售额"),
            "table_unknown": ("广告费", "利润"),
        }.get(variant, ("销售额", "利润"))
        text = f"| 目标 | 特征 | 回归系数 |\n|---|---|---|\n| {target} | {feature} | {value} |"
    check([source], text, variant in {"correct", "reverse_arrow", "table"})


@pytest.mark.parametrize("reverse", [False, True])
def test_signed_group_table_keeps_order(real_sources: dict[str, Source], reverse: bool) -> None:
    source = real_sources["group_compare"]
    pair = source[2]["pairwise"][0]
    left, right = (pair["right"], pair["left"]) if reverse else (pair["left"], pair["right"])
    text = (
        "| 组一 | 组二 | 销售额均值差 |\n|---|---|---|\n"
        f"| {left} | {right} | {pair['mean_difference']} |"
    )
    check([source], text, not reverse)


@pytest.mark.parametrize("header", ["变量1", "变量一", "Column 1", "1. 变量"])
@pytest.mark.parametrize("unknown", [False, True])
def test_numeric_headers_are_structure_not_measurements(
    two_columns: Source, header: str, unknown: bool
) -> None:
    right = "广告费" if unknown else "利润"
    value = two_columns[2]["top_pairs"][0]["corr"]
    text = f"| {header} | 变量2 | corr |\n|---|---|---|\n| 销售额 | {right} | {value} |"
    refs = check([two_columns], text, not unknown)
    assert [ref["token"] for ref in refs] == [str(value)]


@pytest.mark.parametrize(
    "tool",
    [
        "trend_analysis",
        "correlation",
        "regression",
        "anomaly_detect",
        "forecast",
        "group_compare",
        "dimension_contribution",
    ],
)
@pytest.mark.parametrize("uppercase", [False, True])
def test_exact_tool_identifier_uses_shared_case_rules(
    real_sources: dict[str, Source], tool: str, uppercase: bool
) -> None:
    source = real_sources[tool]
    label = tool.upper() if uppercase else tool
    n = source[2]["statistical_evidence"]["sample"]["valid_rows"]
    check(list(real_sources.values()), f"{label}的样本量为{n}。", True)
