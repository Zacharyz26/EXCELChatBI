"""A verified identifier is metadata only in an explicit header, never a value."""

from dataclasses import replace

import pytest
from apps.orchestrator.control.report_claims import validate_report_narratives
from packages.session.models import Artifact

from tests.test_manual02_profile_contract import CAPTURE
from tests.test_profile_subject_boundaries import check, record

REF = "7b" * 16
VERSION = "6d3200d51999d6e0fea7ae261a26645cc4e2301c0d2529e4ce9bbf1c5cadb3f5"


def validate(text, expected, *, version=VERSION):
    evidence = record(CAPTURE["tool_result"], ref=REF, version=version)
    outcome = check(text, [evidence])
    assert outcome["verification"]["passed"] is expected, outcome
    artifact = Artifact(
        "p",
        "c",
        "m",
        "profile",
        CAPTURE["tool_result"],
        None,
        "get_data_profile",
        {"dataset_ref": REF, "data_version_hash": version},
        REF,
        "now",
    )
    if expected:
        report = validate_report_narratives([artifact], {"title": "画像", "insights": text})
        assert report["status"] == "passed"
    else:
        with pytest.raises(ValueError):
            validate_report_narratives([artifact], {"title": "画像", "insights": text})
    return outcome


@pytest.mark.parametrize("version", [VERSION, "1" * 64, "a" * 64])
def test_realistic_and_numeric_only_verified_version_headers(version):
    outcome = validate(
        f"数据集 {REF}，版本 {version}，整行重复多余记录数为1。", True, version=version
    )
    assert [r["token"] for r in outcome["refs"]] == ["1"]
    assert outcome["refs"][0]["semantic_binding"]["dataset_version"] == version
    validate(f"版本 {version}。", True, version=version)


@pytest.mark.parametrize(
    "text",
    [
        f"版本 {VERSION}，整行重复多余记录数为99。",
        f"版本 {VERSION}，整行重复多余记录数为{VERSION}。",
        f"整行重复多余记录数为版本 {VERSION}。",
        f"整行重复多余记录数为说明{VERSION}。",
        f"版本 {VERSION}，整行重复多余记录数为1组。",
        f"版本 {VERSION}，未知设备的整行重复多余记录数为1。",
        f"版本 {VERSION}，记录ID为空的记录中，整行重复多余记录数为1。",
        f"版本 {VERSION} 99，整行重复多余记录数为1。",
        f"版本 {VERSION}z，整行重复多余记录数为1。",
        f"版本 {VERSION}0，整行重复多余记录数为1。",
        f"版本 {VERSION[:-1]}0，整行重复多余记录数为1。",
        "版本 " + "z" * 64 + "，整行重复多余记录数为1。",
        "版本 " + "b" * 64 + "。",
        "版本 " + "1" * 64 + "，整行重复多余记录数为1。",
        f"数据集 {VERSION}，版本 {REF}，整行重复多余记录数为1。",
        f"数据集 {REF[:-1]}0，版本 {VERSION}，整行重复多余记录数为1。",
        f"版本 {VERSION}，版本 " + "b" * 64 + "，整行重复多余记录数为1。",
    ],
)
def test_identity_exemption_does_not_hide_bad_numbers_or_unknown_versions(text):
    validate(text, False)


def test_compact_existing_identity_header_keeps_field_measurement():
    validate(f"数据集{REF}版本{VERSION}：播出时长（分钟）最大999。", True)
    validate(f"数据集{REF}版本{VERSION}：播出时长（分钟）最大998。", False)


def test_identity_requires_a_single_current_source_pair():
    other = record(CAPTURE["tool_result"], "other", ref="e" * 32, version="b" * 64)
    current = record(CAPTURE["tool_result"], ref=REF, version=VERSION)
    text = f"数据集 {REF}，版本 " + "b" * 64 + "，整行重复多余记录数为1。"
    assert not check(text, [current, other])["verification"]["passed"]
    assert not check(f"版本 {VERSION}。", [])["verification"]["passed"]
    assert not check(f"版本 {VERSION}。", [replace(current, kind="memory")])["verification"][
        "passed"
    ]


def test_release_label_is_not_a_dataset_hash_or_measurement_exemption():
    validate("版本 v2.1 已准备。", True)
    validate("整行重复多余记录数为v2.1。", False)
