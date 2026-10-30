"""Frozen independent textual quantities versus measured data counts."""

import dataclasses
import hashlib
import json
import socket
from pathlib import Path

import pytest
from apps.orchestrator.control.claims import extract_numeric_claims
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord

WITNESS = Path(__file__).parent / "fixtures/r1_document_profile.json"
LABELS = "“去重会删除的多余记录数”“参与重复的记录数”“重复组数”"
ORIGINAL = (
    "工具未进一步拆分"
    + LABELS
    + "这三个细分指标，因此我无法从工具结果中确认这些数字，不能自行推算。"
)
CASES = [
    # A01–A03: an unfulfilled outline/report claim is not a completed document.
    ("quality_note", "顺带提示三个数据质量点，供后续分步处理时参考：", False, "三"),
    ("report_tips", "报告（a25）附日期「2024-02-29」和三个提示。", False, "三"),
    ("soft_tip", "以下\n三个提示\n供参考。", False, "三"),
    ("quoted_three_labels", ORIGINAL, True, None),
    ("existing_inline_tips", "以下三个提示供参考。", False, "三"),
    ("abnormal_data_points", "本表有三个异常数据点。", False, "三"),
    ("actual_metrics", "本表有三个指标。", False, "三"),
    ("quality_data_points", "本表存在三个数据质量点。", False, "三"),
    ("document_prefix_cannot_hide_missing_counts", "以下八个缺失值需要处理。", False, "八"),
    ("document_prefix_cannot_hide_duplicate_groups", "以下三个重复组需要处理。", False, "三"),
    (
        "normal_tips_followed_by_wrong_business_count",
        "以下三个提示供参考。本表共有八个月份。",
        False,
        "八",
    ),
    (
        "enumerated_labels_do_not_hide_following_measurement",
        LABELS + "这三个细分指标，本表共有八个月份。",
        False,
        "八",
    ),
    ("quoted_fields_do_not_define_dataset_count", "「频道」「状态」有三个不同取值。", False, "三"),
]


@pytest.fixture
def profile_input(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("Offline independent document quantity review")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    payload = json.loads(WITNESS.read_text())
    return [EvidenceRecord(**e) for e in payload["evidence"]], Artifact(**payload["profile"])


@pytest.mark.parametrize("name,text,expected,reject_token", CASES, ids=[c[0] for c in CASES])
def test_textual_quantity_is_not_a_dataset_fact(name, text, expected, reject_token, profile_input):
    evidence, artifact = profile_input
    contract = build_minimal_contract(
        run_id=evidence[0].run_id,
        user_text="核对画像已知事实",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    claims = extract_numeric_claims(final_text=text, goal=contract.goal, evidence=evidence)
    refs = [r for c in claims for r in c.value_refs]
    result = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=evidence,
        claims=claims,
    )
    assert result.passed is expected, (name, refs, dataclasses.asdict(result))
    if expected:
        # A04 retains PASS: the count belongs to three explicit quoted labels,
        # and must remain auditable without becoming a business measurement.
        assert refs and all(r.get("quantity_purpose") == "document" for r in refs), refs
        assert all(r.get("supported") is True for r in refs), refs
        count = next(r for r in refs if r["token"] == "三")
        proof = count["document_count_proof"]
        assert proof["source_hash"] == hashlib.sha256(text.encode()).hexdigest()
        assert proof["node_id"] and len(proof["members"]) == 3, proof
    else:
        assert any(r["token"] == reject_token and r.get("supported") is False for r in refs), (
            name,
            refs,
        )
        if name in {"quality_note", "report_tips", "soft_tip", "existing_inline_tips"}:
            assert result.verdict == "NEEDS_ACTION", dataclasses.asdict(result)
    try:
        validate_report_narratives([artifact], {"title": "画像", "insights": text})
        report_ok = True
    except ValueError:
        report_ok = False
    assert report_ok is expected, (name, refs)
