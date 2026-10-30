"""R1 core gate: independent expectations, shared source across all publication paths."""

import json
from dataclasses import asdict

import pytest
from apps.orchestrator.agent_loop import _enforce_prepared_candidate
from apps.orchestrator.control.quantity_presentation import prepare_candidate, validate_candidate
from apps.orchestrator.control.verifier import VerificationResult

from tests import test_delimited_numeric_values as report_checks
from tests import test_profile_subject_boundaries as source


@pytest.fixture(scope="module")
def profile():
    # Real deterministic parser and quality producer; no model/network response.
    return source.actual_profile.__wrapped__()


@pytest.mark.parametrize(
    "text,expected",
    [
        ("收入（元）有（\nb17\n）个\n「缺失值」。", False),
        ("收入（元）有（b17）个「缺失值」。", False),
        ("收入（元）有（\n2026-10-16\n）个\n「缺失值」。", False),
        ("收入（元）有（\n1\n）个\n「缺失值」。", True),
        ("收入（元）有1个缺失值。画像结果也印证了这一点。", True),
        ("收入（元）有1个缺失值；另有八个异常数据点均已核实。", False),
        ("以下三个提示供参考。", False),
        ("```\n收入（元）最小1。", False),
    ],
)
def test_all_completion_paths_share_core_obligations(profile, text, expected, tmp_path):
    records = [source.record(profile)]
    checks = report_checks.evaluate(profile, text)
    prepared = prepare_candidate(text, records, goal="复核画像", run_id="run")
    candidate = validate_candidate(text, records, goal="复核画像", run_id="run")
    repair_gate = _enforce_prepared_candidate(VerificationResult("PASS"), prepared)
    witness = {
        "input": text,
        "expected": expected,
        "checks": checks,
        "candidate": asdict(candidate),
        "repair_gate": asdict(repair_gate),
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert checks["verification"]["passed"] is expected, witness
    assert (checks["report"]["status"] == "passed") is expected, witness
    assert candidate.passed is expected, witness
    assert prepared.valid is expected, witness
    assert repair_gate.passed is expected, witness
    if not expected:
        assert candidate.issues, "An unresolved quantity must not vanish with claims=[]"


def test_empty_binder_output_cannot_override_quantity_obligation(profile, monkeypatch):
    """Fault injection checks the defense itself; real producer cases run above."""
    from apps.orchestrator.control import claims, verifier
    from apps.orchestrator.control.contracts import build_minimal_contract

    text = "收入（元）有（\nb17\n）个\n「缺失值」。"
    records = [source.record(profile)]
    # Simulate the original lost-claim failure at the binder boundary. This must
    # never be interpreted as an assertion that the source contains no quantity.
    monkeypatch.setattr(claims, "extract_numeric_claims", lambda **kwargs: [])
    monkeypatch.setattr(verifier, "extract_numeric_claims", lambda **kwargs: [])
    contract = build_minimal_contract(
        run_id="run",
        user_text="复核画像",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    result = verifier.verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=records,
        claims=[],
    )
    assert not result.passed
    assert "unresolved_quantity_obligation" in {i.code for i in result.issues}
    prepared = prepare_candidate(text, records, goal="复核画像", run_id="run")
    assert not prepared.valid
    assert not _enforce_prepared_candidate(VerificationResult("PASS"), prepared).passed
