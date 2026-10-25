"""第一批深审反例：语义/目标/范围/脱敏/精度，全部使用隔离合成数据。"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pandas as pd
import pytest
from apps.orchestrator.control.claims import build_evidence_summary, extract_claims
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from mcp_servers.dataset_ops.tools import aggregate_preview
from mcp_servers.excel_parser.advisor import diagnose_data_quality, infer_data_roles
from mcp_servers.excel_parser.tools import data_preview, infer_schema, parse_excel
from mcp_servers.report.tools import gen_report_md
from openpyxl import Workbook
from packages.common.dataset_store import load_dataframe, save_dataframe, save_metadata
from packages.common.exact_numbers import decode_exact_number
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_hashing import business_invocation_key
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

from tests.test_agent_tools import _registry

D, F = "d" * 32, "f" * 32


def invocation(name, args, ident="i"):
    return ToolInvocation(
        ident,
        "r",
        None,
        ident,
        name,
        ident,
        "hash",
        args,
        "succeeded",
        "hash",
        None,
        None,
        "now",
        "now",
    )


def evidence(result, args, ident="i"):
    return EvidenceRecord(
        "e-" + ident,
        "r",
        ident,
        None,
        "tool_result",
        {},
        "hash",
        build_evidence_summary(summary="", result=result, artifact_id=None, arguments=args),
        "now",
    )


@pytest.mark.parametrize(
    "result,answer,accepted",
    [
        ({"row_count": 10, "total_value": 900}, "销售额为10元。", False),
        ({"row_count": 10, "total_value": 900}, "销售额为900元。", True),
        ({"total_value": 10}, "销售额为10万元。", False),
        ({"total_value": 100000}, "销售额为10万元。", True),
        ({"total_value": 25}, "比例为25%。", False),
        # A raw sales sum is not a ratio, even when its value equals .25.
        ({"total_value": 0.25}, "比例为25%。", False),
        ({"total_value": 1.234}, "销售额为1.23元。", True),
        (
            {"rows": [{"group": "华东", "value": 100}, {"group": "华西", "value": 900}]},
            "华东销售额为900元；华西销售额为100元。",
            False,
        ),
        (
            {"rows": [{"group": "华东", "value": 100}, {"group": "华西", "value": 900}]},
            "华东销售额为100元；华西销售额为900元。",
            True,
        ),
    ],
)
def test_semantic_final_and_report_share_contract(result, answer, accepted):
    args = {"dataset_ref": D, "value_col": "销售额", "group_col": "地区", "agg": "sum"}
    item = evidence(result, args)
    contract = build_minimal_contract(
        run_id="r",
        user_text="说明销售额",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(D,),
    )
    verdict = verify_completion(
        contract=contract,
        final_text=answer,
        artifacts=[],
        invocations=[invocation("aggregate_preview", args)],
        evidence=[item],
        claims=extract_claims(final_text=answer, goal=contract.goal, evidence=[item]),
    )
    assert verdict.passed is accepted
    artifact = Artifact("a", "c", "m", "table", result, None, "aggregate_preview", args, D, "now")
    if accepted:
        assert (
            validate_report_narratives([artifact], {"title": "报告", "insights": answer})["status"]
            == "passed"
        )
    else:
        with pytest.raises(ValueError):
            validate_report_narratives([artifact], {"title": "报告", "insights": answer})


def test_typed_target_and_metric_survive_multiple_opaque_ids():
    contract = build_minimal_contract(
        run_id="r",
        user_text="analysis_id=" + "a" * 32 + " analysis_id=" + "b" * 32 + " 销售额的总计是多少",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        columns=("销售额", "利润"),
        dataset_refs=(D,),
    )
    assert contract.analysis_requirements.dataset_ref == D
    assert contract.analysis_requirements.total_column == "销售额"
    for ref, metric in [("e" * 32, "销售额"), (D, "利润")]:
        args = {"dataset_ref": ref, "value_col": metric, "agg": "sum"}
        item = evidence({"total_value": 900}, args)
        verdict = verify_completion(
            contract=contract,
            final_text="销售额总计900元",
            artifacts=[],
            invocations=[invocation("aggregate_preview", args)],
            evidence=[item],
            claims=extract_claims(
                final_text="销售额总计900元", goal=contract.goal, evidence=[item]
            ),
        )
        assert not verdict.passed


@pytest.mark.parametrize(
    "tool",
    [
        "trend_analysis",
        "forecast",
        "anomaly_detect",
        "regression",
        "correlation",
        "group_compare",
        "aggregate_preview",
        "dimension_contribution",
        "gen_chart",
    ],
)
@pytest.mark.parametrize("ref,accepted", [(D, False), (F, True)])
def test_every_dataset_consumer_uses_filtered_scope(tool, ref, accepted):
    filters = [{"column": "地区", "op": "==", "value": "华东"}]
    filter_args = {"dataset_ref": D, "filters": filters}
    args = {"dataset_ref": ref, "value_col": "销售额"}
    contract = build_minimal_contract(
        run_id="r",
        user_text="筛选条件为 地区=华东。分析销售额。",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(D,),
    )
    items = [evidence({"dataset_ref": F}, filter_args, "filter"), evidence({"value": 900}, args)]
    verdict = verify_completion(
        contract=contract,
        final_text="分析已完成。",
        artifacts=[],
        invocations=[
            invocation("transform_dataset", filter_args, "filter"),
            invocation(tool, args),
        ],
        evidence=items,
        dataset_bindings=[TaskDatasetBinding("r", F, "derived", D, "filter", "now", "now")],
    )
    assert (
        not any(issue.code == "required_filter_not_satisfied" for issue in verdict.issues)
    ) is accepted


def test_exclude_all_profile_outlets_are_unavailable():
    ref = save_dataframe(pd.DataFrame({"public": [1, 2, 3], "secret": [None, 42, 42]}))
    save_metadata(ref, {"policy": {"columns": {"secret": "exclude"}}})
    profile = infer_schema({"dataset_ref": ref})
    secret = profile.to_dict()["columns"][1]
    assert secret["name"] == "secret" and secret["dtype"] == "float"
    assert all(
        secret[key] is None
        for key in ["null_ratio", "distinct_count", "min", "max", "mean", "std", "median"]
    )
    assert secret["sample_values"] == []
    assert all("secret" not in row for row in data_preview({"dataset_ref": ref})["rows"])
    roles = infer_data_roles(profile)
    assert roles["columns"][1]["profile_evidence"]["distinct_count"] is None
    quality = diagnose_data_quality(profile, roles, duplicate_rows=None)
    assert not any("secret" in issue["columns"] for issue in quality["issues"])
    md = gen_report_md({"title": "画像", "profile": profile.to_dict()})["markdown"]
    assert "| secret | float | — | — | — | — | — |" in md


@pytest.mark.parametrize("extension", ["csv", "xlsx", "xls"])
def test_import_large_text_identifiers_remains_distinct(tmp_path, monkeypatch, extension):
    identifiers = ["18446744073709551616", "18446744073709551617"]
    path = tmp_path / ("ids." + extension)
    if extension == "csv":
        path.write_text(
            "id,amount\n" + "\n".join(f"{value},10" for value in identifiers), encoding="utf-8"
        )
    elif extension == "xls":
        from tests import test_spreadsheet_imports as legacy

        original_label = legacy._biff8_label

        def label(row, col, text):
            return original_label(
                row, col, {"00123": identifiers[0], "00456": identifiers[1]}.get(text, text)
            )

        monkeypatch.setattr(legacy, "_biff8_label", label)
        path.write_bytes(legacy._xls_bytes())
    else:
        book = Workbook()
        sheet = book.active
        sheet.append(["id", "amount"])
        for value in identifiers:
            sheet.append([value, 10])
        book.save(path)
    ref = parse_excel({"file_ref": str(path)})["dataset_ref"]
    assert load_dataframe(ref)["code" if extension == "xls" else "id"].tolist() == identifiers


def test_exact_aggregate_survives_json_and_report():
    ref = save_dataframe(
        pd.DataFrame({"group": ["A", "A"], "amount": pd.Series([2**53, 1], dtype="int64")})
    )
    args = {"dataset_ref": ref, "group_col": "group", "value_col": "amount", "agg": "sum"}
    result = json.loads(json.dumps(aggregate_preview(args)))
    assert decode_exact_number(result["total_value"]) == Decimal("9007199254740993")
    assert result["rows"][0]["value"] == result["total_value"]
    item = evidence(result, args)
    claims = extract_claims(final_text="amount总计9007199254740993", goal="", evidence=[item])
    assert all(ref["supported"] for claim in claims for ref in claim.value_refs)
    claims = extract_claims(final_text="amount总计9007199254740992", goal="", evidence=[item])
    assert not all(ref["supported"] for claim in claims for ref in claim.value_refs)


def test_business_key_is_stable_but_version_arguments_and_run_are_not():
    args = {"dataset_ref": D, "value_col": "销售额"}
    key = business_invocation_key("r", "aggregate_preview", args)
    assert key == business_invocation_key(
        "r", "aggregate_preview", dict(reversed(list(args.items())))
    )
    assert key != business_invocation_key("r", "aggregate_preview", {**args, "dataset_ref": F})
    assert key != business_invocation_key("r", "aggregate_preview", {**args, "value_col": "利润"})
    assert key != business_invocation_key("new-user-rerun", "aggregate_preview", args)


def test_report_cannot_mix_same_project_different_populations():
    args = {"value_col": "销售额", "group_col": "地区", "agg": "sum"}
    artifacts = [
        Artifact(
            ref,
            "c",
            "m",
            "table",
            {"total_value": 900},
            None,
            "aggregate_preview",
            args,
            ref,
            "now",
        )
        for ref in (D, F)
    ]
    with pytest.raises(ValueError, match="不同数据集版本"):
        validate_report_narratives(artifacts, {"title": "报告", "insights": "销售额900元"})


def test_csv_decimal_and_scientific_forms_never_hide_large_integer_loss(tmp_path):
    values = ["18446744073709551616.0", "18446744073709551617.0"]
    path = tmp_path / "decimal-identifiers.csv"
    path.write_text("id\n" + "\n".join(values), encoding="utf-8")
    ref = parse_excel({"file_ref": str(path)})["dataset_ref"]
    assert load_dataframe(ref)["id"].tolist() == values


@pytest.mark.asyncio
@pytest.mark.parametrize("sign", ["%", "％", " ％"])
async def test_actual_profile_ratios_and_equal_raw_values_keep_typed_units(tmp_path, sign):
    ref = save_dataframe(
        pd.DataFrame(
            {
                "金额（元）": [0.25, 0.25, 0.25, None],
                "时长（分钟）": [0.25, 0.25, 0.25, None],
                "全空指标": pd.Series([None] * 4, dtype="float64"),
            }
        )
    )
    args = {"dataset_ref": ref}
    registry = _registry()
    try:
        result = registry.execute("get_data_profile", json.dumps(args))
    finally:
        await registry.aclose()
    columns = {column["name"]: column for column in result["profile"]["columns"]}
    assert columns["金额（元）"]["max"] == columns["金额（元）"]["null_ratio"] == 0.25
    assert columns["时长（分钟）"]["median"] == 0.25
    assert columns["金额（元）"]["null_count"] == columns["全空指标"]["null_ratio"] == 1
    item = replace(evidence(result, args), source={"tool": "get_data_profile"})
    artifact = Artifact(
        "profile", "c", "m", "profile", result, None, "get_data_profile", args, ref, "now"
    )
    contract = build_minimal_contract(
        run_id="r",
        user_text="复核画像单位",
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(ref,),
    )
    cases = [
        ("ratio_25", f"金额（元）空值率25{sign}。", True, "ratio"),
        ("ratio_100", f"全空指标空值率100{sign}。", True, "ratio"),
        ("raw_money", "金额（元）最大0.25。", True, "raw"),
        ("raw_duration", "时长（分钟）中位数0.25。", True, "raw"),
        ("raw_count", "金额（元）空值数1。", True, "count"),
        ("wrong_money_percent", f"金额（元）最大25{sign}。", False, None),
        ("wrong_duration_percent", f"时长（分钟）中位数25{sign}。", False, None),
        ("wrong_count_percent", f"金额（元）空值数100{sign}。", False, None),
    ]
    witness = {"tool": "get_data_profile", "args": args, "result": result, "cases": []}
    for label, text, accepted, unit in cases:
        claims = extract_claims(final_text=text, goal=contract.goal, evidence=[item])
        verdict = verify_completion(
            contract=contract,
            final_text=text,
            artifacts=[],
            invocations=[invocation("get_data_profile", args)],
            evidence=[item],
            claims=claims,
        )
        refs = [r for c in claims for r in c.value_refs]
        try:
            report = validate_report_narratives([artifact], {"title": "画像", "insights": text})
        except ValueError as error:
            report = {"status": "rejected", "error": str(error)}
        witness["cases"].append(
            {
                "label": label,
                "text": text,
                "expected": accepted,
                "verdict": verdict.verdict,
                "refs": refs,
                "report": report,
            }
        )
        (tmp_path / "typed-ratio-witness.json").write_text(
            json.dumps(witness, ensure_ascii=False, indent=2)
        )
        assert refs and verdict.passed is accepted, witness["cases"][-1]
        assert (report["status"] == "passed") is accepted, witness["cases"][-1]
        if accepted:
            assert all(r["semantic_binding"]["unit"] == unit for r in refs)
        if label == "ratio_25":
            assert refs[0]["path"].endswith(".null_ratio")
            assert refs[0]["display_factor"] == "0.01"
            assert refs[0]["semantic_binding"]["analysis"]["tool"] == "get_data_profile"
