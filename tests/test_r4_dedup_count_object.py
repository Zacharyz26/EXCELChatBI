"""Frozen count-object replay of real current-chain Evidence; no model/tool rerun."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import _restore_task_contract
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

FIXTURE = Path(__file__).parent / "fixtures/r4_execution/captured-dedup-facts.json"
CASES = [
    ("original", "去重前37行，去重后36行。去重删除了1行多余记录。", True),
    ("duplicate-excess", "去重删除了1条重复多余记录。", True),
    ("duplicate-record", "去重移除了1条重复记录。", True),
    ("plain-rows", "去重删除了1行记录。", True),
    ("remaining-excess", "去重后剩余1行多余记录。", False),
    ("participating-records", "去重参与重复的记录有1行。", False),
    ("groups", "去重删除了1组重复记录。", False),
    ("abnormal", "去重删除了1行异常记录。", False),
    ("wrong-delta", "去重删除了2行多余记录。", False),
    ("wrong-role", "去重前36行，去重后37行。", False),
    ("unsupported-remaining-groups", "去重后还剩1个重复组。", False),
]
CASES = [(*row, "dedup") for row in CASES] + [
    ("nulls-are-not-duplicate-excess", "删除了1行多余记录。", False, "null"),
    ("nulls-removed-rows", "清洗空值前38行，清洗空值后37行。删除了1行记录。", True, "null"),
]


@pytest.mark.parametrize("name,text,expected,kind", CASES, ids=[x[0] for x in CASES])
def test_operation_count_objects_are_bound_to_removed_delta(tmp_path, name, text, expected, kind):
    raw = json.loads(
        (FIXTURE if kind == "dedup" else FIXTURE.with_name("captured-null-facts.json")).read_text()
    )
    evidence = [EvidenceRecord(**r) for r in raw["evidence"]]
    invocations = [ToolInvocation(**r) for r in raw["invocations"]]
    bindings = [TaskDatasetBinding(**r) for r in raw["bindings"]]
    contract = _restore_task_contract(raw["contract"], raw["run"]["run_id"])
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
    witness = {
        "text": text,
        "expected": expected,
        "candidate": asdict(candidate),
        "completion": asdict(completion),
        "layer": "real-tool Evidence offline replay",
    }
    (tmp_path / "quantity-role.json").write_text(
        json.dumps(witness, ensure_ascii=False, default=str, indent=2)
    )
    assert candidate.passed is expected, witness
    assert (completion.verdict == "PASS") is expected, witness
    numeric_refs = [
        ref
        for claim in candidate.claims
        for ref in claim.value_refs
        if ref.get("token") in {"1", "2", "36", "37", "38"}
    ]
    assert numeric_refs, "The asserted quantity must survive extraction"
