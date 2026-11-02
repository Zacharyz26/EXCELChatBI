"""Independent same-value role contrasts using captured real-chain Evidence.

This is offline replay, not a new Host/provider run. No Evidence value changes.
The wrong amount deliberately equals an available value: value mismatch must
not be the reason these claims fail.
"""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import _restore_task_contract
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

FIXTURE = Path(__file__).parent / "fixtures/r4_execution/captured-dedup-facts.json"


@pytest.mark.parametrize(
    "text,token",
    [
        ("去重后剩余36行多余记录。", "36"),
        ("去重删除了1行重复参与记录。", "1"),
        ("去重删除了1组重复记录。", "1"),
    ],
)
def test_same_numeric_value_cannot_substitute_duplicate_count_basis(tmp_path, text, token):
    raw = json.loads(FIXTURE.read_text())
    evidence = [EvidenceRecord(**r) for r in raw["evidence"]]
    invocations = [ToolInvocation(**r) for r in raw["invocations"]]
    bindings = [TaskDatasetBinding(**r) for r in raw["bindings"]]
    contract = _restore_task_contract(raw["contract"], raw["run"]["run_id"])
    facts = {item["fact"]["role"]: item["value"] for item in evidence[0].summary["value_index"]}
    assert facts["after"] == "36" and facts["removed"] == "1"
    candidate = validate_candidate(
        text, evidence, goal=contract.goal, run_id=contract.run_id, invocations=invocations
    )
    completion = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=invocations,
        evidence=evidence,
        dataset_bindings=bindings,
    )
    refs = [
        ref for claim in candidate.claims for ref in claim.value_refs if ref.get("token") == token
    ]
    witness = {
        "input": text,
        "expected": "REJECT wrong count basis despite equal value",
        "candidate": asdict(candidate),
        "completion": asdict(completion),
        "matching_refs": refs,
        "layer": "captured real Evidence offline replay",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert not candidate.passed, witness
    assert not completion.passed, witness
    assert refs and any(ref.get("supported") is not True for ref in refs), witness
