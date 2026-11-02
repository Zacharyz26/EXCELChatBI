"""Frozen original aggregate response; actual tool Evidence offline full-Verifier replay."""

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import _restore_task_contract
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

CASES = [
    (
        "original",
        "播出时长（分钟）各月合计：2026-01为75；2026-02为135；2026-03为195；2026-04为255；2026-05为315；2026-06为375。播出时长（分钟）总计为1350。",
        True,
    ),
    (
        "general-groups",
        "播出时长（分钟）各组合计：2026-01为75；2026-02为135；2026-03为195；2026-04为255；2026-05为315；2026-06为375。播出时长（分钟）总计为1350。",
        True,
    ),
    (
        "swapped-groups",
        "播出时长（分钟）各月合计：2026-01为135；2026-02为75；2026-03为195；2026-04为255；2026-05为315；2026-06为375。播出时长（分钟）总计为1350。",
        False,
    ),
    (
        "wrong-aggregation",
        "播出时长（分钟）各月均值：2026-01为75；2026-02为135；2026-03为195；2026-04为255；2026-05为315；2026-06为375。播出时长（分钟）总计为1350。",
        False,
    ),
    (
        "wrong-field",
        "利润各月合计：2026-01为75；2026-02为135；2026-03为195；2026-04为255；2026-05为315；2026-06为375。播出时长（分钟）总计为1350。",
        False,
    ),
    (
        "hard-paragraph",
        "播出时长（分钟）各月合计：\n\n2026-01为75；2026-02为135；2026-03为195；2026-04为255；2026-05为315；2026-06为375。播出时长（分钟）总计为1350。",
        False,
    ),
]


@pytest.mark.parametrize("name,text,expected", CASES, ids=[x[0] for x in CASES])
def test_aggregate_introduction_has_bounded_fact_ownership(tmp_path, name, text, expected):
    raw = json.loads(
        (Path(__file__).parent / "fixtures/r4_execution/captured-aggregate-facts.json").read_text()
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
        "layer": "actual aggregate Evidence offline replay",
    }
    (tmp_path / "aggregate-introduction.json").write_text(
        json.dumps(witness, ensure_ascii=False, default=str, indent=2)
    )
    assert candidate.passed is expected, witness
    assert completion.passed is expected, witness
    refs = [
        v
        for c in candidate.claims
        for v in c.value_refs
        if v.get("source_span")
        and v.get("token") in {"75", "135", "195", "255", "315", "375", "1350"}
    ]
    assert refs, "True quantities may not disappear"
    if expected:
        assert len([v for v in refs if v.get("supported")]) == 7, witness
