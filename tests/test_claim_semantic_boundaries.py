"""统计角色与数字维度的最终回答/报告共用成对回归。"""

from __future__ import annotations

from typing import Any

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_claims
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.stats.tools import anomaly_detect, correlation, trend_analysis
from packages.common.dataset_store import save_dataframe
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord


def check_both(
    data: dict[str, Any],
    args: dict[str, Any],
    text: str,
    accepted: bool,
    kind: str = "stats",
    tool: str = "trend_analysis",
) -> list[Any]:
    evidence = EvidenceRecord(
        "e",
        "r",
        "i",
        "a",
        "tool_result",
        {"tool": tool},
        "hash",
        build_evidence_summary(summary="", result=data, artifact_id="a", arguments=args),
        "now",
    )
    claims = extract_claims(final_text=text, goal="说明计算结果", evidence=[evidence])
    assert claims, "A positive must contain verified measurements, not only ignored labels"
    contract = build_minimal_contract(
        run_id="r",
        user_text="说明计算结果",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(args["dataset_ref"],),
    )
    verification = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[evidence],
        claims=claims,
    )
    assert verification.passed is accepted, [c.value_refs for c in claims]
    payload = {"kind": "trend", "result": data} if kind == "stats" else data
    artifact = Artifact("a", "c", "m", kind, payload, None, tool, args, args["dataset_ref"], "now")
    if accepted:
        assert (
            validate_report_narratives([artifact], {"title": "结果", "insights": text})["status"]
            == "passed"
        )
    else:
        with pytest.raises(ValueError):
            validate_report_narratives([artifact], {"title": "结果", "insights": text})
    return [ref for claim in claims for ref in claim.value_refs if "token" in ref]


@pytest.mark.parametrize(
    "label,value,accepted,role",
    [
        ("斜率", 10, True, "slope"),
        ("斜率", 12, False, None),
        ("斜率", 3, False, None),
        ("斜率", 11, False, None),
        ("样本量", 12, True, "sample_size"),
        ("样本量", 10, False, None),
        ("移动平均窗口", 3, True, "ma_window"),
        ("移动平均窗口", 12, False, None),
    ],
)
def test_real_trend_statistic_roles(
    label: str, value: int, accepted: bool, role: str | None
) -> None:
    ref = save_dataframe(
        pd.DataFrame(
            {
                "日期": pd.date_range("2025-01-01", periods=12),
                "销售额": [10 * (i + 1) for i in range(12)],
            }
        )
    )
    args = {"dataset_ref": ref, "time_col": "日期", "value_col": "销售额", "method": "ma"}
    data = trend_analysis(args)
    refs = check_both(data, args, f"销售额{label}为{value}。", accepted)
    if accepted:
        assert refs[0]["semantic_binding"]["statistic"] == role


@pytest.mark.parametrize(
    "field,label,tool",
    [
        ("seasonality_strength", "季节强度", "trend_analysis"),
        ("corr", "相关系数", "correlation"),
        ("p_value", "p值", "correlation"),
        ("adjusted_p_value", "校正后p值", "correlation"),
        ("r_squared", "决定系数", "regression"),
        ("coef", "回归系数", "regression"),
        ("statistic", "检验统计量", "group_compare"),
        ("score", "异常得分", "anomaly_detect"),
        ("mean", "均值", "group_compare"),
        ("std", "标准差", "group_compare"),
    ],
)
@pytest.mark.parametrize("replacement", [False, True])
def test_related_statistic_roles_do_not_borrow_parameters(
    field: str, label: str, tool: str, replacement: bool
) -> None:
    data = {field: 0.25, "n": 12, "ma_window": 3}
    args = {"dataset_ref": "d" * 32, "value_col": "销售额"}
    check_both(
        data, args, f"销售额{label}为{12 if replacement else 0.25}。", not replacement, tool=tool
    )


@pytest.mark.parametrize("wrong_label", ["斜率", "相关系数", "回归系数", "检验统计量"])
def test_equal_value_does_not_merge_statistic_roles(wrong_label: str) -> None:
    args = {"dataset_ref": "d" * 32, "value_col": "销售额"}
    check_both({"n": 10, "ma_window": 10}, args, f"销售额{wrong_label}为10。", False)


@pytest.mark.parametrize("tool,label", [("correlation", "相关系数"), ("anomaly_detect", "异常数")])
@pytest.mark.parametrize("replacement", [False, True])
def test_real_adjacent_tools_keep_roles(tool: str, label: str, replacement: bool) -> None:
    ref = save_dataframe(
        pd.DataFrame({"销售额": [1, 2, 3, 4, 5, 6, 7, 100], "利润": [2, 3, 7, 8, 13, 15, 17, 200]})
    )
    args: dict[str, Any] = {"dataset_ref": ref}
    if tool == "correlation":
        args["columns"] = ["销售额", "利润"]
        data = correlation(args)
        value = data["n_obs"] if replacement else data["top_pairs"][0]["corr"]
    else:
        args["value_col"] = "销售额"
        data = anomaly_detect(args)
        value = data["n_total"] if replacement else data["n_anomalies"]
    check_both(data, args, f"销售额{label}为{value}。", not replacement, tool=tool)


@pytest.mark.parametrize(
    "groups,dimension",
    [
        (["2024", "2025"], "年份"),
        ([2024, 2025], "年份"),
        (["1", "2"], "地区编号"),
        ([1, 2], "地区编号"),
        (["01", "02"], "地区编号"),
        (["R1", "R2"], "地区编号"),
        ([100, 900], "地区编号"),
    ],
)
@pytest.mark.parametrize("form", ["prose", "table", "bar"])
@pytest.mark.parametrize("swapped", [False, True])
def test_numeric_dimensions_are_labels_not_measurements(
    groups: list[Any], dimension: str, form: str, swapped: bool
) -> None:
    args: dict[str, Any] = {
        "dataset_ref": "d" * 32,
        "value_col": "销售额",
        "group_col": dimension,
        "agg": "sum",
    }
    data = {
        "rows": [
            {"group": group, "value": value}
            for group, value in zip(groups, [100, 900], strict=True)
        ]
    }
    values = [900, 100] if swapped else [100, 900]
    if form == "table":
        text = (
            f"| {dimension} | 销售额 |\n|---|---|\n"
            f"| {groups[0]} | {values[0]} |\n| {groups[1]} | {values[1]} |"
        )
    else:
        text = (
            "；".join(
                f"{dimension}{group}的销售额为{value}元"
                for group, value in zip(groups, values, strict=True)
            )
            + "。"
        )
    kind, tool = "table", "aggregate_preview"
    if form == "bar":
        kind, tool = "chart", "gen_chart"
        args = {"dataset_ref": "d" * 32, "encoding": {"x": dimension, "y": "销售额", "agg": "sum"}}
        data = {
            "option": {
                "xAxis": {"type": "category", "data": groups},
                "series": [{"type": "bar", "name": "销售额", "data": [100, 900]}],
            }
        }
    refs = check_both(data, args, text, not swapped, kind, tool)
    assert [ref["token"] for ref in refs] == list(map(str, values))
    if not swapped:
        assert [ref["semantic_binding"]["group_key"] for ref in refs] == list(map(str, groups))


@pytest.mark.parametrize(
    "text,accepted",
    [
        ("2024年销售额100元；2025年销售额900元。", True),
        ("100元对应2024年销售额；900元对应2025年销售额。", True),
        ("2024年销售额900元；2025年销售额100元。", False),
        ("2024年销售额2024元。", False),
        ("2026年销售额100元。", False),
        ("销售额2024元。", False),
    ],
)
def test_year_labels_are_not_blanket_number_exemptions(text: str, accepted: bool) -> None:
    args = {"dataset_ref": "d" * 32, "group_col": "年份", "value_col": "销售额", "agg": "sum"}
    check_both(
        {"rows": [{"group": "2024", "value": 100}, {"group": "2025", "value": 900}]},
        args,
        text,
        accepted,
        "table",
        "aggregate_preview",
    )


def test_numeric_group_value_cannot_be_borrowed_as_measurement() -> None:
    args = {"dataset_ref": "d" * 32, "group_col": "地区编号", "value_col": "销售额", "agg": "sum"}
    check_both(
        {"rows": [{"group": 100, "value": 900}]},
        args,
        "地区编号100的销售额为100元。",
        False,
        "table",
        "aggregate_preview",
    )


def test_same_named_diagnostic_statistic_needs_unique_scope() -> None:
    args = {"dataset_ref": "d" * 32, "value_col": "销售额"}
    check_both(
        {"normality": {"statistic": 3}, "heteroskedasticity": {"statistic": 3}},
        args,
        "销售额检验统计量为3。",
        False,
        tool="regression",
    )


@pytest.mark.parametrize("data", [{"corr": 0.25}, {"matrix": [[1, 0.25], [0.25, 1]]}])
def test_statistics_without_value_col_cannot_be_business_amounts(data: dict[str, Any]) -> None:
    args = {"dataset_ref": "d" * 32, "columns": ["销售额", "利润"]}
    check_both(data, args, "销售额为0.25元。", False, tool="correlation")
