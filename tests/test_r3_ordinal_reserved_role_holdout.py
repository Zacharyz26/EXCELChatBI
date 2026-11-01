"""Only complete reserved subject-slot labels prove ordinal metadata.

Independent typed component fixture, not real tools/Host/model.
Frozen before execution against candidate5; no existing expectations changed.
"""

import json
import socket
from dataclasses import asdict

import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.report_claims import validate_report_narratives
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.session.models import Artifact
from packages.session.task_models import EvidenceRecord


@pytest.mark.parametrize(
    "header,expected", [("变量一", True), ("共有一", False), ("广告费1", False)]
)
def test_unknown_numeric_header_is_not_promoted_by_default_subject_role(
    tmp_path, monkeypatch, header, expected
):
    def denied(*_args, **_kwargs):
        raise AssertionError("Pure ordinal assignment holdout forbids network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    # A declared independent relation fixture supports only the pair's corr.
    # It supplies neither a numeric profit value nor a generic amount of one.
    args = {"dataset_ref": "c" * 32, "columns": ["销售额", "利润"]}
    payload = {"top_pairs": [{"a": "销售额", "b": "利润", "corr": 0.75}], "method": "pearson"}
    record = EvidenceRecord(
        "relation-proof",
        "ordinal-run",
        "ordinal-inv",
        "relation-artifact",
        "tool_result",
        {"tool": "correlation"},
        "independent-component-fixture",
        build_evidence_summary(
            summary="declared relation only", result=payload, artifact_id=None, arguments=args
        ),
        "2026-11-01T00:00:00Z",
    )
    artifact = Artifact(
        "relation-artifact",
        "ordinal-conversation",
        "ordinal-message",
        "stats",
        {"kind": "correlation", "result": payload},
        None,
        "correlation",
        args,
        args["dataset_ref"],
        "2026-11-01T00:00:00Z",
    )
    text = f"|{header}|变量2|corr|\n|---|---|---|\n|销售额|利润|0.75|"
    candidate = validate_candidate(text, [record], goal="说明计算结果")
    contract = build_minimal_contract(
        run_id="ordinal-run",
        user_text="说明计算结果",
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[],
        evidence=[record],
        claims=None,
    )
    try:
        report = validate_report_narratives([artifact], {"title": "关系结果", "insights": text})
    except ValueError as error:
        report = {"status": "rejected", "error": str(error)}
    witness = {
        "input": text,
        "expected": expected,
        "source": asdict(record),
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "report": report,
        "layer": "independent typed component fixture; no tools/Host/SQLite/model",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
    assert (report.get("status") == "passed") is expected, witness
    if not expected:
        refs = [ref for claim in candidate.claims for ref in claim.value_refs]
        assert any(
            ref.get("token") in {"1", "一"} and ref.get("supported") is False for ref in refs
        )
