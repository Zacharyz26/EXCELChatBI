"""MANUAL-02: exact named facts, bounded scope, and explicit duplicate bases."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pandas as pd
import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from mcp_servers.excel_parser.advisor import diagnose_data_quality, infer_data_roles
from mcp_servers.excel_parser.tools import infer_schema
from packages.common.dataset_store import duplicate_row_count, save_dataframe
from packages.governance.data_boundary import ColumnRule, EffectivePolicy
from packages.governance.redaction import apply_policy
from packages.session.models import Artifact

from tests.test_profile_subject_boundaries import check, record

ROOT = Path(__file__).resolve().parents[1]
CAPTURE = json.loads((ROOT / "tests/fixtures/manual02_profile.json").read_text())
FIELD = "播出时长（分钟）"
EXPLICIT_ANSWER = (
    CAPTURE["answer"]
    .replace(
        "记录ID 有 1 条重复（75 行中 distinct 为 74）",
        "记录ID重复多余记录数为1，记录ID distinct为74",
    )
    .replace("存在 1 条完全重复的行记录", "整行重复多余记录数为1")
)


def tool_profile(frame):
    ref = save_dataframe(frame)
    profile = infer_schema({"dataset_ref": ref})
    roles = infer_data_roles(profile)
    return {
        "profile": profile.to_dict(),
        "roles": roles,
        "quality": diagnose_data_quality(profile, roles, duplicate_rows=duplicate_row_count(ref)),
    }


def evaluate(result, text, expected):
    outcome = check(text, [record(result)])
    assert outcome["refs"]
    assert outcome["verification"]["passed"] is expected, outcome
    artifact = Artifact(
        "profile",
        "c",
        "m",
        "profile",
        result,
        None,
        "get_data_profile",
        {"dataset_ref": "d" * 32},
        "d" * 32,
        "now",
    )
    try:
        report = validate_report_narratives([artifact], {"title": "画像", "insights": text})
    except ValueError as error:
        report = {"status": "rejected", "error": str(error)}
    assert (report["status"] == "passed") is expected, report
    return outcome, report


@pytest.mark.parametrize("index", range(21))
def test_frozen_twenty_one_cases_on_new_summary(index, tmp_path):
    case = CAPTURE["cases"][index]
    # Ambiguous duplicates remain unsupported, including both generic old aliases.
    expected = case["case"] in {
        "original-bullet-10",
        "original-bullet-13",
        "english-ratio",
        "chinese-ratio",
        "non-null-count",
        "combined-same-clause",
        "semicolon-subject-elision",
        "semicolon-repeated-subject",
        "distinct-english",
        "distinct-chinese",
    }
    if case["case"] == "original-bullet-13":
        assert not check(case["text"], [record(CAPTURE["tool_result"])])["refs"]
        return
    outcome, report = evaluate(CAPTURE["tool_result"], case["text"], expected)
    if case["case"] == "original-full":
        unsupported = [v["token"] for v in outcome["refs"] if not v["supported"]]
        assert unsupported == ["1", "1"], outcome
        non_null = next(
            v
            for v in outcome["refs"]
            if v.get("semantic_binding", {}).get("statistic") == "non_null_count"
        )
        assert non_null["semantic_binding"]["metric"] == FIELD
        assert non_null["path"] == "$.roles.columns[3].profile_evidence.non_null_count"
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {"case": case["case"], "chat": outcome, "report": report}, ensure_ascii=False, indent=2
        )
    )


def test_explicit_answer_and_original_are_separate(tmp_path):
    outcome, report = evaluate(CAPTURE["tool_result"], EXPLICIT_ANSWER, True)
    bases = {r["semantic_binding"].get("count_basis") for r in outcome["refs"]}
    assert {"non_null_rows", "excess_non_null_occurrences", "excess_rows_keep_one"} <= bases
    assert CAPTURE["answer"] != EXPLICIT_ANSWER
    (tmp_path / "witness.json").write_text(
        json.dumps(
            {"answer": EXPLICIT_ANSWER, "chat": outcome, "report": report},
            ensure_ascii=False,
            indent=2,
        )
    )


@pytest.mark.parametrize(
    "text,expected",
    [
        (f"{FIELD}有1个空值；最小值10，最大值999。", True),
        (f"{FIELD}非空74条；最小值10；最大值999。", True),
        (f"{FIELD}最小值10；{FIELD}最大值999。", True),
        (f"{FIELD}最小值10；记录ID有74个不同值。", True),
        (f"{FIELD}最小值10；记录ID有74个不同值；最大值999。", False),
        (f"{FIELD}最小值10；以下改看广告时长；最大值999。", False),
        (f"{FIELD}最小值10；广告时长最大值999。", False),
        (f"{FIELD}最小值10；以下改看广告时长，最大值999。", False),
        (f"{FIELD}最小值10；最大值999然后改看其他列；均值50.12。", False),
        (f"{FIELD}最小值10；该列最大值999。", True),
        (f"{FIELD}最小值10；\n最大值999。", False),
        (f"{FIELD}最小值10；\n\n最大值999。", False),
        (f"- {FIELD}最小值10；\n- 最大值999。", False),
        (f"{FIELD}最小值10。最大值999。", False),
        (f"{FIELD}最小值10；非空75条。", False),
        (f"{FIELD}最小值10；非空74小时。", False),
        (f"{FIELD}最小值10；最大值999小时。", False),
        (f"{FIELD}最小值10；最大值b17。", False),
    ],
)
def test_scope_contract(text, expected):
    evaluate(CAPTURE["tool_result"], text, expected)


@pytest.mark.parametrize(
    "mutation",
    ["legacy", "wrong_count", "wrong_name", "duplicate_name", "wrong_field", "hidden", "reorder"],
)
def test_named_exact_non_null_provenance(mutation):
    result = copy.deepcopy(CAPTURE["tool_result"])
    if mutation == "legacy":
        result["profile"]["columns"][3].pop("null_count")
    elif mutation == "wrong_count":
        result["roles"]["columns"][3]["profile_evidence"]["non_null_count"] = 73
    elif mutation == "wrong_name":
        result["roles"]["columns"][3]["column"] = "不存在的字段"
    elif mutation == "duplicate_name":
        result["profile"]["columns"].append(copy.deepcopy(result["profile"]["columns"][3]))
    elif mutation == "hidden":
        result["profile"]["columns"][3]["null_ratio"] = None
    elif mutation == "reorder":
        result["roles"]["columns"].reverse()
    text = "记录ID非空74条。" if mutation == "wrong_field" else f"{FIELD}非空74条。"
    evaluate(result, text, mutation == "reorder")


@pytest.mark.parametrize("ref,version", [("e" * 32, "a" * 64), ("d" * 32, "b" * 64)])
def test_non_null_cannot_select_another_dataset_or_version(ref, version):
    result = CAPTURE["tool_result"]
    outcome = check(f"{FIELD}非空74条。", [record(result), record(result, "other", ref, version)])
    assert not outcome["verification"]["passed"]


@pytest.mark.parametrize(
    "values,expected", [([], 0), ([None] * 3, 0), ([1.0] * 3, 3), ([None] + [1.0] * 50000, 50000)]
)
def test_exact_non_null_and_exclude(values, expected):
    frame = pd.DataFrame({"金额": pd.Series(values, dtype="float64")})
    result = tool_profile(frame)
    evaluate(result, f"金额非空{expected}条。", True)
    if len(values) > 100:
        assert result["profile"]["columns"][0]["null_ratio"] == 0.0
    profile = infer_schema({"dataset_ref": save_dataframe(frame)})
    hidden = apply_policy(profile, EffectivePolicy(columns={"金额": ColumnRule.EXCLUDE}))
    hidden_roles = infer_data_roles(hidden)
    evaluate({"profile": hidden.to_dict(), "roles": hidden_roles}, f"金额非空{expected}条。", False)


@pytest.mark.parametrize(
    "ids,metric,column_excess,row_excess",
    [
        (["a", "a", "a", "b"], [1, 1, 1, 2], 2, 2),
        (["a", "a", "a", "b", "b"], [1, 1, 1, 2, 2], 3, 3),
        (["a", "a", "a", "b", "b", None, None], [1, 2, 3, 4, 4, 5, 5], 3, 2),
    ],
)
def test_duplicate_bases_never_use_numeric_coincidence(ids, metric, column_excess, row_excess):
    result = tool_profile(pd.DataFrame({"记录ID": ids, "金额": metric}))
    evaluate(result, f"记录ID重复多余记录数为{column_excess}。", True)
    evaluate(result, f"整行重复多余记录数为{row_excess}。", True)
    evaluate(result, f"记录ID重复多余记录数为{column_excess + 1}。", False)
    evaluate(result, f"整行重复多余记录数为{row_excess + 1}。", False)
    for text in [
        f"记录ID有{column_excess}条重复。",
        f"记录ID存在{column_excess}个重复值。",
        f"存在{row_excess}条完全重复的行记录。",
        f"另有{row_excess}行疑似完全重复的记录。",
        f"记录ID参与重复记录数为{column_excess}。",
        f"记录ID重复组数为{column_excess}。",
        f"整行参与重复记录数为{row_excess}。",
        f"整行重复组数为{row_excess}。",
    ]:
        evaluate(result, text, False)


@pytest.mark.parametrize("field", ["收入（元；税后）", "净收入「甲；乙」"])
def test_semicolons_in_real_field_names_still_keep_identity(field):
    result = tool_profile(pd.DataFrame({field: [4.0, 8.0, None]}))
    outcome, _ = evaluate(result, f"{field}非空2条；最大8。", True)
    assert all(v["semantic_binding"]["metric"] == field for v in outcome["refs"])


@pytest.mark.parametrize(
    "text",
    [
        "播出时长（分钟）非空74组。",
        "记录ID重复多余记录数为1组。",
        "整行重复多余记录数为1组。",
        "播出时长（分钟）最小10；记录ID重复多余记录数为1；最大999002。",
    ],
)
def test_count_units_and_competing_duplicate_subject(text):
    evaluate(CAPTURE["tool_result"], text, False)


def test_exact_count_does_not_override_inconsistent_profile():
    result = copy.deepcopy(CAPTURE["tool_result"])
    result["profile"]["columns"][3]["null_ratio"] = 0.5
    evaluate(result, f"{FIELD}非空74条。", False)


@pytest.mark.parametrize("remove_null_count", [False, True])
def test_historical_count_binding_without_exact_proof_is_not_admitted(remove_null_count):
    from dataclasses import replace

    evidence = record(CAPTURE["tool_result"])
    summary = copy.deepcopy(evidence.summary)
    for item in summary["value_index"]:
        if item["path"].endswith(".non_null_count"):
            item.pop("profile_count_basis", None)
    if remove_null_count:
        summary["value_index"] = [
            item for item in summary["value_index"] if not item["path"].endswith(".null_count")
        ]
    # Historical indexes are not rewritten or upgraded from a matching number.
    old = replace(evidence, summary=summary)
    outcome = check("记录ID非空75条。", [old])
    assert outcome["refs"] and not outcome["verification"]["passed"]
