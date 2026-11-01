"""Small independent parent-context matrix, pure typed Evidence components.

Aggregate270 is120+150 for the whole source; shape4x2 is independent fixture
metadata. This is not actual tool/Host execution or a new scope schema.
"""

import hashlib
import json
import socket
from dataclasses import asdict

import pytest
from apps.orchestrator.control.claims import build_evidence_summary
from apps.orchestrator.control.quantity_presentation import validate_candidate
from apps.orchestrator.control.verifier import verify_completion
from packages.orchestration.contracts import build_minimal_contract
from packages.orchestration.evidence_facts import seal_count_facts
from packages.session.task_models import EvidenceRecord, TaskDatasetBinding, ToolInvocation

DATASET = "d" * 32
VERSION = "a" * 64


def record(kind):
    args = {"dataset_ref": DATASET, "data_version_hash": VERSION}
    if kind == "aggregate":
        tool = "aggregate_preview"
        args.update(group_col="月份", value_col="销售额", agg="sum")
        payload = {
            "group_col": "月份",
            "value_col": "销售额",
            "agg": "sum",
            "total_value": 270,
            "group_total": 2,
            "truncated": False,
            "rows": [{"group": "一月", "value": 120}, {"group": "二月", "value": 150}],
        }
    else:
        tool = "get_data_profile"
        payload = {"profile": {"dataset_ref": DATASET, "row_count": 4, "column_count": 2}}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    summary = build_evidence_summary(
        summary="independent whole-source oracle", result=payload, artifact_id=None, arguments=args
    )
    if kind == "aggregate":
        summary = seal_count_facts(
            summary=summary,
            result=payload,
            tool=tool,
            arguments={key: value for key, value in args.items() if key != "data_version_hash"},
            run_id="parent-proof",
            invocation_id="parent-call",
            result_hash=digest,
            branch_hash=VERSION,
        )
    return EvidenceRecord(
        "parent-evidence",
        "parent-proof",
        "parent-call",
        None,
        "tool_result",
        {"tool": tool},
        digest,
        summary,
        "2026-11-01T00:00:00Z",
    )


@pytest.mark.parametrize("kind", ["aggregate", "shape"])
@pytest.mark.parametrize(
    "case,expected",
    [("matching-dataset", True), ("wrong-dataset", False), ("unsupported-scope", False)],
)
def test_parent_identity_and_scope_govern_every_quantity_branch(
    tmp_path, monkeypatch, kind, case, expected
):
    def denied(*_args, **_kwargs):
        raise AssertionError("Pure parent-context test forbids network")

    monkeypatch.setattr(socket.socket, "connect", denied)
    source = record(kind)
    # Complete the isolated source contract instead of bypassing the full
    # Verifier's successful-invocation/projection gate. No product expectation
    # or input text changes; this is still a typed component fixture.
    arguments = {
        key: value
        for key, value in source.summary["numeric_context"].items()
        if key != "data_version_hash"
    }
    invocation = ToolInvocation(
        "parent-call",
        "parent-proof",
        None,
        "parent-call",
        source.source["tool"],
        "parent-key",
        hashlib.sha256(
            json.dumps(
                arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest(),
        arguments,
        "succeeded",
        source.result_hash,
        None,
        None,
        "2026-11-01T00:00:00Z",
        "2026-11-01T00:00:00Z",
    )
    bindings = [
        TaskDatasetBinding(
            "parent-proof",
            DATASET,
            "initial",
            None,
            None,
            "2026-11-01T00:00:00Z",
            "2026-11-01T00:00:00Z",
        )
    ]
    prefix = f"关于数据集{DATASET if case == 'matching-dataset' else 'e' * 32}"
    if case == "unsupported-scope":
        prefix = "筛选条件：频道=A"
    child = "销售额总计270" if kind == "aggregate" else f"数据集{DATASET}，数据有4行×2列"
    text = f"|{prefix}（{child}）|说明|\n|---|---|\n|已核对|完成|"
    candidate = validate_candidate(
        text, [source], goal="复核已有事实", run_id="parent-proof", invocations=[invocation]
    )
    contract = build_minimal_contract(
        run_id="parent-proof",
        user_text="复核已有事实",
        dataset_refs=(DATASET,),
        chart_required=False,
        report_required=False,
        pdf_required=False,
    )
    verdict = verify_completion(
        contract=contract,
        final_text=text,
        artifacts=[],
        invocations=[invocation],
        evidence=[source],
        claims=None,
        dataset_bindings=bindings,
    )
    witness = {
        "kind": kind,
        "case": case,
        "expected": expected,
        "input": text,
        "source": asdict(source),
        "invocation": asdict(invocation),
        "bindings": [asdict(item) for item in bindings],
        "candidate": asdict(candidate),
        "verdict": asdict(verdict),
        "layer": "independent typed component fixture, no tools/DB/Host/model",
    }
    (tmp_path / "witness.json").write_text(json.dumps(witness, ensure_ascii=False, indent=2))
    assert candidate.passed is expected, witness
    assert verdict.passed is expected, witness
