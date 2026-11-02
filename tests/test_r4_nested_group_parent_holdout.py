"""Independent per-fact parent preservation, not whole-answer PASS evidence."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import _restore_task_contract
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

FIXTURE = Path(__file__).parent / "fixtures/r4_execution/independent-aggregate-captured.json"


@pytest.mark.parametrize("correct_identity", [True, False])
def test_group_member_retains_existing_outer_header_constraint(tmp_path, correct_identity):
    raw = json.loads(FIXTURE.read_text())
    evidence = [EvidenceRecord(**r) for r in raw["evidence"]]
    invocations = [ToolInvocation(**r) for r in raw["invocations"]]
    bindings = [TaskDatasetBinding(**r) for r in raw["bindings"]]
    contract = _restore_task_contract(raw["contract"], raw["run"]["run_id"])
    actual = evidence[0].summary["numeric_context"]["dataset_ref"]
    parent = actual if correct_identity else "f" * 32
    text = (
        f"| 数据集（dataset_ref: {parent}）（播出时长（分钟）各组合计：2026-01为75） | 项目 |\n"
        "| --- | --- |\n| 标记 | 说明 |\n\n播出时长（分钟）总计为1350。"
    )
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
        ref for claim in candidate.claims for ref in claim.value_refs if ref.get("token") == "75"
    ]
    witness = {
        "input": text,
        "expected_target_supported": correct_identity,
        "target_refs": refs,
        "candidate": asdict(candidate),
        "completion": asdict(completion),
        "layer": (
            "offline captured facts, per-fact parent proof; "
            "no claim that entire positive completes"
        ),
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert refs, witness
    assert any(ref.get("supported") is True for ref in refs) is correct_identity, witness
    if not correct_identity:
        assert not candidate.passed and not completion.passed, witness
