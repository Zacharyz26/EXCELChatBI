"""Shared count-owner boundary, independent bounded positive/negative matrix.

Ordinary deictic/lexical Chinese words must not become fake numeric facts.
A count predicate plus an unbound value remains unknown even without a counter.
Frozen before the shared count-owner repair. No existing expectations changed.
Layer: declared typed component Evidence; not real tools, Host, SQLite or models.
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


@pytest.mark.parametrize("placement", ["body", "header"])
@pytest.mark.parametrize(
    "label,expected",
    [("这一点", True), ("一致", True), ("共有1", False), ("共有一", False)],
)
def test_count_owner_and_ordinary_words_keep_same_contract_across_surface(
    tmp_path, monkeypatch, placement, label, expected
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
    table = "|变量1|变量2|corr|\n|---|---|---|\n|销售额|利润|0.75|"
    if placement == "header":
        text = table.replace("变量1", label, 1)
    else:
        # These are ordinary references/lexical words, not counted members.
        prose = {"这一点": "这一点值得注意。", "一致": "说明一致。"}.get(label, label + "。")
        text = prose + "\n" + table
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
        count_start = text.index(label) + len(label) - 1
        expected_span = [count_start, count_start + 1]
        assert any(
            ref.get("token") == label[-1]
            and ref.get("supported") is False
            and ref.get("source_span") == expected_span
            for ref in refs
        ), {"expected_source_span": expected_span, "refs": refs}
