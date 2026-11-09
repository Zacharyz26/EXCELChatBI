"""Frozen real-response facts. No model calls; independent expected role/value.

Captured Evidence stays byte-for-byte historical. Producer additions are tested
separately and cannot retroactively give these records new facts.
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.quantity_presentation import prepare_candidate
from packages.session.task_models import EvidenceRecord

OLD = Path(__file__).parent / "fixtures" / "segmented_quantities"


def captured(scene):
    data = json.loads((OLD / (scene + ".json")).read_text())
    records = [
        EvidenceRecord(**{k: v for k, v in e.items() if k not in ("source_json", "summary_json")})
        for e in data["evidence"]
    ]
    return data, records, data["candidate"]


def claims(scene, text):
    data, evidence, _ = captured(scene)
    return extract_numeric_claims(
        final_text=text, evidence=evidence, goal=data["task_runs"][0]["goal"]
    )


def check(scene, text):
    data, evidence, _ = captured(scene)
    run = data["task_runs"][0]
    return prepare_candidate(text, evidence, goal=run["goal"], run_id=run["run_id"])


CASES = [
    (
        "clean-from-to",
        "s2-clean",
        "删除播出时长（分钟）为空的记录后，数据从38行变为37行，共移除1条空值记录。",
        True,
    ),
    (
        "clean-explicit-label",
        "s2-clean",
        "依据：变换工具返回清洗前38行、清洗后37行，移除1行。",
        True,
    ),
    ("clean-swap", "s2-clean", "数据从37行变为38行，共移除1条空值记录。", False),
    ("clean-wrong-filter", "s2-clean", "依据频道清洗，清洗前38行、清洗后37行，移除1行。", False),
    ("clean-wrong-object", "s2-clean", "清洗前38行，清洗后37行，移除1条重复记录。", False),
    (
        "dedup-complete-noun",
        "s3-dedup",
        "处理结果：去重前37行，去重后36行，共移除1条完全重复的记录。",
        True,
    ),
    ("dedup-participants", "s3-dedup", "去重前37行，去重后36行，参与整行重复的记录数为1。", False),
    ("dedup-remaining", "s3-dedup", "去重前37行，去重后36行，剩余重复多余记录数为1。", False),
    ("dedup-groups", "s3-dedup", "去重前37行，去重后36行，重复组数为1。", False),
    (
        "profile-inline-scope",
        "s0-chain-profile",
        "播出时长（分钟）统计口径：基于当前上传数据、未做任何筛选或清洗，统计对象为该列全部非空记录（非空数74）。最小值为10.0，最大值为999.0，均值为50.121622，中位数为37.5，不同值数量为37，非空数为74。",
        True,
    ),
    (
        "profile-wrong-value",
        "s0-chain-profile",
        "播出时长（分钟）统计口径：基于当前上传数据、未做任何筛选或清洗，统计对象为该列全部非空记录（非空数74）。最大值为888。",
        False,
    ),
    (
        "profile-hard-boundary",
        "s0-chain-profile",
        "播出时长（分钟）统计口径：基于当前上传数据。\n\n最大值为999。",
        False,
    ),
    (
        "profile-wrong-field",
        "s0-chain-profile",
        "其他时长（分钟）统计口径：基于当前上传数据。最大值为999。",
        False,
    ),
    ("profile-id-counts", "s0-chain-profile", "记录ID非空数为75、不同值数量为74。", True),
    ("profile-excess", "s0-chain-profile", "存在1条整行重复的多余记录。", True),
    ("profile-participants", "s0-chain-profile", "参与整行重复的记录数为1。", False),
    ("total-unit", "s4-monthly", "整个期间播出时长总计为1350.0分钟。", True),
    ("total-wrong-unit", "s4-monthly", "整个期间播出时长总计为1350.0秒。", False),
    ("total-wrong-value", "s4-monthly", "整个期间播出时长总计为1530.0分钟。", False),
    ("chart-delta", "s5-chart", "播出时长（分钟）每月较上月增加60分钟。", False),
]


@pytest.mark.parametrize("name,scene,text,expected", CASES, ids=[c[0] for c in CASES])
def test_frozen_quantity_contract(tmp_path, name, scene, text, expected):
    actual = check(scene, text)
    witness = {"scene": scene, "text": text, "expected": expected, "actual": asdict(actual)}
    (tmp_path / "quantity.json").write_text(
        json.dumps(witness, ensure_ascii=False, default=str, indent=2)
    )
    assert actual.valid is expected, witness
    if expected:
        refs = [r for c in claims(scene, text) for r in c.value_refs if r.get("source_span")]
        assert refs and all(r.get("supported") for r in refs), witness


@pytest.mark.parametrize(
    "variant,expected",
    [
        ("original", True),
        ("swapped", False),
        ("wrong-metric", False),
        ("wrong-grain", False),
        ("wrong-dataset", False),
        ("unknown-group", False),
    ],
)
def test_original_monthly_list(tmp_path, variant, expected):
    _, _, text = captured("s4-monthly")
    if variant == "swapped":
        text = text.replace("75.0", "135.0", 1)
    if variant == "wrong-metric":
        text = text.replace("播出时长", "利润")
    if variant == "wrong-grain":
        text = text.replace("各月", "各频道")
    if variant == "wrong-dataset":
        text = "数据集: " + "a" * 32 + "。\n" + text
    if variant == "unknown-group":
        text = text.replace("2026-01：75.0", "2026-07：75.0")
    actual = check("s4-monthly", text)
    witness = {"text": text, "expected": expected, "actual": asdict(actual)}
    (tmp_path / "monthly.json").write_text(
        json.dumps(witness, ensure_ascii=False, default=str, indent=2)
    )
    assert actual.valid is expected, witness
    if expected:
        refs = [
            r
            for c in claims("s4-monthly", text)
            for r in c.value_refs
            if r.get("supported") and r.get("source_span")
        ]
        assert len(refs) == 7, witness


def test_s5_original_delta_remains_blocked():
    _, _, text = captured("s5-chart")
    assert not check("s5-chart", text).valid
