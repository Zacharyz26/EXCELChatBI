"""R4 complete-gate fault injection; not a real Agent or persisted-history test.

The source-selection intent is explicit. Removing its frozen host proof cannot
turn the same obligation into an ordinary response-only task. Actual valid
Host selections remain covered by test_r4_context_contract, without mutation.
"""

import json
from dataclasses import asdict, replace

import pytest
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract

REF = "9a8b7c6d5e4f30219283746556473829"


@pytest.mark.parametrize("missing", ["p2-proof", "analysis-contract"])
def test_complete_verifier_does_not_erase_selection_when_proof_is_missing(tmp_path, missing):
    goal = f"请使用数据集（dataset_ref: {REF}）。"
    contract = build_minimal_contract(
        run_id="r4-source-proof-test",
        user_text=goal,
        chart_required=False,
        report_required=False,
        pdf_required=False,
        dataset_refs=(REF,),
    )
    # No fabricated Host signature or success history. This is an explicit
    # consumer-boundary fault, not an assertion that Host produced this input.
    assert contract.assumptions == ()
    if missing == "analysis-contract":
        contract = replace(contract, analysis_requirements=None)
    result = verify_completion(
        contract=contract,
        final_text="已确认使用指定来源。",
        artifacts=[],
        invocations=[],
        evidence=[],
        claims=[],
        plan_steps=[],
        dataset_bindings=[],
    )
    witness = {
        "case": missing,
        "goal": goal,
        "contract": asdict(contract),
        "verdict": asdict(result),
        "layer": "complete Verifier fault injection; no Host/provider/database",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert not result.passed, witness
    assert any(
        issue.code.startswith("source_selection") or issue.code.startswith("selection_")
        for issue in result.issues
    ), witness
