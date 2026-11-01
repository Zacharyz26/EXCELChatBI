"""Independent member-count proof contract; offline, no product expectation edits."""

import hashlib
import json
import socket
from dataclasses import asdict

import pytest
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract

GOAL = "复核文本结构与声明"
CASES = [
    ("two-members", "这里是两个标签：“甲”“乙”。", True, None),
    ("separate-name", "这里是两个标签：“甲”“乙”，另一名称是“附注”。", True, None),
    ("three-vs-two", "这里是三个标签：“甲”“乙”。", False, "三"),
    ("two-vs-one", "这里是两个标签：“甲”。", False, "两"),
    ("open-second", "这里是两个标签：“甲”“乙。", False, "两"),
    ("business-owner", "这里是八个异常数据点：“甲”“乙”。", False, "八"),
    (
        "business-after-labels",
        "这里是两个标签：“甲”“乙”；本表另有八个异常数据点均已核实。",
        False,
        "八",
    ),
    ("assignment-after-labels", "这里是两个标签：“甲”“乙”；重复组数为八。", False, "八"),
]


@pytest.fixture(autouse=True)
def deny_network(monkeypatch):
    def deny(*a, **kw):
        raise AssertionError("independent label contract must remain offline")

    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket.socket, "connect_ex", deny)
    monkeypatch.setattr(socket, "create_connection", deny)


@pytest.mark.parametrize("case,text,expected,required_token", CASES, ids=[c[0] for c in CASES])
def test_closed_label_members_are_evidence_only_for_their_own_count(
    case, text, expected, required_token, tmp_path
):
    # No data Evidence: a count of local quoted members is verifiable from
    # source syntax alone. The same text cannot prove an anomaly/group count.
    candidate = validate_candidate(text, [], goal=GOAL)
    contract = build_minimal_contract(
        run_id="label-count-run",
        user_text=GOAL,
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[],
        claims=list(candidate.claims),
    )
    try:
        report = validate_report_narratives(
            [], {"title": "结构核对", "insights": text}, run_id="label-count-run"
        )
        report_ok = report.get("status") == "passed"
    except ValueError as error:
        report = {"error": str(error)}
        report_ok = False
    witness = {
        "case": case,
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "report": report,
        "layer": "offline shared candidate/full verifier/report consumer, no model/Host/browser",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
    assert report_ok is expected, witness
    refs = [r for c in candidate.claims for r in c.value_refs]
    if expected:
        document_refs = [r for r in refs if r.get("quantity_purpose") == "document"]
        assert len(document_refs) == 1, witness
        ref = document_refs[0]
        assert ref["token"] == "两" and ref["supported"] is True, witness
        proof = ref["document_count_proof"]
        assert proof["source_hash"] == hashlib.sha256(text.encode()).hexdigest(), witness
        assert [text[a:b] for a, b in proof["members"]] == ["甲", "乙"], witness
        assert proof["basis"] == "quoted_labels", witness
        assert not ref.get("evidence_id"), witness
    else:
        assert candidate.issues, witness
        assert any(required_token == q.get("token") for q in candidate.quantities), witness
