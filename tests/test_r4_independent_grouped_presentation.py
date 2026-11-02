"""Independent grouped-delivery contrasts; no model provider or fake Evidence."""

import csv
import io
import json
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path

import pytest
from apps.orchestrator.agent_loop import _restore_task_contract
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

from tests import test_r4_context_contract as support
from tests.test_r4_context_contract import offline_and_isolated  # noqa: F401

FIXTURE = Path(__file__).parent / "fixtures/r4_execution/independent-aggregate-captured.json"
METRIC = "播出时长（分钟）"
TOTAL = f"{METRIC}总计为1350。"


@pytest.mark.parametrize(
    "case,text,token",
    [
        ("unknown-group-known-value", f"{METRIC}各组合计：2026-99为75。" + TOTAL, "75"),
        (
            "additional-header-amount",
            f"{METRIC}各组合计（记录数为888）：2026-01为75。" + TOTAL,
            "888",
        ),
        (
            "wrong-parent-dataset",
            "数据集（dataset_ref: ffffffffffffffffffffffffffffffff）"
            + f"{METRIC}各组合计：2026-01为75。"
            + TOTAL,
            "75",
        ),
    ],
)
def test_parent_and_member_facts_do_not_disappear_or_borrow_evidence(tmp_path, case, text, token):
    raw = json.loads(FIXTURE.read_text())
    evidence = [EvidenceRecord(**r) for r in raw["evidence"]]
    invocations = [ToolInvocation(**r) for r in raw["invocations"]]
    bindings = [TaskDatasetBinding(**r) for r in raw["bindings"]]
    contract = _restore_task_contract(raw["contract"], raw["run"]["run_id"])
    # Sanity only: these are real producer facts. The expectation comes from
    # group identity/field/scope contracts, not the parser's classification.
    assert evidence[0].summary["numeric_context"]["group_col"] == "月份"
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
        "case": case,
        "text": text,
        "candidate": asdict(candidate),
        "completion": asdict(completion),
        "target_refs": refs,
        "layer": "offline replay of captured actual tool/SQLite facts",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert not candidate.passed and not completion.passed, witness
    assert refs and any(ref.get("supported") is not True for ref in refs), witness


@pytest.mark.asyncio
async def test_real_non_calendar_group_delivery_uses_same_structural_contract(tmp_path):
    rows = list(csv.DictReader(io.StringIO(support.CSV.decode())))
    expected = {
        group: sum(
            Decimal(row[support.METRIC])
            for row in rows
            if row["频道"] == group and row[support.METRIC]
        )
        for group in ("A", "B")
    }
    assert expected == {"A": Decimal("110"), "B": Decimal("40")}
    store, project, conversation = support.workspace(tmp_path)
    dataset = support.upload(store, project, conversation)
    answer = f"{support.METRIC}各组合计：A为110；B为40。"
    result = await support.execute(
        store,
        project,
        conversation,
        support.ScriptedModel(
            [
                (
                    "aggregate_preview",
                    {
                        "dataset_ref": dataset,
                        "group_col": "频道",
                        "value_col": support.METRIC,
                        "agg": "sum",
                    },
                )
            ],
            answer,
        ),
        "请按频道汇总时长（分钟），说明各频道合计。",
    )
    support.write_witness(
        tmp_path,
        "non-calendar-group",
        result=result,
        oracle=expected,
        layer="actual upload/Host/tools/SQLite; scripted model; not provider",
    )
    assert result["run"]["status"] == "completed", result
    successful = [item for item in result["invocations"] if item["status"] == "succeeded"]
    assert len(successful) == 1 and successful[0]["tool_name"] == "aggregate_preview", result
    assert (
        successful[0]["args"]["group_col"] == "频道"
        and successful[0]["args"]["dataset_ref"] == dataset
    )
    event = next(
        json.loads(item["data"])
        for item in reversed(result["events"])
        if item["event"] == "verification"
    )
    assert event["payload"]["verdict"] == "PASS", event
