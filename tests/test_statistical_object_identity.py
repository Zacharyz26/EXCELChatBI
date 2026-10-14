"""七类真实统计工具：先确定分析对象，再匹配数值；两个出口共同验证。"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_claims
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.stats import tools
from packages.common.dataset_store import save_dataframe
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

Source = tuple[str, dict[str, Any], dict[str, Any]]


def check(sources: list[Source], text: str, accepted: bool) -> list[dict[str, Any]]:
    evidence = []
    artifacts = []
    for index, (tool, args, data) in enumerate(sources):
        ident = str(index)
        evidence.append(
            EvidenceRecord(
                ident,
                "r",
                "i" + ident,
                ident,
                "tool_result",
                {"tool": tool},
                "hash",
                build_evidence_summary(summary="", result=data, artifact_id=ident, arguments=args),
                "now",
            )
        )
        artifacts.append(
            Artifact(
                ident,
                "c",
                "m",
                "stats",
                {"kind": tool, "result": data},
                None,
                tool,
                args,
                args["dataset_ref"],
                "now",
            )
        )
    claims = extract_claims(final_text=text, goal="说明计算结果", evidence=evidence)
    refs = [ref for claim in claims for ref in claim.value_refs if "token" in ref]
    assert refs, text
    contract = build_minimal_contract(
        run_id="r",
        user_text="说明计算结果",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=tuple({s[1]["dataset_ref"] for s in sources}),
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=evidence,
        claims=claims,
    )
    assert verdict.passed is accepted, (text, refs)
    if accepted:
        report = validate_report_narratives(artifacts, {"title": "结果", "insights": text})
        assert report["status"] == "passed"
        report_bindings = [
            ref["semantic_binding"] for claim in report["claims"] for ref in claim["value_refs"]
        ]
        assert report_bindings == [ref["semantic_binding"] for ref in refs]
    else:
        with pytest.raises(ValueError):
            validate_report_narratives(artifacts, {"title": "结果", "insights": text})
    return refs


@pytest.fixture(scope="module")
def real_sources() -> dict[str, Source]:
    frame = pd.DataFrame(
        {
            "日期": pd.date_range("2025-01-01", periods=36),
            "销售额": [10 * (i + 1) + [0, 30, 150][i % 3] for i in range(36)],
            "利润": [i * 1.7 + (i % 4) ** 2 if i < 28 else None for i in range(36)],
            "成本": [(i * 7) % 19 + (i % 5) * 0.3 for i in range(36)],
            "地区": ["East", "West", "North"] * 12,
            "异常指标": [i % 7 + 10 for i in range(34)] + [500, 900],
        }
    )
    ref = save_dataframe(frame)
    configs = {
        "trend_analysis": {
            "time_col": "日期",
            "value_col": "销售额",
            "method": "ma",
            "ma_window": 3,
        },
        "correlation": {"columns": ["销售额", "利润", "成本"], "method": "pearson"},
        "regression": {"target": "销售额", "features": ["利润", "成本"], "kind": "ols"},
        "forecast": {"time_col": "日期", "value_col": "销售额", "method": "drift", "horizon": 3},
        "anomaly_detect": {"value_col": "异常指标", "method": "iqr"},
        "dimension_contribution": {"value_col": "销售额", "dimension_col": "地区", "method": "sum"},
        "group_compare": {"value_col": "销售额", "group_col": "地区"},
    }
    sources = {}
    for name, config in configs.items():
        args = {"dataset_ref": ref, **config}
        sources[name] = (name, args, getattr(tools, name)(args))
    return sources


@pytest.mark.parametrize("role", ["corr", "p_value", "adjusted_p_value"])
@pytest.mark.parametrize("variant", ["correct", "reversed", "sibling", "unknown", "ambiguous"])
def test_correlation_pair_identity(
    real_sources: dict[str, Source], role: str, variant: str
) -> None:
    source = real_sources["correlation"]
    pairs = {frozenset((p["a"], p["b"])): p for p in source[2]["top_pairs"]}
    pair = pairs[frozenset(("销售额", "利润"))]
    value = (pairs[frozenset(("销售额", "成本"))] if variant == "sibling" else pair)[role]
    subject = {"reversed": "利润与销售额", "unknown": "客流量与广告费", "ambiguous": "销售额"}.get(
        variant, "销售额与利润"
    )
    check([source], f"{subject}的{role}为{value}。", variant in {"correct", "reversed"})


@pytest.mark.parametrize("role", ["p_value", "statistic"])
@pytest.mark.parametrize("subject", ["residual_normality", "heteroskedasticity"])
@pytest.mark.parametrize("variant", ["correct", "sibling", "unknown", "ambiguous"])
def test_diagnostic_identity(
    real_sources: dict[str, Source], role: str, subject: str, variant: str
) -> None:
    source = real_sources["regression"]
    other = "heteroskedasticity" if subject == "residual_normality" else "residual_normality"
    value = source[2]["diagnostics"][other if variant == "sibling" else subject][role]
    label = {"residual_normality": "残差正态性", "heteroskedasticity": "异方差检验"}[subject]
    label = {"unknown": "未知检验", "ambiguous": "回归分析"}.get(variant, label)
    check([source], f"{label}的{role}为{value}。", variant == "correct")


@pytest.mark.parametrize("role", ["coef", "std_err", "p_value", "adjusted_p_value"])
@pytest.mark.parametrize("variant", ["correct", "sibling", "unknown", "ambiguous"])
def test_coefficient_identity(real_sources: dict[str, Source], role: str, variant: str) -> None:
    source = real_sources["regression"]
    coefficients = {c["name"]: c for c in source[2]["coefficients"]}
    value = coefficients["成本" if variant == "sibling" else "利润"][role]
    subject = {"unknown": "客流量", "ambiguous": "回归分析"}.get(variant, "利润")
    check([source], f"{subject}的{role}为{value}。", variant == "correct")


@pytest.mark.parametrize(
    "tool,label",
    [
        ("trend_analysis", "趋势分析"),
        ("correlation", "相关性分析"),
        ("regression", "回归分析"),
        ("forecast", "预测分析"),
        ("anomaly_detect", "异常检测"),
        ("dimension_contribution", "维度贡献"),
        ("group_compare", "组间比较"),
    ],
)
@pytest.mark.parametrize(
    "variant", ["correct", "other_tool", "sole_wrong_source", "ambiguous", "currency"]
)
def test_sample_size_owns_analysis(
    real_sources: dict[str, Source], tool: str, label: str, variant: str
) -> None:
    source = real_sources[tool]
    n = source[2]["statistical_evidence"]["sample"]["valid_rows"]
    sources = list(real_sources.values())
    if variant == "sole_wrong_source":
        sources = [real_sources["correlation" if tool != "correlation" else "trend_analysis"]]
        n = sources[0][2]["statistical_evidence"]["sample"]["valid_rows"]
    elif variant == "other_tool":
        n = 28 if n != 28 else 36
    elif variant == "ambiguous":
        label = ""
    check(
        sources,
        f"{label}的样本量为{n}{'元' if variant == 'currency' else ''}。",
        variant == "correct",
    )


@pytest.mark.parametrize("variant", ["correct", "sibling", "reversed", "unknown", "ambiguous"])
@pytest.mark.parametrize("role", ["p_value", "adjusted_p_value", "mean_difference", "statistic"])
def test_group_pair_direction(real_sources: dict[str, Source], role: str, variant: str) -> None:
    source = real_sources["group_compare"]
    pairs = source[2]["pairwise"]
    first, other = pairs[:2]
    assert first[role] != other[role], "交换值负例必须在真实工具输出中具有不同值"
    members = [first["left"], first["right"]]
    value = (other if variant == "sibling" else first)[role]
    if variant == "reversed":
        members.reverse()
    if variant == "unknown":
        members = ["Absent", "Missing"]
    subject = "与".join(members) if variant != "ambiguous" else ""
    check(
        [source],
        f"{subject}销售额的{role}为{value}。",
        variant == "correct" or (variant == "reversed" and role in {"p_value", "adjusted_p_value"}),
    )


@pytest.mark.parametrize(
    "tool,role",
    [("dimension_contribution", "share"), ("group_compare", "mean"), ("group_compare", "ci95_low")],
)
@pytest.mark.parametrize("variant", ["correct", "sibling", "unknown"])
def test_single_group_identity(
    real_sources: dict[str, Source], tool: str, role: str, variant: str
) -> None:
    source = real_sources[tool]
    rows = source[2]["groups"]
    key = "dimension" if tool == "dimension_contribution" else "group"
    value = rows[1 if variant == "sibling" else 0][role]
    group = "Absent" if variant == "unknown" else rows[0][key]
    check([source], f"地区{group}的销售额{role}为{value}。", variant == "correct")


@pytest.mark.parametrize("variant", ["correct", "sibling", "unknown", "ambiguous"])
def test_anomaly_observation_identity(real_sources: dict[str, Source], variant: str) -> None:
    source = real_sources["anomaly_detect"]
    a, b = source[2]["anomalies"][:2]
    index = 99 if variant == "unknown" else a["index"]
    label = "" if variant == "ambiguous" else f"索引{index}的"
    value = (b if variant == "sibling" else a)["score"]
    check([source], f"{label}异常指标的异常得分为{value}。", variant == "correct")


@pytest.mark.parametrize("variant", ["correct", "sibling", "unknown", "ambiguous"])
def test_forecast_model_identity(real_sources: dict[str, Source], variant: str) -> None:
    source = real_sources["forecast"]
    data = source[2]
    value = (
        data["baseline"]["metrics"]["mae"]
        if variant == "sibling"
        else data["validation_metrics"]["mae"]
    )
    model = {"unknown": "seasonal_naive", "ambiguous": ""}.get(variant, "drift")
    check([source], f"{model}销售额的MAE为{value}。", variant == "correct")


@pytest.mark.parametrize("variant", ["correct", "sibling", "unknown", "ambiguous"])
def test_prediction_time_identity(real_sources: dict[str, Source], variant: str) -> None:
    source = real_sources["forecast"]
    a, b = source[2]["predictions"][:2]
    time = {"unknown": "2099-12-31", "ambiguous": ""}.get(variant, a["time"])
    value = (b if variant == "sibling" else a)["point"]
    check([source], f"时间{time}销售额的预测值为{value}。", variant == "correct")


@pytest.mark.parametrize("variant", ["correct", "other_tool", "other_role"])
def test_parameter_owns_tool(real_sources: dict[str, Source], variant: str) -> None:
    text = {
        "correct": "趋势分析销售额的移动平均窗口为3。",
        "other_tool": "相关性分析销售额的移动平均窗口为3。",
        "other_role": "趋势分析销售额的斜率为3。",
    }[variant]
    check(list(real_sources.values()), text, variant == "correct")


@pytest.mark.parametrize("variant", ["correct", "ambiguous", "wrong_target", "sibling_value"])
def test_regression_target_identity(real_sources: dict[str, Source], variant: str) -> None:
    source = real_sources["regression"]
    args = {**source[1], "target": "成本", "features": ["利润", "销售额"]}
    other: Source = ("regression", args, tools.regression(args))

    def coeff(s: Source) -> float:
        return float(next(row for row in s[2]["coefficients"] if row["name"] == "利润")["coef"])

    label = {"ambiguous": "", "wrong_target": "客流量模型"}.get(variant, "销售额模型")
    value = coeff(other if variant == "sibling_value" else source)
    check([source, other], f"{label}利润的回归系数为{value}。", variant == "correct")


@pytest.mark.parametrize("variant", ["correct", "ambiguous", "wrong_version", "wrong_method"])
def test_version_method_and_units(real_sources: dict[str, Source], variant: str) -> None:
    source = deepcopy(real_sources["correlation"])
    source[1]["data_version_hash"] = "a" * 64
    other = deepcopy(source)
    other[1]["data_version_hash"] = "b" * 64
    pair = next(p for p in source[2]["top_pairs"] if set((p["a"], p["b"])) == {"销售额", "利润"})
    version = (
        ""
        if variant == "ambiguous"
        else "版本=" + ("c" if variant == "wrong_version" else "a") * 64
    )
    method = "spearman" if variant == "wrong_method" else "pearson"
    check(
        [source, other],
        f"{version} {method} 销售额与利润的相关系数为{pair['corr']}。",
        variant == "correct",
    )


def test_equal_values_do_not_resolve_unknown_diagnostics(real_sources: dict[str, Source]) -> None:
    source = deepcopy(real_sources["regression"])
    for name in ["residual_normality", "heteroskedasticity"]:
        source[2]["diagnostics"][name]["p_value"] = 0.25
    check([source], "回归分析的p值为0.25。", False)
    check([source], "异方差检验的p值为0.25。", True)


def test_unknown_nested_results_are_not_business_amounts(real_sources: dict[str, Source]) -> None:
    source = deepcopy(real_sources["trend_analysis"])
    source[2]["unregistered_component"] = {"value": 12345}
    check([source], "销售额为12345元。", False)


@pytest.mark.parametrize(
    "subject,accepted", [("销售额", True), ("异常指标", True), ("客流量", False), ("", False)]
)
def test_count_keeps_business_metric(
    real_sources: dict[str, Source], subject: str, accepted: bool
) -> None:
    source = real_sources["trend_analysis"]
    args = {**source[1], "value_col": "异常指标"}
    other: Source = ("trend_analysis", args, tools.trend_analysis(args))
    check([source, other], f"趋势分析{subject}的样本量为36。", accepted)


def test_conflicting_values_for_one_identity_reject(real_sources: dict[str, Source]) -> None:
    source = real_sources["trend_analysis"]
    other = deepcopy(source)
    other[2]["n"] = 99
    other[2]["statistical_evidence"]["sample"]["valid_rows"] = 99
    check([source, other], "趋势分析的样本量为36。", False)


def test_multiple_parameterizations_are_ambiguous_before_values(
    real_sources: dict[str, Source],
) -> None:
    source = real_sources["trend_analysis"]
    args = {**source[1], "ma_window": 5}
    other: Source = ("trend_analysis", args, tools.trend_analysis(args))
    check([source, other], "趋势分析销售额的移动平均窗口为3。", False)


@pytest.mark.parametrize(
    "component,role,label",
    [
        ("split", "training_observations", "训练验证划分"),
        ("prediction_interval", "radius", "预测区间"),
    ],
)
@pytest.mark.parametrize("wrong_component", [False, True])
def test_named_subresult_identity(
    real_sources: dict[str, Source], component: str, role: str, label: str, wrong_component: bool
) -> None:
    source = real_sources["forecast"]
    value = source[2][component][role]
    if wrong_component:
        label = "时间轴"
    check([source], f"预测分析销售额的{label}统计量={role}为{value}。", not wrong_component)
