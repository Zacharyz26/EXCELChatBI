"""Independent role/scope/identity oracles, authored without changing products.

RealTrace uses actual deterministic tools and SQLite; captured fixtures are
explicit offline evidence replays, not newly completed model tasks.
"""

import json
from copy import deepcopy
from dataclasses import asdict, replace

import pytest
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import prepare_candidate

from tests.test_r2_operation_completion import RealTrace, no_network
from tests.test_segmented_quantity_fix import captured, check

__all__ = ["no_network"]


def dump(tmp_path, name, **values):
    (tmp_path / (name + ".json")).write_text(
        json.dumps(values, ensure_ascii=False, indent=2, default=str)
    )


@pytest.mark.parametrize(
    "parent,expected",
    [
        ("", True),
        ("数据集：{source}。\n", True),
        ("数据集：" + "b" * 32 + "。\n", False),
        ("数据版本：" + "b" * 64 + "。\n", False),
        ("筛选条件：频道=B。\n", False),
    ],
)
def test_real_retention_policy_preserves_parent_identity_scope(tmp_path, parent, expected):
    trace = RealTrace(tmp_path, "按整行去重。")
    result = trace.execute({"drop_duplicates": []})
    assert (result["rows_before"], result["rows_after"]) == (5, 4)
    text = parent.format(source=trace.source) + "每组保留首条。"
    verdict = trace.verify(text)
    dump(
        tmp_path,
        "parent-policy",
        text=text,
        expected=expected,
        verdict=asdict(verdict),
        evidence=[asdict(e) for e in trace.state()[1]],
    )
    assert verdict.passed is expected


@pytest.mark.parametrize(
    "text,expected",
    [
        ("按频道去重（每组保留首条）。", False),
        ("去重前5行，去重后4行，每组保留第一条。", True),
    ],
)
def test_policy_without_exact_operation_or_supported_enum_is_rejected(tmp_path, text, expected):
    trace = RealTrace(tmp_path, "按整行去重。")
    trace.execute({"drop_duplicates": []})
    verdict = trace.verify(text)
    refs = [
        r
        for c in extract_numeric_claims(
            final_text=text, evidence=trace.state()[1], goal="按整行去重。"
        )
        for r in c.value_refs
    ]
    dump(
        tmp_path, "unknown-policy", text=text, expected=expected, verdict=asdict(verdict), refs=refs
    )
    assert verdict.passed is expected
    if expected:
        policies = [r for r in refs if r.get("kind") == "operation_policy"]
        assert policies and all(
            r.get("supported")
            and r.get("semantic_binding", {}).get("policy") == "first"
            and r["semantic_binding"].get("dataset_ref") == trace.source
            and r["semantic_binding"].get("dataset_version")
            for r in policies
        )


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("same", True),
        ("conflicting_value", False),
        ("other_version", False),
        ("other_dataset", False),
    ],
)
def test_profile_same_identity_corroboration_never_resolves_conflict(tmp_path, mode, expected):
    data, records, _ = captured("s0-chain-profile")
    original = records[0]
    summary = deepcopy(original.summary)
    if mode == "conflicting_value":
        for item in summary["value_index"]:
            if item.get("path") == "$.profile.columns[4].distinct_count":
                item["value"] = "73"
    elif mode == "other_version":
        summary["numeric_context"]["data_version_hash"] = "b" * 64
        summary["numeric_context"]["profile_population"]["dataset_version"] = "b" * 64
    elif mode == "other_dataset":
        summary["numeric_context"]["dataset_ref"] = "b" * 32
        population = summary["numeric_context"]["profile_population"]
        population["dataset_ref"] = population["root_ref"] = "b" * 32
    clone = replace(original, evidence_id="independent-second-evidence", summary=summary)
    text = "记录ID不同值数量为74。"
    result = prepare_candidate(
        text, [original, clone], goal=data["task_runs"][0]["goal"], run_id=original.run_id
    )
    refs = [
        r
        for c in extract_numeric_claims(
            final_text=text, evidence=[original, clone], goal=data["task_runs"][0]["goal"]
        )
        for r in c.value_refs
    ]
    dump(
        tmp_path,
        "identity",
        mode=mode,
        text=text,
        expected=expected,
        candidate=asdict(result),
        refs=refs,
    )
    assert result.valid is expected
    if expected:
        selected = [r for r in refs if r.get("token") == "74"]
        assert selected and all(
            r.get("supported") and not r.get("ambiguous_value") for r in selected
        )


@pytest.mark.parametrize(
    "parent,expected",
    [
        ("", True),
        ("数据版本：" + "b" * 64 + "。\n", False),
        ("筛选条件：频道=B。\n", False),
    ],
)
def test_aggregate_list_keeps_original_parent_constraints(tmp_path, parent, expected):
    _, _, original = captured("s4-monthly")
    text = parent + original
    result = check("s4-monthly", text)
    dump(tmp_path, "parent-list", text=text, expected=expected, candidate=asdict(result))
    assert result.valid is expected


@pytest.mark.parametrize("unit,expected", [("分钟", True), ("秒", False), ("桶", False)])
def test_explicit_declared_unit_never_accepts_unproven_display_unit(tmp_path, unit, expected):
    text = "播出时长（分钟）总计为1350" + unit + "。"
    result = check("s4-monthly", text)
    dump(tmp_path, "unit", text=text, expected=expected, candidate=asdict(result))
    assert result.valid is expected


@pytest.mark.parametrize("scene", ["s0-chain-profile", "s5-chart"])
def test_original_whole_response_remains_rejected(scene):
    _, _, text = captured(scene)
    assert not check(scene, text).valid
